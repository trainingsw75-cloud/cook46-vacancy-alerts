"""Робот вакансий: читает rekamore.su (Повар / Матрос AB-OS) и шлёт новые вакансии в Telegram."""
import json
import os
import re

import requests
from bs4 import BeautifulSoup

BOT = os.environ["BOT_TOKEN"]
CHAT = os.environ["CHAT_ID"]
BASE = "https://rekamore.su"
JOBS = {
    "146": "Повар / Повар-матрос",
    "145": "Матрос (AB/OS)",
}
SEEN_FILE = "seen.json"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; VacancyAlertBot/1.0)"}
MAX_PER_RUN = 12


def fetch_vacancies():
    found = {}
    for job, label in JOBS.items():
        url = f"{BASE}/list-vacancies?job={job}"
        try:
            resp = requests.get(url, headers=HEADERS, timeout=30)
            soup = BeautifulSoup(resp.text, "html.parser")
        except Exception as exc:  # noqa
            print("fetch error", job, exc)
            continue
        for link in soup.select('a[href*="/vacancie/"]'):
            match = re.search(r"/vacancie/(\d+)", link.get("href", ""))
            if not match:
                continue
            vid = match.group(1)
            # поднимаемся до блока, где есть заголовок вакансии
            container = link
            for _ in range(6):
                container = container.parent
                if container is None:
                    break
                if container.find("h3"):
                    break
            title, desc = "Вакансия", ""
            if container is not None:
                h3 = container.find("h3")
                if h3:
                    title = h3.get_text(" ", strip=True)
                p = container.find("p")
                if p:
                    desc = p.get_text(" ", strip=True)[:220]
            found[vid] = {"title": title, "desc": desc, "label": label,
                          "url": f"{BASE}/vacancie/{vid}"}
    return found


def send(text):
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT}/sendMessage",
            json={"chat_id": CHAT, "text": text, "parse_mode": "HTML",
                  "disable_web_page_preview": False},
            timeout=30,
        )
    except Exception as exc:  # noqa
        print("send error", exc)


def main():
    vacs = fetch_vacancies()
    print("found", len(vacs), "vacancies")

    first_run = not os.path.exists(SEEN_FILE)
    seen = set()
    if not first_run:
        try:
            seen = set(json.load(open(SEEN_FILE, encoding="utf-8")))
        except Exception:  # noqa
            seen = set()

    new_ids = [v for v in vacs if v not in seen]
    print("new:", len(new_ids))

    if first_run:
        send(f"🤖 Робот вакансий запущен!\n\nСлежу за <b>Повар</b> и <b>Матрос (AB/OS)</b> "
             f"на rekamore.su. Сейчас в базе {len(vacs)} вакансий — дальше буду присылать "
             f"только <b>новые</b>, каждые ~30 минут.")
    else:
        for vid in new_ids[:MAX_PER_RUN]:
            v = vacs[vid]
            msg = f"🍳 <b>Новая вакансия — {v['label']}</b>\n\n<b>{v['title']}</b>"
            if v["desc"]:
                msg += f"\n{v['desc']}"
            msg += f"\n\n🔗 {v['url']}"
            send(msg)

    all_ids = sorted(seen | set(vacs.keys()))
    json.dump(all_ids, open(SEEN_FILE, "w", encoding="utf-8"), ensure_ascii=False)


if __name__ == "__main__":
    main()
