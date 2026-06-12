from __future__ import annotations

from typing import Any
import httpx
from functools import lru_cache
from config import get_settings


@lru_cache(maxsize=1)
def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=3.0), follow_redirects=True)


AUDIO_EXT_BY_MIME = {
    "audio/ogg": "ogg",
    "audio/opus": "ogg",
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/mp4": "m4a",
    "audio/m4a": "m4a",
}


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
    )
    response.raise_for_status()
    return response.json() if response.text else {"ok": True}


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


GENERIC_LEAD_TEXT_MARKERS = [
    "вы оставляли у нас заявку",
    "скажите, что вас беспокоит",
    "скажите, что Вас беспокоит",
]

MEDICAL_TEXT_MARKERS = [
    "спина", "поясниц", "протруз", "грыж", "шея", "немеет", "онем",
    "сустав", "колен", "плеч", "отдаёт", "отдает", "нога", "рука",
]


def _message_text(msg: dict[str, Any]) -> str:
    candidates: list[str] = []
    for value in [
        _dig(msg, "message.text"),
        _dig(msg, "message.body"),
        _dig(msg, "text"),
        _dig(msg, "body"),
        _dig(msg, "content"),
    ]:
        if isinstance(value, dict):
            value = value.get("text") or value.get("body") or ""
        value = str(value or "").strip()
        if value and value not in candidates:
            candidates.append(value)

    if not candidates:
        return ""

    def has_medical_marker(value: str) -> bool:
        low = value.lower()
        return any(marker.lower() in low for marker in MEDICAL_TEXT_MARKERS)

    def is_generic_lead(value: str) -> bool:
        low = value.lower()
        return any(marker.lower() in low for marker in GENERIC_LEAD_TEXT_MARKERS)

    # Wazzup payloads may include the lead template at top level and the real
    # incoming text deeper in message.*.  If any candidate has a medical
    # complaint, never replace it with the generic lead template.
    for candidate in candidates:
        if has_medical_marker(candidate) and not is_generic_lead(candidate):
            return candidate

    for candidate in candidates:
        if not is_generic_lead(candidate):
            return candidate

    return candidates[0]


def _message_media(msg: dict[str, Any]) -> dict[str, str]:
    """Пытается найти аудио/voice в разных вариантах webhook от Wazzup.

    Возвращает поля:
    - media_url: если есть прямая ссылка;
    - file_id: если есть ID файла/медиа;
    - filename;
    - mime_type.
    """
    msg_type = str(
        _dig(msg, "type")
        or _dig(msg, "messageType")
        or _dig(msg, "contentType")
        or _dig(msg, "message.type")
        or ""
    ).lower()

    # Частые места, где провайдеры кладут attachment/media.
    media_obj = (
        _dig(msg, "media")
        or _dig(msg, "attachment")
        or _dig(msg, "file")
        or _dig(msg, "audio")
        or _dig(msg, "voice")
        or _dig(msg, "message.media")
        or _dig(msg, "message.attachment")
        or {}
    )
    if not isinstance(media_obj, dict):
        media_obj = {}

    media_url = str(
        _dig(media_obj, "url")
        or _dig(media_obj, "link")
        or _dig(media_obj, "downloadUrl")
        or _dig(media_obj, "download_url")
        or _dig(msg, "mediaUrl")
        or _dig(msg, "fileUrl")
        or ""
    ).strip()
    file_id = str(
        _dig(media_obj, "id")
        or _dig(media_obj, "fileId")
        or _dig(media_obj, "mediaId")
        or _dig(msg, "fileId")
        or _dig(msg, "mediaId")
        or ""
    ).strip()
    mime_type = str(
        _dig(media_obj, "mimeType")
        or _dig(media_obj, "mime_type")
        or _dig(media_obj, "contentType")
        or _dig(msg, "mimeType")
        or ""
    ).strip()
    filename = str(
        _dig(media_obj, "filename")
        or _dig(media_obj, "fileName")
        or _dig(msg, "filename")
        or "voice.ogg"
    ).strip()

    is_audio_type = any(x in msg_type for x in ["audio", "voice", "ptt"])
    is_audio_mime = mime_type.startswith("audio/")
    if not (is_audio_type or is_audio_mime or media_url or file_id):
        return {}

    if filename == "voice.ogg" and mime_type in AUDIO_EXT_BY_MIME:
        filename = f"voice.{AUDIO_EXT_BY_MIME[mime_type]}"

    return {
        "media_url": media_url,
        "file_id": file_id,
        "filename": filename,
        "mime_type": mime_type,
    }


async def download_media(media_url: str | None = None, file_id: str | None = None) -> tuple[bytes, str]:
    """Скачивает голосовой файл.

    Если webhook даёт прямой media_url — качаем его.
    Если даёт только file_id — пробуем WAZZUP_MEDIA_ENDPOINT_TEMPLATE.
    Возможно, под реальный кабинет Wazzup endpoint придётся поменять в .env.
    """
    settings = get_settings()
    url = (media_url or "").strip()
    if not url and file_id:
        url = settings.wazzup_media_endpoint_template.format(
            api_url=settings.wazzup_api_url.rstrip("/"),
            file_id=file_id,
        )
    if not url:
        raise ValueError("Нет media_url или file_id для скачивания голосового")

    response = await _client().get(
        url,
        headers={"Authorization": f"Bearer {settings.wazzup_api_key}"},
    )
    response.raise_for_status()
    content_type = response.headers.get("content-type", "")
    return response.content, content_type


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
        if msg.get("isEcho") is True or msg.get("fromMe") is True:
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
        message_key = _message_id(msg, payload, chat_id, text or (media.get("file_id") if media else ""))

        if text:
            result.append({"chat_id": chat_id, "text": text, "phone": phone, "chat_type": chat_type, "kind": "text", "channel_id": channel_id, "message_key": message_key})
        elif media:
            item = {"chat_id": chat_id, "phone": phone, "chat_type": chat_type, "kind": "voice", "channel_id": channel_id, "message_key": message_key}
            item.update(media)
            result.append(item)

    return result
