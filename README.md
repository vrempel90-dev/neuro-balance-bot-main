# Neuro Balance Python Bot — GPT-first 1:1 logic

> В текущей ветке также реализован **этап 2 KORGAN Legal AI** — изолированный
> TypeScript/grammY-модуль меню без запуска бота и внешних интеграций.

## KORGAN Legal AI — этап 2

Модуль `src/` содержит двуязычные (русский/қазақша) меню консультаций,
подготовки и проверки документов, обращений, стоимости и заявки юристу.
Типизированная in-memory-сессия хранит незавершённый сценарий только во время
работы процесса. BOT_TOKEN, OpenAI, база данных, платежи и отправка данных не
подключены; `src/index.ts` намеренно не запускает polling.

Проверка модуля:

```bash
npm install
npm run format
npm run typecheck
npm test
```

Версия, где GPT-4o-mini ведёт живой диалог и максимально повторяет логику старого бота Neuro Balance.

## Архитектура

- GPT-4o-mini — понимает клиента, язык, жалобу, намерение, отвечает как живой администратор.
- Python — только контроль и исполнение: Wazzup, режим 20:00–08:00, CRM, запись, перенос, отмена, оператор, outcome, безопасность.
- CRM API — patient-lookup, doctors, services, check-slots, book, cancel, reschedule, escalate, outcome.

## Railway variables

```env
OPENAI_API_KEY=sk-proj-...
OPENAI_MODEL=gpt-4o-mini
OPENAI_VOICE_MODEL=whisper-1
AI_ENABLED=true
HUMAN_DIALOG_MODE=true

CRM_BASE_URL=https://neuro-balance-crm.vercel.app
CRM_BOT_SECRET=...

WAZZUP_API_KEY=...
WAZZUP_CHANNEL_ID=...
WAZZUP_API_URL=https://api.wazzup24.com/v3

PUBLIC_BASE_URL=https://neuro-balance-bot-main-production.up.railway.app
DEPRECATED_PUBLIC_BASE_URLS=https://neuro-balance-bot-final-production.up.railway.app

BOT_ACTIVE_FROM=20
BOT_ACTIVE_TO=8
BOT_SILENT_OUTSIDE_HOURS=true
MESSAGE_DEBOUNCE_SECONDS=5
```

## Wazzup webhook URL

В CRM/Wazzup должен быть указан актуальный Railway URL:

```text
https://neuro-balance-bot-main-production.up.railway.app/webhook/wazzup
```

Старый домен `https://neuro-balance-bot-final-production.up.railway.app/webhook/wazzup` не должен использоваться: Railway отвечает на него `404 Application not found` с `x-railway-fallback: true`, поэтому запрос не доходит до приложения бота.

Проверить URL можно через `GET /health`, `GET /webhook/wazzup` или `GET /debug/wazzup/config`: ответы возвращают `current_wazzup_webhook_url` и список `deprecated_wazzup_webhook_urls`.

## Start command

```bash
uvicorn main:app --host 0.0.0.0 --port $PORT
```

## Важно

- Бот днём молчит и не мешает КЦ.
- На профильные жалобы сначала отвечает по смыслу, потом ведёт к записи.
- На непрофильные жалобы не записывает автоматически, передаёт оператору.
- Не выдумывает врачей/слоты/записи — только через CRM tools.

## Важное по CRM API

Проект использует контракт CRM без изменений:

- `GET /api/bot/check-slots?date=YYYY-MM-DD&doctor=<login>`
- Header: `x-bot-secret: <EXTERNAL_BOOKING_API_SECRET>`
- Ответ читается из `availability[].availableSlots`, слоты сгруппированы по врачам.
- `POST /api/bot/book` без поля `service`. Обязательные поля: `patientName`, `phone`, `doctorLogin`, `date`, `timeStart`.
- Поддерживаются обе переменные секрета: `EXTERNAL_BOOKING_API_SECRET` и `CRM_BOT_SECRET`.
