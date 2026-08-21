from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

os.environ.setdefault("SQLITE_PATH", tempfile.NamedTemporaryFile(delete=False, suffix=".sqlite3").name)
os.environ.setdefault("CRM_BOT_SECRET", "test")
os.environ.setdefault("OPENAI_API_KEY", "")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import bot_tools
import crm
import dialog


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def slot(*, time: str = "09:20", date: str = "2099-01-02", login: str = "doctor1", name: str = "Тестовый врач") -> dict[str, Any]:
    return {
        "doctorLogin": login,
        "doctorName": name,
        "date": date,
        "timeStart": time,
        "doctor_login": login,
        "doctor_name": name,
        "time": time,
    }


def ready_session(selected: dict[str, Any], *, last_slots: list[dict[str, Any]] | None) -> dict[str, Any]:
    return {
        "chat_id": "slot-contract",
        "phone": "77011234567",
        "language": "ru",
        "step": "name",
        "complaint": "болит спина",
        "complaint_gate": bot_tools.COMPLAINT_OK,
        "age": 32,
        "contraindications_ok": True,
        "contraindications_verdict": bot_tools.CONTRA_PROCEED,
        "patient_name": "Дана",
        "preferred_date": selected["date"],
        "selected_slot": dict(selected),
        "selected_date": selected["date"],
        "selected_time": selected["timeStart"],
        "selected_doctor_login": selected["doctorLogin"],
        "selected_doctor_name": selected["doctorName"],
        "last_slots": list(last_slots or []),
    }


def patch_successful_booking(monkeypatch: Any, calls: dict[str, list[Any]]) -> None:
    async def fake_book(**kwargs: Any) -> dict[str, Any]:
        calls.setdefault("book", []).append(kwargs)
        return {"id": 77, "date": kwargs["date"], "timeStart": kwargs["time_start"], "doctorLogin": kwargs["doctor_login"], "doctorName": kwargs.get("doctor_name")}

    async def fake_lookup(phone: str) -> dict[str, Any]:
        calls.setdefault("lookup", []).append(phone)
        booked = calls["book"][-1]
        appt = {
            "id": 77,
            "date": booked["date"],
            "timeStart": booked["time_start"],
            "doctorLogin": booked["doctor_login"],
            "doctorName": booked.get("doctor_name"),
            "status": "booked",
        }
        return {"ok": True, "appointments": [appt], "appointment": appt, "active_count": 1}

    monkeypatch.setattr(crm, "book_appointment", fake_book)
    monkeypatch.setattr(crm, "lookup_active_appointments_by_phone", fake_lookup)


def test_slot_identity_accepts_equivalent_crm_key_shapes() -> None:
    left = {"doctorLogin": "Doctor1", "date": "2099-01-02", "timeStart": "09:20"}
    right = {"doctor_login": "doctor1", "date": "2099-01-02", "time": "9:20"}
    assert dialog._same_slot_identity(left, right) is True


def test_normal_last_slots_path_does_not_recheck_availability(monkeypatch: Any) -> None:
    selected = slot()
    session = ready_session(selected, last_slots=[selected])
    calls: dict[str, list[Any]] = {"check": [], "book": []}

    async def should_not_check(*args: Any, **kwargs: Any) -> dict[str, Any]:
        calls["check"].append((args, kwargs))
        raise AssertionError("normal booking path must not re-fetch availability")

    monkeypatch.setattr(crm, "check_slots", should_not_check)
    patch_successful_booking(monkeypatch, calls)

    answer = run(dialog._book("slot-normal", session, session["phone"]))

    assert calls["check"] == []
    assert len(calls["book"]) == 1
    assert session["booking_confirmed"] is True
    assert "запись подтверждена" in answer.lower()


def test_booking_blocks_slot_not_in_last_offered_set(monkeypatch: Any) -> None:
    offered = slot(time="09:20")
    selected = slot(time="14:00")
    session = ready_session(selected, last_slots=[offered])
    calls: dict[str, list[Any]] = {"book": []}
    patch_successful_booking(monkeypatch, calls)

    answer = run(dialog._book("slot-stale-offer", session, session["phone"]))

    assert calls["book"] == []
    assert session.get("selected_slot") is None
    assert session["step"] == "time"
    assert "09:20" in answer or "время" in answer.lower() or "окош" in answer.lower()


def test_legacy_missing_last_slots_revalidates_then_books(monkeypatch: Any) -> None:
    selected = slot(time="09:20")
    session = ready_session(selected, last_slots=[])
    calls: dict[str, list[Any]] = {"check": [], "book": []}

    async def fake_check(date: str, doctor_login: str | None = None) -> dict[str, Any]:
        calls["check"].append({"date": date, "doctor_login": doctor_login})
        return {"availability": [{"doctorLogin": "doctor1", "doctorName": "Тестовый врач", "date": date, "availableSlots": ["09:20", "14:00"]}]}

    monkeypatch.setattr(crm, "check_slots", fake_check)
    patch_successful_booking(monkeypatch, calls)

    answer = run(dialog._book("slot-legacy-ok", session, session["phone"]))

    assert calls["check"] == [{"date": "2099-01-02", "doctor_login": "doctor1"}]
    assert len(calls["book"]) == 1
    assert session["booking_confirmed"] is True
    assert session.get("legacy_slot_revalidated") is True
    assert "запись подтверждена" in answer.lower()


def test_legacy_missing_last_slots_blocks_slot_that_disappeared(monkeypatch: Any) -> None:
    selected = slot(time="09:20")
    session = ready_session(selected, last_slots=[])
    calls: dict[str, list[Any]] = {"book": []}

    async def fake_check(date: str, doctor_login: str | None = None) -> dict[str, Any]:
        return {"availability": [{"doctorLogin": "doctor1", "doctorName": "Тестовый врач", "date": date, "availableSlots": ["14:00"]}]}

    monkeypatch.setattr(crm, "check_slots", fake_check)
    patch_successful_booking(monkeypatch, calls)

    answer = run(dialog._book("slot-legacy-stale", session, session["phone"]))

    assert calls["book"] == []
    assert session.get("selected_slot") is None
    assert session.get("selected_time") in ("", None)
    assert session["step"] == "time"
    assert any(dialog._slot_time(item) == "14:00" for item in session.get("last_slots") or [])
    assert "14:00" in answer or "время" in answer.lower() or "окош" in answer.lower()


def test_legacy_missing_last_slots_fails_closed_when_crm_unavailable(monkeypatch: Any) -> None:
    selected = slot(time="09:20")
    session = ready_session(selected, last_slots=[])
    calls: dict[str, list[Any]] = {"book": []}

    async def broken_check(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise crm.CRMError("availability unavailable")

    monkeypatch.setattr(crm, "check_slots", broken_check)
    patch_successful_booking(monkeypatch, calls)

    answer = run(dialog._book("slot-legacy-crm-down", session, session["phone"]))

    assert calls["book"] == []
    assert session["manual_takeover"] is True
    assert session["escalated"] is True
    assert session["booking_confirmed"] is False
    assert "администратор" in answer.lower() or "уточн" in answer.lower()
