"""Offline security/contract tests for the production monitoring surface.

These tests sign local tokens and fake HTTP transports. They do not claim
successful live CRM, Wazzup, GitHub OIDC or OpenAI integration.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

import production_monitor as monitor
import state


@pytest.fixture(autouse=True)
def isolated_monitor_database(tmp_path, monkeypatch):
    from config import get_settings
    monkeypatch.setenv('SQLITE_PATH', str(tmp_path / 'monitor.sqlite3'))
    get_settings.cache_clear()
    state.init_db()
    yield
    get_settings.cache_clear()


@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv('OPENAI_DEBUG_ADMIN_TOKEN', raising=False)
    app = FastAPI()
    app.include_router(monitor.router)
    return TestClient(app)


@pytest.fixture
def signing_key(monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    async def fake_key(kid):
        if kid != 'unit-test-key':
            raise ValueError('unrecognized kid')
        return key.public_key()
    monkeypatch.setattr(monitor, '_signing_key', fake_key)
    return key


def _token(key, **changes):
    now = datetime.now(timezone.utc)
    claims = {
        'iss': monitor._ISSUER, 'aud': monitor._AUDIENCE,
        'sub': f'repo:{monitor._REPOSITORY}:ref:refs/heads/main',
        'iat': now, 'nbf': now - timedelta(seconds=1),
        'exp': now + timedelta(minutes=5), 'repository': monitor._REPOSITORY,
        'ref': 'refs/heads/main', 'workflow_ref': monitor._WORKFLOW,
        'event_name': 'schedule',
    }
    claims.update(changes)
    return jwt.encode(claims, key, algorithm='RS256', headers={'kid': 'unit-test-key'})


@pytest.mark.parametrize('path,method', [('/internal/monitor/status', 'get'), ('/internal/monitor/probe', 'post')])
def test_monitor_rejects_missing_authorization(client, path, method):
    response = getattr(client, method)(path)
    assert response.status_code == 401
    assert response.headers['www-authenticate'] == 'Bearer'


@pytest.mark.parametrize('changes', [
    {'aud': 'another-audience'},
    {'iss': 'https://attacker.invalid'},
    {'repository': 'another/repository'},
    {'ref': 'refs/heads/feature'},
    {'workflow_ref': monitor._WORKFLOW.replace('production-monitor.yml', 'other.yml')},
    {'sub': f'repo:{monitor._REPOSITORY}:pull_request'},
    {'event_name': 'pull_request'},
    {'exp': datetime.now(timezone.utc) - timedelta(minutes=1)},
])
def test_monitor_rejects_wrong_workload_identity(client, signing_key, changes):
    response = client.get('/internal/monitor/status', headers={'Authorization': 'Bearer ' + _token(signing_key, **changes)})
    assert response.status_code == 401


def test_monitor_accepts_only_exact_signed_workload(client, signing_key, monkeypatch):
    monkeypatch.setattr(monitor, 'status_snapshot', lambda **kwargs: {'ok': True, 'safe': True})
    response = client.get('/internal/monitor/status', headers={'Authorization': 'Bearer ' + _token(signing_key)})
    assert response.status_code == 200
    assert response.json() == {'ok': True, 'safe': True}


def test_monitor_rejects_token_signed_by_different_key(client, signing_key):
    wrong_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    response = client.get('/internal/monitor/status', headers={'Authorization': 'Bearer ' + _token(wrong_key)})
    assert response.status_code == 401


def test_monitor_accepts_existing_admin_token_without_disclosure(client, monkeypatch):
    monkeypatch.setenv('OPENAI_DEBUG_ADMIN_TOKEN', 'monitor-secret-value')
    monkeypatch.setattr(monitor, 'status_snapshot', lambda **kwargs: {'ok': True})
    response = client.get('/internal/monitor/status', headers={'Authorization': 'bearer monitor-secret-value'})
    assert response.status_code == 200
    assert 'monitor-secret-value' not in response.text


def test_event_summary_excludes_patient_fields(monkeypatch):
    state.init_db()
    with state._connect() as conn:
        conn.execute('DELETE FROM events')
        for name, payload in [
            ('bot_processing_start', {'phone': '77011234567', 'text': 'Synthetic private complaint'}),
            ('wazzup_webhook_received', {'is_incoming': True, 'message_id': 'sample-message-id'}),
            ('crm_lookup_result', {'crm_lookup_ok': True, 'crm_patient_state': 'ACTIVE_BOOKING'}),
            ('agent_booking_crm_success', {'patient_name': 'Private Test Person'}),
            ('dialog_result', {'detected_language': 'kk', 'answer_preview': 'private answer'}),
        ]:
            conn.execute('INSERT INTO events(chat_id,event_type,payload_json,created_at) VALUES (?,?,?,?)', ('77011234567', name, json.dumps(payload), state.now_iso()))
    result = monitor._event_summary((datetime.now(timezone.utc) - timedelta(hours=24)).isoformat())
    assert result['observed_contact_count'] == 1
    assert result['excluded_existing_contact_count'] == 1
    assert result['confirmed_booking_events'] == 1
    encoded = json.dumps(result)
    for private in ['77011234567', 'Private Test Person', 'Synthetic private complaint', 'private answer']:
        assert private not in encoded


def test_live_probe_contract_never_claims_unexecuted_success(client, monkeypatch):
    monkeypatch.setenv('OPENAI_DEBUG_ADMIN_TOKEN', 'monitor-secret-value')
    async def crm_probe():
        return {'ok': True, 'read_only': True}
    async def wazzup_probe():
        return {'ok': False, 'reason': 'not_configured'}
    async def openai_probe():
        return {'ok': False, 'reason': 'not_configured'}
    monkeypatch.setattr(monitor, '_crm_probe', crm_probe)
    monkeypatch.setattr(monitor, '_wazzup_probe', wazzup_probe)
    monkeypatch.setattr(monitor, '_openai_probe', openai_probe)
    response = client.post('/internal/monitor/probe', headers={'Authorization': 'Bearer monitor-secret-value'})
    result = response.json()
    assert response.status_code == 200
    assert result['ok'] is False
    assert result['crm_writes'] == result['patient_messages_sent'] == 0
    assert result['probes']['wazzup']['reason'] == 'not_configured'


def test_crm_probe_uses_read_only_slot_lookup_without_fake_patient(monkeypatch):
    import crm
    observed = []
    async def slots(date):
        observed.append(date)
        return {'ok': True, 'slots': [{'time': '10:00'}], 'availability': [{'doctorName': 'Private staff name'}]}
    monkeypatch.setattr(crm, 'check_slots', slots)
    state.init_db()
    with state._connect() as conn:
        conn.execute('DELETE FROM sessions')
    result = asyncio.run(monitor._crm_probe())
    assert result['ok'] is True
    assert len(observed) == 1
    assert result['available_slots'] == 1
    assert result['existing_patient_check']['executed'] is False
    assert 'Private staff name' not in json.dumps(result)


def test_wazzup_probe_uses_get_and_hides_channel_metadata(monkeypatch):
    import httpx
    requests = []
    class FakeClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return None
        async def get(self, url, headers):
            requests.append((url, headers))
            return httpx.Response(200, json=[{'channelId': 'configured-channel', 'name': 'Private Person'}], request=httpx.Request('GET', url))
    monkeypatch.setattr(monitor.httpx, 'AsyncClient', lambda **kwargs: FakeClient())
    monkeypatch.setattr(monitor, 'get_settings', lambda: SimpleNamespace(wazzup_api_key='private-api-key', wazzup_channel_id='configured-channel', wazzup_api_url='https://api.wazzup24.com/v3'))
    result = asyncio.run(monitor._wazzup_probe())
    assert result['ok'] is True
    assert requests[0][0].endswith('/channels')
    assert result['messages_sent'] == 0
    assert 'private-api-key' not in json.dumps(result)
    assert 'Private Person' not in json.dumps(result)


def test_incoming_counts_exclude_echo_and_deduplicate_provider_id(monkeypatch):
    with state._connect() as conn:
        for incoming in [True, True, False]:
            conn.execute('INSERT INTO events(chat_id,event_type,payload_json,created_at) VALUES (?,?,?,?)', ('private-chat', 'wazzup_webhook_received', json.dumps({'is_incoming': incoming, 'message_id': 'same-incoming-id'}), state.now_iso()))
    result = monitor._event_summary((datetime.now(timezone.utc) - timedelta(hours=24)).isoformat())
    assert result['incoming_webhook_events'] == 2
    assert result['unique_incoming_messages_with_id'] == 1
    assert result['observed_contact_count'] == 1
    assert 'private-chat' not in json.dumps(result)


def test_status_exposes_persistence_boolean_without_path(monkeypatch, tmp_path):
    import production_inbox
    monkeypatch.setenv('RAILWAY_VOLUME_MOUNT_PATH', str(tmp_path))
    monkeypatch.setattr(monitor, 'get_settings', lambda: SimpleNamespace(sqlite_path=str(tmp_path / 'monitor.sqlite3'), durable_webhook_enabled=False))
    monkeypatch.setattr(production_inbox, 'status_summary', lambda: {'counts': {}, 'oldest_pending_seconds': 0})
    result = monitor.status_snapshot()
    assert result['storage']['persistent_volume_configured'] is True
    assert result['storage']['database_on_persistent_volume'] is True
    assert str(tmp_path) not in json.dumps(result)


@pytest.mark.parametrize('running,age,ok', [(True, 0.5, True), (False, 0.5, False), (True, 31, False), (True, None, False)])
def test_status_detects_stopped_or_stale_worker(monkeypatch, running, age, ok):
    import production_inbox
    state.init_db()
    monkeypatch.setattr(monitor, 'get_settings', lambda: SimpleNamespace(durable_webhook_enabled=True))
    monkeypatch.setattr(production_inbox, 'status_summary', lambda: {'counts': {'pending': 0, 'processing': 0, 'failed': 0, 'uncertain': 0}, 'oldest_pending_seconds': 0})
    worker = SimpleNamespace(status=lambda: {'running': running, 'active_jobs': 0, 'heartbeat_age_seconds': age})
    result = monitor.status_snapshot(worker=worker)
    assert result['ok'] is ok
    if not ok:
        assert result['attention_required'] == 'inbox_worker_not_live'


def test_status_detects_stuck_pending_messages(monkeypatch):
    import production_inbox
    state.init_db()
    monkeypatch.setattr(monitor, 'get_settings', lambda: SimpleNamespace(durable_webhook_enabled=False))
    monkeypatch.setattr(production_inbox, 'status_summary', lambda: {'counts': {'pending': 1}, 'oldest_pending_seconds': 181})
    result = monitor.status_snapshot()
    assert result['ok'] is False
    assert result['attention_required'] == 'inbox_pending_over_180_seconds'


def test_openai_probe_does_not_mislabel_unknown_english_as_russian(monkeypatch):
    import ai
    import ai_budget
    async def create(**kwargs):
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='Hello from the clinic'))])
    monkeypatch.setattr(monitor, 'get_settings', lambda: SimpleNamespace(openai_api_key='fake-test-key', ai_brain_model='fake-model', openai_model='fake-model'))
    monkeypatch.setattr(ai, '_openai_client', lambda key: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    monkeypatch.setattr(ai_budget, 'check_allowed', lambda purpose: (True, ''))
    monkeypatch.setattr(ai_budget, 'record_usage', lambda *args, **kwargs: {})
    result = asyncio.run(monitor._openai_probe())
    assert result['ok'] is False
    assert result['languages']['ru']['detected_language'] == 'unknown'
    assert result['languages']['kk']['detected_language'] == 'unknown'
    assert 'Hello from the clinic' not in json.dumps(result)


def test_status_surfaces_unresolved_outgoing_and_booking_without_identifiers(monkeypatch):
    import production_inbox
    monkeypatch.setattr(monitor, 'get_settings', lambda: SimpleNamespace(durable_webhook_enabled=False))
    monkeypatch.setattr(production_inbox, 'status_summary', lambda: {'counts': {}, 'oldest_pending_seconds': 0})
    stamp = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    state.claim_outgoing_message('private-message-key', 'private-chat')
    state.mark_outgoing_message('private-message-key', 'uncertain')
    with state._connect() as conn:
        conn.execute("INSERT INTO booking_claims(claim_key,chat_id,status,created_at) VALUES (?,?,?,?)", ('private-booking-key', 'private-chat', 'pending', stamp))
    result = monitor.status_snapshot()
    assert result['ok'] is False
    assert result['unresolved_delivery']['uncertain'] == 1
    assert result['unresolved_booking']['stale_pending'] == 1
    assert result['attention_required'] == 'unresolved_delivery_or_booking_outcome'
    assert 'private-chat' not in json.dumps(result)
    assert 'private-message-key' not in json.dumps(result)
