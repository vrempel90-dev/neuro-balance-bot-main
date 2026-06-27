from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

os.environ["SQLITE_PATH"] = tempfile.NamedTemporaryFile(delete=False, suffix=".sqlite3").name
os.environ.setdefault("CRM_BOT_SECRET", "test")
os.environ.setdefault("OPENAI_API_KEY", "")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

import crm
import main
import state


def setup_function():
    state.init_db()


def test_debug_chat_working_hours_no_reply_and_no_openai(monkeypatch):
    monkeypatch.setattr(main, "is_bot_work_time", lambda: False)
    async def should_not_call(*args, **kwargs):
        raise AssertionError("handle_message/OpenAI path must not be called during working hours")
    monkeypatch.setattr(main, "handle_message", should_not_call)

    response = TestClient(main.app).post("/debug/chat", json={"chat_id": "wh_day", "text": "Здравствуйте", "force": False})
    data = response.json()

    assert data["answer"] == ""
    assert data["debug"]["no_reply_reason"] == "working_hours_ai_disabled"
    assert data["debug"]["openai_used"] is False
    assert data["debug"]["openai_brain_used"] is False


def test_debug_chat_force_bypasses_working_hours(monkeypatch):
    monkeypatch.setattr(main, "is_bot_work_time", lambda: False)
    async def fake_handle_message(*, chat_id, phone, user_text):
        session = state.get_session(chat_id)
        session["step"] = "complaint"
        state.save_session(chat_id, session)
        return "Подскажите, пожалуйста, что Вас беспокоит? 🌿"
    monkeypatch.setattr(main, "handle_message", fake_handle_message)
    async def no_humanize(chat_id, user_text, base_answer, *, voice_ignored=False):
        return base_answer
    monkeypatch.setattr(main, "_maybe_humanize_answer", no_humanize)

    response = TestClient(main.app).post("/debug/chat", json={"chat_id": "wh_force", "text": "Здравствуйте", "force": True})
    data = response.json()

    assert data["answer"]
    assert data["debug"]["no_reply_reason"] == ""

    assert data["debug"]["working_hours_bypassed_by_force"] is True


def test_wazzup_working_hours_blocks_ai_humanize_and_crm(monkeypatch):
    monkeypatch.setattr(main, "is_bot_work_time", lambda: False)

    async def should_not_handle(*args, **kwargs):
        raise AssertionError("OpenAI Brain/dialog flow must not be called during working hours")

    async def should_not_humanize(*args, **kwargs):
        raise AssertionError("humanize must not be called during working hours")

    async def should_not_crm(*args, **kwargs):
        raise AssertionError("CRM must not be called during working hours")

    monkeypatch.setattr(main, "handle_message", should_not_handle)
    monkeypatch.setattr(main, "_maybe_humanize_answer", should_not_humanize)
    monkeypatch.setattr(crm, "check_slots", should_not_crm, raising=False)
    monkeypatch.setattr(crm, "book_appointment", should_not_crm, raising=False)

    answer = asyncio.run(main._build_answer_for_message({
        "chat_id": "wh_wazzup_day",
        "phone": "77010000000",
        "text": "Здравствуйте, болит спина",
        "kind": "text",
        "source": "wazzup",
    }))

    session = state.get_session("wh_wazzup_day")
    assert answer == ""
    assert session["no_reply_reason"] == "working_hours_ai_disabled"
    assert session["openai_used"] is False
    assert session["openai_brain_used"] is False


def test_wazzup_after_hours_can_reply_to_new_lead(monkeypatch):
    monkeypatch.setattr(main, "is_bot_work_time", lambda: True)

    async def fake_handle_message(*, chat_id, phone, user_text):
        session = state.get_session(chat_id)
        session["step"] = "complaint"
        session["ai_lead_started"] = True
        state.save_session(chat_id, session)
        return "Подскажите, пожалуйста, что Вас беспокоит? 🌿"

    async def no_humanize(chat_id, user_text, base_answer, *, voice_ignored=False):
        return base_answer

    monkeypatch.setattr(main, "handle_message", fake_handle_message)
    monkeypatch.setattr(main, "_maybe_humanize_answer", no_humanize)

    answer = asyncio.run(main._build_answer_for_message({
        "chat_id": "wh_wazzup_night",
        "phone": "77010000001",
        "text": "Здравствуйте, болит спина",
        "kind": "text",
        "source": "wazzup",
    }))

    assert answer
    assert state.get_session("wh_wazzup_night").get("no_reply_reason", "") == ""
