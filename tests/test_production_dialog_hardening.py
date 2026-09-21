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

os.environ["SQLITE_PATH"] = tempfile.NamedTemporaryFile(delete=False, suffix=".sqlite3").name
os.environ.setdefault("CRM_BOT_SECRET", "test")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import agent
import ai
import crm
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


def test_exact_previous_reply_gets_one_recovery_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeOpenAIClient([
        assistant_text("Сколько Вам лет?"),
        assistant_text("Есть ли противопоказания из списка клиники?"),
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

    assert result.reply == "Есть ли противопоказания из списка клиники?"
    assert len(client.calls) == 2


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
