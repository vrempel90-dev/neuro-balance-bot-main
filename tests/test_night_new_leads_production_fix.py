from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import asyncio

import config
import crm
import dialog
import main
import state
state.init_db()
from schedule import is_bot_work_time


def setup_function():
    state.init_db()



def _enable_new_leads_only(monkeypatch):
    monkeypatch.setenv("NEW_LEADS_ONLY", "true")
    config.get_settings.cache_clear()



def test_night_window_almaty_boundaries(monkeypatch):
    monkeypatch.setenv("BOT_TIMEZONE", "Asia/Almaty")
    monkeypatch.setenv("BOT_WORK_START", "20:00")
    monkeypatch.setenv("BOT_WORK_END", "08:00")
    config.get_settings.cache_clear()
    cases = [
        ("2026-07-06T19:59:00+05:00", False),
        ("2026-07-06T20:00:00+05:00", True),
        ("2026-07-07T02:00:00+05:00", True),
        ("2026-07-07T07:59:00+05:00", True),
        ("2026-07-07T08:00:00+05:00", False),
        ("2026-07-07T10:00:00+05:00", False),
    ]
    for value, expected in cases:
        assert is_bot_work_time(datetime.fromisoformat(value)) is expected


def test_phone_normalization_and_lookup_variants():
    assert crm.normalize_phone("+7 700 898 45 05") == "77008984505"
    assert crm.normalize_phone("87008984505") == "77008984505"
    assert crm.normalize_phone("7008984505") == "77008984505"
    assert crm.normalize_phone("+77008984505") == "77008984505"
    assert crm.phone_lookup_variants("+7 700 898 45 05")[:4] == ["77008984505", "+77008984505", "87008984505", "7008984505"]


def test_returning_patient_is_silent(monkeypatch):
    async def scenario():
        _enable_new_leads_only(monkeypatch)
        async def fake_lookup(phone):
            return {"ok": True, "found": True, "isNew": False, "patient": {"name": "Алия"}, "lead": {"id": "l1"}, "lastAppointment": {"id": "a1", "status": "Завершён"}, "hasActiveAppointment": False, "appointment": None, "appointments": []}
        monkeypatch.setattr(crm, "lookup_active_appointments_by_phone", fake_lookup)
        chat_id = "prod_returning_silent"
        state.reset_session(chat_id)
        answer = await dialog.handle_message(chat_id, "77008984505", "Хочу записаться")
        session = state.get_session(chat_id)
        assert answer == ""
        assert session["crm_patient_state"] == "RETURNING_PATIENT_NO_ACTIVE_BOOKING"
        assert session["silent_old_lead"] is True
        assert session["no_reply_reason"] == "old_lead_from_crm"
        assert session["first_touch_allowed"] is False


    asyncio.run(scenario())

def test_active_booking_is_silent(monkeypatch):
    async def scenario():
        _enable_new_leads_only(monkeypatch)
        async def fake_lookup(phone):
            return {"ok": True, "found": True, "isNew": False, "patient": {"name": "Алия"}, "hasActiveAppointment": True, "appointment": {"id": "a2", "status": "booked"}, "appointments": []}
        monkeypatch.setattr(crm, "lookup_active_appointments_by_phone", fake_lookup)
        chat_id = "prod_active_booking_silent"
        state.reset_session(chat_id)
        answer = await dialog.handle_message(chat_id, "77008984505", "Я записан?")
        session = state.get_session(chat_id)
        assert answer == ""
        assert session["crm_patient_state"] == "ACTIVE_BOOKING"
        assert session["silent_old_lead"] is True
        assert session["no_reply_reason"] == "active_booking_old_lead"
        assert session["first_touch_allowed"] is False


    asyncio.run(scenario())

def test_crm_lookup_failed_closes_first_touch(monkeypatch):
    async def scenario():
        _enable_new_leads_only(monkeypatch)
        async def fake_lookup(phone):
            raise RuntimeError("crm down")
        monkeypatch.setattr(crm, "lookup_active_appointments_by_phone", fake_lookup)
        chat_id = "prod_crm_failed"
        state.reset_session(chat_id)
        answer = await dialog.handle_message(chat_id, "77008984505", "Здравствуйте")
        session = state.get_session(chat_id)
        assert answer == ""
        assert session["manual_takeover"] is True
        assert session["no_reply_reason"] == "crm_lookup_failed"
        assert session["first_touch_allowed"] is False
        assert session["first_touch_blocked_reason"] == "crm_lookup_failed"


    asyncio.run(scenario())

def test_debug_payload_keeps_message_id_for_no_reply(monkeypatch):
    monkeypatch.setenv("BOT_ACTIVATED_AT", "2026-07-07T00:00:00+05:00")
    config.get_settings.cache_clear()
    chat_id = "prod_debug_message_id_no_reply"
    state.reset_session(chat_id)

    message = {
        "chat_id": chat_id,
        "message_id": "msg-old-1",
        "timestamp": "2026-07-06T23:59:00+05:00",
        "direction": "incoming",
    }
    reason, is_duplicate, is_old = main._hard_inbound_block_reason(message, {})
    main._mark_no_reply(chat_id, reason, message, duplicate=is_duplicate, old=is_old)

    session = state.get_session(chat_id)
    debug = main._dialog_debug(session, "")
    assert debug["message_id"] == "msg-old-1"
    assert debug["message_timestamp"] == "2026-07-06T23:59:00+05:00"
    assert debug["no_reply_reason"] == "old_message_before_bot_activation"


def test_active_booking_by_requested_status_is_silent(monkeypatch):
    async def scenario():
        _enable_new_leads_only(monkeypatch)
        async def fake_lookup(phone):
            return {"ok": True, "found": True, "isNew": False, "patient": {"name": "Тест"}, "lead": None, "lastAppointment": {"status": "ПОДТВЕРДИЛ_ЗАРАНЕЕ"}, "hasActiveAppointment": True, "appointment": None, "appointments": []}
        monkeypatch.setattr(crm, "lookup_active_appointments_by_phone", fake_lookup)
        chat_id = "prod_active_requested_status"
        state.reset_session(chat_id)
        answer = await dialog.handle_message(chat_id, "77008984505", "Здравствуйте")
        session = state.get_session(chat_id)
        assert answer == ""
        assert session["crm_patient_state"] == "ACTIVE_BOOKING"
        assert session["no_reply_reason"] == "active_booking_old_lead"
    asyncio.run(scenario())


def test_daytime_test_window_almaty_boundaries(monkeypatch):
    monkeypatch.setenv("BOT_TIMEZONE", "Asia/Almaty")
    monkeypatch.setenv("BOT_WORK_START", "20:00")
    monkeypatch.setenv("BOT_WORK_END", "08:00")
    monkeypatch.setenv("BOT_TEST_WINDOW_ENABLED", "true")
    monkeypatch.setenv("BOT_TEST_WINDOW_START", "14:00")
    monkeypatch.setenv("BOT_TEST_WINDOW_END", "14:30")
    monkeypatch.setenv("BOT_TEST_WINDOW_DATE", "2026-07-08")
    config.get_settings.cache_clear()
    cases = [
        ("2026-07-08T13:59:00+05:00", False),
        ("2026-07-08T14:00:00+05:00", True),
        ("2026-07-08T14:15:00+05:00", True),
        ("2026-07-08T14:29:00+05:00", True),
        ("2026-07-08T14:30:00+05:00", False),
        ("2026-07-08T20:00:00+05:00", True),
    ]
    for value, expected in cases:
        assert is_bot_work_time(datetime.fromisoformat(value)) is expected


def test_daytime_test_window_ignored_when_disabled(monkeypatch):
    monkeypatch.setenv("BOT_TIMEZONE", "Asia/Almaty")
    monkeypatch.setenv("BOT_WORK_START", "20:00")
    monkeypatch.setenv("BOT_WORK_END", "08:00")
    monkeypatch.setenv("BOT_TEST_WINDOW_ENABLED", "false")
    monkeypatch.setenv("BOT_TEST_WINDOW_START", "14:00")
    monkeypatch.setenv("BOT_TEST_WINDOW_END", "14:30")
    monkeypatch.setenv("BOT_TEST_WINDOW_DATE", "2026-07-08")
    config.get_settings.cache_clear()
    assert is_bot_work_time(datetime.fromisoformat("2026-07-08T14:15:00+05:00")) is False



def test_new_lead_keeps_bounded_access_when_crm_marks_same_lead_in_progress(monkeypatch):
    async def scenario():
        async def fake_lookup(phone):
            return {
                "ok": True,
                "found": True,
                "isNew": False,
                "lead": {"id": "lead-1", "status": "В работе"},
                "patient": None,
                "appointments": [],
            }

        monkeypatch.setattr(crm, "lookup_active_appointments_by_phone", fake_lookup)
        chat_id = "active_new_lead_continuation"
        state.reset_session(chat_id)
        session = state.get_session(chat_id)
        dialog._activate_ai_admission_lease(session)
        state.save_session(chat_id, session)

        verdict = await dialog._classify_lead(chat_id, "77008984505", session)

        assert verdict.state == "NEW"
        assert verdict.reason == "active_ai_conversation"

    asyncio.run(scenario())


def test_admission_lease_never_turns_existing_patient_into_new_lead(monkeypatch):
    async def scenario():
        async def fake_lookup(phone):
            return {
                "ok": True,
                "found": True,
                "isNew": False,
                "patient": {"id": "patient-1", "name": "Алия"},
                "lead": {"id": "lead-1", "status": "В работе"},
                "appointments": [],
            }

        monkeypatch.setattr(crm, "lookup_active_appointments_by_phone", fake_lookup)
        chat_id = "existing_patient_not_overridden"
        state.reset_session(chat_id)
        session = state.get_session(chat_id)
        dialog._activate_ai_admission_lease(session)
        state.save_session(chat_id, session)

        verdict = await dialog._classify_lead(chat_id, "77008984505", session)

        assert verdict.state == "RETURNING"
        assert verdict.reason == "patient_exists"

    asyncio.run(scenario())


def test_expired_admission_lease_does_not_keep_lead_eligible(monkeypatch):
    async def scenario():
        async def fake_lookup(phone):
            return {
                "ok": True,
                "found": True,
                "isNew": False,
                "lead": {"id": "lead-1", "status": "В работе"},
                "appointments": [],
            }

        monkeypatch.setattr(crm, "lookup_active_appointments_by_phone", fake_lookup)
        chat_id = "expired_new_lead_lease"
        state.reset_session(chat_id)
        session = state.get_session(chat_id)
        session["ai_admission_started_at"] = (
            datetime.now(timezone.utc) - timedelta(hours=13)
        ).isoformat()
        state.save_session(chat_id, session)

        verdict = await dialog._classify_lead(chat_id, "77008984505", session)

        assert verdict.state == "RETURNING"
        assert verdict.reason == "lead_in_progress"

    asyncio.run(scenario())



def test_confirmed_booking_is_never_reopened_as_new_lead(monkeypatch):
    async def scenario():
        lookup_calls = 0

        async def fake_lookup(phone):
            nonlocal lookup_calls
            lookup_calls += 1
            return {"ok": True, "found": False, "isNew": True, "appointments": []}

        monkeypatch.setattr(crm, "lookup_active_appointments_by_phone", fake_lookup)
        chat_id = "confirmed_booking_local_terminal"
        state.reset_session(chat_id)
        session = state.get_session(chat_id)
        session["booking_confirmed"] = True
        session["booked"] = True
        session["appointment_id"] = "a-confirmed"
        state.save_session(chat_id, session)

        answer = await dialog.handle_message(chat_id, "77008984505", "У меня ещё вопрос")
        final = state.get_session(chat_id)

        assert answer == ""
        assert lookup_calls == 0
        assert final["ai_muted"] is True
        assert final["manual_takeover"] is True
        assert final["no_reply_reason"] == "booking_completed_ai_disabled"

    asyncio.run(scenario())
