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

