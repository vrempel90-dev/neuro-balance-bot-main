"""Minimize resolved medical histories; preserve unresolved work and ownership.

No media files are retained by the media processor. This policy concerns the
private SQLite history only. It deliberately does not erase unresolved evidence
or idempotency claims; an authorized operator must reconcile those first.
"""
from datetime import datetime, timedelta, timezone
import json
import os

import state


_SUPPRESSION_KEYS = {
    'manual_admin_intervention', 'manual_takeover', 'ai_muted', 'escalated',
    'operator_handoff_confirmed', 'booked', 'booking_confirmed',
    'ai_admission_closed_at', 'ai_admission_closed_reason', 'patient_context_version',
    'crm_patient_state', 'lead_admission_state', 'created_by_ai',
}


def purge_expired() -> dict[str, int]:
    days = max(7, int(os.getenv('MEDICAL_HISTORY_RETENTION_DAYS', '30')))
    threshold = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    result = {'sessions_minimized': 0, 'messages_deleted': 0, 'orphan_receipts_deleted': 0}
    with state._connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        rows = conn.execute(
            "SELECT s.chat_id,s.data_json FROM sessions s WHERE s.updated_at<? "
            "AND CASE WHEN json_valid(s.data_json) THEN (json_extract(s.data_json,'$.booking_confirmed')=1 "
            "OR json_extract(s.data_json,'$.operator_handoff_confirmed')=1) ELSE 0 END "
            "AND NOT EXISTS (SELECT 1 FROM webhook_inbox j WHERE j.chat_id=s.chat_id AND j.status<>'done') "
            "AND NOT EXISTS (SELECT 1 FROM booking_claims b WHERE b.chat_id=s.chat_id AND b.status IN ('pending','uncertain')) "
            "LIMIT 500", (threshold,),
        ).fetchall()
        for row in rows:
            try:
                session = json.loads(row['data_json'])
            except (ValueError, TypeError):
                continue  # Corruption requires review, not deletion.
            if not isinstance(session, dict) or not (session.get('booking_confirmed') or session.get('operator_handoff_confirmed')):
                continue
            minimized = {key: value for key, value in session.items() if key in _SUPPRESSION_KEYS}
            minimized['history_minimized_at'] = state.now_iso()
            conn.execute('UPDATE sessions SET data_json=?,updated_at=? WHERE chat_id=?',
                         (json.dumps(minimized), state.now_iso(), row['chat_id']))
            result['sessions_minimized'] += 1
            result['messages_deleted'] += conn.execute(
                'DELETE FROM messages WHERE chat_id=? AND created_at<?', (row['chat_id'], threshold)).rowcount
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if {'provider_receipts', 'provider_deliveries'} <= tables:
            result['orphan_receipts_deleted'] = conn.execute(
                'DELETE FROM provider_receipts WHERE created_at<? AND NOT EXISTS '
                '(SELECT 1 FROM provider_deliveries d WHERE d.provider_id=provider_receipts.provider_id)',
                (threshold,),
            ).rowcount
    return result
