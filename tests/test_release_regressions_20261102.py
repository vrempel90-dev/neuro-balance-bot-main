"""Regression checks for the November 2026 Neuro Balance dialogue release.

These are deterministic contracts, not replacements for live LLM evaluations.
"""
from __future__ import annotations

import pytest

import ai
import agent


@pytest.mark.parametrize(
    "key",
    [
        "sk-proj-EXAMPLE_SECRET_1234567890abcdefghijklmnopqrstuvwxyz",
        "sk-EXAMPLE_SECRET_1234567890",
        "sk-svcacct-EXAMPLE_SECRET_abcdefgh123456",
    ],
)
def test_openai_key_never_enters_error_message_or_diagnostics(key: str) -> None:
    error = RuntimeError(f"401 Incorrect API key: {key}\nPlease check your token.")
    safe = ai._safe_openai_error_message(error, limit=200)
    detail = ai._openai_error_detail(error, "gpt-5.6-terra")
    assert key not in safe
    assert "[REDACTED_OPENAI_KEY]" in safe
    assert key not in str(detail)
    assert "401" in detail["openai_error_message_preview"]
    assert "\n" not in safe


def test_diagnostics_preserve_http_status_without_secret() -> None:
    class Unauthorized(Exception):
        status_code = 401

    error = Unauthorized("Incorrect API key: sk-proj-EXAMPLE_1234567890")
    result = ai._openai_error_detail(error, "gpt-5.6-terra")
    assert result["openai_error_detail"]["status_code"] == 401
    assert result["openai_error_detail"]["model"] == "gpt-5.6-terra"
    assert "EXAMPLE_1234567890" not in str(result)


@pytest.mark.parametrize(
    "message",
    [
        "Костанай",
        'get_clinic_info с topic="other_city"',
        "подтверждением доставки",
        "delivered=false",
        "инсулиновая помпа",
        "кохлеарный имплант",
        "ближайшего дня недели",
        "свободное время сейчас подтвердить нельзя",
    ],
)
def test_critical_dialog_contract_is_explicit(message: str) -> None:
    assert message in agent.AGENT_OVERRIDES


def test_no_change_to_core_tool_schemas_or_booking_guards() -> None:
    # A prompt hotfix must not modify the interface used by the real CRM.
    assert "record_patient_facts" in agent.AGENT_TOOL_NAMES
    assert "get_available_slots" in agent.AGENT_TOOL_NAMES
    assert "book_appointment" in agent.AGENT_TOOL_NAMES
    assert "find_my_appointment" in agent.AGENT_TOOL_NAMES
    assert "cancel_appointment" in agent.AGENT_TOOL_NAMES
    assert "reschedule_appointment" in agent.AGENT_TOOL_NAMES
