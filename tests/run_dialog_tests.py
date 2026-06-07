from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path
from typing import Any

# Минимальные переменные, чтобы config.py не требовал Railway/.env.
tmp_db = tempfile.NamedTemporaryFile(delete=False, suffix=".sqlite3")
tmp_db.close()
os.environ.setdefault("SQLITE_PATH", tmp_db.name)
os.environ.setdefault("CRM_BOT_SECRET", "test")
os.environ.setdefault("WAZZUP_API_KEY", "test")
os.environ.setdefault("WAZZUP_CHANNEL_ID", "test")
os.environ.setdefault("OPENAI_API_KEY", "")

import sys
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import state
state.init_db()
import questionnaire
import original_engine
import response_guard
import doctor_router


LAST_CHECK_SLOTS_CALLS: list[dict[str, Any]] = []


async def fake_check_slots(date: str, doctor_login: str | None = None) -> dict[str, Any]:
    LAST_CHECK_SLOTS_CALLS.append({"date": date, "doctor_login": doctor_login})
    login = doctor_login or "doctor1"
    name = "Врач по позвоночнику" if login == "spine_doc" else "Тестовый врач"
    return {
        "availability": [
            {
                "doctorLogin": login,
                "doctorName": name,
                "availableSlots": ["09:20", "10:00", "12:40", "16:00", "16:40"],
            }
        ]
    }


async def fake_book_appointment(**kwargs: Any) -> dict[str, Any]:
    return {
        "appointmentId": 123,
        "status": "booked",
        "date": kwargs.get("date"),
        "timeStart": kwargs.get("time_start"),
        "doctorLogin": kwargs.get("doctor_login"),
    }


async def fake_escalate_to_operator(**kwargs: Any) -> dict[str, Any]:
    return {"ok": True, "escalated": True}


async def fake_log_outcome(**kwargs: Any) -> dict[str, Any]:
    return {"ok": True}


questionnaire.crm.check_slots = fake_check_slots
questionnaire.crm.book_appointment = fake_book_appointment
questionnaire.crm.escalate_to_operator = fake_escalate_to_operator
questionnaire.crm.log_outcome = fake_log_outcome
original_engine.crm.escalate_to_operator = fake_escalate_to_operator
original_engine.crm.log_outcome = fake_log_outcome

async def fake_patient_lookup(phone: str) -> dict[str, Any]:
    return {
        "hasActiveAppointment": True,
        "lastAppointment": {
            "appointmentId": 999,
            "date": "2099-01-01",
            "timeStart": "10:00",
            "doctorName": "Тестовый врач",
        },
        "patient": {"name": "Тест"},
    }

async def fake_cancel_appointment(**kwargs: Any) -> dict[str, Any]:
    return {"ok": True, "cancelled": True, "appointmentId": kwargs.get("appointment_id")}

original_engine.crm.patient_lookup = fake_patient_lookup
original_engine.crm.cancel_appointment = fake_cancel_appointment

async def fake_get_doctors() -> dict[str, Any]:
    return {
        "doctors": [
            {
                "doctorLogin": "spine_doc",
                "doctorName": "Врач по позвоночнику",
                "canTreat": ["спина", "поясница", "шея", "грыжа", "протрузия", "остеохондроз"],
                "preferredDiagnoses": ["протрузия", "грыжа"],
                "description": "Лечение позвоночника и боли в спине",
            },
            {
                "doctorLogin": "joint_doc",
                "doctorName": "Врач по суставам",
                "canTreat": ["колено", "плечо", "сустав", "артроз", "артрит"],
                "preferredDiagnoses": ["артроз"],
                "description": "Суставы",
            },
        ]
    }

doctor_router.crm.get_doctors = fake_get_doctors


def contains_all(text: str, needles: list[str]) -> bool:
    low = text.lower()
    return all(n.lower() in low for n in needles)


def contains_none(text: str, needles: list[str]) -> bool:
    low = text.lower()
    return all(n.lower() not in low for n in needles)


async def run_case(case: dict[str, Any]) -> tuple[bool, str]:
    chat_id = "test_" + case["name"]
    phone = "77011234567"
    state.reset_session(chat_id)
    LAST_CHECK_SLOTS_CALLS.clear()
    session = state.get_session(chat_id)
    if case.get("preset_session"):
        session.update(case["preset_session"])
        state.save_session(chat_id, session)

    last_answer = ""
    if case.get("guard"):
        session = state.get_session(chat_id)
        session.update(case.get("session") or {})
        state.save_session(chat_id, session)
        last_answer, _violations = response_guard.validate_answer(
            chat_id,
            case.get("user_text", ""),
            case.get("bad_answer", ""),
            session,
        )
    else:
        for msg in case["messages"]:
            session = state.get_session(chat_id)
            if case.get("engine"):
                answer = await original_engine.pre_handle_message(chat_id, phone, msg, session)
            else:
                answer = await questionnaire.handle_questionnaire(chat_id, phone, msg, session)
            last_answer = answer or ""

    must_contain = case.get("must_contain", [])
    must_not = case.get("must_not_contain", [])

    ok = contains_all(last_answer, must_contain) and contains_none(last_answer, must_not)

    expected_doctor = case.get("expected_doctor_login")
    if expected_doctor:
        used = any(call.get("doctor_login") == expected_doctor for call in LAST_CHECK_SLOTS_CALLS)
        ok = ok and used
        if not used:
            last_answer += f"\n[TEST DEBUG] expected doctor filter not used: {expected_doctor}; calls={LAST_CHECK_SLOTS_CALLS}"

    details = (
        f"{case['name']}: {'PASS' if ok else 'FAIL'}\n"
        f"answer: {last_answer!r}\n"
        f"must_contain: {must_contain}\n"
        f"must_not_contain: {must_not}"
    )
    return ok, details


async def main() -> int:
    cases_path = Path(__file__).resolve().parent / "dialog_cases.json"
    cases = json.loads(cases_path.read_text(encoding="utf-8"))

    passed = 0
    failed = 0
    for case in cases:
        ok, details = await run_case(case)
        print(details)
        print("-" * 80)
        if ok:
            passed += 1
        else:
            failed += 1

    print(f"RESULT: {passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
