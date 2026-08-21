from __future__ import annotations

from types import SimpleNamespace

import main


def _settings(**overrides):
    base = dict(
        openai_api_key="sk-super-secret-value",
        ai_enabled=True,
        openai_brain_enabled=True,
        ai_brain_model="gpt-5.4-mini",
        ai_brain_temperature=0.2,
        openai_humanize_replies=True,
        openai_model="gpt-4o-mini",
        monthly_ai_budget_usd=17.5,
        monthly_ai_budget_kzt=20000,
        ai_max_classifier_calls_per_day=300,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_openai_runtime_status_is_operational_and_never_exposes_secret(monkeypatch):
    monkeypatch.setattr(main, "get_settings", lambda: _settings())

    status = main._openai_runtime_status()

    assert status["openai_api_key_present"] is True
    assert status["ai_enabled"] is True
    assert status["openai_brain_enabled"] is True
    assert status["ai_brain_model"] == "gpt-5.4-mini"
    assert status["openai_model"] == "gpt-4o-mini"
    assert status["monthly_ai_budget_usd"] == 17.5
    assert status["ai_max_classifier_calls_per_day"] == 300
    assert status["brain_config_ready"] is True
    assert "sk-super-secret-value" not in repr(status)
    assert "openai_api_key" not in status


def test_openai_runtime_status_explains_disabled_or_missing_config(monkeypatch):
    monkeypatch.setattr(
        main,
        "get_settings",
        lambda: _settings(openai_api_key="", ai_enabled=False, openai_brain_enabled=False),
    )

    status = main._openai_runtime_status()

    assert status["openai_api_key_present"] is False
    assert status["brain_config_ready"] is False
    assert "OPENAI_API_KEY" in status["brain_blockers"]
    assert "AI_ENABLED=false" in status["brain_blockers"]
    assert "OPENAI_BRAIN_ENABLED=false" in status["brain_blockers"]


def test_health_contains_safe_openai_operational_status(monkeypatch):
    monkeypatch.setattr(main, "get_settings", lambda: _settings())
    monkeypatch.setattr(main.state, "log_window_status", lambda: {"log_window_allowed": True})
    monkeypatch.setattr(
        main,
        "_time_debug_payload",
        lambda: {
            "timezone": "Asia/Almaty",
            "utc_time": "2026-08-21T16:00:00+00:00",
            "local_time": "2026-08-21T21:00:00+05:00",
            "bot_work_start": "20:00",
            "bot_work_end": "08:00",
            "bot_work_time_now": True,
            "bot_test_window_enabled": False,
            "bot_test_window_start": "14:00",
            "bot_test_window_end": "14:30",
            "bot_test_window_date": "2026-07-08",
            "bot_test_window_now": False,
            "bot_auto_reply_enabled": True,
            "new_leads_only": True,
            "next_bot_start_at": "2026-08-22T20:00:00+05:00",
            "next_bot_end_at": "2026-08-22T08:00:00+05:00",
        },
    )

    payload = main.health()

    assert payload["openai"]["brain_config_ready"] is True
    assert payload["openai"]["openai_api_key_present"] is True
    assert "sk-super-secret-value" not in repr(payload)
