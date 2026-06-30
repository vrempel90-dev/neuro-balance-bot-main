from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

os.environ["SQLITE_PATH"] = tempfile.NamedTemporaryFile(delete=False, suffix=".sqlite3").name
os.environ.setdefault("CRM_BOT_SECRET", "test")
os.environ.setdefault("OPENAI_API_KEY", "")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import dialog
import state


state.init_db()


def run(coro):
    return asyncio.run(coro)


def test_relative_multi_message_semantics_price_medical_booking() -> None:
    history = [
        {"role": "user", "content": "У мужа протрузия"},
        {"role": "user", "content": "Как вылечить ее"},
        {"role": "user", "content": "К вам на прием"},
        {"role": "user", "content": "Хотим приехать"},
        {"role": "user", "content": "Сколько стоит"},
    ]
    combined = dialog.build_combined_recent_user_text(history)
    session: dict = {"step": "complaint", "language": "ru"}

    answer = dialog._combined_profile_booking_answer(session, combined)

    assert all(text in combined for text in ["У мужа протрузия", "Как вылечить ее", "К вам на прием", "Хотим приехать", "Сколько стоит"])
    assert "протрузия" in session["complaint"].lower()
    assert session["patient_relation"] == "муж"
    assert session["patient_gender"] == "male"
    assert session["booking_intent"] is True
    assert session["faq_price"] is True
    assert session["faq_medical"] is True
    assert "По лечению врач сможет сказать точнее после очного осмотра" in answer
    assert "Первичный приём стоит 5 000 тг" in answer
    assert "сколько лет мужу" in answer
    assert session["step"] == "age"
    assert session["manual_takeover"] is False
    assert session["escalated"] is False


def test_relative_age_moves_to_gendered_contraindications() -> None:
    chat_id = "relative_age_contra"
    state.save_session(chat_id, {
        "step": "age",
        "complaint": "У мужа протрузия",
        "patient_relation": "муж",
        "patient_gender": "male",
        "patient_subject": "relative",
        "first_touch_info_sent": True,
        "ai_lead_started": True,
        "conversation_turns_count": 1,
    })

    answer = run(dialog.handle_message(chat_id, "77011234567", "Мужу 40"))
    session = state.get_session(chat_id)

    assert session["age"] == 40
    assert session["patient_relation"] == "муж"
    assert session["step"] == "contraindications"
    assert "у него" in answer
    for term in ["кардиостимулятор", "беременность", "онкология", "металл в зоне лечения", "эпилепсия", "возраст до 16", "более 75", "коляски", "костыли"]:
        assert term in answer
    assert session["manual_takeover"] is False
    assert session["escalated"] is False


def test_first_touch_booking_exact_welcome_once() -> None:
    chat_id = "first_touch_booking_exact"
    answer = run(dialog.handle_message(chat_id, "77011234567", "хочу записаться"))
    session = state.get_session(chat_id)

    assert answer == dialog.FIRST_TOUCH_CLINIC_INFO_RU
    assert session["first_touch_info_sent"] is True
    assert session["step"] == "complaint"
    assert "чем можем помочь: хотите записаться" not in answer.lower()


def test_duplicate_outbound_guard_blocks_same_answer() -> None:
    chat_id = "duplicate_age_question"
    session = {
        "step": "age",
        "complaint": "спина болит",
        "last_assistant_answer": "Подскажите, пожалуйста, сколько Вам лет?",
        "last_bot_answer": "Подскажите, пожалуйста, сколько Вам лет?",
        "first_touch_info_sent": True,
        "ai_lead_started": True,
        "conversation_turns_count": 2,
        "last_user_text": "",
    }

    answer = dialog._finalize(chat_id, session, "Подскажите, пожалуйста, сколько Вам лет?")

    assert answer == ""


def test_price_faq_while_waiting_relative_age_resumes_age() -> None:
    chat_id = "price_faq_relative_age"
    state.save_session(chat_id, {
        "step": "age",
        "complaint": "У мужа протрузия",
        "patient_relation": "муж",
        "patient_gender": "male",
        "patient_subject": "relative",
        "first_touch_info_sent": True,
        "ai_lead_started": True,
        "conversation_turns_count": 2,
    })

    answer = run(dialog.handle_message(chat_id, "77011234567", "сколько стоит"))
    session = state.get_session(chat_id)

    assert "5 000 тг" in answer
    assert "сколько лет мужу" in answer
    assert session["manual_takeover"] is False
    assert not session.get("escalated")


def test_medical_faq_while_waiting_relative_age_resumes_age() -> None:
    chat_id = "medical_faq_relative_age"
    state.save_session(chat_id, {
        "step": "age",
        "complaint": "У мужа протрузия",
        "patient_relation": "муж",
        "patient_gender": "male",
        "patient_subject": "relative",
        "first_touch_info_sent": True,
        "ai_lead_started": True,
        "conversation_turns_count": 2,
    })

    answer = run(dialog.handle_message(chat_id, "77011234567", "как вылечить протрузию"))
    session = state.get_session(chat_id)

    assert "врач сможет сказать точнее после очного осмотра" in answer
    assert "сколько лет мужу" in answer
    assert session["manual_takeover"] is False
