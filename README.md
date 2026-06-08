# Neuro Balance Python Bot — GPT-first 1:1 logic

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

BOT_ACTIVE_FROM=20
BOT_ACTIVE_TO=8
BOT_SILENT_OUTSIDE_HOURS=true
MESSAGE_DEBOUNCE_SECONDS=5
```

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
