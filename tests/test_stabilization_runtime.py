"""Runtime regressions; all fixtures are synthetic and use a temporary DB."""
from __future__ import annotations

import concurrent.futures
import json
import os
from pathlib import Path

import pytest

import state
from config import get_settings
from guards import should_auto_reply


@pytest.fixture(autouse=True)
def isolated_storage(monkeypatch, tmp_path):
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "runtime.sqlite3"))
    monkeypatch.setenv("CRM_BOT_SECRET", "synthetic-test-redaction-secret")
    monkeypatch.setenv("WORK_HOURS_GUARD_ENABLED", "false")
    get_settings.cache_clear()
    state.init_db()
    yield
    get_settings.cache_clear()


def test_outgoing_claim_is_durable_and_single_winner():
    def claim(_):
        return state.claim_outgoing_message("reply:synthetic-inbound", "synthetic-chat")

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(claim, range(16))) == 1
    state.mark_outgoing_message("reply:synthetic-inbound", "uncertain")
    state.init_db()  # Simulates startup schema initialization after restart.
    assert state.outgoing_message_status("reply:synthetic-inbound") == "uncertain"
    assert not claim(None), "timeout/restart must not send the reply again"


def test_outgoing_claim_rejects_missing_identity_and_invalid_status():
    assert state.claim_outgoing_message("", "synthetic-chat") is False
    state.claim_outgoing_message("reply:m1", "synthetic-chat")
    with pytest.raises(ValueError, match="invalid outgoing"):
        state.mark_outgoing_message("reply:m1", "invented_success")
    assert state.outgoing_message_status("reply:m1") == "sending"


def test_new_event_logs_have_no_patient_content_or_secret(caplog):
    phone = "77000000001"
    payload = {
        "phone": phone, "chat_id": phone,
        "patient_name": "Synthetic Private Name",
        "text_preview": "Synthetic medical complaint",
        "answer_preview": "Synthetic medical response",
        "raw_preview": '{"private": "raw body"}',
        "error": "exception containing patient content",
        "nested": {"authorization": "Bearer synthetic-secret", "complaint": "medical private"},
        "crm_lookup_ok": True, "source": "wazzup",
    }
    state.log_event(phone, "crm_lookup_result", payload)
    with state._connect() as conn:
        row = conn.execute("SELECT chat_id,payload_json FROM events ORDER BY id DESC LIMIT 1").fetchone()
    dumped = json.dumps(dict(row)) + json.dumps(state.recent_memory_events(1)) + caplog.text
    for private in [phone, "Synthetic Private Name", "Synthetic medical", "raw body", "patient content", "synthetic-secret", "medical private"]:
        assert private not in dumped
    assert json.loads(row["payload_json"])["crm_lookup_ok"] is True
    events = state.recent_events_for_phone(phone)
    assert events[-1]["event"] == "crm_lookup_result"
    assert events[-1]["chat_id"].startswith("ref_")


def test_corrupt_session_fails_closed_instead_of_becoming_new():
    with state._connect() as conn:
        conn.execute("INSERT INTO sessions(chat_id,data_json,updated_at) VALUES (?,?,?)", ("broken-chat", "{bad", state.now_iso()))
    session = state.get_session("broken-chat")
    assert session["session_state_error"] is True
    assert session["ai_muted"] is True
    assert not should_auto_reply("Hello", session, "wazzup").allowed


def test_session_defaults_are_not_shared_between_clients():
    first = state.get_session("fresh-chat-a")
    first["last_slots"].append({"time": "12:00"})
    assert state.get_session("fresh-chat-b")["last_slots"] == []


def test_booked_session_is_silent_even_when_old_support_window_is_active():
    session = {"created_by_ai": True, "booking_confirmed": True, "post_booking_support_until": "2999-01-01T00:00:00+00:00"}
    decision = should_auto_reply("Follow up", session, "wazzup")
    assert not decision.allowed
    assert decision.no_reply_reason == "booked_session_ai_disabled"


def test_storage_is_private_and_uses_wal():
    path = Path(get_settings().sqlite_path)
    assert os.stat(path).st_mode & 0o777 == 0o600
    with state._connect() as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 15000


def test_exception_tokens_and_urls_are_not_exposed_under_unknown_fields():
    value = state._safe_log_value("diagnostic", "Bearer private-token https://example.test?token=private sk-exampleprivate user@example.test +7 (700) 000-00-01")
    for private in ["private-token", "example.test", "sk-exampleprivate", "user@", "700"]:
        assert private not in value


@pytest.mark.parametrize("body", [b"", b"not-json", b"[]", b"{}", b'{"error":"failed"}', b'{"ok":true,"error":"failed"}', b'{"success":false,"messageId":"synthetic"}', b'{"errors":["failed"]}', b'{"status":"unknown"}'])
def test_wazzup_2xx_without_positive_acknowledgement_is_not_success(monkeypatch, body):
    import asyncio
    import httpx
    import wazzup

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=body))) as client:
            monkeypatch.setattr(wazzup, "_client", lambda: client)
            with pytest.raises(ValueError):
                await wazzup.send_text("synthetic-chat", "synthetic reply")

    asyncio.run(scenario())


def test_wazzup_real_message_identity_acknowledges_delivery(monkeypatch):
    import asyncio
    import httpx
    import wazzup

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(201, json={"messageId": "synthetic-provider-id"}))) as client:
            monkeypatch.setattr(wazzup, "_client", lambda: client)
            result = await wazzup.send_text("synthetic-chat", "synthetic reply")
            assert result["messageId"] == "synthetic-provider-id"
            assert result["status_code"] == 201

    asyncio.run(scenario())


@pytest.mark.parametrize("url", ["http://api.wazzup24.com/media/x", "https://localhost/media/x", "https://127.0.0.1/media/x", "https://api.wazzup24.com.evil.test/media/x", "https://evil.test/media/x", "https://user@api.wazzup24.com/media/x", "https://api.wazzup24.com:444/media/x"])
def test_untrusted_media_rejected_before_network_or_bearer_disclosure(monkeypatch, url):
    import asyncio
    import wazzup

    monkeypatch.setenv("WAZZUP_API_URL", "https://api.wazzup24.com/v3")
    get_settings.cache_clear()
    monkeypatch.setattr(wazzup, "_client", lambda: pytest.fail("untrusted media must not reach HTTP client"))
    with pytest.raises(ValueError, match="Untrusted media URL"):
        asyncio.run(wazzup.download_media(media_url=url))


def test_media_bearer_never_forwarded_to_cdn_and_redirect_not_followed(monkeypatch):
    import asyncio
    import httpx
    import wazzup

    monkeypatch.setenv("WAZZUP_API_URL", "https://api.wazzup24.com/v3")
    monkeypatch.setenv("WAZZUP_API_KEY", "synthetic-provider-token")
    get_settings.cache_clear()
    requests = []

    def transport(request):
        requests.append(request)
        if request.url.path == "/redirect":
            return httpx.Response(302, headers={"location": "https://evil.test/private"})
        return httpx.Response(200, content=b"synthetic audio", headers={"content-type": "audio/ogg"})

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport), follow_redirects=True) as client:
            monkeypatch.setattr(wazzup, "_client", lambda: client)
            await wazzup.download_media(media_url="https://api.wazzup24.com/media/x")
            await wazzup.download_media(media_url="https://cdn.wazzup24.com/media/x")
            with pytest.raises(httpx.HTTPStatusError):
                await wazzup.download_media(media_url="https://api.wazzup24.com/redirect")

    asyncio.run(scenario())
    assert requests[0].headers["authorization"] == "Bearer synthetic-provider-token"
    assert "authorization" not in requests[1].headers
    assert len(requests) == 3
    assert all(request.url.host != "evil.test" for request in requests)


def test_chunked_media_size_limit_enforced_without_content_length(monkeypatch):
    import asyncio
    import httpx
    import wazzup

    class Chunked(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"12345"
            yield b"67890"

    monkeypatch.setenv("WAZZUP_API_URL", "https://api.wazzup24.com/v3")
    get_settings.cache_clear()
    monkeypatch.setattr(wazzup, "MAX_MEDIA_BYTES", 8)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=Chunked()))) as client:
            monkeypatch.setattr(wazzup, "_client", lambda: client)
            with pytest.raises(ValueError, match="size limit"):
                await wazzup.download_media(media_url="https://api.wazzup24.com/media/x")

    asyncio.run(scenario())


def test_oversize_voice_upload_or_bytes_never_calls_openai(monkeypatch):
    import asyncio
    import io
    import voice
    from fastapi import UploadFile

    monkeypatch.setattr(voice, "MAX_MEDIA_BYTES", 8)
    monkeypatch.setattr(voice, "AsyncOpenAI", lambda **kwargs: pytest.fail("oversize audio must not reach OpenAI"))
    with pytest.raises(ValueError, match="size limit"):
        asyncio.run(voice.transcribe_bytes(b"123456789"))
    upload = UploadFile(filename="synthetic.ogg", file=io.BytesIO(b"123456789"))
    with pytest.raises(ValueError, match="size limit"):
        asyncio.run(voice.transcribe_upload(upload))


def test_send_never_follows_redirect_with_bearer(monkeypatch):
    import asyncio
    import httpx
    import wazzup

    monkeypatch.setenv("WAZZUP_API_URL", "https://api.wazzup24.com/v3")
    get_settings.cache_clear()
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(302, headers={"location": "https://evil.test/steal"})

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
            monkeypatch.setattr(wazzup, "_client", lambda: client)
            with pytest.raises(httpx.HTTPStatusError):
                await wazzup.send_text("synthetic-chat", "synthetic reply")

    asyncio.run(scenario())
    assert len(requests) == 1
    assert requests[0].url.host == "api.wazzup24.com"


def test_voice_timeout_closes_client_and_removes_temporary_audio(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    import voice

    observed = {}

    async def create(**kwargs):
        observed["audio_path"] = kwargs["file"].name
        assert Path(observed["audio_path"]).exists()
        raise TimeoutError("synthetic timeout")

    async def close():
        observed["closed"] = True

    def client(**kwargs):
        observed["client_config"] = kwargs
        return SimpleNamespace(audio=SimpleNamespace(transcriptions=SimpleNamespace(create=create)), close=close)

    monkeypatch.setattr(voice, "AsyncOpenAI", client)
    with pytest.raises(TimeoutError):
        asyncio.run(voice.transcribe_bytes(b"synthetic audio"))
    assert observed["client_config"]["timeout"] == 30.0
    assert observed["client_config"]["max_retries"] == 0
    assert observed["closed"] is True
    assert not Path(observed["audio_path"]).exists()
