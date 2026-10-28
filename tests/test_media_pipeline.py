"""Real ingress/SQLite with isolated CRM, ASR and delivery responses.

No external calls or medical records are used. These tests prove pipeline
ordering and recovery, not recognition quality of an external ASR provider.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

import crm
import main
import state
from config import get_settings


PHONE = "77000000001"


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "media-pipeline.sqlite3"))
    monkeypatch.setenv("WAZZUP_CHANNEL_ID", "")
    monkeypatch.setenv("WAZZUP_ALLOWED_CHANNEL_IDS", "")
    monkeypatch.setenv("WORK_HOURS_GUARD_ENABLED", "false")
    get_settings.cache_clear()
    state.init_db()
    monkeypatch.setattr(main, "is_bot_work_time", lambda: True)
    yield
    get_settings.cache_clear()


def raw_media(kind="audio", **overrides):
    extension = {"image": "jpg", "document": "pdf", "video": "mp4"}.get(kind, "ogg")
    return {"chatId": PHONE, "messageId": "synthetic-media-1", "type": kind,
            "chatType": "whatsapp", "fromMe": False,
            "contentUri": "https://api.wazzup24.com/media/synthetic." + extension,
            **overrides}


async def run_payload(payload):
    request = SimpleNamespace(url=SimpleNamespace(path="/webhook/wazzup"), method="POST")
    return await main._process_wazzup_message(request, payload, payload, {"ok": True})


def new_lookup():
    return {"ok": True, "raw": {"found": False, "isNew": True}, "appointments": []}


@pytest.mark.parametrize("lookup,error,expected", [
    ({"ok": True, "raw": {"found": True, "isNew": False,
                             "patient": {"id": "synthetic"}}, "appointments": []}, None,
     "old_lead_or_active_booking"),
    ({"ok": True, "raw": {"found": True, "isNew": False,
                             "hasActiveAppointment": True}, "appointments": []}, None,
     "old_lead_or_active_booking"),
    ({"ok": False, "error": "synthetic_unavailable"}, None, "crm_lookup_failed"),
    ({"ok": True, "raw": {"found": False}}, None, "crm_lookup_failed"),
    (None, TimeoutError("synthetic CRM timeout"), "crm_lookup_failed"),
])
@pytest.mark.parametrize("kind", ["audio", "image", "document"])
def test_ineligible_or_unknown_patient_never_downloads_recognizes_or_replies(
    monkeypatch, lookup, error, expected, kind,
):
    calls = []

    async def crm_lookup(phone):
        calls.append(("lookup", phone))
        if error:
            raise error
        return lookup

    async def forbidden(*args, **kwargs):
        raise AssertionError("Non-new patient reached attachment/AI/send operation")

    monkeypatch.setattr(crm, "lookup_active_appointments_by_phone", crm_lookup)
    monkeypatch.setattr(main, "_transcribe_voice_message", forbidden)
    monkeypatch.setattr(main, "handle_message", forbidden)
    monkeypatch.setattr(main, "send_wazzup_message", forbidden)
    result = asyncio.run(run_payload(raw_media(kind)))
    assert calls == [("lookup", PHONE)]
    assert result["answer"] == ""
    assert result["should_send_wazzup"] is False
    assert result["no_reply_reason"] == expected


@pytest.mark.parametrize("transcript,answer,language", [
    ("Здравствуйте, хочу записаться к врачу", "К какому врачу Вы хотите записаться?", "ru"),
    ("Сәлеметсіз бе, дәрігерге жазылғым келеді", "Қай дәрігерге жазылғыңыз келеді?", "kk"),
])
def test_new_voice_admits_before_single_asr_and_preserves_exact_transcript(
    monkeypatch, transcript, answer, language,
):
    calls = []

    async def lookup(phone):
        calls.append("crm")
        return new_lookup()

    async def transcribe(message):
        calls.append("asr")
        assert message["media_url"].endswith("synthetic.ogg")
        return transcript

    async def dialog_handler(**kwargs):
        calls.append("dialog")
        assert kwargs["user_text"] == transcript, "No Russian wrapper may overwrite KK ASR text"
        session = state.get_session(PHONE)
        session.update(language=language, answer_source="python_template", skip_humanize=True)
        state.save_session(PHONE, session)
        return answer

    async def sender(**kwargs):
        calls.append("send")
        assert kwargs["text"] == answer
        return {"ok": True, "status_code": 201}

    monkeypatch.setattr(crm, "lookup_active_appointments_by_phone", lookup)
    monkeypatch.setattr(main, "_transcribe_voice_message", transcribe)
    monkeypatch.setattr(main, "handle_message", dialog_handler)
    monkeypatch.setattr(main, "send_wazzup_message", sender)

    async def scenario():
        first = await run_payload(raw_media())
        second = await run_payload(raw_media())
        return first, second

    first, second = asyncio.run(scenario())
    assert calls == ["crm", "asr", "dialog", "send"]
    assert first["answer"] == answer and first["should_send_wazzup"]
    assert second["should_send_wazzup"] is False
    assert second["no_reply_reason"] == "duplicate_message"


@pytest.mark.parametrize("language,keyword", [("ru", "текстом"), ("kk", "мәтінмен")])
def test_asr_failure_requests_text_without_second_recognition_or_fallback_ai(monkeypatch, language, keyword):
    state.save_session(PHONE, {"language": language})
    calls = []

    async def lookup(phone):
        calls.append("crm")
        return new_lookup()

    async def transcribe(message):
        calls.append("asr")
        raise TimeoutError("Synthetic ASR timeout")

    async def forbidden(**kwargs):
        raise AssertionError("No guessed transcript or second model on ASR failure")

    async def sender(**kwargs):
        calls.append("send")
        assert keyword in kwargs["text"]
        return {"ok": True}

    monkeypatch.setattr(crm, "lookup_active_appointments_by_phone", lookup)
    monkeypatch.setattr(main, "_transcribe_voice_message", transcribe)
    monkeypatch.setattr(main, "handle_message", forbidden)
    monkeypatch.setattr(main, "send_wazzup_message", sender)
    result = asyncio.run(run_payload(raw_media()))
    assert calls == ["crm", "asr", "send"]
    assert keyword in result["answer"]
    assert result["should_send_wazzup"] is True


@pytest.mark.parametrize("kind", ["image", "document", "video"])
def test_uncaptioned_media_from_new_client_gets_one_truthful_request(monkeypatch, kind):
    calls = []

    async def lookup(phone):
        calls.append("crm")
        return new_lookup()

    async def forbidden(*args, **kwargs):
        raise AssertionError("Attachment content was not read and must not reach model/ASR")

    async def sender(**kwargs):
        calls.append("send")
        assert "Не могу" in kwargs["text"] and "текстом" in kwargs["text"]
        return {"ok": True}

    monkeypatch.setattr(crm, "lookup_active_appointments_by_phone", lookup)
    monkeypatch.setattr(main, "_transcribe_voice_message", forbidden)
    monkeypatch.setattr(main, "handle_message", forbidden)
    monkeypatch.setattr(main, "send_wazzup_message", sender)

    async def scenario():
        first = await run_payload(raw_media(kind))
        second = await run_payload(raw_media(kind))
        return first, second

    first, second = asyncio.run(scenario())
    assert calls == ["crm", "send"]
    assert first["should_send_wazzup"] and not second["should_send_wazzup"]
    assert state.get_session(PHONE)["attachment_not_read"] is True


def test_missing_voice_url_gets_text_request_without_downloading_or_asr(monkeypatch):
    calls = []

    async def lookup(phone):
        calls.append("crm")
        return new_lookup()

    async def forbidden(*args, **kwargs):
        raise AssertionError("Missing media cannot be downloaded or recognized")

    async def sender(**kwargs):
        calls.append("send")
        return {"ok": True}

    monkeypatch.setattr(crm, "lookup_active_appointments_by_phone", lookup)
    monkeypatch.setattr(main, "_transcribe_voice_message", forbidden)
    monkeypatch.setattr(main, "handle_message", forbidden)
    monkeypatch.setattr(main, "send_wazzup_message", sender)
    result = asyncio.run(run_payload(raw_media(contentUri="")))
    assert calls == ["crm", "send"]
    assert "текстом" in result["answer"]


def test_caption_is_passed_as_text_without_inventing_attachment_contents(monkeypatch):
    calls = []
    caption = "Сколько стоит первичная консультация?"

    async def lookup(phone):
        calls.append("crm")
        return new_lookup()

    async def forbidden(*args, **kwargs):
        raise AssertionError("Image caption must not trigger audio recognition")

    async def dialog_handler(**kwargs):
        calls.append("dialog")
        assert kwargs["user_text"] == caption
        session = state.get_session(PHONE)
        session.update(answer_source="python_template", skip_humanize=True)
        state.save_session(PHONE, session)
        return "Уточню стоимость по актуальному списку услуг."

    monkeypatch.setattr(crm, "lookup_active_appointments_by_phone", lookup)
    monkeypatch.setattr(main, "_transcribe_voice_message", forbidden)
    monkeypatch.setattr(main, "handle_message", dialog_handler)
    normalized = main._normalize_wazzup_message({}, raw_media("image", text=caption))
    result = asyncio.run(main.handle_incoming_message(normalized))
    assert result
    assert calls == ["crm", "dialog"]


@pytest.mark.parametrize("kind,language,claim,keyword", [
    ("image", "ru", "На снимке вижу изменения.", "текстом"),
    ("document", "ru", "Я просмотрела документ, у Вас изменения.", "текстом"),
    ("image", "kk", "Суреттен көрінеді, өзгерістер бар.", "мәтінмен"),
    ("document", "kk", "Құжатты оқып шықтым, өзгерістер бар.", "мәтінмен"),
])
def test_captioned_attachment_blocks_model_claim_that_unread_contents_were_seen(
    monkeypatch, kind, language, claim, keyword,
):
    state.save_session(PHONE, {"language": language})

    async def lookup(phone):
        return new_lookup()

    async def model(**kwargs):
        session = state.get_session(PHONE)
        assert session["attachment_not_read"] is True
        assert session["attachment_kind"] == kind
        session.update(answer_source="gpt_agent", skip_humanize=True)
        state.save_session(PHONE, session)
        return claim

    monkeypatch.setattr(crm, "lookup_active_appointments_by_phone", lookup)
    monkeypatch.setattr(main, "handle_message", model)
    payload = raw_media(kind, text="Это вложение")
    result = asyncio.run(main.handle_incoming_message(main._normalize_wazzup_message({}, payload)))
    assert result != claim
    assert keyword in result
    assert state.get_session(PHONE)["attachment_not_read"] is True


def test_new_text_turn_clears_previous_unread_attachment_flag_before_model(monkeypatch):
    state.save_session(PHONE, {"attachment_not_read": True, "attachment_kind": "image"})
    expected = "Какая консультация Вас интересует?"

    async def model(**kwargs):
        session = state.get_session(PHONE)
        assert session["attachment_not_read"] is False
        assert session["attachment_kind"] == ""
        session.update(answer_source="gpt_agent", skip_humanize=True)
        state.save_session(PHONE, session)
        return expected

    monkeypatch.setattr(main, "handle_message", model)
    payload = raw_media("text", contentUri="", text="Хочу консультацию")
    result = asyncio.run(main.handle_incoming_message(main._normalize_wazzup_message({}, payload)))
    assert result == expected
    assert state.get_session(PHONE)["attachment_not_read"] is False


@pytest.mark.parametrize("new_client", [True, False])
def test_legacy_text_kind_with_audio_url_still_checks_crm_before_asr(monkeypatch, new_client):
    calls = []

    async def lookup(phone):
        calls.append("crm")
        if new_client:
            return new_lookup()
        return {"ok": True, "raw": {"found": True, "isNew": False,
                                    "patient": {"id": "synthetic-existing"}},
                "appointments": []}

    async def transcribe(message):
        calls.append("asr")
        assert new_client, "Existing patient cannot reach ASR via legacy voice URL"
        return "Хочу записаться"

    async def model(**kwargs):
        calls.append("model")
        assert kwargs["user_text"] == "Хочу записаться"
        session = state.get_session(PHONE)
        session.update(answer_source="gpt_agent", skip_humanize=True)
        state.save_session(PHONE, session)
        return "К какому врачу Вы хотите записаться?"

    monkeypatch.setattr(crm, "lookup_active_appointments_by_phone", lookup)
    monkeypatch.setattr(main, "_transcribe_voice_message", transcribe)
    monkeypatch.setattr(main, "handle_message", model)
    legacy = {"chat_id": PHONE, "phone": PHONE, "kind": "text", "text": "",
              "source": "wazzup", "message_id": "legacy-audio", "message_key": "legacy-audio",
              "media_url": "https://api.wazzup24.com/media/synthetic.ogg"}
    result = asyncio.run(main.handle_incoming_message(legacy))
    assert calls == (["crm", "asr", "model"] if new_client else ["crm"])
    assert bool(result) is new_client
