"""Regression coverage for production dialog hardening.

These tests target failures that previously allowed stale-slot booking attempts,
deferred "I'll get back to you" dead ends, and exact answer loops.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

os.environ["SQLITE_PATH"] = tempfile.NamedTemporaryFile(delete=False, suffix=".sqlite3").name
os.environ.setdefault("CRM_BOT_SECRET", "test")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import agent
import ai
import crm
import dialog
import main
import state
from config import get_settings
from fake_openai import FakeOpenAIClient, assistant_text

PHONE = "77015550199"
DOCTOR_LOGIN = "zhuma_md"
DOCTOR_NAME = "Жумабек Мади Мухтарович"
DATE = "2026-09-22"


@pytest.fixture(autouse=True)
def _runtime(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("AI_ENABLED", "true")
    monkeypatch.setenv("OPENAI_BRAIN_ENABLED", "true")
    get_settings.cache_clear()
    monkeypatch.setattr(ai, "AsyncOpenAI", object, raising=False)
    monkeypatch.setattr(agent.ai_budget, "check_allowed", lambda purpose: (True, ""))
    monkeypatch.setattr(agent.ai_budget, "record_usage", lambda *a, **k: {})
    yield
    get_settings.cache_clear()


def _ready_session() -> dict[str, Any]:
    return {
        "phone": PHONE,
        "language": "ru",
        "complaint": "болит поясница",
        "complaint_gate": "COMPLAINT_OK",
        "age": 34,
        "contraindications_ok": True,
        "contraindications_verdict": "proceed",
        "contraindications_raw": "нет",
        "patient_name": "Асель",
        "known_user_facts": {
            "complaint": "болит поясница",
            "age": 34,
            "patient_name": "Асель",
        },
    }


def test_booking_revalidates_slot_immediately_before_post(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _ready_session()
    agent._remember_offered_slots(
        session,
        [{
            "doctor_login": DOCTOR_LOGIN,
            "doctor_name": DOCTOR_NAME,
            "date": DATE,
            "time_start": "14:00",
        }],
    )

    calls = {"book": 0, "check": 0}

    async def no_longer_available(date: str, doctor_login: str | None = None) -> dict[str, Any]:
        calls["check"] += 1
        return {"ok": True, "date": date, "availability": []}

    async def must_not_book(**kwargs: Any) -> dict[str, Any]:
        calls["book"] += 1
        return {"ok": True, "id": 1}

    monkeypatch.setattr(crm, "check_slots", no_longer_available)
    monkeypatch.setattr(crm, "book_appointment", must_not_book)
    monkeypatch.setattr(crm, "clear_slots_cache", lambda date=None: None)
    monkeypatch.setattr(agent, "state", None)

    result = asyncio.run(
        agent._tool_book_appointment(
            "revalidate_stale",
            session,
            PHONE,
            {
                "patient_name": "Асель",
                "doctor_login": DOCTOR_LOGIN,
                "date": DATE,
                "time_start": "14:00",
            },
        )
    )

    assert calls["check"] == 1
    assert calls["book"] == 0
    assert result["booking_success"] is False
    assert result["error"] == "slot_conflict"
    assert session.get("booking_confirmed") is not True


def test_deferred_reply_is_not_allowed_to_end_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeOpenAIClient([
        assistant_text("Секунду, уточню информацию и вернусь к Вам 🌿"),
        assistant_text("Как Вас зовут?"),
    ])
    monkeypatch.setattr(ai, "_openai_client", lambda api_key: client)

    result = asyncio.run(
        agent.run_agent_turn(
            chat_id="no_deferred_dead_end",
            phone=PHONE,
            session={"language": "ru"},
            user_text="Хочу записаться",
            recent_history=[],
        )
    )

    assert result.reply == "Как Вас зовут?"
    assert result.error == ""
    assert len(client.calls) == 2


def test_exact_previous_reply_is_detected_without_an_extra_model_call(monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeOpenAIClient([
        assistant_text("Сколько Вам лет?"),
    ])
    monkeypatch.setattr(ai, "_openai_client", lambda api_key: client)
    session = {"language": "ru", "last_assistant_answer": "Сколько Вам лет?"}

    result = asyncio.run(
        agent.run_agent_turn(
            chat_id="duplicate_question_guard",
            phone=PHONE,
            session=session,
            user_text="34",
            recent_history=[{"role": "assistant", "text": "Сколько Вам лет?"}],
        )
    )

    assert result.reply == "Сколько Вам лет?"
    assert result.error == "duplicate_model_reply"
    assert len(client.calls) == 1


def test_repeated_no_progress_escalates_instead_of_looping(monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeOpenAIClient([
        assistant_text("Секунду, уточню информацию и вернусь к Вам 🌿"),
        assistant_text("Секунду, уточню информацию и вернусь к Вам 🌿"),
    ])
    monkeypatch.setattr(ai, "_openai_client", lambda api_key: client)
    session = {"language": "ru"}

    result = asyncio.run(
        agent.run_agent_turn(
            chat_id="no_progress_escalation",
            phone=PHONE,
            session=session,
            user_text="Меня зовут Азамат",
            recent_history=[],
        )
    )

    assert result.escalate is True
    assert result.outcome == agent.OUTCOME_OPERATOR_ESCALATION
    assert session["manual_takeover"] is True
    assert "вернусь" not in result.reply.lower()
    assert result.reply.strip()
    assert len(client.calls) == 2



def test_confirmed_booking_never_reenters_ai_even_if_crm_is_stale(monkeypatch: pytest.MonkeyPatch) -> None:
    chat_id = "post_booking_closed"
    state.init_db()
    state.reset_session(chat_id)
    session = state.get_session(chat_id)
    session.update({
        "phone": PHONE,
        "language": "ru",
        "booking_confirmed": True,
        "booked": True,
        "appointment_id": "9001",
    })
    state.save_session(chat_id, session)

    async def classify_must_not_run(*args: Any, **kwargs: Any):
        raise AssertionError("post-booking turn must not depend on CRM admission lookup")

    monkeypatch.setattr(dialog, "_classify_lead", classify_must_not_run)

    answer = asyncio.run(dialog.handle_message(chat_id, PHONE, "Спасибо"))

    assert answer == ""
    saved = state.get_session(chat_id)
    assert saved["no_reply_reason"] == "booking_already_completed"



def test_debug_routes_can_be_locked_down_for_staging(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEBUG_ENDPOINTS_REQUIRE_TOKEN", "true")
    monkeypatch.setenv("DEBUG_ADMIN_TOKEN", "staging-test-token")
    get_settings.cache_clear()

    with TestClient(main.app) as client:
        denied = client.post("/debug/reset", json={"chat_id": "protected"})
        assert denied.status_code == 401

        allowed = client.post(
            "/debug/reset",
            json={"chat_id": "protected"},
            headers={"x-debug-token": "staging-test-token"},
        )
        assert allowed.status_code == 200
        assert allowed.json()["ok"] is True

    get_settings.cache_clear()



def test_staging_crm_write_switch_blocks_real_booking(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CRM_WRITE_ENABLED", "false")
    get_settings.cache_clear()

    session = _ready_session()
    agent._remember_offered_slots(
        session,
        [{
            "doctor_login": DOCTOR_LOGIN,
            "doctor_name": DOCTOR_NAME,
            "date": DATE,
            "time_start": "14:00",
        }],
    )

    calls = {"book": 0, "check": 0}

    async def must_not_check(*args: Any, **kwargs: Any) -> dict[str, Any]:
        calls["check"] += 1
        raise AssertionError("staging write guard must stop before booking revalidation")

    async def must_not_book(**kwargs: Any) -> dict[str, Any]:
        calls["book"] += 1
        raise AssertionError("staging write guard must prevent CRM booking POST")

    monkeypatch.setattr(crm, "check_slots", must_not_check)
    monkeypatch.setattr(crm, "book_appointment", must_not_book)

    result = asyncio.run(
        agent._tool_book_appointment(
            "staging_write_block",
            session,
            PHONE,
            {
                "patient_name": "Асель",
                "doctor_login": DOCTOR_LOGIN,
                "date": DATE,
                "time_start": "14:00",
            },
        )
    )

    assert result["booking_success"] is False
    assert result["error"] == "crm_write_disabled"
    assert calls == {"book": 0, "check": 0}
    assert session.get("booking_confirmed") is not True

    get_settings.cache_clear()



def test_crm_write_allowlist_only_permits_configured_test_phone(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CRM_WRITE_ENABLED", "true")
    monkeypatch.setenv("CRM_WRITE_TEST_PHONE", "+7 701 111 22 33")
    get_settings.cache_clear()

    assert agent._crm_write_allowed_for_phone("77011112233") == (True, "")
    assert agent._crm_write_allowed_for_phone("77019998877") == (
        False,
        "crm_write_phone_not_allowed",
    )

    get_settings.cache_clear()
