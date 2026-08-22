# Neuro Balance Python Bot — GPT-first 1:1 logic

Production-бот Neuro Balance с отдельным OpenAI Dialog Brain и жёстким Python-контролем записи.

## Архитектура

- OpenAI Dialog Brain (`AI_BRAIN_MODEL`, production: `gpt-5.4-mini`) — понимает клиента, язык, жалобу и намерение и предлагает следующий безопасный шаг диалога.
- `OPENAI_MODEL` (`gpt-4o-mini`) — используется для вспомогательной OpenAI/humanize-логики.
- Python — источник истины для Wazzup, режима 20:00–08:00, state machine, противопоказаний, CRM-слотов, записи, переноса, отмены и safety guards.
- Railway production запускает `live_main:app`. `live_main.py` делегирует обычную обработку в `main.py`; Claude observer запускается только после завершённого Wazzup-turn и не управляет ответом GPT/CRM пациенту.

## Railway variables

```env
OPENAI_API_KEY=sk-proj-...
AI_ENABLED=true
OPENAI_BRAIN_ENABLED=true
AI_BRAIN_MODEL=gpt-5.4-mini
AI_BRAIN_TEMPERATURE=0.2
AI_BRAIN_MAX_COMPLETION_TOKENS=2000

OPENAI_MODEL=gpt-4o-mini
AI_HUMANIZE_MODEL=gpt-4o-mini
OPENAI_HUMANIZE_REPLIES=true
OPENAI_VOICE_MODEL=whisper-1

# NeuroBalance share of the shared ~$15/month OpenAI target.
MONTHLY_AI_BUDGET_USD=5.0
AI_MAX_CLASSIFIER_CALLS_PER_DAY=300
HUMAN_DIALOG_MODE=true

# Dedicated secret for protected OpenAI operational diagnostics.
# Never reuse OPENAI_API_KEY, CRM_BOT_SECRET or Wazzup credentials here.
OPENAI_DEBUG_ADMIN_TOKEN=<strong-random-secret>

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

## OpenAI production diagnostics

После деплоя diagnostics доступны только администратору:

```text
GET /debug/openai/status
Authorization: Bearer <OPENAI_DEBUG_ADMIN_TOKEN>
```

Анонимный или неверно авторизованный запрос получает `401`. Если `OPENAI_DEBUG_ADMIN_TOKEN` не настроен на сервере, endpoint закрывается fail-closed ответом `503`.
