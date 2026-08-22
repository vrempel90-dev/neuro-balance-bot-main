from __future__ import annotations

import asyncio

import weekend_booking_policy as policy


def run(coro):
    return asyncio.run(coro)


def _slot(date_iso: str, time: str = "10:00") -> dict[str, str]:
    return {"doctorLogin": "doctor_1", "doctorName": "Врач Тест", "date": date_iso, "timeStart": time, "doctor_login": "doctor_1", "doctor_name": "Врач Тест", "time": time}


def test_weekend_helpers_detect_and_move_to_nearest_weekday() -> None:
    assert policy.is_procedure_weekend("2026-08-22") is True
    assert policy.is_procedure_weekend("2026-08-23") is True
    assert policy.is_procedure_weekend("2026-08-24") is False
    assert policy.next_weekday("2026-08-22") == "2026-08-24"
    assert policy.next_weekday("2026-08-23") == "2026-08-24"


def test_saturday_redirects_to_monday_and_shows_only_real_slots() -> None:
    calls = []; session = {"language": "ru", "step": "date"}
    async def original(chat_id, current, date_iso):
        calls.append(date_iso); current["last_slots"] = [_slot(date_iso, "11:30")]; current["step"] = "time"; return "Есть такие свободные окошки: 11:30"
    answer = run(policy._show_weekday_slots_for_weekend(original, "chat", session, "2026-08-22"))
    assert calls == ["2026-08-24"] and "24 августа" in answer and "11:30" in answer


def test_if_monday_has_no_slots_next_real_weekday_is_checked() -> None:
    calls = []; session = {"language": "ru", "step": "date"}
    async def original(chat_id, current, date_iso):
        calls.append(date_iso)
        if date_iso == "2026-08-25": current["last_slots"] = [_slot(date_iso, "15:00")]; current["step"] = "time"; return "Есть такие свободные окошки: 15:00"
        current["last_slots"] = []; current["step"] = "date"; return "Нет свободных окошек"
    answer = run(policy._show_weekday_slots_for_weekend(original, "chat", session, "2026-08-22"))
    assert calls == ["2026-08-24", "2026-08-25"] and "25 августа" in answer and "15:00" in answer


def test_weekday_request_is_unchanged() -> None:
    calls = []; session = {"language": "ru", "step": "date"}
    async def original(chat_id, current, date_iso): calls.append(date_iso); return "weekday-original"
    assert run(policy._show_weekday_slots_for_weekend(original, "chat", session, "2026-08-24")) == "weekday-original"
    assert calls == ["2026-08-24"]


def test_crm_or_handoff_failure_does_not_get_hidden() -> None:
    session = {"language": "ru", "step": "date"}
    async def original(chat_id, current, date_iso):
        current["manual_takeover"] = True; current["escalated"] = True; current["last_slots"] = []
        return "Сейчас не вижу свободные окошки по системе."
    answer = run(policy._show_weekday_slots_for_weekend(original, "chat", session, "2026-08-22"))
    assert "процедурные дни" in answer and "не вижу свободные окошки" in answer
    assert "можем записать" not in answer.lower()
    assert "жаза аламыз" not in answer.lower()


def test_kazakh_weekend_message_keeps_real_slot() -> None:
    session = {"language": "kk", "step": "date"}
    async def original(chat_id, current, date_iso): current["last_slots"] = [_slot(date_iso, "09:30")]; current["step"] = "time"; return "09:30 бос уақыт бар"
    answer = run(policy._show_weekday_slots_for_weekend(original, "chat", session, "2026-08-23"))
    assert "Сенбі және жексенбі — процедуралық күндер" in answer and "24 тамыз" in answer and "09:30" in answer
