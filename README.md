# Neuro Balance Python Bot — GPT-first 1:1 logic

Production-бот Neuro Balance с отдельным OpenAI Dialog Brain и жёстким Python-контролем записи.

## Архитектура

GPT ведёт разговор, Python исполняет, CRM — единственный источник истины.

```text
Wazzup inbound -> main.py (webhook, dedup, гейты)
               -> dialog.handle_message
               -> agent.py  ── GPT-first tool loop ──┐
                                                     │
     user -> GPT -> tool_call -> реальный CRM -> tool_result -> GPT -> ответ
                                                     │
               -> legacy state machine (только когда GPT недоступен)
```

- **agent.py (`AI_BRAIN_MODEL`, production: `gpt-5.4-mini`)** — главный
  разговорный слой. GPT понимает сообщение, выбирает следующий шаг диалога и
  сам вызывает инструменты. Все факты о врачах, датах, времени и записи
  приходят из реальных ответов CRM.
- **Инструменты GPT** — тонкие мосты к существующему `crm.py`, контракт CRM не
  менялся: `get_available_slots` → `GET /api/bot/check-slots`, `get_doctors` →
  `GET /api/bot/doctors`, `book_appointment` → `POST /api/bot/book`,
  `get_clinic_info` (утверждённые тексты), `escalate_to_operator`.
- **Python** — исполнительный слой: Wazzup webhook, режим 20:00–08:00,
  дедупликация, валидация CRM-ответов, идемпотентность записи, таймауты,
  телеметрия, безопасность.
- **Детерминированные гарантии остаются в Python:** записать можно только слот,
  который CRM реально предложила в этом диалоге; `doctorLogin` обязан быть из
  CRM; медицинские гейты (жалоба, противопоказания, возраст) блокируют запись;
  `booking_success` выставляется только после подтверждения CRM; повторный
  booking-вызов не создаёт вторую запись; `MAX_TOOL_ITERATIONS` ограничивает
  цикл.
- **Legacy state machine в `dialog.py`** — технический fallback. Работает
  только когда агент не может запуститься (нет `OPENAI_API_KEY`, `AI_ENABLED`
  или `OPENAI_BRAIN_ENABLED` выключены, исчерпан AI-бюджет, ошибка транспорта).
  Сбой OpenAI не останавливает запись пациентов.
- **Инвариант «не молчать»:** каждый принятый активный inbound turn
  заканчивается ответом пациенту, эскалацией с сообщением или корректной
  технической ошибкой. Намеренное молчание возможно только через `_no_reply` с
  явной причиной.
- `OPENAI_MODEL` (`gpt-4o-mini`) — используется для вспомогательной OpenAI/humanize-логики.
- Railway production запускает `live_main:app`. `live_main.py` делегирует обычную обработку в `main.py`; Claude observer запускается только после завершённого Wazzup-turn и не управляет ответом GPT/CRM пациенту.

## Телеметрия booking flow

Диагностика GPT-first пути видна в событиях `state.log_event` (без секретов и
без свободного текста пациента — телефон маскируется, имя/жалоба/родство/причина
эскалации пишутся как признак наличия или фиксированная категория):

- `agent_booking_blocked_by_age`
- `agent_booking_blocked_by_gate`
- `agent_booking_blocked_missing_age`
- `agent_booking_claim_error`
- `agent_booking_claim_settle_error`
- `agent_booking_crm_called`
- `agent_booking_crm_error`
- `agent_booking_crm_rejected` — поле `explicit_rejection` различает «CRM назвала отказ» (слот можно занимать снова) и «CRM ответила, но не подтвердила» (claim остаётся `uncertain`, повторный POST запрещён)
- `agent_booking_crm_success`
- `agent_booking_duplicate_prevented`
- `agent_booking_rejected_unknown_doctor`
- `agent_booking_rejected_unknown_slot`
- `agent_booking_tool_requested`
- `agent_budget_exhausted_mid_turn`
- `agent_clinic_info`
- `agent_crm_availability_error`
- `agent_crm_availability_result`
- `agent_crm_doctors_error`
- `agent_crm_doctors_result`
- `agent_escalated_to_operator`
- `agent_facts_recorded`
- `agent_openai_client_error`
- `agent_openai_error`
- `agent_silent_turn_prevented`
- `agent_skipped`
- `agent_tool_calls_truncated`
- `agent_tool_exception`
- `agent_tool_iteration_limit`
- `agent_tool_requested`
- `agent_turn_finished`
- `agent_turn_started`

Плюс `silent_turn_prevented` в `dialog.py` — срабатывание инварианта «принятый
активный turn не может закончиться молчанием».

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
OPENAI_VOICE_MODEL=whisper-1

MONTHLY_AI_BUDGET_USD=17.5
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

Endpoint не возвращает сам `OPENAI_API_KEY` и не возвращает `OPENAI_DEBUG_ADMIN_TOKEN`. Он показывает только operational status:

- `openai_api_key_present` — реально ли runtime получил непустой ключ;
- `ai_enabled` / `openai_brain_enabled`;
- effective `ai_brain_model` и token limit;
- текущий AI budget и дневной call limit;
- `brain_config_ready`;
- `brain_blockers` (`OPENAI_API_KEY`, `AI_ENABLED=false`, `OPENAI_BRAIN_ENABLED=false`, `monthly_budget_exceeded`, `daily_call_limit_exceeded` и т.д.).

Для конкретного диалога debug/event telemetry дополнительно содержит `openai_brain_used`, `openai_brain_skip_reason`, `openai_brain_fallback_used`, `openai_error_type`, `openai_missing_keys` и `openai_disabled_flags`. Python fallback сохраняется специально: ошибка OpenAI не должна остановить запись пациента.

## Wazzup webhook URL

В CRM/Wazzup должен быть указан актуальный Railway URL:

```text
https://neuro-balance-bot-main-production.up.railway.app/webhook/wazzup
```

Старый домен `https://neuro-balance-bot-final-production.up.railway.app/webhook/wazzup` не должен использоваться: Railway отвечает на него `404 Application not found` с `x-railway-fallback: true`, поэтому запрос не доходит до приложения бота.

Проверить URL можно через `GET /health`, `GET /webhook/wazzup` или `GET /debug/wazzup/config`.

## Start command

Фактический Railway production start command:

```bash
uvicorn live_main:app --host 0.0.0.0 --port $PORT
```

Для локального запуска без Claude observer:

```bash
uvicorn main:app --host 0.0.0.0 --port $PORT
```

## Важно

- Бот днём молчит и не мешает КЦ.
- На профильные жалобы сначала отвечает по смыслу, потом ведёт к записи.
- На непрофильные жалобы не записывает автоматически, передаёт оператору.
- Не выдумывает врачей/слоты/записи — только через CRM tools.
- Ошибка или лимит OpenAI деградирует в Python fallback, но причина должна быть видна в защищённой диагностике.

## Важное по CRM API

Проект использует контракт CRM без изменений:

- `GET /api/bot/check-slots?date=YYYY-MM-DD&doctor=<login>`
- Header: `x-bot-secret: <EXTERNAL_BOOKING_API_SECRET>`
- Ответ читается из `availability[].availableSlots`, слоты сгруппированы по врачам.
- `POST /api/bot/book` без поля `service`. Обязательные поля: `patientName`, `phone`, `doctorLogin`, `date`, `timeStart`.
- Поддерживаются обе переменные секрета: `EXTERNAL_BOOKING_API_SECRET` и `CRM_BOT_SECRET`.
