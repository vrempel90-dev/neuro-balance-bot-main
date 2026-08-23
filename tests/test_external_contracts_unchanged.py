"""Guard rails: the GPT-first work must not touch production integrations.

The Wazzup webhook, the Railway public URL and the CRM API contract are already
configured in production. This file pins them so a future refactor cannot
silently require a new webhook, a new callback URL or a changed CRM endpoint.

It asserts contracts, not behaviour:

* the existing webhook paths still exist and still accept the current payload;
* inbound stays inbound and echoes are still ignored;
* a duplicate webhook delivery is processed once (no double booking);
* the CRM endpoints, auth header and required booking fields are unchanged;
* the new agent tools call exactly those endpoints and nothing else.
"""
from __future__ import annotations

import ast
import asyncio
import inspect
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

os.environ["SQLITE_PATH"] = tempfile.NamedTemporaryFile(delete=False, suffix=".sqlite3").name
os.environ.setdefault("CRM_BOT_SECRET", "test-secret")
# NB: the value above only applies if no other test module set it first, so the
# assertions below compare against the effective setting, not a literal.
os.environ.setdefault("WAZZUP_API_KEY", "test-key")
os.environ.setdefault("WAZZUP_CHANNEL_ID", "test-channel")
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import httpx
import pytest
from fastapi.testclient import TestClient

import agent
import crm
import main
import state
from config import get_settings

state.init_db()


# ---------------------------------------------------------------------------
# Wazzup webhook contract
# ---------------------------------------------------------------------------

EXPECTED_WEBHOOK_PATHS = {
    "/wazzup/webhook",
    "/webhook",
    "/api/wazzup/webhook",
    "/webhook/wazzup",
}


def _payload(**overrides: Any) -> dict[str, Any]:
    data = {
        "chatId": "77010000042",
        "phone": "77010000042",
        "text": "Здравствуйте, болит спина, хочу записаться",
        "messageId": "wh-msg-1",
        "timestamp": "2026-09-01T21:00:00+05:00",
        "direction": "incoming",
        "channelId": "test-channel",
    }
    data.update(overrides)
    return data


def test_existing_webhook_paths_are_unchanged() -> None:
    """A new integration must never be required — these paths already exist."""
    registered = {route.path for route in main.app.routes}
    missing = EXPECTED_WEBHOOK_PATHS - registered
    assert not missing, f"production webhook paths disappeared: {sorted(missing)}"


def test_public_base_url_and_channel_config_unchanged() -> None:
    settings = get_settings()
    assert settings.public_base_url == "https://neuro-balance-bot-main-production.up.railway.app"
    assert settings.wazzup_api_url == "https://api.wazzup24.com/v3"


def test_webhook_accepts_current_payload_and_reaches_the_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict[str, Any]] = []

    async def handler(message: dict[str, Any]) -> str:
        seen.append(message)
        return "Здравствуйте! Подскажите, сколько Вам лет?"

    async def sender(**kwargs: Any) -> dict[str, Any]:
        return {"ok": True, "status_code": 200}

    monkeypatch.setattr(main, "handle_incoming_message", handler)
    monkeypatch.setattr(main, "send_wazzup_message", sender)
    monkeypatch.setattr(main, "is_bot_work_time", lambda: True)

    client = TestClient(main.app)
    response = client.post("/webhook/wazzup", json=_payload())

    assert response.status_code == 200
    assert len(seen) == 1
    assert seen[0]["source"] == "wazzup"
    assert seen[0]["is_incoming"] is True


def test_outbound_echo_is_not_treated_as_inbound(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[dict[str, Any]] = []

    async def handler(message: dict[str, Any]) -> str:
        called.append(message)
        return "should not happen"

    monkeypatch.setattr(main, "handle_incoming_message", handler)
    monkeypatch.setattr(main, "is_bot_work_time", lambda: True)

    client = TestClient(main.app)
    response = client.post(
        "/webhook/wazzup",
        json=_payload(messageId="wh-echo-1", direction="outbound", isEcho=True, fromMe=True),
    )

    assert response.status_code == 200
    assert called == [], "an outbound echo must never enter the inbound pipeline"
    assert response.json().get("no_reply_reason") == "echo_or_outgoing_message"


def test_duplicate_webhook_delivery_is_processed_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """Wazzup retries the same messageId; it must not book twice."""
    processed: list[str] = []

    async def sender(**kwargs: Any) -> dict[str, Any]:
        return {"ok": True, "status_code": 200}

    async def slots(date: str, doctor_login: str | None = None) -> dict[str, Any]:
        return {"ok": True, "availability": []}

    async def lookup(phone: str) -> dict[str, Any]:
        return {"ok": True, "found": False, "isNew": True, "appointments": [], "appointment": None}

    booked: list[dict[str, Any]] = []

    async def book(**kwargs: Any) -> dict[str, Any]:
        booked.append(kwargs)
        return {"ok": True, "id": 1}

    monkeypatch.setattr(main, "send_wazzup_message", sender)
    monkeypatch.setattr(main, "is_bot_work_time", lambda: True)
    monkeypatch.setattr(crm, "check_slots", slots)
    monkeypatch.setattr(crm, "book_appointment", book)
    monkeypatch.setattr(crm, "lookup_active_appointments_by_phone", lookup)

    original = main.handle_incoming_message

    async def counting_handler(message: dict[str, Any]) -> str:
        processed.append(str(message.get("message_id") or ""))
        return await original(message)

    monkeypatch.setattr(main, "handle_incoming_message", counting_handler)

    client = TestClient(main.app)
    payload = _payload(chatId="77010000099", phone="77010000099", messageId="retry-1")
    first = client.post("/webhook/wazzup", json=payload)
    second = client.post("/webhook/wazzup", json=payload)

    assert first.status_code == 200 and second.status_code == 200
    assert second.json().get("no_reply_reason") == "duplicate_message"
    assert booked == [], "a retried webhook must never create a CRM booking"


# ---------------------------------------------------------------------------
# CRM contract
# ---------------------------------------------------------------------------


class _RecordingTransport:
    """Captures the exact HTTP request crm.py builds, without a network call."""

    def __init__(self, status_code: int = 200, json_body: dict[str, Any] | None = None):
        self.status_code = status_code
        self.json_body = json_body if json_body is not None else {"ok": True}
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        await request.aread()
        self.requests.append(request)
        return httpx.Response(self.status_code, json=self.json_body, request=request)


def _install_transport(monkeypatch: pytest.MonkeyPatch, transport: _RecordingTransport) -> _RecordingTransport:
    client = httpx.AsyncClient(transport=transport)  # type: ignore[arg-type]
    monkeypatch.setattr(crm, "_client", lambda: client)
    return transport


def test_check_slots_uses_existing_endpoint_and_secret_header(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = _install_transport(
        monkeypatch,
        _RecordingTransport(
            json_body={
                "ok": True,
                "availability": [
                    {
                        "doctorLogin": "zhuma_md",
                        "doctorName": "Жумабек Мади Мухтарович",
                        "date": "2026-09-01",
                        "availableSlots": ["09:20", "14:00"],
                    }
                ],
            }
        ),
    )
    crm.clear_slots_cache()

    data = asyncio.run(crm.check_slots("2026-09-01", doctor_login="zhuma_md"))

    request = transport.requests[-1]
    assert request.method == "GET"
    assert request.url.path == "/api/bot/check-slots"
    assert dict(request.url.params) == {"date": "2026-09-01", "doctor": "zhuma_md"}
    # The header name is the contract; the value is whatever CRM_BOT_SECRET holds
    # in this process (another test module may have set it first).
    assert request.headers["x-bot-secret"] == get_settings().crm_bot_secret
    assert {slot["timeStart"] for slot in data["slots"]} == {"09:20", "14:00"}


def test_book_uses_existing_endpoint_and_required_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = _install_transport(monkeypatch, _RecordingTransport(json_body={"ok": True, "id": 42}))

    asyncio.run(
        crm.book_appointment(
            patient_name="Асель",
            phone="87011234567",
            doctor_login="zhuma_md",
            date="2026-09-01",
            time_start="14:00",
            doctor_name="Жумабек Мади Мухтарович",
        )
    )

    request = transport.requests[-1]
    assert request.method == "POST"
    assert request.url.path == "/api/bot/book"
    assert request.headers["x-bot-secret"] == get_settings().crm_bot_secret
    body = json.loads(request.content.decode("utf-8"))
    assert set(body) >= {"patientName", "phone", "doctorLogin", "date", "timeStart"}
    assert "service" not in body, "the CRM book contract has no service field"
    assert body["phone"] == "77011234567", "phone must be normalised to 7XXXXXXXXXX"


def test_crm_error_status_is_never_read_as_success(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_transport(monkeypatch, _RecordingTransport(status_code=500, json_body={"error": "boom"}))

    with pytest.raises(crm.CRMResponseError):
        asyncio.run(
            crm.book_appointment(
                patient_name="Асель",
                phone="77011234567",
                doctor_login="zhuma_md",
                date="2026-09-01",
                time_start="14:00",
            )
        )


# ---------------------------------------------------------------------------
# The agent must reuse the existing client, not a parallel implementation
# ---------------------------------------------------------------------------


def _agent_ast() -> ast.Module:
    """Parse agent.py so assertions look at code, not prose.

    Searching ``inspect.getsource`` also matches docstrings and the system
    prompt written for the model, so a time example in a tool description or
    the word "httpx" in a comment would fail these tests without any contract
    change. Only executable nodes are inspected here.
    """
    import ast

    return ast.parse(inspect.getsource(agent))


def _agent_call_targets() -> set[str]:
    targets: set[str] = set()
    for node in ast.walk(_agent_ast()):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            value = node.func.value
            if isinstance(value, ast.Name):
                targets.add(f"{value.id}.{node.func.attr}")
    return targets


def _agent_code_string_literals() -> set[str]:
    """Every string literal that is code, excluding docstrings."""
    tree = _agent_ast()
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                if isinstance(body[0].value.value, str):
                    docstrings.add(id(body[0].value))
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings
    }


def test_agent_tools_call_the_existing_crm_client_only() -> None:
    targets = _agent_call_targets()
    assert "crm.check_slots" in targets
    assert "crm.book_appointment" in targets
    assert "crm.get_doctors" in targets
    # No parallel HTTP layer: the agent must go through crm.py. Checking call
    # targets alone is not enough — `from httpx import AsyncClient` would make
    # the target a bare name — so the imports are checked too.
    assert not any(t.startswith("httpx.") for t in targets), f"raw HTTP in agent: {sorted(targets)}"
    imported: set[str] = set()
    for node in ast.walk(_agent_ast()):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert "httpx" not in imported, "the agent must not import an HTTP client directly"
    assert "requests" not in imported and "aiohttp" not in imported


def test_agent_has_no_hardcoded_slots_or_doctors() -> None:
    """Availability and doctors must originate from CRM responses only."""
    import re as _re

    literals = _agent_code_string_literals()
    # The system prompt lives in a module-level constant, so drop the long
    # model-facing texts and check the short operational literals.
    operational = {value for value in literals if len(value) <= 80}

    time_literals = {v for v in operational if _re.fullmatch(r"\d{1,2}:\d{2}", v)}
    assert time_literals <= {"14:00", "0:00"}, f"unexpected hardcoded time literals: {time_literals}"

    base_urls = {v for v in operational if "vercel.app" in v or "railway.app" in v}
    assert not base_urls, f"agent must not carry its own base URL: {base_urls}"

    assert not any("zhuma_md" in v for v in literals), "no doctor login may be hardcoded in the agent"


def test_agent_booking_requires_a_crm_offered_slot() -> None:
    """The registry that makes fabricated slots impossible is session-scoped."""
    session: dict[str, Any] = {}
    agent._remember_offered_slots(
        session,
        [{"doctor_login": "zhuma_md", "doctor_name": "Ж", "date": "2026-09-01", "time_start": "14:00"}],
    )

    assert agent._offered_slot(session, "2026-09-01", "14:00", "zhuma_md") is not None
    assert agent._offered_slot(session, "2026-09-01", "21:00", "zhuma_md") is None
    assert agent._offered_slot(session, "2026-09-02", "14:00", "zhuma_md") is None
    assert agent._offered_slot(session, "2026-09-01", "14:00", "someone_else") is None


def test_crm_success_detection_rejects_error_shapes() -> None:
    assert agent._crm_booking_succeeded({"ok": True, "id": 1}) is True
    assert agent._crm_booking_succeeded({"success": True}) is True
    assert agent._crm_booking_succeeded({"ok": False}) is False
    assert agent._crm_booking_succeeded({"error": "slot_taken"}) is False
    assert agent._crm_booking_succeeded({"status": "failed"}) is False
    assert agent._crm_booking_succeeded(None) is False
    assert agent._crm_booking_succeeded("ok") is False
