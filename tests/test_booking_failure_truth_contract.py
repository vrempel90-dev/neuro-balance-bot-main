from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

import httpx
import pytest

os.environ["SQLITE_PATH"] = tempfile.NamedTemporaryFile(delete=False, suffix=".sqlite3").name
os.environ.setdefault("CRM_BOT_SECRET", "test")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import crm
import dialog
import state

state.init_db()


def run(coro):
    return asyncio.run(coro)


def _base_session() -> dict[str, Any]:
    slot = {
        "doctorLogin": "zhuma_md",
        "doctorName": "Жумабек Мади Мухтарович",
        "date": "2026-08-21",
        "timeStart": "10:00",
        "doctor_login": "zhuma_md",
        "doctor_name": "Жумабек Мади Мухтарович",
        "time": "10:00",
    }
    return {
        "step": "name",
        "language": "ru",
        "complaint": "болит спина",
        "age": 35,
        "contraindications_ok": True,
        "contraindications_verdict": "proceed",
        "patient_name": "Дана",
        "phone": "77001234567",
        "selected_date": "2026-08-21",
        "selected_time": "10:00",
        "selected_doctor_login": "zhuma_md",
        "selected_doctor_name": "Жумабек Мади Мухтарович",
        "selected_slot": slot,
        "last_slots": [slot],
        "complaint_gate": {"ok": True},
    }


def _crm_error(status: int, message: str) -> crm.CRMResponseError:
    response = httpx.Response(
        status,
        text=message,
        request=httpx.Request("POST", "https://crm.test/api/bot/book"),
    )
    return crm.CRMResponseError("book", response, {"message": message})


def test_generic_crm_failure_never_claims_booking_confirmed(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fail_book(**kwargs):
        raise _crm_error(500, "boom")

    monkeypatch.setattr(crm, "book_appointment", fail_book)
    session = _base_session()
    state.save_session("truth-500", session)

    answer = run(dialog._book("truth-500", session, "77001234567"))

    assert "запись подтверждена" not in answer.lower()
    assert "не подтвержден" in answer.lower() or "не удалось" in answer.lower() or "не получилось" in answer.lower()
    assert session.get("booking_confirmed") is False
    assert session.get("manual_takeover") is True
    assert session.get("escalated") is True


def test_slot_conflict_refreshes_options_instead_of_false_confirmation(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"book": 0, "slots": 0}

    async def conflict_book(**kwargs):
        calls["book"] += 1
        raise _crm_error(409, "slot already occupied")

    async def fresh_slots(date: str, doctor_login: str | None = None):
        calls["slots"] += 1
        return {
            "availability": [{
                "doctorLogin": "zhuma_md",
                "doctorName": "Жумабек Мади Мухтарович",
                "date": date,
                "availableSlots": ["11:00", "12:00"],
            }]
        }

    monkeypatch.setattr(crm, "book_appointment", conflict_book)
    monkeypatch.setattr(crm, "check_slots", fresh_slots)
    monkeypatch.setattr(crm, "clear_slots_cache", lambda date=None: None)

    session = _base_session()
    state.save_session("truth-409", session)
    answer = run(dialog._book("truth-409", session, "77001234567"))

    assert calls == {"book": 1, "slots": 1}
    assert "запись подтверждена" not in answer.lower()
    assert session.get("booking_confirmed") is False
    assert session.get("step") == "time"
    assert session.get("selected_time") == ""
    assert session.get("selected_slot") in (None, {})
    times = [str(x.get("timeStart") or x.get("time") or "") for x in session.get("last_slots") or []]
    assert times == ["11:00", "12:00"]
