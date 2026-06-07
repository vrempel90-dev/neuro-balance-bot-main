from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from typing import Any

from fastapi import UploadFile
from openai import AsyncOpenAI

from config import get_settings
from wazzup import download_media


VOICE_EXT_BY_MIME = {
    "audio/ogg": "ogg",
    "audio/opus": "ogg",
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/mp4": "m4a",
    "audio/m4a": "m4a",
    "audio/webm": "webm",
}


@dataclass
class VoiceTranscript:
    text: str
    filename: str
    content_type: str | None = None
    source: str = "voice"
    ok: bool = True
    error: str = ""


def filename_from_content_type(content_type: str | None, fallback: str = "voice.ogg") -> str:
    if not content_type:
        return fallback
    pure = content_type.split(";")[0].strip().lower()
    ext = VOICE_EXT_BY_MIME.get(pure)
    return f"voice.{ext}" if ext else fallback


async def transcribe_bytes(
    data: bytes,
    filename: str = "voice.ogg",
    language: str = "ru",
) -> VoiceTranscript:
    """Расшифровывает bytes аудио через OpenAI.

    Сделано отдельным модулем, чтобы голосовые не зависели от Wazzup.
    Работает для Wazzup, debug endpoint и любого будущего провайдера.
    """
    if not data:
        return VoiceTranscript(text="", filename=filename, ok=False, error="empty_audio")

    settings = get_settings()
    client = AsyncOpenAI(api_key=settings.openai_api_key)

    # OpenAI SDK стабильнее принимает файл с диска, чем tuple(bytes) на разных версиях.
    suffix = os.path.splitext(filename)[1] or ".ogg"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(data)
        tmp_path = tmp.name

    try:
        with open(tmp_path, "rb") as f:
            result = await client.audio.transcriptions.create(
                model=settings.openai_voice_model,
                file=f,
                language=language,
            )
        return VoiceTranscript(text=(result.text or "").strip(), filename=filename, ok=True)
    except Exception as exc:
        return VoiceTranscript(text="", filename=filename, ok=False, error=str(exc)[:500])
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass


async def transcribe_upload(file: UploadFile, language: str = "ru") -> VoiceTranscript:
    data = await file.read()
    filename = file.filename or filename_from_content_type(file.content_type)
    result = await transcribe_bytes(data, filename=filename, language=language)
    result.content_type = file.content_type
    result.source = "upload"
    return result


async def transcribe_wazzup_voice(message: dict[str, Any], language: str = "ru") -> VoiceTranscript:
    """Скачивает и расшифровывает голосовое из разобранного Wazzup-сообщения."""
    media_url = message.get("media_url") or None
    file_id = message.get("file_id") or None
    filename = message.get("filename") or "voice.ogg"

    try:
        data, content_type = await download_media(media_url=media_url, file_id=file_id)
    except Exception as exc:
        return VoiceTranscript(text="", filename=filename, ok=False, error=str(exc)[:500])

    if (not filename or filename == "voice.ogg") and content_type:
        filename = filename_from_content_type(content_type, fallback="voice.ogg")

    result = await transcribe_bytes(data, filename=filename, language=language)
    result.content_type = content_type
    result.source = "wazzup"
    return result


def voice_text_for_bot(transcript: str) -> str:
    """Как передавать голосовое в основной AI-сценарий."""
    clean = (transcript or "").strip()
    if not clean:
        return ""
    return f"[Голосовое сообщение, расшифровка]: {clean}"
