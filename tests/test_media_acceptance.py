"""Media contract tests use synthetic payloads and mocked ASR, never patients."""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from config import get_settings
import voice
import wazzup


@pytest.fixture(autouse=True)
def provider_origin(monkeypatch):
    monkeypatch.setenv("WAZZUP_API_URL", "https://api.wazzup24.com/v3")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.mark.parametrize("payload,expected_kind", [
    ({"type": "audio", "contentUri": "https://store.wazzup24.com/synthetic/?filename=voice.ogg"}, "voice"),
    ({"type": "image", "contentUri": "https://store.wazzup24.com/synthetic/?filename=photo.jpg"}, "image"),
    ({"type": "document", "contentUri": "https://store.wazzup24.com/synthetic/?filename=file.pdf"}, "document"),
    ({"type": "video", "contentUri": "https://store.wazzup24.com/synthetic/?filename=clip.mp4"}, "video"),
    ({"mimeType": "audio/webm; codecs=opus", "fileId": "synthetic-voice"}, "voice"),
    ({"file": {"mimeType": "application/pdf", "url": "https://store.wazzup24.com/synthetic-doc"}}, "document"),
    ({"media": {"filename": "../voice.wav", "url": "https://api.wazzup24.com/v3/media/synthetic"}}, "voice"),
    ({"type": "unsupported"}, "unsupported"),
])
def test_actual_v3_and_compatible_media_shapes_are_classified(payload, expected_kind):
    result = wazzup.normalize_media_metadata(payload)
    assert result["kind"] == expected_kind
    assert ".." not in result["filename"]
    if expected_kind == "voice":
        assert result["media_requires_text"] is False
    else:
        assert result["media_requires_text"] is True


def test_quoted_voice_and_avatar_do_not_turn_current_text_into_audio():
    payload = {
        "type": "text", "text": "Здравствуйте",
        "quotedMessage": {"type": "audio", "contentUri": "https://store.wazzup24.com/old-voice"},
        "contact": {"avatarUri": "https://store.wazzup24.com/avatar.ogg"},
    }
    assert wazzup.normalize_media_metadata(payload)["kind"] == "text"
    assert wazzup.is_audio_message_payload(payload) is False


def test_audio_caption_is_kept_as_text_by_backwards_compatible_probe():
    payload = {"type": "audio", "text": "Менің сұрағым бар", "contentUri": "https://store.wazzup24.com/synthetic"}
    assert wazzup.is_audio_message_payload(payload) is False
    assert wazzup.normalize_media_metadata(payload)["kind"] == "voice"
    assert payload["text"] == "Менің сұрағым бар"


@pytest.mark.parametrize("url", ["https://evil.test/private.ogg", "https://api.wazzup24.com.evil.test/private.ogg", "http://store.wazzup24.com/private.ogg"])
def test_bad_media_location_never_survives_normalization(url):
    result = wazzup.normalize_media_metadata({"type": "audio", "contentUri": url, "fileId": "second-attempt-id"})
    assert result["kind"] == "voice"
    assert result["media_url"] == result["file_id"] == ""
    assert result["media_requires_text"] is True
    assert result["media_error"] == "untrusted_media_url"


def test_missing_audio_location_is_explicitly_unavailable():
    result = wazzup.normalize_media_metadata({"type": "audio"})
    assert result["media_error"] == "missing_media_location"
    assert result["media_requires_text"] is True


@pytest.mark.parametrize("kind", ["voice", "image", "document", "video", "unsupported"])
def test_attachment_fallbacks_are_bilingual_and_make_no_false_inspection_claim(kind):
    from language_guard import detect_language

    ru = wazzup.media_text_request(kind, "ru")
    kk = wazzup.media_text_request(kind, "kk")
    assert "текстом" in ru
    assert "мәтінмен" in kk
    assert "администратор" not in ru.lower()
    assert "диагноз" not in ru.lower()
    assert detect_language(ru) == "ru"
    assert detect_language(kk) == "kk"


@pytest.mark.parametrize("hint,expected", [(None, None), ("auto", None), ("ru", "ru"), ("kk", "kk")])
def test_asr_omits_language_for_auto_and_accepts_explicit_clinic_languages(monkeypatch, hint, expected):
    observed = {}

    async def create(**kwargs):
        observed["request"] = kwargs
        observed["audio_path"] = kwargs["file"].name
        return SimpleNamespace(text="Сәлеметсіз бе, дәрігерге жазылғым келеді")

    async def close():
        observed["closed"] = True

    def client(**kwargs):
        observed["config"] = kwargs
        return SimpleNamespace(audio=SimpleNamespace(transcriptions=SimpleNamespace(create=create)), close=close)

    monkeypatch.setattr(voice, "AsyncOpenAI", client)
    result = asyncio.run(voice.transcribe_bytes(b"synthetic audio", language=hint))
    if expected is None:
        assert "language" not in observed["request"], "auto ASR must not force Russian on a Kazakh speaker"
    else:
        assert observed["request"]["language"] == expected
    assert result.text.startswith("Сәлеметсіз")
    assert observed["config"]["timeout"] == 30.0
    assert observed["config"]["max_retries"] == 0
    assert observed["closed"] is True
    assert not Path(observed["audio_path"]).exists()


def test_actual_content_uri_reaches_asr_once_without_russian_hint(monkeypatch):
    calls = []

    async def download(**kwargs):
        calls.append(("download", kwargs))
        return b"synthetic audio", "audio/ogg"

    async def transcribe(data, **kwargs):
        calls.append(("asr", kwargs))
        return voice.VoiceTranscript(text="Здравствуйте, хочу записаться", filename=kwargs["filename"])

    monkeypatch.setattr(voice, "download_media", download)
    monkeypatch.setattr(voice, "transcribe_bytes", transcribe)
    result = asyncio.run(voice.transcribe_wazzup_voice({"type": "audio", "contentUri": "https://store.wazzup24.com/synthetic/?filename=voice.ogg"}))
    assert [call[0] for call in calls] == ["download", "asr"]
    assert calls[1][1]["language"] is None
    assert result.source == "wazzup"
    assert result.content_type == "audio/ogg"


def test_declared_audio_with_downloaded_pdf_never_reaches_asr(monkeypatch):
    async def download(**kwargs):
        return b"synthetic PDF", "application/pdf"

    async def transcribe(*args, **kwargs):
        pytest.fail("PDF must not be submitted to audio transcription")

    monkeypatch.setattr(voice, "download_media", download)
    monkeypatch.setattr(voice, "transcribe_bytes", transcribe)
    with pytest.raises(ValueError, match="not audio"):
        asyncio.run(voice.transcribe_wazzup_voice({"type": "audio", "contentUri": "https://store.wazzup24.com/synthetic"}))


def test_unavailable_or_untrusted_audio_never_downloaded(monkeypatch):
    async def download(**kwargs):
        pytest.fail("untrusted attachment must not reach the network")

    monkeypatch.setattr(voice, "download_media", download)
    with pytest.raises(ValueError, match="unavailable"):
        asyncio.run(voice.transcribe_wazzup_voice({"type": "audio", "contentUri": "https://evil.test/synthetic.ogg"}))


def test_legacy_message_extractor_keeps_image_and_document_kinds():
    messages = wazzup.extract_incoming_messages({"messages": [
        {"id": "synthetic-image", "chatId": "synthetic-chat", "type": "image", "contentUri": "https://store.wazzup24.com/photo.jpg"},
        {"id": "synthetic-doc", "chatId": "synthetic-chat", "type": "document", "contentUri": "https://store.wazzup24.com/file.pdf"},
    ]})
    assert [message["kind"] for message in messages] == ["image", "document"]
