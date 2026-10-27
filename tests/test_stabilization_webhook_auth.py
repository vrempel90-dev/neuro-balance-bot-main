"""Safe offline Wazzup webhook migration tests, never contacts the provider."""
from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from config import get_settings
import production_webhook_auth as auth
import state

_REAL_CLIENT = httpx.AsyncClient
_BASE = 'https://neuro-balance-bot-main-production.up.railway.app'
_SUBSCRIPTIONS = {'messagesAndStatuses': True, 'contactsAndDeals': False, 'futureScope': {'untouched': ['a', 'b']}}


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv('SQLITE_PATH', str(tmp_path / 'auth.sqlite3'))
    monkeypatch.setenv('PUBLIC_BASE_URL', _BASE)
    monkeypatch.setenv('WAZZUP_API_URL', 'https://api.wazzup24.com/v3')
    monkeypatch.setenv('WAZZUP_API_KEY', 'synthetic-provider-secret')
    monkeypatch.delenv('WEBHOOK_SECRET', raising=False)
    get_settings.cache_clear()
    state.init_db()
    yield
    get_settings.cache_clear()


def _provider(monkeypatch, *, uri=None, patch_behavior='success', readback_failure=False):
    remote = {'webhooksUri': uri or _BASE + '/webhook/wazzup', 'subscriptions': copy.deepcopy(_SUBSCRIPTIONS)}
    requests = []
    def handle(request):
        requests.append(request)
        assert str(request.url) == 'https://api.wazzup24.com/v3/webhooks'
        assert request.headers['Authorization'] == 'Bearer synthetic-provider-secret'
        if request.method == 'GET':
            if readback_failure and any(r.method == 'PATCH' for r in requests):
                raise httpx.ReadTimeout('synthetic readback failure', request=request)
            return httpx.Response(200, json=remote)
        assert request.method == 'PATCH'
        data = json.loads(request.content)
        assert data['subscriptions'] == _SUBSCRIPTIONS
        # The durable token exists, but migration has not enabled enforcement
        # while Wazzup may send its validation POST to the new URI.
        assert auth._stored()['status'] == 'attempted'
        assert auth.required_token() == ''
        if patch_behavior == 'rejected':
            return httpx.Response(400, json={'error': 'synthetic rejection'})
        if patch_behavior != 'timeout_without_commit':
            remote.update(copy.deepcopy(data))
        if patch_behavior.startswith('timeout'):
            raise httpx.ReadTimeout('synthetic uncertain PATCH', request=request)
        return httpx.Response(200, json={'ok': True})
    transport = httpx.MockTransport(handle)
    monkeypatch.setattr(auth.httpx, 'AsyncClient', lambda **kwargs: _REAL_CLIENT(transport=transport, **kwargs))
    return remote, requests


def test_no_token_before_migration():
    assert auth.required_token() == ''


def test_explicit_secret_always_takes_precedence(monkeypatch):
    monkeypatch.setenv('WEBHOOK_SECRET', 'explicit-current-secret')
    get_settings.cache_clear()
    assert auth.required_token() == 'explicit-current-secret'


@pytest.mark.parametrize('path', sorted(auth._PATHS))
def test_single_patch_preserves_path_query_and_all_subscriptions(monkeypatch, path):
    remote, requests = _provider(monkeypatch, uri=_BASE + path + '?existing=unchanged')
    result = asyncio.run(auth.secure_existing_webhook())
    assert result['ok'] is True and result['auth_enabled'] is True
    assert result['changed'] is True and result['verified'] is True
    assert [r.method for r in requests] == ['GET', 'PATCH', 'GET']
    token = auth.required_token()
    assert len(token) >= 43
    destination = urlsplit(remote['webhooksUri'])
    assert destination.path == path
    assert parse_qs(destination.query) == {'existing': ['unchanged'], 'hook_token': [token]}
    assert remote['subscriptions'] == _SUBSCRIPTIONS
    assert token not in json.dumps(result)
    assert 'synthetic-provider-secret' not in json.dumps(result)
    assert '?' not in result['destination_path']
    assert Path(get_settings().sqlite_path).stat().st_mode & 0o777 == 0o600


def test_migration_is_idempotent_and_token_survives_settings_restart(monkeypatch):
    remote, requests = _provider(monkeypatch)
    first = asyncio.run(auth.secure_existing_webhook())
    token = auth.required_token()
    get_settings.cache_clear()
    assert auth.required_token() == token
    second = asyncio.run(auth.secure_existing_webhook())
    assert first['ok'] is second['ok'] is True
    assert second['changed'] is False
    assert second['reason'] == 'already_secured'
    assert sum(r.method == 'PATCH' for r in requests) == 1


@pytest.mark.parametrize('uri', [
    'https://neuro-balance-crm.vercel.app/api/wazzup/webhook?private=value',
    _BASE + '/unrelated/path',
    'http://neuro-balance-bot-main-production.up.railway.app/webhook/wazzup',
    'https://neuro-balance-bot-main-production.up.railway.app.attacker.invalid/webhook/wazzup',
    'https://user:pass@neuro-balance-bot-main-production.up.railway.app/webhook/wazzup',
])
def test_foreign_or_unowned_uri_is_never_repointed(monkeypatch, uri):
    remote, requests = _provider(monkeypatch, uri=uri)
    result = asyncio.run(auth.secure_existing_webhook())
    assert result['ok'] is False and result['changed'] is False
    assert result['reason'] == 'existing_webhook_is_not_direct_production_destination'
    assert [r.method for r in requests] == ['GET']
    assert remote['webhooksUri'] == uri
    assert auth.required_token() == ''
    assert 'private=value' not in json.dumps(result)


def test_timeout_after_commit_is_reconciled_by_readback_without_second_patch(monkeypatch):
    remote, requests = _provider(monkeypatch, patch_behavior='timeout_after_commit')
    result = asyncio.run(auth.secure_existing_webhook())
    assert result['ok'] is True and result['verified'] is True
    assert auth.required_token()
    assert [r.method for r in requests] == ['GET', 'PATCH', 'GET']


def test_timeout_without_commit_keeps_legacy_and_never_blindly_retries_patch(monkeypatch):
    remote, requests = _provider(monkeypatch, patch_behavior='timeout_without_commit')
    first = asyncio.run(auth.secure_existing_webhook())
    second = asyncio.run(auth.secure_existing_webhook())
    assert first['ok'] is second['ok'] is False
    assert first['reason'] == 'configuration_write_outcome_uncertain'
    assert second['reason'] == 'prior_migration_requires_reconciliation'
    assert auth.required_token() == ''
    assert sum(r.method == 'PATCH' for r in requests) == 1


def test_failed_readback_does_not_enable_guard(monkeypatch):
    remote, requests = _provider(monkeypatch, readback_failure=True)
    result = asyncio.run(auth.secure_existing_webhook())
    assert result['ok'] is False and result['reason'] == 'configuration_write_outcome_uncertain'
    assert auth.required_token() == ''
    assert auth._stored()['status'] == 'uncertain'


def test_explicit_rejection_is_not_mislabeled_as_success(monkeypatch):
    remote, requests = _provider(monkeypatch, patch_behavior='rejected')
    result = asyncio.run(auth.secure_existing_webhook())
    assert result['ok'] is False
    assert result['reason'] == 'configuration_patch_rejected'
    assert auth.required_token() == ''


def test_ambiguous_prior_attempt_can_enable_when_later_get_proves_exact_target(monkeypatch):
    remote, requests = _provider(monkeypatch, patch_behavior='timeout_without_commit')
    asyncio.run(auth.secure_existing_webhook())
    stored = auth._stored()
    remote['webhooksUri'] = stored['target_uri']
    result = asyncio.run(auth.secure_existing_webhook())
    assert result['ok'] is True and result['reason'] == 'already_secured'
    assert auth.required_token() == stored['token']
    assert sum(r.method == 'PATCH' for r in requests) == 1


def test_provider_drift_after_enabled_never_silently_removes_existing_auth(monkeypatch):
    remote, requests = _provider(monkeypatch)
    asyncio.run(auth.secure_existing_webhook())
    token = auth.required_token()
    remote['webhooksUri'] = _BASE + '/webhook/wazzup'
    result = asyncio.run(auth.secure_existing_webhook())
    assert result['ok'] is False
    assert result['auth_enabled'] is True
    assert auth.required_token() == token
    assert sum(r.method == 'PATCH' for r in requests) == 1
