from __future__ import annotations

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


def test_kazakh_complaint_answer_stays_kazakh_and_no_russian_checklist():
    answer = asyncio.run(handle_message("prod_kz", "77010000000", "У меня при беге колено стреляет"))

    assert "Түсіндім" in answer or "тізе" in answer
    assert "Жасыңыз" in answer or "Жасыныз" in answer
    assert "Перед записью" not in answer
    assert "кардиостимулятор" not in answer.lower()
