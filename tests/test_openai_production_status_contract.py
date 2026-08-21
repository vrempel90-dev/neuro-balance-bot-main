from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient

import live_main


def _settings(**overrides: object) -> SimpleNamespace:
    """Build deterministic OpenAI settings for production-status contract tests."""
    values: dict[str, object] = {
        "openai_api_key": "sk-secret-must-never-leak",
        "ai_enabled": True,
        "openai_brain_enabled": True,
        "ai_brain_model": "gpt-5.4-mini",
        "ai_brain_temperature": 0.2,
        "ai_brain_max_completion_tokens": 2000,
        "openai_model": "gpt-4o-mini",
        "openai_humanize_replies": True,
        "monthly_ai_budget_usd": 17.5,
        "ai_max_classifier_calls_per_day": 300,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _healthy_budget() -> dict[str, object]:
    """Return deterministic available budget telemetry for ready-state tests."""
    return {
        "budget_status_available": True,
        "budget_usd": 17.5,
        "spent_usd": 1.25,
        "remaining_usd": 16.25,
        "percent_used": 7.14,
        "over_budget": False,
        "calls_today": 12,
        "calls_limit_per_day": 300,
        "daily_calls_exhausted": False,
    }


def test_production_status_is_safe_and_ready(monkeypatch):
    """A healthy configuration reports readiness without leaking the API key."""
    monkeypatch.setattr(live_main.neuro, "get_settings", _settings)
    monkeypatch.setattr(live_main.ai_budget, "budget_status", _healthy_budget)
    monkeypatch.setattr(live_main.openai_runtime, "AsyncOpenAI", object)

    status = live_main._openai_production_status()

    assert status["openai_api_key_present"] is True
    assert status["openai_package_available"] is True
    assert status["budget_status_available"] is True
    assert status["ai_enabled"] is True
    assert status["openai_brain_enabled"] is True
    assert status["ai_brain_model"] == "gpt-5.4-mini"
    assert status["brain_config_ready"] is True
    assert status["brain_blockers"] == []
    assert status["budget"]["calls_today"] == 12
    assert "sk-secret-must-never-leak" not in repr(status)
    assert "openai_api_key" not in status


def test_production_status_explains_all_common_blockers(monkeypatch):
    """Missing config and exhausted limits are surfaced as explicit blockers."""
    monkeypatch.setattr(
        live_main.neuro,
        "get_settings",
        lambda: _settings(
            openai_api_key="",
            ai_enabled=False,
            openai_brain_enabled=False,
            ai_brain_model="",
            openai_model="",
        ),
    )
    monkeypatch.setattr(
        live_main.ai_budget,
        "budget_status",
        lambda: {
            "budget_status_available": True,
            "over_budget": True,
            "daily_calls_exhausted": True,
        },
    )
    monkeypatch.setattr(live_main.openai_runtime, "AsyncOpenAI", object)

    status = live_main._openai_production_status()

    assert status["brain_config_ready"] is False
    assert set(status["brain_blockers"]) == {
        "OPENAI_API_KEY",
        "AI_ENABLED=false",
        "OPENAI_BRAIN_ENABLED=false",
        "AI_BRAIN_MODEL_or_OPENAI_MODEL",
        "monthly_budget_exceeded",
        "daily_call_limit_exceeded",
    }


def test_production_status_blocks_readiness_when_budget_status_unavailable(monkeypatch):
    """Unavailable budget telemetry must never be reported as fully ready."""
    monkeypatch.setattr(live_main.neuro, "get_settings", _settings)
    monkeypatch.setattr(
        live_main.ai_budget,
        "budget_status",
        lambda: (_ for _ in ()).throw(RuntimeError("budget db unavailable")),
    )
    monkeypatch.setattr(live_main.openai_runtime, "AsyncOpenAI", object)

    status = live_main._openai_production_status()

    assert status["budget_status_available"] is False
    assert status["brain_config_ready"] is False
    assert "budget_status_unavailable" in status["brain_blockers"]


def test_production_status_blocks_readiness_without_openai_runtime(monkeypatch):
    """Missing AsyncOpenAI runtime dependency is an explicit readiness blocker."""
    monkeypatch.setattr(live_main.neuro, "get_settings", _settings)
    monkeypatch.setattr(live_main.ai_budget, "budget_status", _healthy_budget)
    monkeypatch.setattr(live_main.openai_runtime, "AsyncOpenAI", None)

    status = live_main._openai_production_status()

    assert status["openai_package_available"] is False
    assert status["brain_config_ready"] is False
    assert "openai_package" in status["brain_blockers"]


def test_production_status_preserves_zero_brain_temperature(monkeypatch):
    """An explicitly configured zero temperature must not be replaced by the default."""
    monkeypatch.setattr(live_main.neuro, "get_settings", lambda: _settings(ai_brain_temperature=0.0))
    monkeypatch.setattr(live_main.ai_budget, "budget_status", _healthy_budget)
    monkeypatch.setattr(live_main.openai_runtime, "AsyncOpenAI", object)

    status = live_main._openai_production_status()

    assert status["ai_brain_temperature"] == 0.0


def test_debug_endpoint_rejects_anonymous_client(monkeypatch):
    """Production OpenAI diagnostics must not be readable without admin auth."""
    monkeypatch.setenv("OPENAI_DEBUG_ADMIN_TOKEN", "debug-admin-secret")

    response = TestClient(live_main.app).get("/debug/openai/status")

    assert response.status_code == 401
    assert response.json()["detail"] == "Unauthorized"
    assert response.headers.get("www-authenticate") == "Bearer"


def test_debug_endpoint_rejects_invalid_admin_token(monkeypatch):
    """A wrong Bearer token must be rejected with the standard auth challenge."""
    monkeypatch.setenv("OPENAI_DEBUG_ADMIN_TOKEN", "debug-admin-secret")

    response = TestClient(live_main.app).get(
        "/debug/openai/status",
        headers={"Authorization": "Bearer wrong-token"},
    )

    assert response.status_code == 401
    assert response.headers.get("www-authenticate") == "Bearer"


def test_debug_endpoint_accepts_lowercase_bearer_scheme(monkeypatch):
    """HTTP auth schemes are case-insensitive while the secret token remains exact."""
    monkeypatch.setenv("OPENAI_DEBUG_ADMIN_TOKEN", "debug-admin-secret")
    monkeypatch.setattr(live_main.neuro, "get_settings", _settings)
    monkeypatch.setattr(live_main.ai_budget, "budget_status", _healthy_budget)
    monkeypatch.setattr(live_main.openai_runtime, "AsyncOpenAI", object)

    response = TestClient(live_main.app).get(
        "/debug/openai/status",
        headers={"Authorization": "bearer debug-admin-secret"},
    )

    assert response.status_code == 200


def test_debug_endpoint_fails_closed_without_admin_token(monkeypatch):
    """A missing server-side admin token disables diagnostics instead of opening them."""
    monkeypatch.delenv("OPENAI_DEBUG_ADMIN_TOKEN", raising=False)

    response = TestClient(live_main.app).get("/debug/openai/status")

    assert response.status_code == 503
    assert response.json()["detail"] == "OpenAI diagnostics are disabled"


def test_debug_endpoint_returns_only_safe_status_to_authorized_admin(monkeypatch):
    """An authorized admin receives diagnostics while all secrets remain hidden."""
    monkeypatch.setenv("OPENAI_DEBUG_ADMIN_TOKEN", "debug-admin-secret")
    monkeypatch.setattr(live_main.neuro, "get_settings", _settings)
    monkeypatch.setattr(live_main.ai_budget, "budget_status", _healthy_budget)
    monkeypatch.setattr(live_main.openai_runtime, "AsyncOpenAI", object)

    response = TestClient(live_main.app).get(
        "/debug/openai/status",
        headers={"Authorization": "Bearer debug-admin-secret"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["openai"]["production_entrypoint"] == "live_main:app -> main.app"
    assert "sk-secret-must-never-leak" not in repr(payload)
    assert "debug-admin-secret" not in repr(payload)
