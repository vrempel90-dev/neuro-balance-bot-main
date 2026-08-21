from __future__ import annotations

from types import SimpleNamespace

import live_main


def _settings(**overrides):
    values = dict(
        openai_api_key="sk-secret-must-never-leak",
        ai_enabled=True,
        openai_brain_enabled=True,
        ai_brain_model="gpt-5.4-mini",
        ai_brain_temperature=0.2,
        ai_brain_max_completion_tokens=2000,
        openai_model="gpt-4o-mini",
        openai_humanize_replies=True,
        monthly_ai_budget_usd=17.5,
        ai_max_classifier_calls_per_day=300,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def test_production_status_is_safe_and_ready(monkeypatch):
    monkeypatch.setattr(live_main.neuro, "get_settings", lambda: _settings())
    monkeypatch.setattr(
        live_main.ai_budget,
        "budget_status",
        lambda: {
            "budget_usd": 17.5,
            "spent_usd": 1.25,
            "remaining_usd": 16.25,
            "percent_used": 7.14,
            "over_budget": False,
            "calls_today": 12,
            "calls_limit_per_day": 300,
            "daily_calls_exhausted": False,
        },
    )

    status = live_main._openai_production_status()

    assert status["openai_api_key_present"] is True
    assert status["ai_enabled"] is True
    assert status["openai_brain_enabled"] is True
    assert status["ai_brain_model"] == "gpt-5.4-mini"
    assert status["brain_config_ready"] is True
    assert status["brain_blockers"] == []
    assert status["budget"]["calls_today"] == 12
    assert "sk-secret-must-never-leak" not in repr(status)
    assert "openai_api_key" not in status


def test_production_status_explains_all_common_blockers(monkeypatch):
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
            "over_budget": True,
            "daily_calls_exhausted": True,
        },
    )

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


def test_debug_endpoint_returns_only_safe_status(monkeypatch):
    monkeypatch.setattr(live_main.neuro, "get_settings", lambda: _settings())
    monkeypatch.setattr(
        live_main.ai_budget,
        "budget_status",
        lambda: {"over_budget": False, "daily_calls_exhausted": False},
    )

    payload = live_main.openai_production_status()

    assert payload["ok"] is True
    assert payload["openai"]["production_entrypoint"] == "live_main:app -> main.app"
    assert "sk-secret-must-never-leak" not in repr(payload)
