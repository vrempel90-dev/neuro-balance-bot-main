from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest

os.environ["SQLITE_PATH"] = tempfile.NamedTemporaryFile(delete=False, suffix=".sqlite3").name
os.environ.setdefault("CRM_BOT_SECRET", "test")
os.environ.setdefault("OPENAI_API_KEY", "test-key")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import ai
import ai_budget
import dialog
import main
import state
from config import get_settings

state.init_db()


def run(coro: Any) -> Any:
    return asyncio.run(coro)


class _FakeMessage:
    content = "{}"


class _FakeChoice:
    message = _FakeMessage()
    finish_reason = "stop"


class _FakeResponse:
    choices = [_FakeChoice()]


class _FakeCompletions:
    async def create(self, **kwargs: Any) -> _FakeResponse:
        return _FakeResponse()


class _FakeChat:
    completions = _FakeCompletions()


class _FakeClient:
    chat = _FakeChat()


def _decision() -> dict[str, Any]:
    return {
        "intent": "complaint",
        "action": "ask_age",
        "next_step": "age",
        "patient_meaning": "болит спина",
        "reply": "Подскажите, пожалуйста, сколько Вам лет?",
        "extracted": {
            "complaint": "болит спина",
            "age": None,
            "contraindications_clear": None,
            "contraindication_confirmed": False,
            "contraindication_term_asked": "",
            "contraindication_red_flags": [],
            "symptom_duration": "",
            "preferred_date_text": "",
            "time_preference": "",
            "slot_choice": None,
            "patient_name": "",
            "patient_relation": "",
            "wants_human": False,
            "faq_type": "",
            "language": "ru",
            "detected_language": "ru",
            "preferred_response_language": "ru",
            "language_confidence": 0.9,
        },
        "needs_python_tool": "none",
        "safety": {
            "hard_stop": False,
            "reason": "",
            "unsafe_medical_claim": False,
            "tries_to_book_without_rules": False,
        },
        "safety_flags": {},
    }


def test_brain_debug_exposes_recorded_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "openai_api_key", "test-key")
    monkeypatch.setattr(settings, "ai_enabled", True)
    monkeypatch.setattr(settings, "openai_brain_enabled", True)
    monkeypatch.setattr(ai, "_openai_client", lambda key: _FakeClient())
    monkeypatch.setattr(ai, "_normalize_dialog_brain_decision", lambda raw: (_decision(), ""))
    monkeypatch.setattr(ai_budget, "check_allowed", lambda purpose: (True, ""))
    monkeypatch.setattr(
        ai_budget,
        "record_usage",
        lambda response, *, model, purpose: {
            "prompt_tokens": 321,
            "completion_tokens": 45,
            "cached_tokens": 120,
            "cost_usd": 0.001234,
        },
    )

    decision, debug = run(
        ai.run_openai_dialog_brain(
            user_text="болит спина",
            session={"chat_id": "telemetry", "step": "complaint"},
        )
    )

    assert decision["action"] == "ask_age"
    assert debug["openai_brain_used"] is True
    assert debug["openai_prompt_tokens"] == 321
    assert debug["openai_completion_tokens"] == 45
    assert debug["openai_cached_tokens"] == 120
    assert debug["openai_cost_usd"] == pytest.approx(0.001234)


def test_brain_debug_fields_are_persisted_to_session() -> None:
    session: dict[str, Any] = {}
    dialog._apply_openai_brain_debug(
        session,
        {
            "openai_prompt_tokens": 100,
            "openai_completion_tokens": 20,
            "openai_cached_tokens": 50,
            "openai_cost_usd": 0.0004,
            "openai_config_missing_detail": {"missing_keys": ["OPENAI_API_KEY"]},
            "openai_missing_keys": ["OPENAI_API_KEY"],
            "openai_disabled_flags": [],
        },
    )
    assert session["openai_prompt_tokens"] == 100
    assert session["openai_completion_tokens"] == 20
    assert session["openai_cached_tokens"] == 50
    assert session["openai_cost_usd"] == pytest.approx(0.0004)
    assert session["openai_missing_keys"] == ["OPENAI_API_KEY"]


def test_dialog_debug_surfaces_openai_runtime_evidence() -> None:
    debug = main._dialog_debug(
        {
            "openai_used": True,
            "openai_brain_used": True,
            "openai_brain_model": "gpt-test",
            "openai_prompt_tokens": 111,
            "openai_completion_tokens": 22,
            "openai_cached_tokens": 33,
            "openai_cost_usd": 0.0005,
            "openai_config_missing_detail": {},
            "openai_missing_keys": [],
            "openai_disabled_flags": [],
        },
        "ok",
    )
    assert debug["openai_prompt_tokens"] == 111
    assert debug["openai_completion_tokens"] == 22
    assert debug["openai_cached_tokens"] == 33
    assert debug["openai_cost_usd"] == pytest.approx(0.0005)


def test_ai_diagnostic_events_are_visible_in_railway_stdout() -> None:
    required = {
        "openai_brain_called",
        "openai_brain_decision",
        "openai_config_missing_detail",
        "openai_error_detail",
        "ai_usage_recorded",
    }
    assert required.issubset(state._STDOUT_EVENTS)


def test_startup_source_logs_non_secret_ai_configuration() -> None:
    source = Path(main.__file__).read_text(encoding="utf-8")
    assert '"openai_api_key_present"' in source
    assert '"ai_enabled"' in source
    assert '"openai_brain_enabled"' in source
    assert '"openai_brain_model"' in source
    assert '"openai_humanize_replies"' in source


def test_disabled_humanizer_is_not_reported_as_missing_openai_config(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "openai_api_key", "test-key")
    monkeypatch.setattr(settings, "ai_enabled", True)
    monkeypatch.setattr(settings, "openai_humanize_replies", False)

    answer, debug = run(
        ai.humanize_reply_with_openai(
            base_answer="Подскажите, пожалуйста, сколько Вам лет?",
            user_text="болит спина",
            session={"chat_id": "humanizer-disabled", "step": "age"},
        )
    )

    assert answer == "Подскажите, пожалуйста, сколько Вам лет?"
    assert debug["openai_used"] is False
    assert debug["openai_skip_reason"] == "humanizer_disabled"
    assert debug.get("openai_missing_keys") in (None, [])
    assert debug["openai_disabled_flags"] == ["OPENAI_HUMANIZE_REPLIES=false"]
