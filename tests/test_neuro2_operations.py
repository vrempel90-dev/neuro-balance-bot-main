import asyncio
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

import pytest

import ai_budget
from config import get_settings
import production_monitor
import state


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv('SQLITE_PATH', str(tmp_path / 'ops.sqlite3'))
    get_settings.cache_clear()
    state.init_db()
    yield
    get_settings.cache_clear()


def test_whisper_duration_is_charged_without_token_usage():
    cost = ai_budget.record_usage(SimpleNamespace(duration=120.0), model='whisper-1', purpose='voice_transcription')
    assert cost['cost_usd'] == pytest.approx(0.012)
    assert state.ai_usage_month_cost_usd(ai_budget.current_month()) == pytest.approx(0.012)


def test_late_provider_failure_remains_monitor_attention():
    state.claim_outgoing_message('synthetic-outbound', 'synthetic-chat')
    state.mark_outgoing_message('synthetic-outbound', 'confirmed')
    state.bind_provider_message('synthetic-outbound', provider_id='synthetic-provider', channel_id='synthetic-channel', provider_chat_id='synthetic-chat')
    state.record_delivery_receipt({'messageId': 'synthetic-provider', 'status': 'error'})
    snapshot = production_monitor.status_snapshot()
    assert snapshot['ok'] is False
    assert snapshot['provider_delivery']['FAILED_NEEDS_ATTENTION'] == 1


def test_current_transcribe_duration_usage_is_charged():
    result = ai_budget.record_usage(SimpleNamespace(usage={"type":"duration", "seconds":120.0}),
        model="gpt-transcribe", purpose="voice_transcription")
    assert result["cost_usd"] == pytest.approx(0.009)
