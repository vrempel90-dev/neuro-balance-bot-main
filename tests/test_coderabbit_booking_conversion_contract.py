from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

os.environ["SQLITE_PATH"] = tempfile.NamedTemporaryFile(delete=False, suffix=".sqlite3").name
os.environ.setdefault("CRM_BOT_SECRET", "test")
os.environ.setdefault("OPENAI_API_KEY", "")
os.environ.setdefault("CRM_BASE_URL", "http://127.0.0.1:9")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import crm
import dialog
import state

state.init_db()

PHONE = "77011234567"
DOCTOR_LOGIN = "zhuma_md"
DOCTOR_NAME = "Жумабек Мади Мухтарович"


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def answer(chat_id: str, text: str) -> str:
    return run(dialog.handle_message(chat_id, PHONE, text))


def reset(chat_id: str, preset: dict[str, Any] | None = None) -> None:
    state.reset_session(chat_id)
    session = state.get_session(chat_id)
    session.update({"ai_lead_started": True, "phone": PHONE})
    if preset:
        session.update(preset)
    state.save_session(chat_id, session)


def slot(date_iso: str, time_start: str = "14:00") -> dict[str, str]:
    return {
        "doctorLogin": DOCTOR_LOGIN,
        "doctorName": DOCTOR_NAME,
        "date": date_iso,
        "timeStart": time_start,
        "doctor_login": DOCTOR_LOGIN,
        "doctor_name": DOCTOR_NAME,
        "time": time_start,
    }


def install_crm(monkeypatch: pytest.MonkeyPatch, times: list[str]) -> dict[str, list[Any]]:
    calls: dict[str, list[Any]] = {"slots": [], "book": [], "lookup": []}

    async def fake_slots(date: str, doctor_login: str | None = None) -> dict[str, Any]:
        calls["slots"].append({"date": date, "doctor_login": doctor_login})
        return {
            "availability": [{
                "doctorLogin": doctor_login or DOCTOR_LOGIN,
                "doctorName": DOCTOR_NAME,
                "date": date,
                "availableSlots": list(times),
            }]
        }

    async def fake_book(**kwargs: Any) -> dict[str, Any]:
        calls["book"].append(kwargs)
        return {
            "ok": True,
            "id": "booking-1",
            "date": kwargs["date"],
            "timeStart": kwargs["time_start"],
            "doctorLogin": kwargs["doctor_login"],
            "doctorName": kwargs.get("doctor_name") or DOCTOR_NAME,
            "status": "booked",
        }

    async def fake_lookup(phone: str) -> dict[str, Any]:
        calls["lookup"].append(phone)
        if not calls["book"]:
            return {"appointments": []}
        booked = calls["book"][-1]
        return {
            "appointments": [{
                "id": "booking-1",
                "date": booked["date"],
                "timeStart": booked["time_start"],
                "doctorLogin": booked["doctor_login"],
                "doctorName": booked.get("doctor_name") or DOCTOR_NAME,
                "status": "booked",
            }]
        }

    monkeypatch.setattr(crm, "check_slots", fake_slots)
    monkeypatch.setattr(crm, "book_appointment", fake_book)
    monkeypatch.setattr(crm, "lookup_active_appointments_by_phone", fake_lookup)
    return calls


def expected_named_date(day: int, month: int) -> str:
    today = (datetime.now(timezone.utc) + timedelta(hours=5)).date()
    for offset in range(0, 5):
        try:
            candidate = datetime(today.year + offset, month, day).date()
        except ValueError:
            continue
        if candidate >= today:
            return candidate.isoformat()
    raise AssertionError("could not resolve expected date")


@pytest.mark.parametrize("text", ["22 августа", "22 август", "22 тамыз", "22 тамызда"])
def test_named_month_dates_are_parsed_ru_and_kk(text: str) -> None:
    assert dialog._parse_date(text) == expected_named_date(22, 8)


def test_named_month_invalid_date_is_rejected() -> None:
    assert dialog._parse_date("31 февраля") is None


def test_existing_relative_numeric_and_weekday_formats_still_work() -> None:
    today = (datetime.now(timezone.utc) + timedelta(hours=5)).date()
    assert dialog._parse_date("завтра") == (today + timedelta(days=1)).isoformat()

    numeric = dialog._parse_date("22.08")
    assert numeric is not None
    assert numeric[5:] == "08-22"

    parsed_saturday = dialog._parse_date("в субботу")
    assert parsed_saturday is not None
    assert datetime.fromisoformat(parsed_saturday).date().weekday() == 5


@pytest.mark.parametrize("confirmation", ["да", "подходит", "давайте", "запишите", "хорошо", "на это время"])
def test_natural_confirmation_selects_the_only_crm_slot(confirmation: str) -> None:
    only = slot("2026-09-01")
    assert dialog._select_slot(confirmation, [only]) == only


@pytest.mark.parametrize("confirmation", ["да", "подходит", "давайте", "запишите", "хорошо", "на это время"])
def test_natural_confirmation_never_guesses_between_multiple_slots(confirmation: str) -> None:
    slots = [slot("2026-09-01", "14:00"), slot("2026-09-01", "15:00")]
    assert dialog._select_slot(confirmation, slots) is None


@pytest.mark.parametrize("negative", ["не подходит", "не надо", "другое время", "не это время", "басқа уақыт", "жоқ"])
def test_negative_reply_never_selects_single_slot(negative: str) -> None:
    assert dialog._select_slot(negative, [slot("2026-09-01")]) is None


def next_named_weekday() -> tuple[str, str]:
    months = {
        1: "января", 2: "февраля", 3: "марта", 4: "апреля", 5: "мая", 6: "июня",
        7: "июля", 8: "августа", 9: "сентября", 10: "октября", 11: "ноября", 12: "декабря",
    }
    today = (datetime.now(timezone.utc) + timedelta(hours=5)).date()
    for offset in range(2, 40):
        candidate = today + timedelta(days=offset)
        if candidate.weekday() < 5:
            return f"{candidate.day} {months[candidate.month]}", candidate.isoformat()
    raise AssertionError("weekday not found")


def test_named_date_step_calls_real_crm_slot_source(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = install_crm(monkeypatch, ["14:00"])
    date_text, expected_iso = next_named_weekday()
    reset("named-date-crm", {
        "step": "date",
        "questionnaire_step": "date",
        "complaint": "поясница болит",
        "complaint_gate": "COMPLAINT_OK",
        "age": 54,
        "contraindications_ok": True,
        "contraindications_verdict": "proceed",
    })

    response = answer("named-date-crm", date_text)
    session = state.get_session("named-date-crm")

    assert calls["slots"] == [{"date": expected_iso, "doctor_login": None}]
    assert session["step"] == "time"
    assert session["last_slots"]
    assert session["last_slots"][0]["doctorLogin"] == DOCTOR_LOGIN
    assert "14:00" in response


def test_real_transcript_reaches_exactly_one_crm_booking(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = install_crm(monkeypatch, ["14:00"])
    chat_id = "real-transcript-booking"
    reset(chat_id)

    response = answer(chat_id, "Поясница болит")
    session = state.get_session(chat_id)
    assert session["step"] == "age"
    assert session.get("complaint")
    assert "лет" in response.lower()

    response = answer(chat_id, "54")
    session = state.get_session(chat_id)
    assert session["age"] == 54
    assert session["step"] == "contraindications"
    assert "противопоказ" in response.lower()

    answer(chat_id, "Нету никаких противопоказаний")
    session = state.get_session(chat_id)
    assert session["contraindications_ok"] is True
    assert session["contraindications_verdict"] == "proceed"
    assert session["step"] == "date"

    date_text, expected_iso = next_named_weekday()
    response = answer(chat_id, date_text)
    session = state.get_session(chat_id)
    assert calls["slots"][-1] == {"date": expected_iso, "doctor_login": None}
    assert session["step"] == "time"
    assert len(session["last_slots"]) == 1
    assert session["last_slots"][0]["timeStart"] == "14:00"
    assert "14:00" in response

    response = answer(chat_id, "давайте")
    session = state.get_session(chat_id)
    assert session["step"] == "name"
    assert session["selected_slot"]["doctorLogin"] == DOCTOR_LOGIN
    assert session["selected_date"] == expected_iso
    assert session["selected_time"] == "14:00"
    assert "имя" in response.lower()

    response = answer(chat_id, "Алия")
    session = state.get_session(chat_id)
    assert len(calls["book"]) == 1
    booking = calls["book"][0]
    assert booking["doctor_login"] == DOCTOR_LOGIN
    assert booking["date"] == expected_iso
    assert booking["time_start"] == "14:00"
    assert session["step"] == "booked"
    assert session["booking_confirmed"] is True
    assert session["booking_visible_in_crm"] is True
    assert "подтверждена" in response.lower()

    # A later patient message must never create a duplicate appointment.
    answer(chat_id, "спасибо")
    assert len(calls["book"]) == 1


def test_multi_slot_davaite_stays_on_time_step(monkeypatch: pytest.MonkeyPatch) -> None:
    install_crm(monkeypatch, ["14:00", "15:00"])
    date_iso = "2026-09-01"
    reset("multi-slot-confirm", {
        "step": "time",
        "questionnaire_step": "time",
        "complaint": "поясница болит",
        "complaint_gate": "COMPLAINT_OK",
        "age": 54,
        "contraindications_ok": True,
        "contraindications_verdict": "proceed",
        "preferred_date": date_iso,
        "last_slots": [slot(date_iso, "14:00"), slot(date_iso, "15:00")],
    })

    response = answer("multi-slot-confirm", "давайте")
    session = state.get_session("multi-slot-confirm")
    assert session["step"] == "time"
    assert not session.get("selected_slot")
    assert "14:00" in response or "15:00" in response or "время" in response.lower()
