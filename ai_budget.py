"""Учёт расхода токенов OpenAI и жёсткий контроль месячного бюджета.

Зачем: MONTHLY_AI_BUDGET_KZT и AI_MAX_CLASSIFIER_CALLS_PER_DAY были объявлены в
config.py, но нигде не использовались — реального лимита трат не существовало.

Механизм:
  * после каждого вызова OpenAI записываем response.usage в state.ai_usage
    с разбивкой по дню/месяцу/модели (включая cached-токены, если API их отдаёт);
  * переводим токены в USD по таблице ниже и агрегируем месячную сумму;
  * на 80% лимита — warning-событие в логи Railway (видно заранее);
  * на 100% — вызовы OpenAI прекращаются, бот деградирует в rule-based fallback
    до начала следующего расчётного месяца (не падает и не молчит);
  * AI_MAX_CLASSIFIER_CALLS_PER_DAY ограничивает число вызовов в сутки — защита
    от резкого скачка и зацикленных ретраев.

Расчётный месяц и сутки считаются по бизнес-таймзоне клиники (BOT_TIMEZONE).
"""

from __future__ import annotations

from typing import Any

from config import get_settings
from schedule import astana_now

try:
    import state
except Exception:  # pragma: no cover - state недоступен только в изолированных тестах
    state = None


# Цены OpenAI в USD за 1M токенов.
# ВАЖНО: проверять на https://openai.com/api/pricing при смене модели.
# Проверено 17.08.2026: gpt-5.4-mini дороже gpt-4o-mini в 5x по input и 7.5x по output.
MODEL_PRICING_USD_PER_1M: dict[str, dict[str, float]] = {
    "gpt-5.4-mini": {"input": 0.75, "cached_input": 0.075, "output": 4.50},
    "gpt-4o-mini": {"input": 0.15, "cached_input": 0.075, "output": 0.60},
}

# Неизвестная модель считается по самому дорогому известному тарифу:
# лучше переоценить расход и притормозить, чем незаметно уйти за бюджет.
_FALLBACK_PRICING = {"input": 0.75, "cached_input": 0.075, "output": 4.50}

# Роли вызовов — совпадают с purpose в логах openai_called.
PURPOSE_BRAIN = "dialog_brain"
PURPOSE_COMPLAINT_ACK = "complaint_ack"
PURPOSE_HUMAN_MESSAGE = "human_message"
PURPOSE_HUMANIZE = "humanize_reply"


def _normalize_model(model: str) -> str:
    name = str(model or "").strip().lower()
    if name in MODEL_PRICING_USD_PER_1M:
        return name
    # gpt-4o-mini-2024-07-18 → gpt-4o-mini
    for known in MODEL_PRICING_USD_PER_1M:
        if name.startswith(known):
            return known
    return name


def pricing_for(model: str) -> dict[str, float]:
    return MODEL_PRICING_USD_PER_1M.get(_normalize_model(model), _FALLBACK_PRICING)


def estimate_cost_usd(
    model: str,
    *,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    cached_tokens: int = 0,
) -> float:
    """Стоимость одного вызова в USD.

    cached_tokens — часть prompt_tokens, поэтому кэшированные токены тарифицируются
    по сниженной ставке, а не добавляются сверху.
    """
    price = pricing_for(model)
    prompt_tokens = max(0, int(prompt_tokens or 0))
    completion_tokens = max(0, int(completion_tokens or 0))
    cached_tokens = max(0, min(int(cached_tokens or 0), prompt_tokens))
    fresh_prompt = prompt_tokens - cached_tokens
    cost = (
        fresh_prompt * price["input"]
        + cached_tokens * price["cached_input"]
        + completion_tokens * price["output"]
    ) / 1_000_000
    return round(cost, 8)


def extract_usage(response: Any) -> dict[str, int]:
    """Достать usage из ответа OpenAI, не падая на отсутствующих полях."""
    usage = getattr(response, "usage", None)
    if usage is None:
        return {"prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0}

    def _int(source: Any, name: str) -> int:
        value = getattr(source, name, None)
        if value is None and isinstance(source, dict):
            value = source.get(name)
        try:
            return max(0, int(value or 0))
        except Exception:
            return 0

    cached = 0
    details = getattr(usage, "prompt_tokens_details", None)
    if details is None and isinstance(usage, dict):
        details = usage.get("prompt_tokens_details")
    if details is not None:
        cached = _int(details, "cached_tokens")

    return {
        "prompt_tokens": _int(usage, "prompt_tokens"),
        "completion_tokens": _int(usage, "completion_tokens"),
        "cached_tokens": cached,
    }


def current_month() -> str:
    return astana_now().strftime("%Y-%m")


def current_day() -> str:
    return astana_now().strftime("%Y-%m-%d")


def monthly_budget_usd() -> float:
    settings = get_settings()
    return max(0.0, float(getattr(settings, "monthly_ai_budget_usd", 0.0) or 0.0))


def max_calls_per_day() -> int:
    settings = get_settings()
    return max(0, int(getattr(settings, "ai_max_classifier_calls_per_day", 0) or 0))


def budget_status() -> dict[str, Any]:
    """Текущее состояние бюджета: сколько потрачено, сколько осталось, что блокируется."""
    month = current_month()
    day = current_day()
    budget = monthly_budget_usd()
    spent = float(state.ai_usage_month_cost_usd(month)) if state is not None else 0.0
    calls_today = int(state.ai_usage_day_calls(day)) if state is not None else 0
    calls_limit = max_calls_per_day()
    percent = (spent / budget * 100.0) if budget > 0 else 0.0
    return {
        "month": month,
        "day": day,
        "budget_usd": round(budget, 4),
        "spent_usd": round(spent, 6),
        "remaining_usd": round(max(0.0, budget - spent), 6),
        "percent_used": round(percent, 2),
        "over_budget": bool(budget > 0 and spent >= budget),
        "warning_threshold_reached": bool(budget > 0 and percent >= 80.0),
        "calls_today": calls_today,
        "calls_limit_per_day": calls_limit,
        "daily_calls_exhausted": bool(calls_limit > 0 and calls_today >= calls_limit),
    }


def check_allowed(purpose: str) -> tuple[bool, str]:
    """Можно ли сейчас звать OpenAI.

    Возвращает (allowed, reason). reason непустой только когда вызов запрещён:
      * monthly_budget_exceeded — исчерпан месячный бюджет;
      * daily_call_limit_exceeded — исчерпан суточный лимит вызовов.
    В обоих случаях вызывающий код обязан вернуть свой rule-based fallback.
    """
    if state is None:
        return True, ""
    try:
        status = budget_status()
    except Exception:
        # Учёт не должен ломать диалог: если статистика недоступна — не блокируем.
        return True, ""

    if status["over_budget"]:
        _log_block(purpose, "monthly_budget_exceeded", status)
        return False, "monthly_budget_exceeded"
    if status["daily_calls_exhausted"]:
        _log_block(purpose, "daily_call_limit_exceeded", status)
        return False, "daily_call_limit_exceeded"
    return True, ""


def _log_block(purpose: str, reason: str, status: dict[str, Any]) -> None:
    if state is None:
        return
    try:
        state.log_event("system", "ai_budget_blocked_call", {
            "level": "warning",
            "purpose": purpose,
            "reason": reason,
            "month": status["month"],
            "spent_usd": status["spent_usd"],
            "budget_usd": status["budget_usd"],
            "percent_used": status["percent_used"],
            "calls_today": status["calls_today"],
            "calls_limit_per_day": status["calls_limit_per_day"],
            "degraded_to": "rule_based_fallback",
        })
    except Exception:
        pass


def record_usage(response: Any, *, model: str, purpose: str) -> dict[str, Any]:
    """Записать расход одного вызова и, при пересечении 80%, предупредить в логи."""
    if state is None:
        return {"cost_usd": 0.0}
    try:
        tokens = extract_usage(response)
        cost = estimate_cost_usd(
            model,
            prompt_tokens=tokens["prompt_tokens"],
            completion_tokens=tokens["completion_tokens"],
            cached_tokens=tokens["cached_tokens"],
        )
        month = current_month()
        budget = monthly_budget_usd()
        spent_before = float(state.ai_usage_month_cost_usd(month))

        state.record_ai_usage(
            day=current_day(),
            month=month,
            model=model,
            purpose=purpose,
            prompt_tokens=tokens["prompt_tokens"],
            completion_tokens=tokens["completion_tokens"],
            cached_tokens=tokens["cached_tokens"],
            cost_usd=cost,
        )

        spent_after = spent_before + cost
        state.log_event("system", "ai_usage_recorded", {
            "purpose": purpose,
            "model": model,
            "prompt_tokens": tokens["prompt_tokens"],
            "completion_tokens": tokens["completion_tokens"],
            "cached_tokens": tokens["cached_tokens"],
            "cost_usd": round(cost, 6),
            "month_spent_usd": round(spent_after, 6),
            "budget_usd": round(budget, 4),
        })

        if budget > 0:
            before_pct = spent_before / budget * 100.0
            after_pct = spent_after / budget * 100.0
            # Предупреждаем один раз — в момент пересечения порога, а не на каждом вызове.
            if before_pct < 80.0 <= after_pct:
                state.log_event("system", "ai_budget_warning", {
                    "level": "warning",
                    "month": month,
                    "percent_used": round(after_pct, 2),
                    "spent_usd": round(spent_after, 6),
                    "budget_usd": round(budget, 4),
                    "remaining_usd": round(max(0.0, budget - spent_after), 6),
                    "note": "80% месячного бюджета AI израсходовано",
                })
            if before_pct < 100.0 <= after_pct:
                state.log_event("system", "ai_budget_exceeded", {
                    "level": "critical",
                    "month": month,
                    "percent_used": round(after_pct, 2),
                    "spent_usd": round(spent_after, 6),
                    "budget_usd": round(budget, 4),
                    "note": "бюджет исчерпан, бот переходит в rule-based fallback до следующего месяца",
                })
        return {"cost_usd": cost, **tokens}
    except Exception:
        # Учёт расхода никогда не должен ронять ответ пациенту.
        return {"cost_usd": 0.0}
