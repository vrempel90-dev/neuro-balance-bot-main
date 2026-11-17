"""Synthetic SQLite contracts for staff-owned and attention-required inbox work."""
from __future__ import annotations
import asyncio
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
import pytest
import production_inbox as inbox
import production_monitor as monitor
import state
from config import get_settings

@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv('SQLITE_PATH', str(tmp_path / 'attention.sqlite3'))
    monkeypatch.setenv('OPENAI_DEBUG_ADMIN_TOKEN', 'synthetic-operator-token')
    get_settings.cache_clear()
    state.init_db()
    inbox.init_db()
    yield
    get_settings.cache_clear()

def add(key='synthetic-message'):
    return inbox.enqueue(message_key=key, chat_id='synthetic-private-chat',
        payload={}, raw_msg={'text': 'Synthetic private report'}, parse_meta={'ok': True})

def row(job_id):
    with state._connect() as conn:
        return dict(conn.execute('SELECT * FROM webhook_inbox WHERE job_id=?', (job_id,)).fetchone())

def process(result):
    item = add()
    async def processor(job):
        return result
    asyncio.run(inbox.InboxWorker(processor)._process(inbox.claim_next()))
    return item

@pytest.mark.parametrize('status', ['handoff_pending', 'handoff_confirmed'])
def test_explicit_staff_outcome_retains_body_across_reinitialization(status):
    item = process({'ok': True, 'inbox_status': status})
    inbox.init_db()
    saved = row(item['job_id'])
    assert saved['status'] == status
    assert json.loads(saved['payload_json'])['raw_msg']['text'] == 'Synthetic private report'
    assert inbox.claim_next() is None
    assert add()['duplicate'] is True

@pytest.mark.parametrize('reason', ['outside_work_time', 'working_hours_ai_disabled', 'operator_handoff_failed', 'media_processing_failed'])
def test_unhandled_request_is_retained_even_after_fallback_answer_sent(reason):
    item = process({'ok': True, 'should_send_wazzup': True, 'no_reply_reason': reason})
    assert row(item['job_id'])['status'] == 'handoff_pending'
    assert row(item['job_id'])['payload_json'] != '{}'

def test_unknown_explicit_status_is_quarantined_not_erased():
    item = process({'ok': True, 'inbox_status': 'delivery_maybe'})
    assert row(item['job_id'])['status'] == 'uncertain'
    assert row(item['job_id'])['payload_json'] != '{}'

def test_attention_counts_are_private_and_do_not_claim_delivery():
    item = process({'ok': True, 'inbox_status': 'handoff_pending'})
    summary = inbox.status_summary()
    assert summary['counts']['handoff_pending'] == 1
    assert summary['lifecycle_counts']['HUMAN_HANDOFF_PENDING'] == 1
    assert summary['attention_required_count'] == 1
    assert 'DELIVERED' not in summary['lifecycle_counts']
    assert 'Synthetic private report' not in json.dumps(summary)
    assert 'synthetic-private-chat' not in json.dumps(summary)

def test_monitor_surfaces_unconfirmed_handoff(monkeypatch):
    process({'ok': True, 'inbox_status': 'handoff_pending'})
    monkeypatch.setattr(monitor, 'get_settings', lambda: SimpleNamespace(durable_webhook_enabled=False))
    result = monitor.status_snapshot()
    assert result['ok'] is False
    assert result['attention_required'] == 'inbox_handoff_pending'
    assert 'Synthetic private report' not in json.dumps(result)

@pytest.mark.parametrize('token', ['', 'wrong-token'])
def test_operator_retrieval_and_resolution_require_configured_admin_authorization(token):
    item = process({'ok': True, 'inbox_status': 'handoff_pending'})
    with pytest.raises(PermissionError):
        inbox.get_for_operator(item['job_id'], operator_token=token)
    with pytest.raises(PermissionError):
        inbox.resolve_attention(item['job_id'], resolution='done', operator_token=token)
    assert row(item['job_id'])['status'] == 'handoff_pending'

def test_authorized_operator_reads_by_opaque_id_and_resolves_without_replay():
    item = process({'ok': True, 'inbox_status': 'handoff_confirmed'})
    record = inbox.get_for_operator(item['job_id'], operator_token='synthetic-operator-token')
    assert record['raw_msg']['text'] == 'Synthetic private report'
    assert inbox.resolve_attention(item['job_id'], resolution='done', operator_token='synthetic-operator-token') is True
    assert row(item['job_id'])['payload_json'] == '{}'
    assert inbox.claim_next() is None
    assert inbox.resolve_attention(item['job_id'], resolution='done', operator_token='synthetic-operator-token') is False

def test_retention_never_deletes_unresolved_staff_work():
    item = process({'ok': True, 'inbox_status': 'handoff_pending'})
    with state._connect() as conn:
        conn.execute('UPDATE webhook_inbox SET updated_at=?', ((datetime.now(timezone.utc)-timedelta(days=60)).isoformat(),))
    assert inbox.purge_completed(retention_days=7) == 0
    assert row(item['job_id'])['payload_json'] != '{}'
