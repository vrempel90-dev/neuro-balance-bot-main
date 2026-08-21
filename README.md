# Neuro Balance Python Bot — GPT-first 1:1 logic

Production-бот Neuro Balance с отдельным OpenAI Dialog Brain и жёстким Python-контролем записи.

## Архитектура

- OpenAI Dialog Brain (`AI_BRAIN_MODEL`, сейчас `gpt-5.4-mini`) — понимает клиента, язык, жалобу и намерение и предлагает следующий безопасный шаг диалога.
- `OPENAI_MODEL` (`gpt-4o-mini`) — используется для вспомогательной humanize/совместимой OpenAI-логики, когда это разрешено Python-гейтами.
- Python — источник истины для Wazzup, режима 20:00–08:00, state machine, противопоказаний, CRM-слотов, записи, переноса, отмены, оператора, outcome и safety guards.
- CRM API — patient-lookup, doctors, services, check-slots, book, cancel, reschedule, escalate, outcome.
- Railway production запускает `live_main:app`. `live_main.py` делегирует обычную обработку в `main.py` и только после завершённого Wazzup-turn запускает Claude observer; observer не управляет GPT/CRM-ответом пациенту.

## Railway variables

```env
OPENAI_API_KEY=sk-proj-...
AI_ENABLED=true
OPENAI_BRAIN_ENABLED=true
AI_BRAIN_MODEL=gpt-5.4-mini
AI_BRAIN_TEMPERATURE=0.2
AI_BRAIN_MAX_COMPLETION_TOKENS=2000

OPENAI_MODEL=gpt-4o-mini
OPENAI_HUMANIZE_REPLIES=true
OPENAI_HUMANIZE_TEMPERATURE=0.3
OPENAI_MAX_TOKENS=700
OPENAI_VOICE_MODEL=whisper-1

MONTHLY_AI_BUDGET_USD=17.5
AI_MAX_CLASSIFIER_CALLS_PER_DAY=300
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

### Как понять, участвует ли GPT в production

`GET /health` возвращает безопасный блок `openai` без значений секретов. Проверять нужно:

- `openai_api_key_present` — Railway реально передал непустой `OPENAI_API_KEY`;
- `ai_enabled` и `openai_brain_enabled` — Brain не отключён флагами;
- `ai_brain_model` — фактическая модель Brain;
- `brain_config_ready` — базовая конфигурация пригодна для вызова OpenAI;
- `brain_blockers` — причины конфигурационного блокирования;
- effective budget limits — месячный бюджет и дневной лимит вызовов.

Для конкретного диалога `/debug/chat` и event telemetry содержат `openai_brain_used`, `openai_brain_skip_reason`, `openai_brain_fallback_used`, `openai_error_type`, `openai_missing_keys` и `openai_disabled_flags`. Rule-based fallback сохраняется специально: ошибка OpenAI не должна останавливать запись пациента.

## Wazzup webhook URL

В CRM/Wazzup должен быть указан актуальный Railway URL:

```text
https://neuro-balance-bot-main-production.up.railway.app/webhook/wazzup
```

Старый домен `https://neuro-balance-bot-final-production.up.railway.app/webhook/wazzup` не должен использоваться: Railway отвечает на него `404 Application not found` с `x-railway-fallback: true`, поэтому запрос не доходит до приложения бота.

Проверить URL можно через `GET /health`, `GET /webhook/wazzup` или `GET /debug/wazzup/config`: ответы возвращают `current_wazzup_webhook_url` и список `deprecated_wazzup_webhook_urls`.

## Start command

Фактический Railway production start command:

```bash
uvicorn live_main:app --host 0.0.0.0 --port $PORT
```

Для локального запуска без Claude observer допустим основной app:

```bash
uvicorn main:app --host 0.0.0.0 --port $PORT
```

## Важно

- Бот днём молчит и не мешает КЦ.
- На профильные жалобы сначала отвечает по смыслу, потом ведёт к записи.
- На непрофильные жалобы не записывает автоматически, передаёт оператору.
- Не выдумывает врачей/слоты/записи — только через CRM tools.
- Ошибка/лимит OpenAI не должен ломать диалог: используется Python fallback, а причина должна быть видна в диагностике.

## Важное по CRM API

Проект использует контракт CRM без изменений:

- `GET /api/bot/check-slots?date=YYYY-MM-DD&doctor=<login>`
- Header: `x-bot-secret: <EXTERNAL_BOOKING_API_SECRET>`
- Ответ читается из `availability[].availableSlots`, слоты сгруппированы по врачам.
- `POST /api/bot/book` без поля `service`. Обязательные поля: `patientName`, `phone`, `doctorLogin`, `date`, `timeStart`.
- Поддерживаются обе переменные секрета: `EXTERNAL_BOOKING_API_SECRET` и `CRM_BOT_SECRET`.
