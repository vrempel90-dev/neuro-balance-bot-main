from __future__ import annotations

import pytest

import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

os.environ["SQLITE_PATH"] = tempfile.NamedTemporaryFile(delete=False, suffix=".sqlite3").name
os.environ.setdefault("CRM_BOT_SECRET", "test")
os.environ.setdefault("OPENAI_API_KEY", "")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import asyncio

import state
from dialog import handle_message
from guards import should_auto_reply


def setup_function():
    state.init_db()


def _astana(hour: int) -> datetime:
    return datetime(2026, 6, 29, hour, 0, tzinfo=ZoneInfo("Asia/Almaty"))


def test_unified_guard_wazzup_0900_blocks_all_side_effects():
    decision = should_auto_reply("Здравствуйте", {}, "wazzup", force=False, now=_astana(9))

    assert decision.allowed is False
    assert decision.no_reply_reason == "working_hours_ai_disabled"
    assert decision.should_call_openai is False
    assert decision.should_call_crm is False
    assert decision.should_send_wazzup is False


def test_unified_guard_debug_force_0900_allows_testing():
    decision = should_auto_reply("Здравствуйте", {}, "debug", force=True, now=_astana(9))

    assert decision.allowed is True
    assert decision.no_reply_reason == ""
    assert decision.should_call_openai is True


def test_unified_guard_wazzup_2100_allows_night_reply():
    decision = should_auto_reply("Здравствуйте", {}, "wazzup", force=False, now=_astana(21))

    assert decision.allowed is True
    assert decision.should_send_wazzup is True


def test_irritated_user_gets_one_apology_then_manual_takeover_silence():
    chat_id = "prod_irritated_once"
    session = state.get_session(chat_id)
    session.update({"step": "age", "ai_lead_started": True})
    state.save_session(chat_id, session)

    first = asyncio.run(handle_message(chat_id, "77010000000", "Я уже от вас ничего не хочу"))
    after_first = state.get_session(chat_id)
    second = asyncio.run(handle_message(chat_id, "77010000000", "Еще раз пишете"))
    after_second = state.get_session(chat_id)

    assert "больше не буду беспокоить" in first
    assert after_first["ai_muted"] is True
    assert after_first["manual_takeover"] is True
    assert after_first["escalated"] is True
    assert second == ""
    assert after_second["no_reply_reason"] == "manual_takeover"


def test_contra_clear_phrase_advances_without_reasking_contraindications():
    chat_id = "prod_contra_clear"
    session = state.get_session(chat_id)
    session.update({
        "step": "contraindications",
        "last_required_step": "contraindications",
        "complaint": "поясница",
        "age": 35,
        "ai_lead_started": True,
    })
    state.save_session(chat_id, session)

    answer = asyncio.run(handle_message(chat_id, "77010000000", "То что перечислено, этого нет"))
    saved = state.get_session(chat_id)

    assert saved["contraindications_ok"] is True
    assert saved["contraindications_raw"] == "То что перечислено, этого нет"
    assert "противопоказ" not in answer.lower()


def test_instagram_detail_request_does_not_start_booking_questionnaire():
    answer = asyncio.run(handle_message(
        "prod_ig_detail",
        "77010000000",
        "Привет! Можно узнать об этом подробнее? https://instagram.com/neurobalance/post/1",
    ))

    assert "что именно заинтересовало" in answer
    assert "сколько Вам лет" not in answer
    assert "противопоказ" not in answer.lower()


@pytest.mark.xfail(reason="legacy expectation", strict=False)
def test_kazakh_complaint_answer_stays_kazakh_and_no_russian_checklist():
    answer = asyncio.run(handle_message("prod_kz", "77010000000", "У меня при беге колено стреляет"))

    assert "Түсіндім" in answer or "тізе" in answer
    assert "Жасыңыз" in answer or "Жасыныз" in answer
    assert "Перед записью" not in answer
    assert "кардиостимулятор" not in answer.lower()


def test_booking_age_asks_short_contraindications_question_without_checklist():
    chat_id = "prod_short_contra_after_age"

    first = asyncio.run(handle_message(chat_id, "77010000000", "Здравствуйте, хочу записаться. Спина болит"))
    answer = asyncio.run(handle_message(chat_id, "77010000000", "35"))

    assert "сколько Вам лет" in first
    assert answer == "Перед записью уточню для безопасности 🌿 Есть ли у Вас какие-нибудь противопоказания?"
    forbidden = [
        "кардиостим",
        "беремен",
        "онколог",
        "металл",
        "анамнез",
        "у нашего метода есть противопоказания",
    ]
    assert all(fragment not in answer.lower() for fragment in forbidden)


def test_contraindications_list_allowed_only_on_explicit_question():
    chat_id = "prod_explicit_contra_list"
    session = state.get_session(chat_id)
    session.update({
        "step": "contraindications",
        "last_required_step": "contraindications",
        "complaint": "спина болит",
        "age": 35,
        "ai_lead_started": True,
        "language": "ru",
        "language_locked": True,
    })
    state.save_session(chat_id, session)

    answer = asyncio.run(handle_message(chat_id, "77010000000", "Какие противопоказания?"))

    assert "Основные противопоказания" in answer or answer == "Лучше уточню это через администратора, чтобы не ошибиться 🌿"


def test_no_on_contraindications_advances_to_date_exact_answer():
    chat_id = "prod_no_contra_to_date"
    session = state.get_session(chat_id)
    session.update({
        "step": "contraindications",
        "last_required_step": "contraindications",
        "complaint": "спина болит",
        "age": 35,
        "ai_lead_started": True,
    })
    state.save_session(chat_id, session)

    answer = asyncio.run(handle_message(chat_id, "77010000000", "Нет"))
    saved = state.get_session(chat_id)

    assert saved["contraindications_ok"] is True
    assert saved["step"] == "date"
    assert answer == "Отлично 🌿 На какой день Вам удобно прийти?"
    assert "противопоказ" not in answer.lower()
