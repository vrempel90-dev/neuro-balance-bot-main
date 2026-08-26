"""Фаза 1A: реальный контроль расхода OpenAI и месячного бюджета.

MONTHLY_AI_BUDGET_KZT и AI_MAX_CLASSIFIER_CALLS_PER_DAY были объявлены в config.py,
но нигде не использовались — лимита трат не существовало. Здесь закреплено, что:
  * usage каждого вызова пишется в state с разбивкой по дню/месяцу/модели;
  * токены переводятся в USD по таблице цен;
  * на 80% лимита появляется warning-событие в логах Railway;
  * на 100% вызовы OpenAI прекращаются и бот уходит в rule-based fallback,
    не падая и не молча;
  * AI_MAX_CLASSIFIER_CALLS_PER_DAY ограничивает число вызовов в сутки.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

os.environ["SQLITE_PATH"] = tempfile.NamedTemporaryFile(delete=False, suffix=".sqlite3").name
os.environ.setdefault("CRM_BOT_SECRET", "test")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

import ai
import ai_budget
import state
from config import get_settings


def run(coro):
    return asyncio.run(coro)


class _FakeUsage:
    def __init__(self, prompt: int, completion: int, cached: int = 0):
        self.prompt_tokens = prompt
        self.completion_tokens = completion
        self.prompt_tokens_details = type("D", (), {"cached_tokens": cached})()


class _FakeResponse:
    def __init__(self, content: str = "ok", prompt: int = 1000, completion: int = 100, cached: int = 0):
        message = type("M", (), {"content": content})()
        self.choices = [type("C", (), {"message": message})()]
        self.usage = _FakeUsage(prompt, completion, cached)


@pytest.fixture(autouse=True)
def _isolated_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    settings = get_settings()
    monkeypatch.setattr(settings, "sqlite_path", str(tmp_path / "budget.sqlite3"))
    state.init_db()
    yield


# --- таблица цен и расчёт стоимости ---------------------------------------


def test_pricing_table_matches_documented_ratio() -> None:
    """gpt-5.4-mini дороже gpt-4o-mini в 5x по input и 7.5x по output."""
    brain = ai_budget.MODEL_PRICING_USD_PER_1M["gpt-5.4-mini"]
    cheap = ai_budget.MODEL_PRICING_USD_PER_1M["gpt-4o-mini"]

    assert brain["input"] / cheap["input"] == pytest.approx(5.0)
    assert brain["output"] / cheap["output"] == pytest.approx(7.5)


def test_estimate_cost_usd_uses_per_million_rates() -> None:
    # 1M input + 1M output на gpt-4o-mini = 0.15 + 0.60
    cost = ai_budget.estimate_cost_usd("gpt-4o-mini", prompt_tokens=1_000_000, completion_tokens=1_000_000)
    assert cost == pytest.approx(0.75)


def test_cached_tokens_are_billed_at_reduced_rate() -> None:
    """cached_tokens — часть prompt_tokens, а не добавка сверху."""
    full = ai_budget.estimate_cost_usd("gpt-4o-mini", prompt_tokens=1_000_000, cached_tokens=0)
    half_cached = ai_budget.estimate_cost_usd("gpt-4o-mini", prompt_tokens=1_000_000, cached_tokens=500_000)

    assert full == pytest.approx(0.15)
    assert half_cached == pytest.approx(0.5 * 0.15 + 0.5 * 0.075)
    assert half_cached < full


def test_unknown_model_uses_conservative_pricing() -> None:
    """Неизвестная модель считается по дорогому тарифу — чтобы не занизить расход."""
    unknown = ai_budget.estimate_cost_usd("some-future-model", prompt_tokens=1_000_000)
    # Дорогая верхняя граница — самая дорогая известная модель, а не первая
    # попавшаяся: тариф gpt-5.4-mini в прайсе устарел ещё до перехода на 5.6.
    most_expensive = max(
        ai_budget.estimate_cost_usd(model, prompt_tokens=1_000_000)
        for model in ai_budget.MODEL_PRICING_USD_PER_1M
    )
    assert unknown == pytest.approx(most_expensive)


def test_model_name_with_date_suffix_is_normalized() -> None:
    assert ai_budget.pricing_for("gpt-4o-mini-2024-07-18") == ai_budget.MODEL_PRICING_USD_PER_1M["gpt-4o-mini"]


def test_extract_usage_survives_missing_fields() -> None:
    assert ai_budget.extract_usage(object()) == {"prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0}


# --- запись расхода --------------------------------------------------------


def test_usage_is_recorded_with_day_month_model_breakdown() -> None:
    ai_budget.record_usage(_FakeResponse(prompt=1000, completion=500), model="gpt-4o-mini", purpose="humanize_reply")

    breakdown = state.ai_usage_breakdown(month=ai_budget.current_month())
    assert len(breakdown) == 1
    row = breakdown[0]
    assert row["model"] == "gpt-4o-mini"
    assert row["purpose"] == "humanize_reply"
    assert row["prompt_tokens"] == 1000
    assert row["completion_tokens"] == 500
    assert row["cost_usd"] > 0
    assert state.ai_usage_day_calls(ai_budget.current_day()) == 1


def test_cached_tokens_are_persisted() -> None:
    ai_budget.record_usage(_FakeResponse(prompt=1000, completion=10, cached=800), model="gpt-4o-mini", purpose="dialog_brain")
    assert state.ai_usage_breakdown(month=ai_budget.current_month())[0]["cached_tokens"] == 800


# --- пороги 80% и 100% -----------------------------------------------------


def _spend(usd: float, monkeypatch: pytest.MonkeyPatch) -> None:
    """Записать заданный расход напрямую, не гоняя токены."""
    state.record_ai_usage(
        day=ai_budget.current_day(),
        month=ai_budget.current_month(),
        model="gpt-5.4-mini",
        purpose="dialog_brain",
        cost_usd=usd,
    )


def test_warning_event_at_80_percent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "monthly_ai_budget_usd", 10.0)
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(state, "log_event", lambda chat_id, event_type, payload: events.append((event_type, payload)))

    _spend(7.9, monkeypatch)
    # Этот вызов переводит расход через порог 80%.
    ai_budget.record_usage(_FakeResponse(prompt=1_000_000, completion=0), model="gpt-4o-mini", purpose="dialog_brain")

    warnings = [p for name, p in events if name == "ai_budget_warning"]
    assert len(warnings) == 1
    assert warnings[0]["level"] == "warning"
    assert warnings[0]["percent_used"] >= 80.0


def test_warning_is_emitted_once_not_on_every_call(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "monthly_ai_budget_usd", 10.0)
    events: list[str] = []
    monkeypatch.setattr(state, "log_event", lambda chat_id, event_type, payload: events.append(event_type))

    _spend(7.9, monkeypatch)
    for _ in range(3):
        ai_budget.record_usage(_FakeResponse(prompt=1_000_000, completion=0), model="gpt-4o-mini", purpose="dialog_brain")

    assert events.count("ai_budget_warning") == 1


def test_budget_status_reports_over_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "monthly_ai_budget_usd", 10.0)
    _spend(10.5, monkeypatch)

    status = ai_budget.budget_status()
    assert status["over_budget"] is True
    assert status["percent_used"] >= 100.0
    assert status["remaining_usd"] == 0.0


def test_check_allowed_blocks_when_budget_exhausted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "monthly_ai_budget_usd", 10.0)
    _spend(10.5, monkeypatch)

    allowed, reason = ai_budget.check_allowed("dialog_brain")
    assert allowed is False
    assert reason == "monthly_budget_exceeded"


def test_check_allowed_passes_below_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "monthly_ai_budget_usd", 10.0)
    _spend(1.0, monkeypatch)

    assert ai_budget.check_allowed("dialog_brain") == (True, "")


def test_daily_call_limit_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "monthly_ai_budget_usd", 1000.0)
    monkeypatch.setattr(get_settings(), "ai_max_classifier_calls_per_day", 3)

    for _ in range(3):
        ai_budget.record_usage(_FakeResponse(prompt=10, completion=1), model="gpt-4o-mini", purpose="dialog_brain")

    allowed, reason = ai_budget.check_allowed("dialog_brain")
    assert allowed is False
    assert reason == "daily_call_limit_exceeded"


def test_budget_accounting_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Учёт расхода не должен ронять ответ пациенту."""
    def boom(*args, **kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(state, "record_ai_usage", boom)
    assert ai_budget.record_usage(_FakeResponse(), model="gpt-4o-mini", purpose="dialog_brain") == {"cost_usd": 0.0}

    monkeypatch.setattr(state, "ai_usage_month_cost_usd", boom)
    assert ai_budget.check_allowed("dialog_brain") == (True, "")


# --- деградация в rule-based fallback --------------------------------------


def test_brain_degrades_to_rule_based_fallback_over_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """Главное требование: превышение лимита → rule-based fallback, без падения и молчания."""
    settings = get_settings()
    monkeypatch.setattr(settings, "monthly_ai_budget_usd", 10.0)
    monkeypatch.setattr(settings, "openai_api_key", "test-key")
    monkeypatch.setattr(settings, "ai_enabled", True)
    monkeypatch.setattr(settings, "openai_brain_enabled", True)
    _spend(10.5, monkeypatch)

    called = {"openai": False}

    def fail_if_called(*args, **kwargs):
        called["openai"] = True
        raise AssertionError("OpenAI не должен вызываться при исчерпанном бюджете")

    monkeypatch.setattr(ai, "_openai_client", fail_if_called)

    decision, debug = run(ai.run_openai_dialog_brain(
        user_text="болит спина",
        session={"chat_id": "budget_test", "step": "complaint"},
    ))

    assert called["openai"] is False
    assert decision["action"] == "fallback_rule_based"
    assert debug["openai_brain_skip_reason"] == "monthly_budget_exceeded"


def test_humanize_returns_base_answer_over_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """Очеловечивание отключается, но ответ пациенту остаётся непустым."""
    settings = get_settings()
    monkeypatch.setattr(settings, "monthly_ai_budget_usd", 10.0)
    monkeypatch.setattr(settings, "openai_api_key", "test-key")
    monkeypatch.setattr(settings, "ai_enabled", True)
    monkeypatch.setattr(settings, "openai_humanize_replies", True)
    _spend(10.5, monkeypatch)

    def fail_if_called(*args, **kwargs):
        raise AssertionError("OpenAI не должен вызываться при исчерпанном бюджете")

    monkeypatch.setattr(ai, "_openai_client", fail_if_called)

    answer, debug = run(ai.humanize_reply_with_openai(
        base_answer="Подскажите, сколько Вам лет?",
        user_text="болит спина",
        session={"chat_id": "budget_test", "step": "age"},
    ))

    assert answer == "Подскажите, сколько Вам лет?"
    assert answer.strip(), "бот не должен молчать при исчерпанном бюджете"
    assert debug["openai_skip_reason"] == "monthly_budget_exceeded"


def test_complaint_ack_falls_back_over_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "monthly_ai_budget_usd", 10.0)
    monkeypatch.setattr(settings, "openai_api_key", "test-key")
    monkeypatch.setattr(settings, "ai_enabled", True)
    _spend(10.5, monkeypatch)

    def fail_if_called(*args, **kwargs):
        raise AssertionError("OpenAI не должен вызываться при исчерпанном бюджете")

    monkeypatch.setattr(ai, "_openai_client", fail_if_called)

    answer = run(ai.generate_complaint_ack("болит спина", "ru", chat_id="budget_test"))
    assert answer.strip(), "бот не должен молчать при исчерпанном бюджете"


def test_new_month_resets_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """Лимит снимается с началом следующего расчётного месяца."""
    monkeypatch.setattr(get_settings(), "monthly_ai_budget_usd", 10.0)
    state.record_ai_usage(day="2026-07-31", month="2026-07", model="gpt-5.4-mini", purpose="dialog_brain", cost_usd=99.0)

    # Расход прошлого месяца не блокирует текущий.
    assert ai_budget.check_allowed("dialog_brain") == (True, "")
    assert state.ai_usage_month_cost_usd("2026-07") == pytest.approx(99.0)
