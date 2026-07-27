"""Сторож проекта multiclaude.

Проект dlorenc/multiclaude на GitHub сейчас спит. Робот работает не как
новостная лента, а как СТОРОЖ ПРОБУЖДЕНИЯ: сидит тихо и подаёт голос,
только когда там правда что-то произошло.

Что проверяем и как часто:
  РАЗ В НЕДЕЛЮ
    1. Проснулся ли код (поле pushed_at). Именно оно, а НЕ updated_at:
       updated_at меняется даже от чужих «звёздочек».
       Но само по себе pushed_at меняется и от служебной отправки, поэтому
       перед криком «ожил!» робот дополнительно проверяет, что есть
       настоящие правки кода.
    2. Обсуждения и предложенные правки, тронутые за последние 7 дней.
  РАЗ В МЕСЯЦ
    3. Не вышла ли готовая версия (релиз).
    4. Не переехала ли работа в чужую копию проекта.
  ЕСЛИ ПРОЕКТ ОЖИЛ
    5. Робот переходит в режим сводки РАЗ В НЕДЕЛЮ (не ежедневно:
       список чужих правок по-английски каждый день Роману не нужен).
  ПУЛЬС
    6. Раз в 30 дней, если новостей не было, одна строка «сторож на месте».
       Иначе молчание сломанного робота не отличить от молчания исправного.

Главные правила:
  • новостей нет — молчим;
  • память двигается ТОЛЬКО после того, как сообщение реально доставлено
    (иначе новость пропадёт навсегда);
  • «проверка сделана» отмечается только после успешного ответа GitHub
    (иначе один обрыв связи стоит недели или месяца тишины);
  • чужой сервер не ответил — выходим спокойно (код 0);
    сломан сам робот — выходим с ошибкой (код 1), чтобы это было видно;
  • ключи только из переменных окружения (секреты репозитория).
"""

import html
import json
import os
import re
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone

import requests

# ---------------------------------------------------------------- настройки

REPO = "dlorenc/multiclaude"                 # за каким проектом следим
OWN_OWNER = "trainingsw75-cloud"             # копия самого Романа — про неё новостей не шлём
STATE_FILE = "multiclaude_state.json"        # файл памяти робота (в корне репозитория)
BASELINE_PUSHED_AT = "2026-01-28T23:14:09Z"  # эталон: последняя правка кода на момент установки
STATE_FORMAT = 2                             # номер формата памяти; сменился — начинаем заново

# Одна строка «зачем это Роману». Попадает в первое сообщение.
# Если описание станет неточным — правится здесь, в одном месте.
PROJECT_WHY = (
    "Это диспетчерская для нескольких помощников Claude сразу: "
    "каждый работает в своей отдельной рабочей копии проекта.\n"
    "Тебе она как инструмент не нужна (всё по-английски и через командную строку) — "
    "следим ради идей и чтобы быть в курсе."
)

# Подсказка про ссылки: добавляется один раз в самом низу письма.
LINK_HINT = "Страницы по ссылкам английские — в браузере нажми правой кнопкой → «Перевести»."

API = "https://api.github.com"
TIMEOUT = 30            # секунд на один запрос
TRIES = 3               # столько попыток на запрос
PAUSES = [5, 10]        # паузы между попытками, секунд

# Ограничители, чтобы файл памяти не рос бесконечно
MAX_SEEN_ISSUES = 80    # помним последние 80 обсуждений
MAX_SEEN_RELEASES = 30  # помним последние 30 выпущенных версий
MAX_SEEN_FORKS = 25     # помним 25 самых свежих чужих копий (запрашиваем 20)

# Длина под телефон: цель — не больше 8 строк в сообщении
DIGEST_LINES = 5        # не больше 5 строк в списке обсуждений
FORK_LINES = 3          # не больше 3 чужих копий
TITLE_CUT = 50          # длина чужого заголовка

RESHOW_AFTER_DAYS = 30  # то же обсуждение показываем повторно не чаще, чем раз в 30 дней
DIGEST_EVERY_DAYS = 7   # в режиме «ожил» — сводка раз в неделю
QUIET_DIGESTS_TO_SLEEP = 2   # две пустые недельные сводки подряд — снова засыпаем
HEARTBEAT_DAYS = 30     # раз в 30 дней — «сторож на месте»
FAIL_STREAK_ALERT = 3   # столько неудач подряд — сообщить, что робот не достучался
TG_LIMIT = 3500         # запас до телеграмного предела в 4096 знаков

BOT = os.environ.get("BOT_TOKEN", "")
CHAT = os.environ.get("CHAT_ID", "")
GH_TOKEN = os.environ.get("GITHUB_TOKEN", "")  # ключ только на чтение, выдаётся сайтом GitHub


# ---------------------------------------------------------------- мелкие помощники

def now():
    """Текущее время в UTC."""
    return datetime.now(timezone.utc)


def today_str():
    """Сегодняшняя дата без времени — для счёта суток по календарю."""
    return now().strftime("%Y-%m-%d")


def parse_dt(value):
    """Дата вида 2026-01-28T23:14:09Z → объект времени. Не вышло — None."""
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def parse_day(value):
    """Дата вида 2026-07-27 → объект времени. Не вышло — None."""
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def days_since(value):
    """Сколько дней прошло с даты (принимает и полный вид, и только дату)."""
    dt = parse_dt(value) or parse_day(value)
    if dt is None:
        return None
    return max(0, (now() - dt).days)


def plural(n, one, few, many):
    """Русское окончание: 1 правка, 2-4 правки, 5-20 правок."""
    n = abs(int(n))
    if n % 100 in (11, 12, 13, 14):
        return many
    last = n % 10
    if last == 1:
        return one
    if last in (2, 3, 4):
        return few
    return many


def human_period(days):
    """Человеческий срок: «5 дней», «8 месяцев», «больше года»."""
    if days is None:
        return "долгое время"
    if days < 1:
        return "меньше суток"
    if days < 45:
        return f"{days} {plural(days, 'день', 'дня', 'дней')}"
    months = int(round(days / 30.44))
    if months < 12:
        return f"{months} {plural(months, 'месяц', 'месяца', 'месяцев')}"
    years = days / 365.25
    if years < 1.5:
        return "больше года"
    if years < 2.5:
        return "около двух лет"
    return f"больше {int(years)} лет"


def esc(text):
    """Обезвредить угловые скобки в чужом тексте, иначе Telegram не примет сообщение."""
    return html.escape(str(text or ""), quote=False)


def cut(text, limit=TITLE_CUT):
    """Подрезать длинный чужой текст, чтобы сообщение оставалось коротким."""
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def link(url, fallback=""):
    """Ссылка. Пометка про английский язык добавляется один раз в конце письма."""
    return f"🔗 {esc(url or fallback)}"


def dict_of(state, key):
    """Взять из памяти словарь. Если там лежит что-то другое — начать с пустого."""
    value = state.get(key)
    if not isinstance(value, dict):
        value = {}
        state[key] = value
    return value


def list_of(state, key):
    """Взять из памяти список. Если там лежит что-то другое — начать с пустого."""
    value = state.get(key)
    if not isinstance(value, list):
        value = []
        state[key] = value
    return value


# ---------------------------------------------------------------- разговор с GitHub

def gh(path, params=None):
    """Один запрос к GitHub с тремя попытками.

    Никогда не роняет робота: при любой беде возвращает None,
    а вызывающий код честно сообщает, что проверка не состоялась.
    """
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "cook46-multiclaude-watch/2.0",
    }
    if GH_TOKEN:
        headers["Authorization"] = f"Bearer {GH_TOKEN}"

    for attempt in range(TRIES):
        try:
            resp = requests.get(f"{API}/{path}", headers=headers, params=params, timeout=TIMEOUT)
        except requests.RequestException as exc:
            # В тексте ошибки бывает полный адрес запроса — печатаем только вид ошибки.
            print("GitHub недоступен:", path, type(exc).__name__)
            if attempt < TRIES - 1:
                time.sleep(PAUSES[attempt])
                continue
            return None

        code = resp.status_code

        if code == 403 and "rate limit" in resp.text.lower():
            print("Упёрлись в лимит запросов GitHub, пробуем в следующий раз")
            return None  # повторять бессмысленно

        if code == 429 or 500 <= code <= 504:
            wait = PAUSES[attempt] if attempt < len(PAUSES) else PAUSES[-1]
            retry_after = resp.headers.get("Retry-After")
            if code == 429 and retry_after and retry_after.isdigit():
                wait = min(int(retry_after), 60)
            print("GitHub ответил", code, "на", path, "— подождём", wait, "с")
            if attempt < TRIES - 1:
                time.sleep(wait)
                continue
            return None

        if code != 200:
            print("GitHub ответил", code, "на", path)
            return None

        try:
            return resp.json()
        except ValueError:
            print("GitHub прислал не-JSON на", path)
            return None
    return None


def _post_telegram(payload):
    """Одна попытка отправки. Возвращает объект ответа или None."""
    try:
        return requests.post(
            f"https://api.telegram.org/bot{BOT}/sendMessage",
            json=payload,
            timeout=TIMEOUT,
        )
    except requests.RequestException as exc:
        # ВАЖНО: печатаем только вид ошибки. В тексте ошибки лежит адрес
        # запроса, а в адресе — токен бота, и он попал бы в журнал автопроверки.
        print("Не смог отправить в Телеграм:", type(exc).__name__)
        return None


def send(text):
    """Отправить сообщение Роману. Возвращает True только при настоящей доставке."""
    if not BOT or not CHAT:
        print("Нет BOT_TOKEN или CHAT_ID — сообщение не отправлено")
        return False

    # Общий предохранитель по длине: Телеграм отказывает после 4096 знаков.
    if len(text) > TG_LIMIT:
        head = text[:TG_LIMIT]
        cutoff = head.rfind("\n")
        text = (head[:cutoff] if cutoff > TG_LIMIT // 2 else head) + "\n…"

    base = {"chat_id": CHAT, "text": text, "disable_web_page_preview": True}

    for attempt in range(2):
        resp = _post_telegram(dict(base, parse_mode="HTML"))
        if resp is None:
            if attempt == 0:
                time.sleep(PAUSES[0])
                continue
            return False

        if resp.status_code == 200:
            return True

        if resp.status_code == 429:
            wait = 30
            try:
                wait = int((resp.json().get("parameters") or {}).get("retry_after", 30))
            except Exception:
                pass
            wait = max(1, min(wait, 60))
            print("Телеграм просит подождать", wait, "с")
            if attempt == 0:
                time.sleep(wait)
                continue
            return False

        print("Телеграм ответил", resp.status_code)
        if resp.status_code == 400 and attempt == 0:
            # Скорее всего чужой текст сломал разметку. Пробуем без разметки:
            # лучше некрасивое сообщение, чем потерянная новость.
            plain = re.sub(r"</?[a-zA-Z]+>", "", text)
            plain = html.unescape(plain)
            resp2 = _post_telegram(dict(base, text=plain))
            if resp2 is not None and resp2.status_code == 200:
                return True
            return False
        if attempt == 0:
            time.sleep(PAUSES[0])
            continue
        return False
    return False


# ---------------------------------------------------------------- память робота

def load_state():
    """Прочитать память.

    Возвращает пару (память, был_ли_файл). Память None — начинаем заново.
    Файл был, но формат чужой или битый — начинаем заново МОЛЧА,
    без повторного приветствия.
    """
    if not os.path.exists(STATE_FILE):
        return None, False
    try:
        with open(STATE_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception as exc:
        print("Файл памяти не читается, начинаем заново:", type(exc).__name__)
        return None, True
    if not isinstance(data, dict):
        print("Файл памяти не того вида, начинаем заново")
        return None, True
    if data.get("format") != STATE_FORMAT:
        print("Формат памяти сменился, начинаем заново (без приветствия)")
        return None, True
    return data, True


def save_state(state):
    """Записать память, предварительно подрезав всё, что могло разрастись.

    Если ничего не изменилось — файл не трогаем, чтобы робот не делал
    в хранилище пустую запись каждый день.
    """
    state["format"] = STATE_FORMAT

    def trim(key, limit):
        data = dict_of(state, key)
        if len(data) > limit:
            def freshness(item):
                value = item[1]
                if isinstance(value, dict):
                    return str(value.get("t") or "")
                return str(value or "")
            newest = sorted(data.items(), key=freshness, reverse=True)[:limit]
            state[key] = dict(newest)

    trim("seen_issues", MAX_SEEN_ISSUES)
    trim("seen_releases", MAX_SEEN_RELEASES)   # словарь «метка → дата», режем по свежести
    trim("forks", MAX_SEEN_FORKS)

    text = json.dumps(state, ensure_ascii=False, indent=1, sort_keys=True)
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, encoding="utf-8") as fh:
                if fh.read() == text:
                    print("Память не изменилась — файл не трогаем")
                    return
        except OSError:
            pass
    with open(STATE_FILE, "w", encoding="utf-8") as fh:
        fh.write(text)


# ---------------------------------------------------------------- проверки
# Каждая проверка возвращает пару (текст или None, состоялась ли проверка).
# «Состоялась» = GitHub ответил. Не состоялась — повторим завтра,
# а не через неделю или через месяц.

def check_wakeup(state, repo_info):
    """Пункт 1. Проснулся ли проект.

    Одного pushed_at мало: он меняется и от служебной отправки (удалили ветку,
    поправили метку). Кричим «ожил!» только если есть настоящие правки кода.
    """
    fresh = repo_info.get("pushed_at")
    known = state.get("pushed_at") or BASELINE_PUSHED_AT
    if not fresh or fresh == known:
        return None, True  # тишина — молчим

    commits = gh(f"repos/{REPO}/commits", {"since": known, "per_page": 1})
    if commits is None:
        return None, False  # GitHub не ответил — память не двигаем, проверим завтра

    if not isinstance(commits, list) or not commits:
        # Отправка была служебная, настоящих правок нет. Тихо запоминаем и молчим.
        state["pushed_at"] = fresh
        print("Была служебная отправка без правок кода — Роману не пишем")
        return None, True

    quiet = human_period(days_since(known))
    branch = repo_info.get("default_branch") or state.get("branch") or "main"
    state["pushed_at"] = fresh
    state["mode"] = "awake"          # переходим в режим недельной сводки
    state["quiet_digests"] = 0
    state["commits_since"] = known   # с этой даты считаем правки в первой сводке
    state["last_digest_date"] = today_str()
    return (
        "🔔 <b>Проект multiclaude ожил!</b>\n\n"
        "В нём снова правят код — до этого тишина была "
        f"{quiet}.\n"
        "Значит, программой, возможно, снова можно будет пользоваться.\n\n"
        + link(f"https://github.com/{REPO}/commits/{branch}") +
        "\n\nДальше буду присылать короткую сводку раз в неделю."
    ), True


def issue_kind(it):
    """Во что превратился разговор: обсуждение, правка, принятая правка…"""
    pr = it.get("pull_request") or {}
    if pr:
        if pr.get("merged_at"):
            return "merged", "✅ правку приняли"
        if it.get("state") == "closed":
            return "closed_pr", "⛔ правку закрыли, не приняв"
        return "open_pr", "🛠 предложенная правка"
    if it.get("state") == "closed":
        return "closed_issue", "💬 обсуждение закрыли"
    return "open_issue", "💬 обсуждение"


def check_community(state):
    """Пункт 2. Обсуждения и предложенные правки за последние 7 дней."""
    items = gh(
        f"repos/{REPO}/issues",
        {"state": "all", "sort": "updated", "direction": "desc", "per_page": 20},
    )
    if items is None or not isinstance(items, list):
        return None, False
    if not items:
        return None, True

    edge = now() - timedelta(days=7)
    seen = dict_of(state, "seen_issues")
    candidates = []

    for it in items:
        if not isinstance(it, dict) or it.get("number") is None:
            continue
        number = str(it.get("number"))
        updated = it.get("updated_at") or ""
        dt = parse_dt(updated)
        if dt is None or dt < edge:
            continue                                  # слишком старое

        kind, label = issue_kind(it)
        was = seen.get(number)
        was = was if isinstance(was, dict) else {}
        shown_days = days_since(was.get("t")) if was else None
        if was:
            # Показываем повторно, только если сменилось состояние (открыто /
            # закрыто / приняли) или прошло больше месяца. Иначе один чужой
            # комментарий гнал бы одну и ту же строку неделю за неделей.
            if was.get("k") == kind and (shown_days is None or shown_days <= RESHOW_AFTER_DAYS):
                continue

        candidates.append(
            (updated, number, kind, f"№{number} {label}: {esc(cut(it.get('title')))}")
        )

    if not candidates:
        return None, True

    candidates.sort(reverse=True)
    shown = candidates[:DIGEST_LINES]
    # Помечаем прочитанным ТОЛЬКО то, что реально показали.
    # Остальное придёт в следующий раз, а не пропадёт навсегда.
    for updated, number, kind, _ in shown:
        seen[number] = {"k": kind, "t": today_str(), "u": updated}

    merged = sum(1 for _, _, kind, _ in shown if kind == "merged")
    head = "👥 <b>В проекте multiclaude зашевелились люди</b>\n"
    if merged:
        head = ("🎉 <b>В проект приняли новые правки</b>\n"
                "Похоже, работа над ним понемногу идёт.\n")

    body = "\n".join(text for _, _, _, text in shown)
    rest = len(candidates) - len(shown)
    tail = ""
    if rest > 0:
        tail = f"\n…и ещё {rest} {plural(rest, 'обсуждение', 'обсуждения', 'обсуждений')} — покажу позже."

    return (
        head +
        "Названия ниже — как их написал автор, по-английски:\n\n" +
        body + tail + "\n\n" +
        link(f"https://github.com/{REPO}/issues")
    ), True


def check_releases(state):
    """Пункт 3. Не вышла ли готовая версия.

    Метки версий (tags) не проверяем: для повара «автор начал помечать
    готовые состояния проекта» не значит ничего и дублирует эту же новость.
    """
    releases = gh(f"repos/{REPO}/releases", {"per_page": 5})
    if releases is None or not isinstance(releases, list):
        return None, False
    if not releases:
        return None, True

    known = dict_of(state, "seen_releases")
    fresh = [
        r for r in releases
        if isinstance(r, dict) and r.get("tag_name") and r.get("tag_name") not in known
    ]
    if not fresh:
        return None, True

    shown = fresh[:3]
    for r in shown:  # помечаем только то, что показали
        known[str(r.get("tag_name"))] = r.get("published_at") or today_str()

    lines = []
    for r in shown:
        name = esc(cut(r.get("name") or r.get("tag_name"), 60))
        url = r.get("html_url") or f"https://github.com/{REPO}/releases"
        lines.append(f"• {name}\n  🔗 {esc(url)}")

    return (
        "🚀 <b>У multiclaude вышла готовая версия</b>\n\n"
        "Готовую версию ставить проще, чем собирать программу из кода.\n"
        "Название дал автор, оно английское:\n\n"
        + "\n".join(lines)
    ), True


def check_forks(state):
    """Пункт 4. Не переехала ли работа в чужую копию проекта."""
    forks = gh(f"repos/{REPO}/forks", {"sort": "newest", "per_page": 20})
    if forks is None or not isinstance(forks, list):
        return None, False
    if not forks:
        return None, True

    known = dict_of(state, "forks")
    # «Первый раз» определяем по явному признаку, а не по пустоте списка:
    # у заброшенного проекта копий может не быть вовсе, и тогда самая первая
    # копия — главная новость — молча провалилась бы в «просто запомнили».
    first_time = not state.get("forks_initialized")
    edge = now() - timedelta(days=45)
    movers = []

    for fk in forks:
        if not isinstance(fk, dict):
            continue
        name = fk.get("full_name")
        pushed = fk.get("pushed_at") or ""
        if not name:
            continue
        if str(name).split("/")[0].lower() == OWN_OWNER.lower():
            known[name] = pushed          # это копия самого Романа, новостью не считаем
            continue
        was = known.get(name)
        dt = parse_dt(pushed)
        if first_time or dt is None or dt < edge:
            known[name] = pushed          # в первый раз только запоминаем; старьё не интересно
            continue
        if was and was == pushed:
            continue                      # ничего не изменилось
        movers.append((pushed, name, fk.get("html_url") or f"https://github.com/{name}"))

    if first_time:
        state["forks_initialized"] = True

    if not movers:
        return None, True

    movers.sort(reverse=True)
    shown = movers[:FORK_LINES]
    for pushed, name, _ in shown:       # помечаем только показанные
        known[name] = pushed

    body = "\n".join(f"• {esc(n)}\n  🔗 {esc(u)}" for _, n, u in shown)
    rest = len(movers) - len(shown)
    tail = f"\n…и ещё {rest}." if rest > 0 else ""
    return (
        "🔀 <b>Работу над multiclaude, похоже, продолжают другие</b>\n\n"
        "Кто-то сделал себе копию и правит её вместо заброшенного оригинала.\n"
        "Если программа понадобится — качать теперь оттуда:\n\n"
        + body + tail
    ), True


def count_commits(since, per_page=100, max_pages=3):
    """Честно посчитать правки. Возвращает (список, число, упёрлись_ли_в_предел, ок)."""
    collected = []
    for page in range(1, max_pages + 1):
        chunk = gh(f"repos/{REPO}/commits",
                   {"since": since, "per_page": per_page, "page": page})
        if chunk is None or not isinstance(chunk, list):
            return [], 0, False, False
        collected.extend([c for c in chunk if isinstance(c, dict)])
        if len(chunk) < per_page:
            return collected, len(collected), False, True
    return collected, len(collected), True, True


def activity_digest(state):
    """Пункт 5. Режим «проект ожил»: одна короткая сводка раз в неделю."""
    since = state.get("commits_since") or (now() - timedelta(days=DIGEST_EVERY_DAYS)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    commits, total, capped, ok = count_commits(since)
    if not ok:
        return None, False  # GitHub не ответил — память не двигаем

    state["commits_since"] = now().strftime("%Y-%m-%dT%H:%M:%SZ")
    state["last_digest_date"] = today_str()

    if total == 0:
        # Тишина. Две пустые недели подряд — возвращаемся в сторожевой режим.
        quiet = int(state.get("quiet_digests") or 0) + 1
        state["quiet_digests"] = quiet
        if quiet >= QUIET_DIGESTS_TO_SLEEP:
            state["mode"] = "sleep"
            state["quiet_digests"] = 0
            print("Проект снова затих — вернулись в сторожевой режим")
        return None, True

    state["quiet_digests"] = 0

    topics, seen_topics = [], set()
    for c in commits:
        msg = ((c.get("commit") or {}).get("message") or "").split("\n")[0]
        msg = cut(msg, 60)
        if msg and msg.lower() not in seen_topics:
            seen_topics.add(msg.lower())
            topics.append(f"• {esc(msg)}")
        if len(topics) >= 3:
            break

    word = plural(total, "правка", "правки", "правок")
    count_text = f"{total} и больше {word}" if capped else f"{total} {word}"
    branch = state.get("branch") or "main"
    period = human_period(days_since(since))
    return (
        f"🛠 <b>multiclaude: за {period} {count_text}</b>\n"
        "Проект живой, работа идёт.\n\n"
        "О чём правили (по-английски, как у автора):\n" + "\n".join(topics) + "\n\n"
        + link(f"https://github.com/{REPO}/commits/{branch}")
    ), True


# ---------------------------------------------------------------- главный ход робота

def fresh_state(repo_info):
    """Заготовка памяти."""
    pushed = repo_info.get("pushed_at") or BASELINE_PUSHED_AT
    return {
        "format": STATE_FORMAT,
        "pushed_at": pushed,
        "branch": repo_info.get("default_branch") or "main",
        "mode": "sleep",
        "quiet_digests": 0,
        "fail_streak": 0,
        "last_weekly": "",
        "last_monthly": "",
        "last_digest_date": "",
        "last_heartbeat": today_str(),
        "seen_issues": {},
        "seen_releases": {},
        "forks": {},
        "forks_initialized": False,
        "commits_since": pushed,
    }


def first_run(repo_info, greet):
    """Первый запуск: истории не вываливаем, только запоминаем точку отсчёта."""
    state = fresh_state(repo_info)

    # Молча запоминаем нынешние обсуждения, версии и чужие копии, чтобы
    # не объявить новостью то, что было до установки робота.
    items = gh(f"repos/{REPO}/issues",
               {"state": "all", "sort": "updated", "direction": "desc", "per_page": 20})
    if isinstance(items, list):
        for it in items:
            if isinstance(it, dict) and it.get("number") is not None:
                kind, _ = issue_kind(it)
                state["seen_issues"][str(it["number"])] = {
                    "k": kind, "t": today_str(), "u": it.get("updated_at") or ""
                }

    releases = gh(f"repos/{REPO}/releases", {"per_page": 10})
    if isinstance(releases, list):
        for r in releases:
            if isinstance(r, dict) and r.get("tag_name"):
                state["seen_releases"][str(r["tag_name"])] = r.get("published_at") or today_str()

    forks = gh(f"repos/{REPO}/forks", {"sort": "newest", "per_page": 20})
    if isinstance(forks, list):
        for fk in forks:
            if isinstance(fk, dict) and fk.get("full_name"):
                state["forks"][fk["full_name"]] = fk.get("pushed_at") or ""
        state["forks_initialized"] = True

    if not greet:
        save_state(state)
        print("Память пересоздана без приветствия")
        return

    quiet = human_period(days_since(state["pushed_at"]))
    delivered = send(
        "🤖 <b>Слежу за проектом multiclaude</b>\n\n"
        "Это программа с открытым кодом на сайте GitHub (склад открытых программ).\n"
        + PROJECT_WHY + "\n\n"
        f"Сейчас её забросили: код не трогали {quiet}.\n"
        "Напишу, только если автор вернётся, выйдет готовая версия "
        "или проект подхватят другие. Новостей нет — молчу.\n\n"
        "Надоест — скажи, отключу."
    )
    if delivered:
        save_state(state)
        print("Первый запуск: запомнили точку отсчёта", state["pushed_at"])
    else:
        # Память не двигаем: приветствие не дошло, поздороваемся завтра.
        print("Приветствие не доставлено — память не сохраняем, повторим завтра")


def safe(check, *args):
    """Выполнить проверку. Обрыв связи — не беда, проверку просто отложим."""
    try:
        return check(*args)
    except requests.RequestException as exc:
        print("Связь подвела на проверке", check.__name__, type(exc).__name__)
        return None, False


def main():
    state, had_file = load_state()

    # Один-единственный обязательный запрос: карточка проекта.
    repo_info = gh(f"repos/{REPO}")
    if not isinstance(repo_info, dict) or not repo_info.get("pushed_at"):
        print("Не получили данные о проекте — попробуем завтра")
        if state is None:
            return
        # Пульс: три неудачи подряд — значит, дело не в разовом сбое.
        streak = int(state.get("fail_streak") or 0) + 1
        state["fail_streak"] = streak
        messages = []
        if streak == FAIL_STREAK_ALERT:
            messages.append(
                "🤖 <b>Третий день не могу достучаться до GitHub</b>\n\n"
                "Новости по multiclaude пока не приходят. Если так и дальше — зови на помощь."
            )
        deliver_and_save(state, messages)
        return

    if state is None:
        first_run(repo_info, greet=not had_file)
        return

    state["fail_streak"] = 0
    state["branch"] = repo_info.get("default_branch") or state.get("branch") or "main"
    today = now()
    messages = []

    # Пункт 1 — проверяем каждый день: запрос карточки уже сделан.
    woke, _ok = safe(check_wakeup, state, repo_info)
    if woke:
        messages.append(woke)

    # Пункт 5 — если проект живой, сводка раз в неделю (и не чаще раза в сутки).
    if state.get("mode") == "awake" and not woke:
        last_digest = days_since(state.get("last_digest_date"))
        if last_digest is None or last_digest >= DIGEST_EVERY_DAYS:
            digest, _ok = safe(activity_digest, state)
            if digest:
                messages.append(digest)

    # Пункт 2 — раз в неделю, по понедельникам (или если понедельник пропустили).
    last_weekly = parse_dt(state.get("last_weekly")) or parse_day(state.get("last_weekly"))
    if today.weekday() == 0 or last_weekly is None or (today - last_weekly).days >= 8:
        people, ok = safe(check_community, state)
        if ok:  # отметку «проверил» ставим только после успеха
            state["last_weekly"] = today.strftime("%Y-%m-%dT%H:%M:%SZ")
        if people:
            messages.append(people)

    # Пункты 3 и 4 — раз в месяц, не чаще.
    last_monthly = parse_dt(state.get("last_monthly")) or parse_day(state.get("last_monthly"))
    if last_monthly is None or (today - last_monthly).days >= 28:
        rel, ok_rel = safe(check_releases, state)
        if rel:
            messages.append(rel)
        frk, ok_frk = safe(check_forks, state)
        if frk:
            messages.append(frk)
        if ok_rel and ok_frk:
            state["last_monthly"] = today.strftime("%Y-%m-%dT%H:%M:%SZ")

    # Пульс: раз в 30 дней одна строка, чтобы Роман видел — робот жив.
    if not messages:
        since_beat = days_since(state.get("last_heartbeat"))
        if since_beat is None or since_beat >= HEARTBEAT_DAYS:
            messages.append("🤖 Сторож на месте, новостей по multiclaude нет.")
            state["last_heartbeat"] = today_str()

    deliver_and_save(state, messages)


def deliver_and_save(state, messages):
    """Одно сообщение в день, и память двигаем только после доставки."""
    if not messages:
        save_state(state)
        print("Новостей нет — молчим")
        return

    combined = "\n\n➖➖➖\n\n".join(messages)
    if "🔗" in combined:
        combined += "\n\n" + LINK_HINT
    if send(combined):
        save_state(state)
        print("Сообщение доставлено, новостей в нём:", len(messages))
    else:
        # Память НЕ двигаем: иначе новость считалась бы показанной и пропала.
        print("Сообщение не доставлено — память не двигаем, повторим завтра")


if __name__ == "__main__":
    try:
        main()
    except requests.RequestException as exc:
        # Чужой сервер не ответил — это не поломка робота, не краснеем.
        print("Связь с интернетом подвела:", type(exc).__name__)
        sys.exit(0)
    except Exception:
        # А вот это уже поломка самого робота: битая память, опечатка в коде.
        # Пусть запуск покраснеет и GitHub пришлёт письмо — молчащий сломанный
        # робот неотличим от молчащего исправного.
        traceback.print_exc()
        sys.exit(1)
    sys.exit(0)
