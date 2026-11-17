"""Synthetic media contracts; no external calls or real patient documents."""
import asyncio
import io
import json
from types import SimpleNamespace

import pytest


def png_bytes():
    from PIL import Image
    stream = io.BytesIO()
    Image.new("RGB", (24, 24), "white").save(stream, format="PNG")
    return stream.getvalue()


def pdf_bytes(pages=2):
    import pypdfium2 as pdfium
    stream = io.BytesIO()
    with pdfium.PdfDocument.new() as document:
        for _ in range(pages):
            document.new_page(40, 60).close()
        document.save(stream)
    return stream.getvalue()


def fake_client(monkeypatch, module, answer, *, fail=False):
    seen = []
    closed = []

    async def create(**kwargs):
        seen.append(kwargs)
        if fail:
            raise RuntimeError("private signed-url or patient text")
        return SimpleNamespace(text=answer, choices=[SimpleNamespace(message=SimpleNamespace(content=answer))], usage=None)

    async def close():
        closed.append(True)

    monkeypatch.setattr(module, "AsyncOpenAI", lambda **kwargs: SimpleNamespace(
        audio=SimpleNamespace(transcriptions=SimpleNamespace(create=create)),
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)), close=close))
    return seen, closed


def test_voice_auto_language_and_client_closed(monkeypatch):
    import voice
    seen, closed = fake_client(monkeypatch, voice, "Сәлеметсіз бе, мамамның белі ауырады")
    result = asyncio.run(voice.transcribe_bytes(b"OggS" + b"\x00" * 24))
    assert result.text.startswith("Сәлеметсіз")
    assert "language" not in seen[0]
    assert closed == [True]


def test_voice_explicit_language_and_cleanup_on_api_failure(monkeypatch):
    import voice
    seen, closed = fake_client(monkeypatch, voice, "", fail=True)
    with pytest.raises(RuntimeError):
        asyncio.run(voice.transcribe_bytes(b"OggS" + b"\x00" * 24, language="kk"))
    assert seen[0]["language"] == "kk"
    assert seen[0]["file"].closed
    assert closed == [True]


@pytest.mark.parametrize("data", [b"<html>bad</html>", b"MZfake.exe", b"not audio"])
def test_voice_rejects_non_audio_before_api(monkeypatch, data):
    import voice
    seen, _ = fake_client(monkeypatch, voice, "invented")
    with pytest.raises(ValueError):
        asyncio.run(voice.transcribe_bytes(data))
    assert seen == []


def test_attachment_extraction_order_and_caption_independence():
    import media
    result = media.extract_attachments({"text": "МРТ", "attachments": [
        {"type": "image", "url": "https://files.wazzup24.com/a.png", "mimeType": "image/png"},
        {"type": "audio", "contentUri": "https://files.wazzup24.com/a.ogg"},
        {"type": "document", "fileId": "pdf-1", "fileName": "report.pdf"},
    ]})
    assert [p["kind"] for p in result] == ["image", "voice", "document"]
    assert result[1]["media_url"].endswith("a.ogg")
    assert result[2]["file_id"] == "pdf-1"


def test_document_disabled_never_downloads(monkeypatch):
    import media
    monkeypatch.delenv("MEDICAL_DOCUMENT_PROCESSING_ENABLED", raising=False)
    async def forbidden(**kwargs):
        pytest.fail("document downloaded without processing approval")
    monkeypatch.setattr(media, "download_media", forbidden)
    message = {"text": "Посмотрите", "attachments": [{"kind": "image", "media_url": "https://files.wazzup24.com/a.png"}]}
    answer = asyncio.run(media.prepare_message(message))
    assert "Посмотрите" in answer
    assert message["media_status"] == "needs_attention"
    assert message["media_errors"] == ["document_processing_disabled"]


def test_mixed_caption_voice_document_order(monkeypatch):
    import media
    monkeypatch.setenv("MEDICAL_DOCUMENT_PROCESSING_ENABLED", "true")
    async def transcribe(part, language=None):
        assert language is None
        return SimpleNamespace(text="Маме 62 года")
    async def document(part):
        return "L4-L5 протрузия"
    monkeypatch.setattr(media.voice, "transcribe_wazzup_voice", transcribe)
    monkeypatch.setattr(media, "extract_document", document)
    message = {"text": "МРТ мамы", "attachments": [{"kind": "voice"}, {"kind": "document"}]}
    result = asyncio.run(media.prepare_message(message))
    assert result.index("МРТ мамы") < result.index("Маме 62 года") < result.index("L4-L5 протрузия")
    assert message["media_status"] == "processed"
    assert "недоверенные" in result.lower()


def test_one_failed_attachment_does_not_discard_other_part(monkeypatch):
    import media
    async def transcribe(part, language=None):
        if part.get("file_id") == "broken":
            raise RuntimeError("private patient message")
        return SimpleNamespace(text="Жасы 62-де")
    monkeypatch.setattr(media.voice, "transcribe_wazzup_voice", transcribe)
    message = {"text": "Сәлем", "attachments": [{"kind": "voice", "file_id": "broken"}, {"kind": "voice", "file_id": "good"}]}
    result = asyncio.run(media.prepare_message(message, language="kk"))
    assert "Жасы 62-де" in result and "Сәлем" in result
    assert "private" not in result
    assert message["media_status"] == "needs_attention"
    assert message["media_errors"] == ["voice_processing_failed"]


@pytest.mark.parametrize("data,mime", [(b"<html>bad</html>", "image/png"), (b"MZfake.exe", "application/pdf")])
def test_bad_document_magic_rejected(monkeypatch, data, mime):
    import media
    async def download(**kwargs):
        return data, mime
    monkeypatch.setenv("MEDICAL_DOCUMENT_PROCESSING_ENABLED", "true")
    monkeypatch.setattr(media, "download_media", download)
    seen, _ = fake_client(monkeypatch, media, "{}")
    with pytest.raises(ValueError):
        asyncio.run(media.extract_document({"media_url": "https://files.wazzup24.com/file"}))
    assert seen == []


@pytest.mark.parametrize("data,mime,pages", [(png_bytes(), "image/png", 1), (pdf_bytes(), "application/pdf", 2)], ids=["png", "two-page-pdf"])
def test_real_image_and_pdf_pages_reach_vision(monkeypatch, data, mime, pages):
    import media
    monkeypatch.setenv("MEDICAL_DOCUMENT_PROCESSING_ENABLED", "true")
    async def download(**kwargs):
        return data, mime
    monkeypatch.setattr(media, "download_media", download)
    seen, closed = fake_client(monkeypatch, media, json.dumps({"kind": "report", "text": "Заключение: протрузия L4-L5"}))
    result = asyncio.run(media.extract_document({"media_url": "https://files.wazzup24.com/file"}))
    assert "протрузия L4-L5" in result
    assert len([x for x in seen[0]["messages"][1]["content"] if x["type"] == "image_url"]) == pages
    assert closed == [True]


def test_page_limit_refuses_whole_pdf_instead_of_silent_truncation(monkeypatch):
    import media
    monkeypatch.setenv("MEDICAL_DOCUMENT_PROCESSING_ENABLED", "true")
    async def download(**kwargs):
        return pdf_bytes(media.MAX_DOCUMENT_PAGES + 1), "application/pdf"
    monkeypatch.setattr(media, "download_media", download)
    seen, _ = fake_client(monkeypatch, media, "{}")
    with pytest.raises(ValueError):
        asyncio.run(media.extract_document({"file_id": "many-pages"}))
    assert seen == []


@pytest.mark.parametrize("kind", ["scan", "unreadable"])
def test_scan_or_unreadable_never_returns_invented_diagnosis(monkeypatch, kind):
    import media
    monkeypatch.setenv("MEDICAL_DOCUMENT_PROCESSING_ENABLED", "true")
    async def download(**kwargs):
        return png_bytes(), "image/png"
    monkeypatch.setattr(media, "download_media", download)
    fake_client(monkeypatch, media, json.dumps({"kind": kind, "text": "invented diagnosis"}))
    with pytest.raises(media.DocumentNeedsReview):
        asyncio.run(media.extract_document({"file_id": "scan"}))


def test_vision_failure_closes_client(monkeypatch):
    import media
    monkeypatch.setenv("MEDICAL_DOCUMENT_PROCESSING_ENABLED", "true")
    async def download(**kwargs):
        return png_bytes(), "image/png"
    monkeypatch.setattr(media, "download_media", download)
    _, closed = fake_client(monkeypatch, media, "", fail=True)
    with pytest.raises(RuntimeError):
        asyncio.run(media.extract_document({"file_id": "scan"}))
    assert closed == [True]


def test_voice_exhausted_budget_never_calls_api(monkeypatch):
    import voice
    import ai_budget
    monkeypatch.setattr(ai_budget, "check_allowed", lambda purpose: (False, "monthly_budget_exhausted"))
    seen, _ = fake_client(monkeypatch, voice, "not allowed")
    with pytest.raises(ValueError, match="budget"):
        asyncio.run(voice.transcribe_bytes(b"OggS" + b"\x00" * 24))
    assert seen == []


def test_voice_temp_file_failure_closes_client(monkeypatch):
    import voice
    _, closed = fake_client(monkeypatch, voice, "not reached")
    def fail_file(**kwargs):
        raise OSError("disk unavailable")
    monkeypatch.setattr(voice.tempfile, "NamedTemporaryFile", fail_file)
    with pytest.raises(OSError):
        asyncio.run(voice.transcribe_bytes(b"OggS" + b"\x00" * 24))
    assert closed == [True]


def test_voice_flac_supported(monkeypatch):
    import voice
    seen, _ = fake_client(monkeypatch, voice, "Аудио")
    result = asyncio.run(voice.transcribe_bytes(b"fLaC" + b"\x00" * 24, filename="sample.flac"))
    assert result.text == "Аудио"
    assert len(seen) == 1


@pytest.mark.parametrize("format", ["JPEG", "WEBP"])
def test_supported_image_formats_are_decoded_and_metadata_stripped(monkeypatch, format):
    import media
    from PIL import Image
    source = io.BytesIO()
    Image.new("RGB", (24, 24), "white").save(source, format=format)
    result = media._document_pages(source.getvalue())
    assert len(result) == 1
    assert result[0].startswith(b"\x89PNG\r\n\x1a\n")


def test_document_size_and_pixel_limits(monkeypatch):
    import media
    monkeypatch.setattr(media, "MAX_MEDIA_BYTES", 8)
    with pytest.raises(ValueError, match="size"):
        media._document_pages(png_bytes())
    monkeypatch.setattr(media, "MAX_MEDIA_BYTES", 10_000)
    monkeypatch.setattr(media, "MAX_DOCUMENT_PIXELS", 100)
    with pytest.raises(ValueError, match="dimensions"):
        media._document_pages(png_bytes())


def test_document_budget_blocks_download(monkeypatch):
    import media
    monkeypatch.setenv("MEDICAL_DOCUMENT_PROCESSING_ENABLED", "true")
    monkeypatch.setattr(media.ai_budget, "check_allowed", lambda purpose: (False, "budget"))
    async def forbidden(**kwargs):
        pytest.fail("budget-exhausted document was downloaded")
    monkeypatch.setattr(media, "download_media", forbidden)
    with pytest.raises(media.DocumentNeedsReview, match="budget"):
        asyncio.run(media.extract_document({"file_id": "a"}))


def test_attachment_limit_is_visible_and_not_silently_truncated(monkeypatch):
    import media
    async def forbidden(*args, **kwargs):
        pytest.fail("partial attachment batch was analyzed")
    monkeypatch.setattr(media.voice, "transcribe_wazzup_voice", forbidden)
    message = {"text": "caption", "attachments": [{"kind": "voice"}] * (media.MAX_ATTACHMENTS + 1)}
    result = asyncio.run(media.prepare_message(message))
    assert "caption" in result
    assert message["media_errors"] == ["attachment_limit_exceeded"]
    assert message["media_status"] == "needs_attention"


def test_voice_consent_is_separate_from_document_evidence(monkeypatch):
    import media
    from types import SimpleNamespace
    async def speech(*args, **kwargs):
        return SimpleNamespace(text="Нет")
    async def document(*args, **kwargs):
        return "Противопоказаний нет"
    monkeypatch.setenv("MEDICAL_DOCUMENT_PROCESSING_ENABLED", "true")
    monkeypatch.setattr(media.voice, "transcribe_wazzup_voice", speech)
    monkeypatch.setattr(media, "extract_document", document)
    message = {"text": "", "attachments": [{"kind": "voice"}, {"kind": "document"}]}
    asyncio.run(media.prepare_message(message))
    assert message.get("patient_speech_text") == "Нет"
