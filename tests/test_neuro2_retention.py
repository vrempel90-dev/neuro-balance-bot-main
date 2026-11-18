from datetime import datetime, timedelta, timezone
import json

import pytest

from config import get_settings
import production_inbox as inbox
import state


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv('SQLITE_PATH', str(tmp_path / 'retention.sqlite3'))
    get_settings.cache_clear()
    state.init_db()
    inbox.init_db()
    yield
    get_settings.cache_clear()


def stale(chat, **flags):
    state.save_session(chat, {'complaint': 'Synthetic private condition', 'patient_name': 'Synthetic', **flags})
    state.add_message(chat, 'user', 'Synthetic private report')
    stamp = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
    with state._connect() as conn:
        conn.execute('UPDATE sessions SET updated_at=? WHERE chat_id=?', (stamp, chat))
        conn.execute('UPDATE messages SET created_at=? WHERE chat_id=?', (stamp, chat))


def test_terminal_history_is_minimized_but_staff_suppression_remains():
    import data_retention
    stale('completed', manual_admin_intervention=True, manual_takeover=True, operator_handoff_confirmed=True)
    result = data_retention.purge_expired()
    assert result['sessions_minimized'] == 1
    assert result['messages_deleted'] == 1
    saved = state.get_session('completed')
    assert not saved['complaint'] and not saved['patient_name']
    assert saved['manual_admin_intervention'] and saved['manual_takeover']


def test_unresolved_or_active_history_never_silently_disappears():
    import data_retention
    stale('active')
    stale('unresolved', booking_confirmed=True)
    inbox.enqueue(message_key='unresolved-request', chat_id='unresolved', payload={}, raw_msg={'text': 'pending'}, parse_meta={})
    assert data_retention.purge_expired()['sessions_minimized'] == 0
    assert state.get_session('active')['complaint']
    assert state.get_session('unresolved')['complaint']


def test_retention_does_not_remove_uncertain_booking_evidence():
    import data_retention
    stale('uncertain', operator_handoff_confirmed=True)
    with state._connect() as conn:
        conn.execute("INSERT INTO booking_claims VALUES (?,?,?,?)", ('opaque-claim', 'uncertain', 'uncertain', state.now_iso()))
    assert data_retention.purge_expired()['sessions_minimized'] == 0


def test_abandoned_sessions_cannot_starve_completed_retention():
    import data_retention
    stamp = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
    with state._connect() as conn:
        conn.executemany('INSERT INTO sessions VALUES (?,?,?)',
                         [(f'active-{n}', '{}', stamp) for n in range(501)])
    stale('completed-after-active', booking_confirmed=True)
    assert data_retention.purge_expired()['sessions_minimized'] == 1
