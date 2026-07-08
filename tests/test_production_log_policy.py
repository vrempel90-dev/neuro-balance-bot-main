from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

os.environ["SQLITE_PATH"] = tempfile.NamedTemporaryFile(delete=False, suffix=".sqlite3").name
os.environ["BOT_TIMEZONE"] = "Asia/Almaty"
os.environ["BOT_WORK_START"] = "20:00"
os.environ["BOT_WORK_END"] = "08:00"
os.environ["PRODUCTION_LOG_ACTIVE_WINDOW_ONLY"] = "true"
os.environ["BOT_TEST_WINDOW_ENABLED"] = "false"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import state
from config import get_settings


def setup_function():
    get_settings.cache_clear()
    state.init_db()


def _almaty(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 7, 8, hour, minute, tzinfo=ZoneInfo("Asia/Almaty"))


def test_log_window_boundaries():
    assert state.log_window_status(_almaty(7, 59))["log_window_allowed"] is True
    assert state.log_window_status(_almaty(8, 0))["log_window_allowed"] is False
    assert state.log_window_status(_almaty(14, 0))["log_window_allowed"] is False
    assert state.log_window_status(_almaty(20, 0))["log_window_allowed"] is True


def test_error_event_at_1400_is_logged(monkeypatch):
    monkeypatch.setattr(state, "astana_now", lambda: _almaty(14, 0))

    assert state.should_emit_production_event("crm_error", {"source": "wazzup"}) is True


def test_normal_wazzup_received_at_1400_is_not_logged(monkeypatch):
    monkeypatch.setattr(state, "astana_now", lambda: _almaty(14, 0))

    assert state.should_emit_production_event("wazzup_received", {"source": "wazzup"}) is False


def test_normal_wazzup_received_at_2100_is_logged(monkeypatch):
    monkeypatch.setattr(state, "astana_now", lambda: _almaty(21, 0))

    assert state.should_emit_production_event("wazzup_received", {"source": "wazzup"}) is True
