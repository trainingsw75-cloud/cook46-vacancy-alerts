# Cook46 — робот вакансий

Каждые ~30 минут проверяет вакансии **Повар** и **Матрос (AB/OS)** на rekamore.su
и присылает новые в Telegram через бота `@cook46_bot`.

- Источник: https://rekamore.su/list-vacancies (job=146 повар, job=145 матрос AB/OS)
- Отправка: Telegram Bot API → личка владельцу
- Состояние (какие вакансии уже отправлены): `seen.json`

Секреты (Settings → Secrets → Actions):
- `BOT_TOKEN` — токен бота @cook46_bot
- `CHAT_ID` — Telegram ID получателя

Запуск вручную: вкладка **Actions → Vacancy Alerts → Run workflow**.
