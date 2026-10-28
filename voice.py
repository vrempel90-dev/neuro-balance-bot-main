from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from typing import Any

from fastapi import UploadFile
from openai import AsyncOpenAI

from config import get_settings
from wazzup import MAX_MEDIA_BYTES, download_media, normalize_media_metadata


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


def filename_from_content_type(content_type: str | None, fallback: str = "voice.ogg") -> str:
    if not content_type:
        return fallback
    pure = content_type.split(";")[0].strip().lower()
    ext = VOICE_EXT_BY_MIME.get(pure)
    return f"voice.{ext}" if ext else fallback


async def transcribe_bytes(
    data: bytes,
    filename: str = "voice.ogg",
    language: str | None = None,
) -> VoiceTranscript:
    """Расшифровывает bytes аудио через OpenAI.

    Сделано отдельным модулем, чтобы голосовые не зависели от Wazzup.
    Работает для Wazzup, debug endpoint и любого будущего провайдера.
    """
    if not data:
        return VoiceTranscript(text="", filename=filename)

    if len(data) > MAX_MEDIA_BYTES:
        raise ValueError("Audio exceeds size limit")

    language_hint = str(language or "").strip().lower()
    if language_hint not in {"", "auto", "ru", "kk"}:
        raise ValueError("Unsupported transcription language hint")
    settings = get_settings()
    client = AsyncOpenAI(api_key=settings.openai_api_key, timeout=30.0, max_retries=0)
    tmp_path: str | None = None
    try:
        # No language parameter means multilingual auto-detection. In
        # particular, a Kazakh speaker must not be forced through Russian ASR.
        kwargs: dict[str, Any] = {"model": settings.openai_voice_model}
        if language_hint in {"ru", "kk"}:
            kwargs["language"] = language_hint
        suffix = os.path.splitext(filename)[1].lower()
        if suffix not in {".flac", ".mp3", ".mp4", ".mpeg", ".mpga", ".m4a", ".ogg", ".wav", ".webm"}:
            suffix = ".ogg"
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp.write(data)
            tmp_path = tmp.name
        with open(tmp_path, "rb") as audio_file:
            result = await client.audio.transcriptions.create(file=audio_file, **kwargs)
        return VoiceTranscript(text=str(result.text or "").strip(), filename=filename)
    finally:
        if tmp_path is not None:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        await client.close()


async def transcribe_upload(file: UploadFile, language: str | None = None) -> VoiceTranscript:
    data = await file.read(MAX_MEDIA_BYTES + 1)
    if len(data) > MAX_MEDIA_BYTES:
        raise ValueError("Audio exceeds size limit")
    filename = file.filename or filename_from_content_type(file.content_type)
    result = await transcribe_bytes(data, filename=filename, language=language)
    result.content_type = file.content_type
    result.source = "upload"
    return result


async def transcribe_wazzup_voice(message: dict[str, Any], language: str | None = None) -> VoiceTranscript:
    """Скачивает и расшифровывает голосовое из разобранного Wazzup-сообщения."""
    metadata = normalize_media_metadata(message)
    media_url = metadata.get("media_url") or None
    file_id = metadata.get("file_id") or None
    filename = metadata.get("filename") or "voice.ogg"
    if metadata.get("kind") != "voice" or metadata.get("media_requires_text"):
        raise ValueError("Voice attachment is unavailable")

    data, content_type = await download_media(media_url=media_url, file_id=file_id)
    pure_type = str(content_type or "").split(";", 1)[0].strip().lower()
    if pure_type and not (pure_type.startswith("audio/") or pure_type in {"application/ogg", "application/octet-stream", "binary/octet-stream"}):
        raise ValueError("Attachment is not audio")
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
        return "[Голосовое сообщение]: аудио не удалось распознать. Попроси пациента написать текстом."
    return f"[Голосовое сообщение, расшифровка]: {clean}"
