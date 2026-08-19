from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest

os.environ.setdefault("SQLITE_PATH", tempfile.NamedTemporaryFile(delete=False, suffix=".sqlite3").name)
os.environ.setdefault("CRM_BOT_SECRET", "test")
os.environ.setdefault("OPENAI_API_KEY", "")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import crm
import dialog
import state

state.init_db()
PHONE = "77011234567"


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def slot(time: str, date: str = "2099-01-02") -> dict[str, Any]:
    return {
        "doctorLogin": "doctor1",
        "doctorName": "Тестовый врач",
        "date": date,
        "timeStart": time,
        "doctor_login": "doctor1",
        "doctor_name": "Тестовый врач",
        "time": time,
    }


def seed(chat_id: str, *, step: str, language: str = "ru") -> dict[str, Any]:
    state.init_db()
    state.reset_session(chat_id)
    session = state.get_session(chat_id)
    offered = [slot("14:00"), slot("15:20")]
    session.update({
        "chat_id": chat_id,
        "phone": PHONE,
        "language": language,
        "step": step,
        "ai_lead_started": True,
        "gate_reason": "active_ai_lead",
        "first_touch_info_sent": True,
        "complaint": "болит спина",
        "complaint_gate": "proceed",
        "age": 35,
        "contraindications_ok": True,
        "contraindications_verdict": "proceed",
        "preferred_date": "2099-01-02",
        "preferred_date_text": "старый день",
        "last_slots": offered,
        "selected_slot": dict(offered[0]) if step == "name" else None,
        "selected_date": "2099-01-02" if step == "name" else "",
        "selected_time": "14:00" if step == "name" else "",
        "selected_doctor_login": "doctor1" if step == "name" else "",
        "selected_doctor_name": "Тестовый врач" if step == "name" else "",
        "patient_name": "",
    })
    if session["selected_slot"] is None:
        session.pop("selected_slot", None)
    state.save_session(chat_id, session)
    return session


@pytest.fixture(autouse=True)
def offline_dependencies(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_lookup(phone: str) -> dict[str, Any]:
        return {
            "ok": True,
            "appointments": [],
            "appointment": None,
            "active_count": 0,
            "raw": {"found": False},
        }

    async def dead_brain(**kwargs: Any):
        return {
            "action": "fallback_rule_based",
            "reply": "",
            "extracted": {},
            "needs_python_tool": "none",
            "safety": {},
            "safety_flags": {},
        }, {
            "openai_brain_used": False,
            "openai_brain_fallback_used": True,
            "openai_brain_skip_reason": "test_fallback",
        }

    monkeypatch.setattr(crm, "lookup_active_appointments_by_phone", fake_lookup)
    monkeypatch.setattr(dialog, "run_openai_dialog_brain", dead_brain)


def install_show_slots(monkeypatch: pytest.MonkeyPatch, calls: list[str]) -> None:
    async def fake_show_slots(chat_id: str, session: dict[str, Any], date_iso: str) -> str:
        calls.append(date_iso)
        session["preferred_date"] = date_iso
        session["preferred_date_text"] = date_iso
        session["last_slots"] = [slot("10:00", date_iso), slot("11:20", date_iso)]
        session["step"] = "time"
        return f"Новые окошки: {date_iso} 10:00, 11:20"

    monkeypatch.setattr(dialog, "_show_slots", fake_show_slots)


def test_time_step_can_switch_to_another_explicit_date(monkeypatch: pytest.MonkeyPatch) -> None:
    chat_id = "change-time-date"
    seed(chat_id, step="time")
    calls: list[str] = []
    install_show_slots(monkeypatch, calls)
    monkeypatch.setattr(dialog, "_parse_date", lambda text: "2099-01-05")

    answer = run(dialog.handle_message(chat_id, PHONE, "давайте в другой день, 5 января"))
    saved = state.get_session(chat_id)

    assert calls == ["2099-01-05"]
    assert saved["step"] == "time"
    assert saved["preferred_date"] == "2099-01-05"
    assert saved.get("selected_slot") is None
    assert saved.get("selected_time") in ("", None)
    assert "10:00" in answer and "11:20" in answer


def test_time_step_generic_other_day_returns_to_date_and_clears_old_slots() -> None:
    chat_id = "change-time-other-day"
    seed(chat_id, step="time")

    answer = run(dialog.handle_message(chat_id, PHONE, "нет, давайте другой день"))
    saved = state.get_session(chat_id)

    assert saved["step"] == "date"
    assert saved.get("last_slots") in ([], None)
    assert saved.get("selected_slot") is None
    assert saved.get("selected_time") in ("", None)
    assert "день" in answer.lower() or "дат" in answer.lower()


def test_name_step_can_change_to_another_offered_time_without_booking(monkeypatch: pytest.MonkeyPatch) -> None:
    chat_id = "change-name-time"
    seed(chat_id, step="name")
    book_calls: list[Any] = []

    async def forbidden_book(**kwargs: Any):
        book_calls.append(kwargs)
        raise AssertionError("changing time must not book before name is provided")

    monkeypatch.setattr(crm, "book_appointment", forbidden_book)

    answer = run(dialog.handle_message(chat_id, PHONE, "нет, давайте 15:20"))
    saved = state.get_session(chat_id)

    assert book_calls == []
    assert saved["step"] == "name"
    assert saved["selected_time"] == "15:20"
    assert dialog._slot_time(saved["selected_slot"]) == "15:20"
    assert saved.get("patient_name") in ("", None)
    assert "имя" in answer.lower() or "аты" in answer.lower()


def test_name_step_generic_other_time_returns_to_time_without_old_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    chat_id = "change-name-other-time"
    seed(chat_id, step="name")
    book_calls: list[Any] = []

    async def forbidden_book(**kwargs: Any):
        book_calls.append(kwargs)
        raise AssertionError("changing time must not create booking")

    monkeypatch.setattr(crm, "book_appointment", forbidden_book)

    answer = run(dialog.handle_message(chat_id, PHONE, "нет, хочу другое время"))
    saved = state.get_session(chat_id)

    assert book_calls == []
    assert saved["step"] == "time"
    assert saved.get("selected_slot") is None
    assert saved.get("selected_time") in ("", None)
    assert "14:00" in answer and "15:20" in answer


def test_name_step_can_switch_to_new_date(monkeypatch: pytest.MonkeyPatch) -> None:
    chat_id = "change-name-date"
    seed(chat_id, step="name")
    calls: list[str] = []
    install_show_slots(monkeypatch, calls)
    monkeypatch.setattr(dialog, "_parse_date", lambda text: "2099-01-06")

    answer = run(dialog.handle_message(chat_id, PHONE, "лучше 6 января"))
    saved = state.get_session(chat_id)

    assert calls == ["2099-01-06"]
    assert saved["preferred_date"] == "2099-01-06"
    assert saved["step"] == "time"
    assert saved.get("selected_slot") is None
    assert saved.get("patient_name") in ("", None)
    assert "10:00" in answer


@pytest.mark.parametrize("phrase", ["басқа күн", "баска кун"])
def test_kazakh_other_day_is_understood(phrase: str) -> None:
    chat_id = "change-kk-day-" + phrase.replace(" ", "-")
    seed(chat_id, step="time", language="kk")

    answer = run(dialog.handle_message(chat_id, PHONE, phrase))
    saved = state.get_session(chat_id)

    assert saved["step"] == "date"
    assert saved.get("last_slots") in ([], None)
    assert saved.get("selected_slot") is None
    assert answer


def test_state_reset_does_not_erase_clinical_gates() -> None:
    session = seed("change-preserve-gates", step="name")
    dialog._reset_slot_choice_for_change(session, clear_offered=True)

    assert session["complaint"] == "болит спина"
    assert session["age"] == 35
    assert session["contraindications_ok"] is True
    assert session["contraindications_verdict"] == "proceed"
    assert session.get("selected_slot") is None
    assert session.get("last_slots") == []
