from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlsplit
import httpx
from functools import lru_cache
from config import get_settings


@lru_cache(maxsize=1)
def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=3.0), follow_redirects=False)


MAX_MEDIA_BYTES = 25 * 1024 * 1024


AUDIO_EXT_BY_MIME = {
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

_AUDIO_MESSAGE_TYPES = {"audio", "voice", "ptt", "voice_message"}
_AUDIO_FILE_EXTENSIONS = (".ogg", ".oga", ".opus", ".mp3", ".m4a", ".wav", ".webm", ".mpga", ".mpeg")


def _iter_dicts(obj: Any):
    if isinstance(obj, dict):
        yield obj
        for value in obj.values():
            yield from _iter_dicts(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from _iter_dicts(item)


def _text_value(msg: dict[str, Any]) -> str:
    return _message_text(msg)


def is_audio_message_payload(msg: dict[str, Any]) -> bool:
    """Classify current audio, never an attachment in a quoted old message.

    A current nonempty text has priority for backwards-compatible routing.
    """
    return not _text_value(msg) and normalize_media_metadata(msg).get("kind") == "voice"



async def send_text(chat_id: str, text: str, chat_type: str = "whatsapp", channel_id: str | None = None) -> dict[str, Any]:
    settings = get_settings()
    # Важно: отвечаем через тот же WhatsApp-канал, с которого пришло сообщение.
    # Если channel_id не пришёл во входящем webhook, используем дефолтный канал из .env.
    payload = {
        "channelId": channel_id or settings.wazzup_channel_id,
        "chatType": chat_type,
        "chatId": chat_id,
        "text": text,
    }
    response = await _client().post(
        f"{settings.wazzup_api_url}/message",
        json=payload,
        headers={
            "Authorization": f"Bearer {settings.wazzup_api_key}",
            "Content-Type": "application/json",
        },
        follow_redirects=False,
    )
    response.raise_for_status()
    try:
        data = response.json()
    except ValueError as exc:
        # A 2xx with an empty/HTML body is not a delivery acknowledgement.
        # The caller keeps its outgoing claim uncertain, without a blind retry.
        raise ValueError("Invalid Wazzup acknowledgement JSON") from exc
    if (
        not isinstance(data, dict)
        or data.get("ok") is False
        or data.get("success") is False
        or bool(data.get("error"))
        or bool(data.get("errors"))
        or str(data.get("status") or "").lower() in {"error", "failed", "failure"}
    ):
        raise ValueError("Wazzup did not acknowledge the message")
    acknowledged = (
        data.get("ok") is True
        or data.get("success") is True
        or bool(str(data.get("messageId") or data.get("id") or "").strip())
    )
    if not acknowledged:
        raise ValueError("Wazzup acknowledgement has no message identity or success flag")
    data["status_code"] = response.status_code
    return data


def _dig(obj: dict[str, Any], *paths: str) -> Any:
    """Достаёт первое непустое значение из вложенных путей вида 'a.b.c'."""
    for path in paths:
        cur: Any = obj
        ok = True
        for key in path.split("."):
            if isinstance(cur, dict) and key in cur:
                cur = cur[key]
            else:
                ok = False
                break
        if ok and cur not in (None, ""):
            return cur
    return None


def _message_id(msg: dict[str, Any], payload: dict[str, Any], chat_id: str, text: str) -> str:
    mid = (
        _dig(msg, "id")
        or _dig(msg, "messageId")
        or _dig(msg, "message_id")
        or _dig(msg, "message.id")
        or _dig(msg, "message.messageId")
        or _dig(msg, "data.id")
        or ""
    )
    if mid:
        return str(mid)
    ts = _message_ts(msg)
    channel_id = str(_dig(msg, "channelId") or _dig(payload, "channelId") or "")
    # fallback: защищает от повторной пересылки одного и того же сообщения CRM/Wazzup.
    return f"fallback:{channel_id}:{chat_id}:{ts}:{text[:80]}"


def _message_ts(msg: dict[str, Any]) -> int:
    raw = (
        _dig(msg, "dateTime")
        or _dig(msg, "timestamp")
        or _dig(msg, "time")
        or _dig(msg, "createdAt")
        or _dig(msg, "message.timestamp")
        or 0
    )
    try:
        if isinstance(raw, (int, float)):
            return int(raw)
        if isinstance(raw, str) and raw.isdigit():
            return int(raw)
    except Exception:
        pass
    return 0


def _message_text(msg: dict[str, Any]) -> str:
    text = (
        _dig(msg, "text")
        or _dig(msg, "body")
        or _dig(msg, "content")
        or _dig(msg, "message.text")
        or _dig(msg, "message.body")
        or ""
    )
    if isinstance(text, dict):
        text = text.get("text") or text.get("body") or ""
    return str(text).strip()


def _media_leaf(value: Any) -> str:
    return str(value).strip() if isinstance(value, (str, int)) and not isinstance(value, bool) else ""


def normalize_media_metadata(msg: dict[str, Any]) -> dict[str, Any]:
    """Normalize Wazzup v3 media metadata without downloading or interpreting it.

    Official v3 webhooks use ``type`` and ``contentUri``. Only the current
    message and its direct media fields are inspected; quotedMessage, avatars
    and advertisements can contain unrelated attachment URLs.
    """
    if not isinstance(msg, dict):
        return {"kind": "text", "media_requires_text": False}
    nested = msg.get("message") if isinstance(msg.get("message"), dict) else {}
    current = {**nested, **msg}
    media_obj: dict[str, Any] = {}
    for key in ("media", "attachment", "file", "audio", "voice"):
        value = current.get(key)
        if isinstance(value, dict):
            media_obj = value
            break
    media_url = _media_leaf(_dig(current, "contentUri", "media_url", "mediaUrl", "file_url", "fileUrl", "downloadUrl")) or _media_leaf(_dig(media_obj, "contentUri", "url", "link", "downloadUrl", "download_url"))
    file_id = _media_leaf(_dig(current, "file_id", "fileId", "mediaId")) or _media_leaf(_dig(media_obj, "id", "fileId", "mediaId"))
    mime_type = (_media_leaf(_dig(current, "mime_type", "mimeType", "mime", "contentType")) or _media_leaf(_dig(media_obj, "mime_type", "mimeType", "mime", "contentType"))).split(";", 1)[0].lower()
    filename = _media_leaf(_dig(current, "filename", "fileName")) or _media_leaf(_dig(media_obj, "filename", "fileName", "name"))
    if not filename and media_url:
        try:
            parsed = urlsplit(media_url)
            params = parse_qs(parsed.query, max_num_fields=30)
            filename = _media_leaf((params.get("filename") or params.get("fileName") or [""])[0]) or unquote(parsed.path.rsplit("/", 1)[-1])
        except ValueError:
            filename = ""
    # Only a filename hint is retained. It is never used as a local path.
    filename = filename.replace("\\", "/").rsplit("/", 1)[-1][:160]
    declared_type = (_media_leaf(_dig(current, "kind", "type", "messageType", "payloadType", "mediaType")) or _media_leaf(_dig(media_obj, "type", "kind"))).lower()
    if declared_type in _AUDIO_MESSAGE_TYPES or declared_type.startswith("audio/"):
        kind = "voice"
    elif declared_type in {"image", "photo", "picture", "sticker"}:
        kind = "image"
    elif declared_type in {"document", "file"}:
        kind = "document"
    elif declared_type in {"video", "video_message"}:
        kind = "video"
    elif mime_type.startswith("audio/") or mime_type == "application/ogg":
        kind = "voice"
    elif mime_type.startswith("image/"):
        kind = "image"
    elif mime_type.startswith("video/"):
        kind = "video"
    elif filename.lower().endswith(_AUDIO_FILE_EXTENSIONS):
        kind = "voice"
    elif mime_type == "application/pdf" or filename.lower().endswith((".pdf", ".doc", ".docx", ".txt", ".xls", ".xlsx")):
        kind = "document"
    elif media_url or file_id or declared_type in {"unsupported", "unknown"}:
        kind = "unsupported"
    else:
        kind = "text"
    if kind == "text":
        return {"kind": "text", "media_requires_text": False}
    media_error = ""
    if media_url:
        try:
            media_request_headers(media_url)
        except ValueError:
            media_url = ""
            # Do not fall back to a second location supplied beside a bad URL.
            file_id = ""
            media_error = "untrusted_media_url"
    if kind == "voice" and not filename:
        filename = "voice." + AUDIO_EXT_BY_MIME.get(mime_type, "ogg")
    unavailable = not bool(media_url or file_id)
    return {
        "kind": kind,
        "media_url": media_url,
        "file_id": file_id,
        "filename": filename,
        "mime_type": mime_type,
        "media_requires_text": kind != "voice" or unavailable,
        "media_error": media_error or ("missing_media_location" if unavailable else ""),
    }


def _message_media(msg: dict[str, Any]) -> dict[str, Any]:
    media = normalize_media_metadata(msg)
    return media if media.get("kind") != "text" else {}


def media_text_request(kind: str, language: str | None = None) -> str:
    """Truthful fallback; no invented image reading, diagnosis or handoff."""
    kk = str(language or "").lower() == "kk"
    if kind == "voice":
        return "Дыбыстық хабарламаны түсіне алмадым. Өтініш, сұрағыңызды мәтінмен жазыңыз." if kk else "Не удалось распознать голосовое сообщение. Напишите, пожалуйста, Ваш вопрос текстом."
    if kind == "image":
        return "Суреттің мазмұнын оқи алмаймын. Өтініш, сұрағыңызды мәтінмен жазыңыз." if kk else "Не могу надёжно прочитать содержимое фотографии. Напишите, пожалуйста, Ваш вопрос текстом."
    if kind == "document":
        return "Құжаттың мазмұнын оқи алмаймын. Өтініш, сұрағыңызды мәтінмен жазыңыз." if kk else "Не могу надёжно прочитать содержимое документа. Напишите, пожалуйста, Ваш вопрос текстом."
    return "Бұл тіркеменің мазмұнын оқи алмаймын. Өтініш, сұрағыңызды мәтінмен жазыңыз." if kk else "Не могу прочитать содержимое этого вложения. Напишите, пожалуйста, Ваш вопрос текстом."


def media_request_headers(url: str) -> dict[str, str]:
    """Validate provider media location; credentials go only to the API origin.

    Redirects are deliberately disabled by the downloader. A trusted CDN URL
    must work without the account bearer; we never forward it across origins.
    """
    settings = get_settings()
    try:
        target = urlsplit(url)
        configured = urlsplit(settings.wazzup_api_url)
        host = str(target.hostname or "").lower()
        api_host = str(configured.hostname or "").lower()
        port = target.port or 443
        api_port = configured.port or 443
    except ValueError as exc:
        raise ValueError("Invalid media URL") from exc
    same_origin = target.scheme == configured.scheme == "https" and host == api_host and port == api_port
    trusted_cdn = (host == "wazzup24.com" or host.endswith(".wazzup24.com")) and port == 443
    if target.scheme != "https" or not host or target.username is not None or target.password is not None or not (same_origin or trusted_cdn):
        raise ValueError("Untrusted media URL")
    return {"Authorization": f"Bearer {settings.wazzup_api_key}"} if same_origin else {}


async def download_media(media_url: str | None = None, file_id: str | None = None) -> tuple[bytes, str]:
    """Download bounded media from a trusted provider origin without redirects."""
    settings = get_settings()
    url = (media_url or "").strip()
    if not url and file_id:
        url = settings.wazzup_media_endpoint_template.format(
            api_url=settings.wazzup_api_url.rstrip("/"),
            file_id=quote(str(file_id), safe=""),
        )
    if not url:
        raise ValueError("Missing media URL or file identity")
    headers = media_request_headers(url)
    async with _client().stream("GET", url, headers=headers, follow_redirects=False) as response:
        response.raise_for_status()
        length = response.headers.get("content-length")
        if length is not None:
            try:
                declared_length = int(length)
            except ValueError as exc:
                raise ValueError("Invalid media content length") from exc
            if declared_length < 0 or declared_length > MAX_MEDIA_BYTES:
                raise ValueError("Media exceeds size limit")
        chunks = bytearray()
        async for chunk in response.aiter_bytes():
            if len(chunks) + len(chunk) > MAX_MEDIA_BYTES:
                raise ValueError("Media exceeds size limit")
            chunks.extend(chunk)
        return bytes(chunks), response.headers.get("content-type", "")


def extract_incoming_messages(payload: dict[str, Any]) -> list[dict[str, str]]:
    """Мягко разбирает Wazzup webhook.

    Возвращает элементы:
    - chat_id
    - text, если это текст
    - media_url/file_id/filename/mime_type, если это голосовое
    - phone
    - chat_type
    """
    raw_messages = payload.get("messages") or payload.get("message") or []
    if isinstance(raw_messages, dict):
        raw_messages = [raw_messages]

    # Иногда CRM/Wazzup присылает пачку сообщений не в хронологическом порядке.
    # Сортируем по timestamp, чтобы бот не отвечал на старое приветствие после нового имени.
    try:
        raw_messages = sorted(raw_messages, key=lambda m: _message_ts(m) if isinstance(m, dict) else 0)
    except Exception:
        pass

    result: list[dict[str, str]] = []
    for msg in raw_messages:
        if not isinstance(msg, dict):
            continue

        chat_id = str(msg.get("chatId") or msg.get("chat_id") or msg.get("from") or "").strip()
        if not chat_id:
            continue

        chat_type = str(msg.get("chatType") or msg.get("chat_type") or "whatsapp")
        channel_id = str(
            msg.get("channelId")
            or msg.get("channel_id")
            or _dig(msg, "message.channelId")
            or _dig(msg, "message.channel_id")
            or _dig(msg, "channel.id")
            or _dig(payload, "channelId")
            or _dig(payload, "channel_id")
            or ""
        ).strip()
        contact = msg.get("contact") or {}
        if not isinstance(contact, dict):
            contact = {}
        phone = str(contact.get("phone") or msg.get("phone") or chat_id)

        text = _message_text(msg)
        media = _message_media(msg)
        direction = str(_dig(msg, "direction") or _dig(msg, "message.direction") or ("outgoing" if (msg.get("isEcho") is True or msg.get("fromMe") is True) else "incoming")).lower()
        is_from_me = bool(msg.get("isEcho") is True or msg.get("fromMe") is True or msg.get("isFromMe") is True)
        timestamp = str(_message_ts(msg) or "")
        message_key = _message_id(msg, payload, chat_id, text or (media.get("file_id") if media else ""))

        if text:
            result.append({"chat_id": chat_id, "text": text, "phone": phone, "chat_type": chat_type, "kind": "text", "channel_id": channel_id, "message_key": message_key, "message_id": message_key, "timestamp": timestamp, "direction": direction, "is_from_me": str(is_from_me).lower()})
        elif media:
            item = {"chat_id": chat_id, "phone": phone, "chat_type": chat_type, "kind": media.get("kind") or "unsupported", "channel_id": channel_id, "message_key": message_key, "message_id": message_key, "timestamp": timestamp, "direction": direction, "is_from_me": str(is_from_me).lower()}
            item.update(media)
            result.append(item)

    return result
