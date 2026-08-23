"""Production smoke check that creates no appointments and holds no slots.

The task asks for a safe way to verify the production path without booking a
real person into the clinic. Rather than issuing a live booking, this proves
the path by contract:

* the production runtime resolves to the real CRM client, not a stub;
* CRM configuration (base URL, secret header) is present and wired;
* the agent's availability and booking tools reach exactly those functions;
* the dialog entry point actually routes through the GPT-first agent;
* webhook configuration is unchanged and needs no new integration.

Everything here is read-only: the only CRM calls are monkeypatched, so no
HTTP request leaves the process and no appointment or slot is ever touched.
"""
from __future__ import annotations

import inspect
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

os.environ["SQLITE_PATH"] = tempfile.NamedTemporaryFile(delete=False, suffix=".sqlite3").name
os.environ.setdefault("CRM_BOT_SECRET", "test")
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest

import agent
import crm
import dialog
import main
import state
from config import get_settings

state.init_db()


def test_production_crm_client_is_a_real_http_client() -> None:
    """The shipped CRM client must be real HTTP, not a stub.

    This inspects the module source rather than the live attributes: legacy
    test runners install offline fakes onto ``crm`` for their own scenarios,
    and that says nothing about what production runs. What matters here is that
    ``crm.py`` itself defines async functions issuing httpx requests to the
    existing endpoints.
    """
    import ast

    tree = ast.parse(inspect.getsource(crm))
    # Only module-level functions: CRMClient wraps several of these by name, and
    # a wrapper delegating to the real function is not itself an HTTP call.
    async_defs = {
        node.name: node for node in tree.body if isinstance(node, ast.AsyncFunctionDef)
    }
    # patient_lookup delegates to patient_lookup_raw, which is the one that
    # actually issues the request.
    for name in ("check_slots", "book_appointment", "get_doctors", "patient_lookup_raw"):
        assert name in async_defs, f"crm.{name} must be an async production function"
        body = ast.dump(async_defs[name])
        assert "_client" in body, f"crm.{name} must issue a real HTTP request"
    assert "patient_lookup" in async_defs

    source = inspect.getsource(crm)
    assert "_client().get(" in source and "_client().post(" in source


def test_crm_configuration_is_present_and_wired() -> None:
    settings = get_settings()
    assert settings.crm_base_url.startswith("https://"), "CRM base URL must be configured"
    assert crm._url("/api/bot/check-slots").endswith("/api/bot/check-slots")
    assert crm._url("/api/bot/book").endswith("/api/bot/book")
    # The auth header name is part of the existing contract.
    assert "x-bot-secret" in crm._headers()


def test_secrets_are_never_exposed_in_redacted_headers() -> None:
    redacted = crm._redacted_headers()
    assert redacted.get("x-bot-secret") == "***", "the CRM secret must be redacted before logging"


def test_agent_availability_and_booking_reach_the_crm_functions(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prove the wiring end-to-end without sending anything to the CRM."""
    import asyncio

    reached: list[str] = []

    async def check_slots(date: str, doctor_login: str | None = None) -> dict[str, Any]:
        reached.append("check_slots")
        return {
            "ok": True,
            "availability": [
                {"doctorLogin": "d1", "doctorName": "Doctor", "date": date, "availableSlots": ["10:00"]}
            ],
        }

    async def book_appointment(**kwargs: Any) -> dict[str, Any]:
        reached.append("book_appointment")
        return {"ok": True, "id": 1}

    monkeypatch.setattr(crm, "check_slots", check_slots)
    monkeypatch.setattr(crm, "book_appointment", book_appointment)

    session: dict[str, Any] = {
        "complaint": "спина",
        "complaint_gate": "COMPLAINT_OK",
        "age": 40,
        "contraindications_ok": True,
        "contraindications_verdict": "proceed",
        "crm_known_doctors": {"d1": "Doctor"},
    }

    availability = asyncio.run(
        agent._tool_get_available_slots("smoke", session, {"date_from": "2026-09-10"})
    )
    assert availability["ok"] is True
    assert availability["slot_count"] == 1

    booking = asyncio.run(
        agent._tool_book_appointment(
            "smoke",
            session,
            "77011234567",
            {"patient_name": "Тест", "doctor_login": "d1", "date": "2026-09-10", "time_start": "10:00"},
        )
    )
    assert booking["booking_success"] is True
    assert reached == ["check_slots", "book_appointment"], (
        "the agent tools must reach the existing CRM client functions"
    )


def test_dialog_routes_through_the_gpt_first_agent() -> None:
    """The GPT-first path is wired into the production entry point."""
    source = inspect.getsource(dialog.handle_message)
    assert "_try_gpt_first_agent" in source, "handle_message must call the agent loop"
    assert inspect.iscoroutinefunction(dialog._try_gpt_first_agent)


def test_agent_is_skipped_only_for_technical_reasons() -> None:
    """Falling back to Python must never be a conversational decision."""
    source = inspect.getsource(agent.agent_skip_reason)
    for technical in ("openai_key_missing", "ai_disabled", "brain_disabled", "manual_takeover"):
        assert technical in source
    # No step/intent based gating: those would make Python a second brain again.
    assert "step" not in source


def test_webhook_configuration_needs_no_change() -> None:
    settings = get_settings()
    registered = {route.path for route in main.app.routes}
    assert "/webhook/wazzup" in registered
    assert settings.public_base_url == "https://neuro-balance-bot-main-production.up.railway.app"
    expected_callback = f"{settings.public_base_url}/webhook/wazzup"
    assert expected_callback.startswith("https://neuro-balance-bot-main-production.up.railway.app")
