"""Regression checks: optional OpenAI spending/output caps can be disabled.

This does not remove CRM admission, safety, idempotency or provider rate limits.
"""
from datetime import datetime

import ai
import ai_budget
import config
from schedule import time_gate_status


def test_zero_completion_cap_omits_api_limit(monkeypatch):
    monkeypatch.setenv("AI_BRAIN_MAX_COMPLETION_TOKENS", "0")
    config.get_settings.cache_clear()
    try:
        assert ai._brain_token_limit_kwargs("gpt-5.6-terra") == {}
        assert ai._brain_token_limit_kwargs("gpt-5.6") == {}
    finally:
        config.get_settings.cache_clear()


def test_positive_completion_cap_is_still_supported(monkeypatch):
    monkeypatch.setenv("AI_BRAIN_MAX_COMPLETION_TOKENS", "4096")
    config.get_settings.cache_clear()
    try:
        assert ai._brain_token_limit_kwargs("gpt-5.6-terra") == {"max_completion_tokens": 4096}
    finally:
        config.get_settings.cache_clear()


def test_zero_app_budget_and_daily_call_cap(monkeypatch):
    monkeypatch.setenv("MONTHLY_AI_BUDGET_USD", "0")
    monkeypatch.setenv("MONTHLY_AI_BUDGET_KZT", "0")
    monkeypatch.setenv("AI_MAX_CLASSIFIER_CALLS_PER_DAY", "0")
    config.get_settings.cache_clear()
    try:
        assert ai_budget.monthly_budget_usd() == 0
        assert ai_budget.max_calls_per_day() == 0
    finally:
        config.get_settings.cache_clear()


def test_around_clock_mode_does_not_require_night_window(monkeypatch):
    monkeypatch.setenv("WORK_HOURS_GUARD_ENABLED", "false")
    config.get_settings.cache_clear()
    try:
        gate = time_gate_status(datetime.fromisoformat("2026-11-11T12:00:00+05:00"))
        assert gate["bot_work_time_now"] is True
        assert gate["time_gate_reason"] == "work_hours_guard_disabled"
    finally:
        config.get_settings.cache_clear()
