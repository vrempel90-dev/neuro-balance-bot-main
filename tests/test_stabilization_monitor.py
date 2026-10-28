"""Offline security/contract tests for the production monitoring surface.

These tests sign local tokens and fake HTTP transports. They do not claim
successful live CRM, Wazzup, GitHub OIDC or OpenAI integration.
"""
from __future__ import annotations

import asyncio
import ast
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
from pathlib import Path
import subprocess

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
    monkeypatch.setenv('WEBHOOK_SECRET', 'synthetic-ingress-secret')
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
    async def crm_probe(**kwargs):
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
            if url.endswith('/webhooks'):
                return httpx.Response(200, json={'webhooksUri': 'https://production.invalid/webhook/wazzup?hook_token=synthetic-ingress-secret'}, request=httpx.Request('GET', url))
            return httpx.Response(200, json=[{'channelId': 'configured-channel', 'name': 'Private Person'}], request=httpx.Request('GET', url))
    monkeypatch.setattr(monitor.httpx, 'AsyncClient', lambda **kwargs: FakeClient())
    monkeypatch.setattr(monitor, 'get_settings', lambda: SimpleNamespace(wazzup_api_key='private-api-key', wazzup_channel_id='configured-channel', wazzup_api_url='https://api.wazzup24.com/v3', public_base_url='https://production.invalid'))
    result = asyncio.run(monitor._wazzup_probe())
    assert result['ok'] is True
    assert requests[0][0].endswith('/channels')
    assert result['messages_sent'] == 0
    assert 'private-api-key' not in json.dumps(result)
    assert 'Private Person' not in json.dumps(result)
    assert 'synthetic-ingress-secret' not in json.dumps(result)
    assert result['webhook_routing']['ingress_auth_configuration_verified'] is True
    assert result['webhook_routing']['end_to_end_message_flow_tested'] is False


def test_authenticated_channel_does_not_hide_unverified_crm_proxy(monkeypatch):
    import httpx
    class FakeClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return None
        async def get(self, url, headers):
            data = [{'channelId': 'configured'}] if url.endswith('/channels') else {'webhooksUri': 'https://neuro-balance-crm.vercel.app/api/webhooks/wazzup?secret=private-query'}
            return httpx.Response(200, json=data, request=httpx.Request('GET', url))
    monkeypatch.setattr(monitor.httpx, 'AsyncClient', lambda **kwargs: FakeClient())
    monkeypatch.setattr(monitor, 'get_settings', lambda: SimpleNamespace(wazzup_api_key='synthetic', wazzup_channel_id='configured', wazzup_api_url='https://api.wazzup24.com/v3', public_base_url='https://production.invalid'))
    result = asyncio.run(monitor._wazzup_probe())
    assert result['channel_api_ok'] is True
    assert result['ok'] is False and result['blocked'] is True
    assert result['reason'] == 'external_webhook_proxy_authentication_unverified'
    assert result['webhook_routing']['mode'] == 'external_proxy'
    assert result['webhook_routing']['end_to_end_ingress_verified'] is False
    assert 'private-query' not in json.dumps(result)


@pytest.mark.parametrize('callback,observed,expected', [
    ('https://neuro-balance-crm.vercel.app/api/webhooks/wazzup', True, True),
    ('https://neuro-balance-crm.vercel.app/api/webhooks/wazzup', False, False),
    ('https://neuro-balance-crm.vercel.app/api/webhooks/wazzup/extra', True, False),
    ('https://neuro-balance-crm.vercel.app.attacker.invalid/api/webhooks/wazzup', True, False),
    ('http://neuro-balance-crm.vercel.app/api/webhooks/wazzup', True, False),
])
def test_crm_proxy_probe_requires_exact_destination_and_observed_auth(monkeypatch, callback, observed, expected):
    import httpx
    import proxy_auth
    from config import get_settings
    monkeypatch.delenv('WEBHOOK_SECRET', raising=False)
    monkeypatch.setenv('CRM_BOT_SECRET', 'synthetic-crm-secret')
    get_settings.cache_clear()
    if observed:
        proxy_auth.observe({'X-Bot-Secret': 'synthetic-crm-secret'})
        assert proxy_auth.enable_if_observed()['ok'] is True
    class FakeClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return None
        async def get(self, url, headers):
            data = [{'channelId': 'configured'}] if url.endswith('/channels') else {'webhooksUri': callback + '?private=never-export'}
            return httpx.Response(200, json=data, request=httpx.Request('GET', url))
    monkeypatch.setattr(monitor.httpx, 'AsyncClient', lambda **kwargs: FakeClient())
    monkeypatch.setattr(monitor, 'get_settings', lambda: SimpleNamespace(wazzup_api_key='synthetic', wazzup_channel_id='configured', wazzup_api_url='https://api.wazzup24.com/v3', public_base_url='https://production.invalid', crm_base_url='https://neuro-balance-crm.vercel.app', crm_bot_secret='synthetic-crm-secret'))
    result = asyncio.run(monitor._wazzup_probe())
    assert result['ok'] is expected
    assert result['webhook_routing']['crm_proxy_auth_enabled'] is expected
    assert result['webhook_routing']['ingress_auth_configuration_verified'] is expected
    assert result['webhook_routing']['end_to_end_message_flow_tested'] is False
    assert 'synthetic-crm-secret' not in json.dumps(result)
    assert 'never-export' not in json.dumps(result)


def test_status_reports_observed_enforced_proxy_as_authenticated(monkeypatch):
    import proxy_auth
    from config import get_settings
    monkeypatch.delenv('WEBHOOK_SECRET', raising=False)
    monkeypatch.setenv('CRM_BOT_SECRET', 'synthetic-crm-secret')
    get_settings.cache_clear()
    proxy_auth.observe({'Authorization': 'Bearer synthetic-crm-secret'})
    assert proxy_auth.enable_if_observed()['ok'] is True
    result = monitor.status_snapshot()
    assert result['config']['webhook_auth_enabled'] is True
    assert result['config']['crm_proxy_auth_enabled'] is True
    assert result['proxy_auth']['authenticated_count'] == 1
    assert 'synthetic-crm-secret' not in json.dumps(result)


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


def test_unsecured_ingress_is_not_reported_green(monkeypatch):
    import production_inbox
    from config import get_settings
    monkeypatch.delenv('WEBHOOK_SECRET', raising=False)
    get_settings.cache_clear()
    monkeypatch.setattr(monitor, 'get_settings', lambda: SimpleNamespace(durable_webhook_enabled=False))
    monkeypatch.setattr(production_inbox, 'status_summary', lambda: {'counts': {}, 'oldest_pending_seconds': 0})
    result = monitor.status_snapshot()
    assert result['operational_ok'] is True
    assert result['ok'] is False
    assert result['security']['ok'] is False
    assert result['security_attention_required'] == 'ingress_authentication_not_configured'


def _health_poller(request, clock):
    import yaml
    document = yaml.safe_load((Path(__file__).parents[1] / '.github/workflows/production-monitor.yml').read_text())
    script = document['jobs']['monitor']['steps'][0]['run'].split("python - <<'PY'\n", 1)[1].rsplit('\nPY', 1)[0]
    function = next(node for node in ast.parse(script).body if isinstance(node, ast.FunctionDef) and node.name == 'poll_health')
    namespace = {'request': request, 'time': clock}
    exec(compile(ast.Module(body=[function], type_ignores=[]), 'workflow-health-poller', 'exec'), namespace)
    return namespace['poll_health']


def test_deploy_health_retries_initial_timeout_and_observes_release():
    calls = []
    clock = SimpleNamespace(now=0.0)
    clock.monotonic = lambda: clock.now
    clock.sleep = lambda seconds: setattr(clock, 'now', clock.now + seconds)
    def request(url, timeout):
        calls.append(timeout)
        if len(calls) == 1:
            raise TimeoutError('synthetic deployment gap')
        return {'ok': True, 'release_sha': 'new-release'}
    health, evidence = _health_poller(request, clock)('https://production.invalid/health', 'new-release', budget_seconds=30)
    assert health['release_sha'] == 'new-release'
    assert evidence['attempts'] == 2
    assert evidence['recovered_error_type'] == 'TimeoutError'
    assert clock.now == 5


def test_permanent_health_outage_stops_at_bounded_deadline():
    clock = SimpleNamespace(now=0.0)
    clock.monotonic = lambda: clock.now
    clock.sleep = lambda seconds: setattr(clock, 'now', clock.now + seconds)
    def request(url, timeout):
        raise TimeoutError('synthetic permanent outage')
    health, evidence = _health_poller(request, clock)('https://production.invalid/health', 'new-release', budget_seconds=12)
    assert health == {}
    assert evidence['deadline_exhausted'] is True
    assert clock.now == 12
    assert evidence['attempts'] <= 4


def test_repair_workflow_is_valid_manual_only_and_shell_parses():
    import yaml
    root = Path(__file__).parents[1]
    document = yaml.load((root / '.github/workflows/claude-real-repair.yml').read_text(), Loader=yaml.BaseLoader)
    assert set(document['on']) == {'workflow_dispatch'}
    for step in document['jobs']['repair']['steps']:
        if 'run' in step:
            checked = subprocess.run(['bash', '-n'], input=step['run'], text=True, capture_output=True)
            assert checked.returncode == 0, (step['name'], checked.stderr)


def test_only_verified_scheduled_oidc_counts_toward_external_cadence(client, signing_key, monkeypatch):
    monkeypatch.setattr(monitor, 'status_snapshot', lambda **kwargs: {'ok': True})
    client.get('/internal/monitor/status', headers={'Authorization': 'Bearer ' + _token(signing_key, event_name='push')})
    assert monitor._external_coverage()['scheduled_observations_24h'] == 0
    client.get('/internal/monitor/status', headers={'Authorization': 'Bearer ' + _token(signing_key, event_name='schedule')})
    assert monitor._external_coverage()['scheduled_observations_24h'] == 1


def test_external_schedule_gaps_remain_visible():
    monitor._record_observation('external_schedule', {'event': 'schedule'})
    with state._connect() as conn:
        conn.execute('UPDATE production_monitor_observations SET checked_at=?', ((datetime.now(timezone.utc) - timedelta(hours=3)).isoformat(),))
    coverage = monitor._external_coverage()
    assert coverage['state'] == 'gap_observed'
    assert coverage['maximum_observed_gap_seconds'] >= 10800
    assert coverage['five_minute_cadence_proven'] is False


def test_internal_periodic_checks_run_without_recurring_openai(monkeypatch):
    calls = []
    monkeypatch.setattr(monitor, 'status_snapshot', lambda **kwargs: {'ok': True})
    async def crm_probe():
        calls.append('crm')
        return {'ok': True, 'read_only': True}
    async def wazzup_probe():
        calls.append('wazzup')
        return {'ok': True, 'read_only': True}
    async def forbidden_openai():
        raise AssertionError('periodic checks must not spend recurring AI tokens')
    monkeypatch.setattr(monitor, '_crm_probe', crm_probe)
    monkeypatch.setattr(monitor, '_wazzup_probe', wazzup_probe)
    monkeypatch.setattr(monitor, '_openai_probe', forbidden_openai)
    async def scenario():
        worker = monitor.PeriodicMonitor(lambda: None, local_interval=0.05, dependency_interval=0.05)
        worker.start()
        await asyncio.sleep(0.14)
        running = worker.status()
        await worker.stop()
        return worker, running
    worker, running = asyncio.run(scenario())
    assert running['running'] is True
    assert running['local_checked_at'] and running['dependencies_checked_at']
    assert calls.count('crm') >= 2 and calls.count('wazzup') >= 2
    assert worker.status()['running'] is False
    with state._connect() as conn:
        kinds = {row[0] for row in conn.execute('SELECT DISTINCT kind FROM production_monitor_observations')}
    assert kinds == {'local', 'dependencies'}


def test_cached_dependency_failure_cannot_be_reported_as_healthy(monkeypatch):
    import production_inbox
    monkeypatch.setattr(monitor, 'get_settings', lambda: SimpleNamespace(durable_webhook_enabled=False))
    monkeypatch.setattr(production_inbox, 'status_summary', lambda: {'counts': {}, 'oldest_pending_seconds': 0})
    periodic = SimpleNamespace(status=lambda: {'running': True, 'local_check_age_seconds': 1, 'dependency_results': {'crm': {'ok': False}, 'wazzup': {'ok': True}}})
    result = monitor.status_snapshot(periodic_monitor=periodic)
    assert result['ok'] is False
    assert result['monitoring_attention_required'] == 'internal_dependency_check_failed'


def test_crm_readback_unavailable_is_explicit_without_fake_creation(monkeypatch):
    import crm
    import production_acceptance
    async def slots(date): return {'ok': True, 'slots': [], 'availability': []}
    async def no_record(): return {'executed': False, 'verified': False, 'attempted_records': 0, 'reason': 'no_existing_ai_booking'}
    monkeypatch.setattr(crm, 'check_slots', slots)
    monkeypatch.setattr(production_acceptance, 'verify_existing_ai_booking', no_record)
    result = asyncio.run(monitor._crm_probe())
    assert result['existing_booking_readback']['executed'] is False
    assert result['existing_booking_readback']['verified'] is False
    assert result['read_only'] is True


def test_completed_daily_report_uses_exact_clinic_boundaries_and_excludes_end():
    # 09:00 Almaty = 04:00 UTC, including at the exact boundary.
    with state._connect() as conn:
        for stamp in ['2026-10-26T03:59:59+00:00', '2026-10-26T04:00:00+00:00', '2026-10-27T03:59:59+00:00', '2026-10-27T04:00:00+00:00']:
            conn.execute('INSERT INTO events(chat_id,event_type,payload_json,created_at) VALUES (?,?,?,?)', ('private-contact', 'agent_booking_crm_success', '{}', stamp))
    report = monitor._completed_daily_report(datetime(2026, 10, 27, 4, 0, tzinfo=timezone.utc))
    assert report['start_inclusive'] == '2026-10-26T09:00:00+05:00'
    assert report['end_exclusive'] == '2026-10-27T09:00:00+05:00'
    assert report['counters']['confirmed_booking_events'] == 2
    assert report['delivery_verified'] is False
    assert 'private-contact' not in json.dumps(report)
    # A run before 09:00 must report the previous completed period.
    earlier = monitor._completed_daily_report(datetime(2026, 10, 27, 3, 59, tzinfo=timezone.utc))
    assert earlier['end_exclusive'] == '2026-10-26T09:00:00+05:00'


def test_durable_inbox_duplicates_are_reported_separately():
    state.log_event('inbox', 'inbox_webhook_accepted', {'accepted_count': 4, 'duplicate_count': 3})
    counters = monitor._event_summary((datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat())
    assert counters['durable_inbox_duplicate_events'] == 3
    assert counters['duplicate_block_events'] == 0


def test_openai_probe_uses_application_prompt_and_two_real_context_turns(monkeypatch):
    import ai
    import ai_budget
    observed = []
    async def create(**kwargs):
        observed.append(kwargs)
        assert kwargs['messages'][0]['content'] == ai._full_dialog_brain_system_prompt()
        assert kwargs['response_format'] == {'type': 'json_object'}
        context = json.loads(kwargs['messages'][1]['content'])['dialog_context']
        text = context['current_user_message']
        kk = 'белім' in text or 'қазақша' in text
        age = 34 if '34' in text else None
        response = {
            'intent': 'age_answer' if age else 'complaint',
            'next_step': 'contraindications' if age else 'age',
            'extracted': {'age': age, 'language': 'kk' if kk else 'ru'},
            'needs_python_tool': 'none',
            'reply': ('Қарсы көрсетілімдеріңіз бар ма?' if age else 'Сәлеметсіз бе! Жасыңыз нешеде?') if kk else ('У Вас есть противопоказания?' if age else 'Здравствуйте! Сколько Вам лет?'),
        }
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(response)))])
    monkeypatch.setattr(monitor, 'get_settings', lambda: SimpleNamespace(openai_api_key='synthetic', ai_brain_model='synthetic', openai_model='synthetic'))
    monkeypatch.setattr(ai, '_openai_client', lambda key: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    monkeypatch.setattr(ai_budget, 'check_allowed', lambda purpose: (True, ''))
    monkeypatch.setattr(ai_budget, 'record_usage', lambda *args, **kwargs: {})
    result = asyncio.run(monitor._openai_probe())
    assert result['ok'] is True
    assert set(result['scenarios']) == {'ru', 'kk', 'ru_to_kk', 'kk_to_ru'}
    assert len(observed) == 4
    switched = [json.loads(call['messages'][1]['content'])['dialog_context'] for call in observed if '34' in call['messages'][1]['content']]
    assert len(switched) == 2
    assert all(len(context['recent_history']) == 2 for context in switched)
    assert all(context['session_state']['language_locked'] for context in switched)
    assert result['native_language_quality_verified'] is False
    assert result['crm_writes'] == result['patient_messages_sent'] == 0
    assert 'Жасыңыз нешеде?' in result['scenarios']['kk']['synthetic_reply']
    assert all(len(item.get('synthetic_reply', '')) <= 500 for item in result['scenarios'].values())


def test_openai_probe_reports_budget_block_as_unexecuted(monkeypatch):
    import ai
    import ai_budget
    monkeypatch.setattr(monitor, 'get_settings', lambda: SimpleNamespace(openai_api_key='synthetic', ai_brain_model='synthetic', openai_model='synthetic'))
    monkeypatch.setattr(ai, '_openai_client', lambda key: object())
    monkeypatch.setattr(ai_budget, 'check_allowed', lambda purpose: (False, 'synthetic_budget_exhausted'))
    result = asyncio.run(monitor._openai_probe())
    assert result['ok'] is False
    assert len(result['scenarios']) == 4
    assert all(value['executed'] is False for value in result['scenarios'].values())


def test_rotated_crm_credential_is_not_reported_verified(monkeypatch):
    import proxy_auth
    from config import get_settings
    monkeypatch.delenv('WEBHOOK_SECRET', raising=False)
    monkeypatch.setenv('CRM_BOT_SECRET', 'first-synthetic-secret')
    get_settings.cache_clear()
    proxy_auth.observe({'X-Bot-Secret': 'first-synthetic-secret'})
    assert proxy_auth.enable_if_observed()['ok'] is True
    monkeypatch.setenv('CRM_BOT_SECRET', 'second-synthetic-secret')
    get_settings.cache_clear()
    result = monitor.status_snapshot()
    assert proxy_auth.required() is True  # Enforcement must remain fail-closed.
    assert result['proxy_auth']['current_credential_matches'] is False
    assert result['config']['webhook_auth_enabled'] is False
    assert result['security']['ok'] is False
    assert 'synthetic-secret' not in json.dumps(result)


@pytest.mark.parametrize('path', ['/internal/monitor/probe', '/internal/monitor/voice-probe', '/internal/monitor/secure-webhook'])
def test_scheduled_identity_cannot_trigger_ai_or_voice_spend(client, signing_key, monkeypatch, path):
    import production_voice_probe
    async def forbidden():
        raise AssertionError('scheduled monitor must not invoke synthetic AI')
    monkeypatch.setattr(monitor, '_openai_probe', forbidden)
    monkeypatch.setattr(production_voice_probe, 'synthetic_voice_probe', forbidden)
    response = client.post(path, headers={'Authorization': 'Bearer ' + _token(signing_key, event_name='schedule')})
    assert response.status_code == 403


def test_voice_probe_uses_same_auth_and_returns_only_probe_contract(client, monkeypatch):
    import production_voice_probe
    observed = []
    async def synthetic():
        observed.append('synthetic')
        return {'ok': True, 'executed': True, 'native_quality_verified': False, 'channel_delivery_verified': False}
    monkeypatch.setattr(production_voice_probe, 'synthetic_voice_probe', synthetic)
    assert client.post('/internal/monitor/voice-probe').status_code == 401
    assert observed == []
    monkeypatch.setenv('OPENAI_DEBUG_ADMIN_TOKEN', 'synthetic-monitor-secret')
    response = client.post('/internal/monitor/voice-probe', headers={'Authorization': 'Bearer synthetic-monitor-secret'})
    assert response.status_code == 200
    assert response.json()['ok'] is True
    assert response.json()['voice']['channel_delivery_verified'] is False
    assert response.json()['patient_messages_sent'] == response.json()['crm_writes'] == 0
    assert observed == ['synthetic']
    assert 'synthetic-monitor-secret' not in response.text


@pytest.mark.parametrize('returning', [False, True])
def test_manual_crm_probe_rechecks_prior_new_session_without_forcing_new(monkeypatch, returning):
    import crm
    state.save_session('77010000001', {'phone': '77010000001', 'raw_crm_isNew': True, 'crm_patient_state': 'NEW'})
    calls = []
    async def slots(date):
        return {'ok': True, 'slots': [], 'availability': []}
    async def lookup(phone):
        calls.append(phone)
        return {'ok': True, 'raw': {'found': returning, 'isNew': not returning}, 'appointments': []}
    async def doctors(force):
        assert force is True
        return {'ok': True, 'doctors': [{'name': 'Synthetic Provider', 'specialty': 'Neurology', 'phone': 'must-never-export'}]}
    async def services(force):
        assert force is True
        return {'ok': True, 'treatable': ['synthetic service']}
    monkeypatch.setattr(crm, 'check_slots', slots)
    monkeypatch.setattr(crm, 'lookup_active_appointments_by_phone', lookup)
    monkeypatch.setattr(crm, 'get_doctors', doctors)
    monkeypatch.setattr(crm, 'get_services', services)
    result = asyncio.run(monitor._crm_probe(include_new_client=True))
    assert calls == ['77010000001']
    assert result['new_client_check']['executed'] is True
    assert result['new_client_check']['currently_new'] is (not returning)
    assert result['new_client_check']['bot_may_reply'] is (not returning)
    assert result['new_client_check']['crm_classification'] == ('RETURNING' if returning else 'NEW')
    assert '77010000001' not in json.dumps(result)
    assert result['directory_checks']['doctors']['confirmed_provider_descriptors'] == [{'name': 'Synthetic Provider', 'specialty': 'Neurology'}]
    assert result['directory_checks']['services']['count'] == 1
    assert 'must-never-export' not in json.dumps(result)


def test_empty_unauthenticated_probe_requires_verified_auth_and_expects401(monkeypatch):
    import yaml
    import urllib.request
    import urllib.error
    document = yaml.safe_load((Path(__file__).parents[1] / '.github/workflows/production-monitor.yml').read_text())
    script = document['jobs']['monitor']['steps'][0]['run'].split("python - <<'PY'\n", 1)[1].rsplit('\nPY', 1)[0]
    function = next(node for node in ast.parse(script).body if isinstance(node, ast.FunctionDef) and node.name == 'verify_unauthenticated_webhook')
    namespace = {'urllib': __import__('urllib')}
    exec(compile(ast.Module(body=[function], type_ignores=[]), 'workflow-empty-ingress-probe', 'exec'), namespace)
    calls = []
    def rejected(request, timeout):
        calls.append(request)
        raise urllib.error.HTTPError(request.full_url, 401, 'Unauthorized', None, None)
    monkeypatch.setattr(urllib.request, 'urlopen', rejected)
    probe = namespace['verify_unauthenticated_webhook']
    assert probe('https://production.invalid/webhook/wazzup', {'security': {'ok': False}})['executed'] is False
    assert calls == []
    result = probe('https://production.invalid/webhook/wazzup', {'security': {'ok': True, 'provider_route_checked': True}})
    assert result['ok'] is True and result['http_status'] == 401
    assert calls[0].data == b'{}'
    assert not calls[0].has_header('Authorization')
