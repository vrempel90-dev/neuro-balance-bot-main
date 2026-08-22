import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import crm
import dialog
import main
import state


def run(coro):
    return asyncio.run(coro)


def reset(chat_id, data=None):
    state.init_db()
    state.reset_session(chat_id)
    if data:
        s = state.get_session(chat_id)
        s.update(data)
        state.save_session(chat_id, s)


def test_old_message_before_bot_activation_no_reply(monkeypatch):
    monkeypatch.setattr(main, "is_bot_work_time", lambda: True)
    chat_id = "hard_old_msg"
    reset(chat_id)
    ans = run(main._build_answer_for_message({"chat_id": chat_id, "phone": "7701", "text": "Здравствуйте", "message_key": "old-1", "timestamp": "2026-07-01T23:59:00+05:00", "direction": "incoming"}))
    s = state.get_session(chat_id)
    assert ans == ""
    assert s["no_reply_reason"] == "old_message_before_bot_activation"


def test_duplicate_message_id_no_reply(monkeypatch):
    monkeypatch.setattr(main, "is_bot_work_time", lambda: True)
    chat_id = "hard_dup_msg"
    reset(chat_id)
    state.mark_processed_message("dup-1", chat_id)
    ans = run(main._build_answer_for_message({"chat_id": chat_id, "phone": "7701", "text": "Здравствуйте", "message_key": "dup-1", "timestamp": "2026-07-03T01:00:00+05:00", "direction": "incoming"}))
    assert ans == ""
    assert state.get_session(chat_id)["no_reply_reason"] == "duplicate_message_already_processed"


def test_outgoing_operator_no_reply(monkeypatch):
    monkeypatch.setattr(main, "is_bot_work_time", lambda: True)
    chat_id = "hard_outgoing_msg"
    reset(chat_id)
    ans = run(main._build_answer_for_message({"chat_id": chat_id, "phone": "7701", "text": "ответ", "message_key": "out-1", "timestamp": "2026-07-03T01:00:00+05:00", "direction": "outgoing", "is_from_me": "true"}))
    assert ans == ""
    assert state.get_session(chat_id)["no_reply_reason"] == "not_client_incoming_message"


def test_new_chat_greeting_does_not_send_clinic_presentation(monkeypatch):
    monkeypatch.setattr(main, "is_bot_work_time", lambda: True)
    async def fake_lookup(phone):
        return {"found": False, "isNew": True, "hasActiveAppointment": False}
    monkeypatch.setattr(crm, "patient_lookup", fake_lookup)
    chat_id = "hard_new_first_touch"
    reset(chat_id)
    ans = run(dialog.handle_message(chat_id, "77011234567", "Здравствуйте"))
    assert "Подскажите, пожалуйста, что Вас беспокоит" in ans
    assert "Клиника Neuro Balance помогает" not in ans
    assert "плазмотерап" not in ans and "TikTok" not in ans


def test_new_chat_pain_starts_clarifying_dialog(monkeypatch):
    monkeypatch.setattr(main, "is_bot_work_time", lambda: True)
    async def fake_lookup(phone):
        return {"found": False, "isNew": True, "hasActiveAppointment": False}
    monkeypatch.setattr(crm, "patient_lookup", fake_lookup)
    chat_id = "hard_new_pain"
    reset(chat_id)
    ans = run(dialog.handle_message(chat_id, "77011234567", "Болит спина"))
    assert "спина" in ans.lower()
    assert "сколько Вам лет" in ans
    assert "Клиника Neuro Balance помогает" not in ans


def test_new_chat_methods_question_stays_concise_and_continues(monkeypatch):
    monkeypatch.setattr(main, "is_bot_work_time", lambda: True)
    async def fake_lookup(phone):
        return {"found": False, "isNew": True, "hasActiveAppointment": False}
    monkeypatch.setattr(crm, "patient_lookup", fake_lookup)
    chat_id = "hard_new_methods"
    reset(chat_id)
    ans = run(dialog.handle_message(chat_id, "77011234567", "Как лечите?"))
    assert ans
    assert "что" in ans.lower() or "подскажите" in ans.lower()
    assert ans != dialog.FIRST_TOUCH_CLINIC_INFO_RU


def test_new_chat_booking_starts_booking_scenario(monkeypatch):
    monkeypatch.setattr(main, "is_bot_work_time", lambda: True)
    async def fake_lookup(phone):
        return {"found": False, "isNew": True, "hasActiveAppointment": False}
    monkeypatch.setattr(crm, "patient_lookup", fake_lookup)
    chat_id = "hard_new_booking_intent"
    reset(chat_id)
    ans = run(dialog.handle_message(chat_id, "77011234567", "Хочу записаться"))
    s = state.get_session(chat_id)
    assert "можно записаться" in ans.lower()
    assert "что Вас беспокоит" in ans
    assert "Клиника Neuro Balance помогает" not in ans
    assert s["ai_lead_started"] is True
    assert s["step"] == "complaint"


def test_new_chat_detail_request_is_gpt_ready_not_locked_presentation(monkeypatch):
    monkeypatch.setattr(main, "is_bot_work_time", lambda: True)
    async def fake_lookup(phone):
        return {"found": False, "isNew": True, "hasActiveAppointment": False}
    monkeypatch.setattr(crm, "patient_lookup", fake_lookup)
    for text in ["Расскажите подробнее", "Как лечите?"]:
        chat_id = "hard_new_detail_" + str(abs(hash(text)))
        reset(chat_id)
        ans = run(dialog.handle_message(chat_id, "77011234567", text))
        assert ans
        assert ans != dialog.FIRST_TOUCH_CLINIC_INFO_RU
        assert state.get_session(chat_id)["ai_lead_started"] is True


def test_new_chat_price_answers_only_price(monkeypatch):
    monkeypatch.setattr(main, "is_bot_work_time", lambda: True)
    async def fake_lookup(phone):
        return {"found": False, "isNew": True, "hasActiveAppointment": False}
    monkeypatch.setattr(crm, "patient_lookup", fake_lookup)
    chat_id = "hard_new_price_only"
    reset(chat_id)
    ans = run(dialog.handle_message(chat_id, "77011234567", "Сколько стоит консультация?"))
    assert "5 000" in ans
    assert "Клиника Neuro Balance помогает" not in ans
    assert "TikTok" not in ans


def test_existing_old_chat_active_appointment_post_booking(monkeypatch):
    monkeypatch.setattr(main, "is_bot_work_time", lambda: True)
    async def fake_lookup(phone):
        return {"hasActiveAppointment": True, "appointment": {"id": 7, "date": "2026-07-10", "timeStart": "12:00", "doctorName": "Тестовый врач"}}
    monkeypatch.setattr(crm, "patient_lookup", fake_lookup)
    chat_id = "hard_old_active_appt"
    reset(chat_id, {"old_chat": True})
    ans = run(main._build_answer_for_message({"chat_id": chat_id, "phone": "7701", "text": "Здравствуйте ок", "message_key": "old-new-1", "timestamp": "2026-07-03T01:00:00+05:00", "direction": "incoming"}))
    s = state.get_session(chat_id)
    assert ans != dialog.FIRST_TOUCH_CLINIC_INFO_RU
    assert "Хорошо" in ans
    assert s["first_touch_blocked_reason"] == "active_appointment"


def test_booked_address_and_cancel(monkeypatch):
    chat_id = "hard_booked_cancel"
    reset(chat_id, {"booking_confirmed": True, "step": "booked", "appointment_id": 1, "appointment_date": "2026-07-10", "appointment_time": "12:00"})
    assert "Кабанбай батыра 28" in run(dialog.handle_message(chat_id, "7701", "адрес"))
    async def fake_cancel(**kwargs):
        return {"success": True, "cancelled": True}
    monkeypatch.setattr(crm, "cancel_appointment", fake_cancel)
    ans = run(dialog.handle_message(chat_id, "7701", "отмените запись"))
    assert "Запись отменена" in ans


def test_banned_phrase_guard_and_relative_combined():
    assert dialog._has_banned_final_phrase("Не беспокоит?") is True
    session = {}
    ans = dialog._combined_profile_booking_answer(session, "У мужа протрузия\nКак вылечить\nСколько стоит\nХотим приехать")
    assert "у мужа" in ans.lower()
    assert "5 000 тг" in ans
    assert "сколько лет мужу" in ans.lower()
    assert session["step"] == "age"
