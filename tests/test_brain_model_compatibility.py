"""Фаза 1B: совместимость reasoning-модели gpt-5.4-mini с вызовом брейна.

Подтверждённая проблема (проверено 17.08.2026 по первоисточникам):
gpt-5.x отвергает max_tokens и требует max_completion_tokens —
  "Unsupported parameter: 'max_tokens' is not supported with this model.
   Use 'max_completion_tokens' instead."
С AI_BRAIN_MODEL=gpt-5.4-mini брейн падал бы в openai_error и уходил в
rule-based fallback на КАЖДОМ вызове, то есть понимание пациента не работало бы.

temperature gpt-5.4-mini поддерживает — её намеренно не трогаем.

Роли моделей не унифицируются: брейн на gpt-5.4-mini, тон/форма на gpt-4o-mini.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

os.environ["SQLITE_PATH"] = tempfile.NamedTemporaryFile(delete=False, suffix=".sqlite3").name
os.environ.setdefault("CRM_BOT_SECRET", "test")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

import ai
import state
from config import get_settings


def run(coro):
    return asyncio.run(coro)


_VALID_DECISION = (
    '{"intent":"complaint","action":"ask_age","next_step":"age","patient_meaning":"",'
    '"reply":"Подскажите, пожалуйста, сколько Вам лет?","extracted":{},"needs_python_tool":"none",'
    '"safety":{"hard_stop":false,"reason":"","unsafe_medical_claim":false,"tries_to_book_without_rules":false},'
    '"safety_flags":{"promised_cure":false,"asked_name_too_early":false,'
    '"offered_date_before_contra":false,"medical_diagnosis":false}}'
)


class _CapturingClient:
    """Запоминает kwargs вызова, чтобы проверить имя параметра лимита токенов."""

    def __init__(self, content: str = _VALID_DECISION, finish_reason: str = "stop"):
        self.captured: dict[str, Any] = {}
        self._content = content
        self._finish_reason = finish_reason
        outer = self

        class _Completions:
            async def create(self, **kwargs: Any):
                outer.captured = kwargs
                message = type("M", (), {"content": outer._content})()
                choice = type("C", (), {"message": message, "finish_reason": outer._finish_reason})()
                usage = type("U", (), {"prompt_tokens": 100, "completion_tokens": 50, "prompt_tokens_details": None})()
                return type("R", (), {"choices": [choice], "usage": usage})()

        self.chat = type("Chat", (), {"completions": _Completions()})()


@pytest.fixture(autouse=True)
def _enabled_openai(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    settings = get_settings()
    monkeypatch.setattr(settings, "sqlite_path", str(tmp_path / "compat.sqlite3"))
    monkeypatch.setattr(settings, "openai_api_key", "test-key")
    monkeypatch.setattr(settings, "ai_enabled", True)
    monkeypatch.setattr(settings, "openai_brain_enabled", True)
    monkeypatch.setattr(settings, "monthly_ai_budget_usd", 1000.0)
    monkeypatch.setattr(settings, "ai_max_classifier_calls_per_day", 0)
    state.init_db()
    yield


def _run_brain(monkeypatch: pytest.MonkeyPatch, client: _CapturingClient):
    monkeypatch.setattr(ai, "_openai_client", lambda *a, **k: client)
    return run(ai.run_openai_dialog_brain(
        user_text="болит спина",
        session={"chat_id": "compat_test", "step": "complaint"},
    ))


# --- имя параметра лимита токенов ------------------------------------------


@pytest.mark.parametrize("model", ["gpt-5.4-mini", "gpt-5-mini", "gpt-5.4"])
def test_gpt5_models_use_max_completion_tokens(model: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "ai_brain_model", model)
    client = _CapturingClient()
    _run_brain(monkeypatch, client)

    assert "max_completion_tokens" in client.captured
    assert "max_tokens" not in client.captured, "gpt-5.x отвергает max_tokens — вызов упал бы в fallback"


def test_gpt4o_mini_keeps_max_tokens_700(monkeypatch: pytest.MonkeyPatch) -> None:
    """Старое поведение для не-reasoning модели сохранено без изменений."""
    monkeypatch.setattr(get_settings(), "ai_brain_model", "gpt-4o-mini")
    client = _CapturingClient()
    _run_brain(monkeypatch, client)

    assert client.captured["max_tokens"] == 700
    assert "max_completion_tokens" not in client.captured


def test_reasoning_budget_is_larger_than_classic_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """В max_completion_tokens входят токены рассуждения — лимит должен быть выше 700."""
    monkeypatch.setattr(get_settings(), "ai_brain_model", "gpt-5.4-mini")
    client = _CapturingClient()
    _run_brain(monkeypatch, client)

    assert client.captured["max_completion_tokens"] > 700


def test_brain_token_limit_is_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "ai_brain_model", "gpt-5.4-mini")
    monkeypatch.setattr(get_settings(), "ai_brain_max_completion_tokens", 4096)
    client = _CapturingClient()
    _run_brain(monkeypatch, client)

    assert client.captured["max_completion_tokens"] == 4096


# --- temperature: поддерживается, не трогаем -------------------------------


def test_temperature_is_still_sent_for_gpt5(monkeypatch: pytest.MonkeyPatch) -> None:
    """gpt-5.4-mini поддерживает temperature — параметр намеренно оставлен."""
    monkeypatch.setattr(get_settings(), "ai_brain_model", "gpt-5.4-mini")
    monkeypatch.setattr(get_settings(), "ai_brain_temperature", 0.2)
    client = _CapturingClient()
    _run_brain(monkeypatch, client)

    assert client.captured["temperature"] == pytest.approx(0.2)


# --- обрезанный JSON и латентность -----------------------------------------


def test_truncated_response_is_logged_as_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    """finish_reason=length должен быть виден в логах, а не выглядеть как «модель глупая»."""
    monkeypatch.setattr(get_settings(), "ai_brain_model", "gpt-5.4-mini")
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(state, "log_event", lambda chat_id, event_type, payload: events.append((event_type, payload)))

    client = _CapturingClient(content='{"intent":"comp', finish_reason="length")
    decision, debug = _run_brain(monkeypatch, client)

    warnings = [p for name, p in events if name == "openai_brain_truncated_warning"]
    assert len(warnings) == 1
    assert warnings[0]["level"] == "warning"
    # Обрезанный JSON не ломает бота — уходим в rule-based fallback.
    assert decision["action"] == "fallback_rule_based"


def test_latency_is_measured(monkeypatch: pytest.MonkeyPatch) -> None:
    """P50/P95 брейна считаются из latency_ms в логах Railway."""
    monkeypatch.setattr(get_settings(), "ai_brain_model", "gpt-5.4-mini")
    client = _CapturingClient()
    decision, debug = _run_brain(monkeypatch, client)

    assert "openai_brain_latency_ms" in debug
    assert isinstance(debug["openai_brain_latency_ms"], int)
    assert debug["openai_brain_latency_ms"] >= 0


def test_latency_lands_in_decision_event(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "ai_brain_model", "gpt-5.4-mini")
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(state, "log_event", lambda chat_id, event_type, payload: events.append((event_type, payload)))

    _run_brain(monkeypatch, client=_CapturingClient())

    decisions = [p for name, p in events if name == "openai_brain_decision"]
    assert len(decisions) == 1
    assert "latency_ms" in decisions[0]
    assert decisions[0]["model"] == "gpt-5.4-mini"


# --- распределение моделей по ролям не унифицировано -----------------------


def test_model_roles_stay_separate() -> None:
    """Брейн и очеловечивание намеренно живут на разных моделях."""
    settings = get_settings()
    assert hasattr(settings, "ai_brain_model")
    assert hasattr(settings, "openai_model")
    assert hasattr(settings, "ai_humanize_model")


def test_humanize_call_untouched_by_phase_1b(monkeypatch: pytest.MonkeyPatch) -> None:
    """generate_complaint_ack/humanize остаются на max_tokens — их эта фаза не трогает."""
    settings = get_settings()
    monkeypatch.setattr(settings, "openai_model", "gpt-4o-mini")
    client = _CapturingClient(content="Понимаю Вас")
    monkeypatch.setattr(ai, "_openai_client", lambda *a, **k: client)

    run(ai.generate_complaint_ack("болит спина", "ru", chat_id="compat_test"))

    assert client.captured["model"] == "gpt-4o-mini"
    assert "max_tokens" in client.captured
