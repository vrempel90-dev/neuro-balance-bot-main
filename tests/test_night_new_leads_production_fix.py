from datetime import datetime
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import asyncio

import config
import crm
import dialog
import state
state.init_db()
from schedule import is_bot_work_time


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


def test_new_patient_gets_locked_first_touch(monkeypatch):
    async def scenario():
        _enable_new_leads_only(monkeypatch)
        async def fake_lookup(phone):
            return {"ok": True, "found": False, "isNew": True, "patient": None, "lead": None, "lastAppointment": None, "hasActiveAppointment": False, "appointment": None, "appointments": []}
        monkeypatch.setattr(crm, "lookup_active_appointments_by_phone", fake_lookup)
        chat_id = "prod_new_patient_first_touch"
        state.reset_session(chat_id)
        answer = await dialog.handle_message(chat_id, "87008984505", "Здравствуйте")
        session = state.get_session(chat_id)
        assert answer == dialog.FIRST_TOUCH_CLINIC_INFO_RU
        assert session["crm_patient_state"] == "NEW_PATIENT"
        assert session["first_touch_allowed"] is True
        assert session["answer_source"] == "locked_template:first_touch"


    asyncio.run(scenario())

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

def test_first_touch_template_is_full_and_no_telegram_env_required(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    config.get_settings.cache_clear()
    settings = config.get_settings()
    assert not hasattr(settings, "telegram_bot_token")
    for fragment in ["Мы специализируемся на:", "плазмотерапию", "TikTok", "Подскажите, пожалуйста, что именно Вас беспокоит?"]:
        assert fragment in dialog.FIRST_TOUCH_CLINIC_INFO_RU
