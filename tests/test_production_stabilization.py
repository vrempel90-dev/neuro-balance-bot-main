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

