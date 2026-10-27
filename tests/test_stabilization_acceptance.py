"""Additional production-path acceptance regressions, with no external calls.

These tests use real SQLite, process restarts and the shipped webhook processor.
CRM/model/channel responses remain isolated doubles; they do not prove that an
external provider or a production medical record works.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any

from fastapi import Request
import pytest

import agent
import crm
import dialog
import main
import production_inbox
import state
from config import get_settings


PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def isolated_storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    target = tmp_path / "acceptance.sqlite3"
    monkeypatch.setenv("SQLITE_PATH", str(target))
    get_settings.cache_clear()
    state.init_db()
    yield target
    get_settings.cache_clear()


def _request() -> Request:
    return Request({"type": "http", "method": "POST", "scheme": "https",
                    "server": ("qa.invalid", 443), "path": "/webhook/wazzup",
                    "query_string": b"", "headers": []})


def test_real_process_restart_preserves_context_and_uncertain_claims(
    isolated_storage: Path,
) -> None:
    """A fresh interpreter must refuse duplicate work after a crash/restart."""
    env = dict(os.environ, SQLITE_PATH=str(isolated_storage), OPENAI_API_KEY="")
    write_script = """
import state
state.init_db()
state.save_session('qa-restart', {'step': 'name', 'language': 'kk',
    'last_slots': [{'doctor_login': 'qa-doctor', 'date': '2030-01-02', 'time': '10:00'}]})
state.add_message('qa-restart', 'user', 'QA_SYNTHETIC_CONTEXT')
assert state.claim_message('qa-inbound-restart', 'qa-restart')
assert state.claim_booking('qa-booking-restart', 'qa-restart')
state.mark_booking_claim('qa-booking-restart', 'uncertain')
"""
    read_script = """
import json, state
state.init_db()
session = state.get_session('qa-restart')
assert session['step'] == 'name'
assert session['language'] == 'kk'
assert session['last_slots'][0]['doctor_login'] == 'qa-doctor'
assert state.get_history('qa-restart')[-1]['content'] == 'QA_SYNTHETIC_CONTEXT'
assert not state.claim_message('qa-inbound-restart', 'qa-restart')
assert state.booking_claim_status('qa-booking-restart') == 'uncertain'
assert not state.claim_booking('qa-booking-restart', 'qa-restart')
print(json.dumps({'context_restored': True, 'duplicate_blocked': True,
                  'uncertain_booking_retained': True}))
"""
    for script in (write_script, read_script):
        result = subprocess.run([sys.executable, "-c", script], env=env,
                                cwd=PROJECT_ROOT, text=True, capture_output=True,
                                timeout=15, check=False)
        assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "context_restored": True, "duplicate_blocked": True,
        "uncertain_booking_retained": True,
    }


@pytest.mark.parametrize("kind,mime", [("image", "image/jpeg"), ("document", "application/pdf")])
def test_uncaptioned_attachment_never_invents_text_or_calls_ai(
    isolated_storage: Path, monkeypatch: pytest.MonkeyPatch, kind: str, mime: str,
) -> None:
    """Unsupported attachment interpretation fails silently before AI/CRM."""
    async def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("An unreadable attachment must not call AI, CRM or sender")

    monkeypatch.setattr(main, "handle_incoming_message", forbidden)
    monkeypatch.setattr(main, "send_wazzup_message", forbidden)
    monkeypatch.setattr(crm, "lookup_active_appointments_by_phone", forbidden)
    payload = {"messageId": f"qa-unreadable-{kind}", "chatId": f"qa-media-{kind}",
               "chatType": "whatsapp", "type": kind, "mimeType": mime,
               "fromMe": False, "text": "", "fileUrl": "https://qa.invalid/synthetic"}
    result = asyncio.run(main._process_wazzup_message(
        _request(), payload, payload, {"ok": True, "content_type": "application/json"},
    ))
    assert result["answer"] == ""
    assert result["should_send_wazzup"] is False
    assert result["no_reply_reason"] == "empty_text"


def test_two_clients_run_concurrently_without_sharing_session_or_answer(
    isolated_storage: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per-chat serialization must not become a global lock or leak state."""
    settings = get_settings()
    settings.wazzup_channel_id = ""
    settings.wazzup_allowed_channel_ids = ""
    settings.bot_auto_reply_enabled = True
    active = 0
    maximum = 0
    sent: dict[str, str] = {}

    async def handler(message: dict[str, Any]) -> dict[str, Any]:
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        chat = message["chat_id"]
        session = state.get_session(chat)
        session["qa_turn_token"] = message["text"]
        state.save_session(chat, session)
        await asyncio.sleep(0.03)
        assert state.get_session(chat)["qa_turn_token"] == message["text"]
        active -= 1
        return {"answer": f"QA_REPLY_{message['text']}", "should_send_wazzup": True}

    async def sender(**kwargs: Any) -> dict[str, Any]:
        sent[kwargs["chat_id"]] = kwargs["text"]
        return {"ok": True, "status_code": 201}

    monkeypatch.setattr(main, "handle_incoming_message", handler)
    monkeypatch.setattr(main, "send_wazzup_message", sender)

    def payload(token: str) -> dict[str, Any]:
        return {"messageId": f"qa-concurrent-{token}", "chatId": f"qa-chat-{token}",
                "chatType": "whatsapp", "type": "text", "fromMe": False,
                "text": token, "dateTime": "2030-01-02T21:00:00+05:00"}

    async def run() -> list[dict[str, Any]]:
        first, second = payload("A"), payload("B")
        return list(await asyncio.gather(*[
            main._process_wazzup_message(_request(), item, item, {"ok": True})
            for item in (first, second)
        ]))

    results = asyncio.run(run())
    assert maximum == 2, "Independent clients were serialized by one global lock"
    assert sent == {"qa-chat-A": "QA_REPLY_A", "qa-chat-B": "QA_REPLY_B"}
    assert all(result["should_send_wazzup"] for result in results)


def test_completed_booking_ends_ai_dialog_before_crm_or_model(
    isolated_storage: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A thank-you after booking cannot reopen acquisition or create a duplicate."""
    chat_id = "qa-completed-booking"
    state.save_session(chat_id, {"booking_confirmed": True, "phone": "77000000001"})

    async def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Completed booking must remain outside the acquisition dialog")

    monkeypatch.setattr(crm, "lookup_active_appointments_by_phone", forbidden)
    monkeypatch.setattr(crm, "book_appointment", forbidden)
    monkeypatch.setattr(agent, "run_agent_turn", forbidden)
    assert asyncio.run(dialog.handle_message(chat_id, "77000000001", "Спасибо, до свидания")) == ""
    session = state.get_session(chat_id)
    assert session["booking_confirmed"] is True
    assert session["ai_muted"] is True
    assert session["manual_takeover"] is True


def test_idle_patient_never_generates_an_unsolicited_worker_turn(
    isolated_storage: Path,
) -> None:
    """The running queue worker must not contact a silent patient by itself."""
    state.save_session("qa-waiting-patient", {"step": "date", "language": "ru"})
    invoked = []

    async def processor(job: dict[str, Any]) -> dict[str, Any]:
        invoked.append(job)
        raise AssertionError("No incoming message exists for the silent patient")

    async def observe() -> None:
        worker = production_inbox.InboxWorker(processor, poll_seconds=0.05)
        worker.start()
        await asyncio.sleep(0.16)  # observe several actual idle worker polls
        assert worker.status()["running"] is True
        assert worker.status()["active_jobs"] == 0
        await worker.stop(timeout=1)

    asyncio.run(observe())
    assert invoked == []
    assert state.get_session("qa-waiting-patient")["step"] == "date"
    assert state.get_history("qa-waiting-patient") == []
