from __future__ import annotations

import asyncio
import uuid
import mimetypes
from typing import Any

import httpx

from fastapi import FastAPI, Header, HTTPException, Request, UploadFile, File

import state
from config import get_settings
from dialog import handle_message
from schedule import is_bot_work_time
from voice import transcribe_wazzup_voice, transcribe_bytes, transcribe_upload, voice_text_for_bot
from wazzup import extract_incoming_messages, send_text

try:
    from strict_prompt_guard import enforce_prompt_only
except Exception:
    def enforce_prompt_only(answer: str, session: dict[str, Any] | None = None) -> str:
        return answer or ""


app = FastAPI(title="Neuro Balance Hybrid WhatsApp Booking Bot")


@app.get("/")
def root() -> dict[str, Any]:
    return {
        "ok": True,
        "service": "Neuro Balance WhatsApp Bot",
        "docs": "/docs",
    }


@app.on_event("startup")
def startup() -> None:
    state.init_db()


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "mode": "gpt4o-mini-night-only",
        "bot_work_time_now": is_bot_work_time(),
    }


def _get_session_safe(chat_id: str) -> dict[str, Any]:
    try:
        session = state.get_session(chat_id)
        return session if isinstance(session, dict) else {}
    except Exception:
        return {}


def _guard_answer(chat_id: str, answer: str) -> str:
    """Финальная защита перед отправкой пациенту.

    Не даёт ответу уйти в Wazzup без strict_prompt_guard:
    - убирает обращение по имени;
    - убирает лишние фразы;
    - ограничивает длинные ответы вне разрешённых шаблонов.
    """
    session = _get_session_safe(chat_id)
    return enforce_prompt_only(answer or "", session)


def _voice_fallback_answer() -> str:
    return (
        "Спасибо за Ваше обращение 🌿 "
        "Передам информацию врачу — он свяжется с Вами в ближайшее время."
    )



def _deep_find_audio_items(obj: Any) -> list[dict[str, Any]]:
    """Ищет в payload Wazzup вложения/медиа, похожие на голосовое или аудио.

    Сделано максимально безопасно: если Wazzup пришлёт обычный текст,
    функция ничего не добавит и старый путь не изменится.
    """
    found: list[dict[str, Any]] = []

    audio_keys = {
        "audio", "voice", "ptt", "media", "attachment", "attachments",
        "file", "files", "document", "content", "message",
    }

    def walk(x: Any, parents: list[dict[str, Any]]) -> None:
        if isinstance(x, dict):
            low_keys = {str(k).lower() for k in x.keys()}
            type_text = " ".join(str(x.get(k, "")) for k in [
                "type", "messageType", "contentType", "mimeType", "mime", "mediaType",
            ]).lower()

            url_text = " ".join(str(x.get(k, "")) for k in [
                "url", "fileUrl", "mediaUrl", "downloadUrl", "contentUri",
                "contentUrl", "link", "href",
            ]).lower()

            filename_text = " ".join(str(x.get(k, "")) for k in [
                "filename", "fileName", "name",
            ]).lower()

            looks_audio = (
                "audio" in type_text
                or "voice" in type_text
                or "ptt" in type_text
                or "ogg" in filename_text
                or "oga" in filename_text
                or "opus" in filename_text
                or "mp3" in filename_text
                or "m4a" in filename_text
                or "wav" in filename_text
                or ".ogg" in url_text
                or ".oga" in url_text
                or ".mp3" in url_text
                or ".m4a" in url_text
                or ".wav" in url_text
            )

            if looks_audio and (low_keys & audio_keys or url_text):
                item = dict(x)
                for parent in reversed(parents):
                    for k in [
                        "chatId", "chat_id", "chat_id_external", "phone", "from", "sender",
                        "chatType", "chat_type", "channelId", "channel_id", "messageId",
                        "id", "messageKey", "message_key",
                    ]:
                        if k not in item and k in parent:
                            item[k] = parent[k]
                found.append(item)

            walk_parents = parents + [x]
            for v in x.values():
                walk(v, walk_parents)

        elif isinstance(x, list):
            for v in x:
                walk(v, parents)

    walk(obj, [])
    return found


def _first_value(d: dict[str, Any], keys: list[str]) -> Any:
    for key in keys:
        if key in d and d.get(key) not in (None, ""):
            return d.get(key)
    return None


def _message_from_audio_item(item: dict[str, Any]) -> dict[str, Any] | None:
    """Превращает найденное audio/media-вложение в формат сообщения для обработчика."""
    url = _first_value(item, [
        "url", "fileUrl", "mediaUrl", "downloadUrl", "contentUri",
        "contentUrl", "link", "href",
    ])

    # Иногда URL лежит глубже: file.url / media.url / audio.url
    if not url:
        for nested_key in ["file", "media", "audio", "voice", "attachment", "content"]:
            nested = item.get(nested_key)
            if isinstance(nested, dict):
                url = _first_value(nested, [
                    "url", "fileUrl", "mediaUrl", "downloadUrl", "contentUri",
                    "contentUrl", "link", "href",
                ])
                if url:
                    break

    if not url:
        return None

    chat_id = _first_value(item, [
        "chat_id", "chatId", "chat_id_external", "phone", "from", "sender", "contactPhone",
    ])
    if isinstance(chat_id, dict):
        chat_id = _first_value(chat_id, ["phone", "id", "chatId", "chat_id"])

    phone = _first_value(item, ["phone", "from", "sender", "contactPhone"]) or chat_id
    if isinstance(phone, dict):
        phone = _first_value(phone, ["phone", "id", "chatId", "chat_id"]) or chat_id

    if not chat_id:
        return None

    message_key = _first_value(item, ["message_key", "messageKey", "messageId", "id"])
    filename = _first_value(item, ["filename", "fileName", "name"]) or "voice.ogg"
    content_type = _first_value(item, ["contentType", "mimeType", "mime"]) or ""

    return {
        "chat_id": str(chat_id),
        "phone": str(phone or chat_id),
        "chat_type": str(_first_value(item, ["chat_type", "chatType"]) or "whatsapp"),
        "channel_id": _first_value(item, ["channel_id", "channelId"]),
        "message_key": str(message_key or f"voice:{chat_id}:{url}"),
        "kind": "voice",
        "media_url": str(url),
        "file_url": str(url),
        "filename": str(filename),
        "content_type": str(content_type),
        "raw_audio_item": item,
    }


def _extract_voice_messages_from_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    seen: set[str] = set()

    for item in _deep_find_audio_items(payload):
        msg = _message_from_audio_item(item)
        if not msg:
            continue
        key = str(msg.get("message_key") or msg.get("media_url") or "")
        if key in seen:
            continue
        seen.add(key)
        messages.append(msg)

    return messages


def _message_has_voice_url(message: dict[str, Any]) -> bool:
    if str(message.get("kind") or "").lower() == "voice":
        return True

    maybe = " ".join(str(message.get(k, "")) for k in [
        "media_url", "file_url", "url", "fileUrl", "mediaUrl", "downloadUrl",
        "content_type", "mimeType", "type",
    ]).lower()

    return any(x in maybe for x in ["audio", "voice", "ogg", "oga", "opus", "mp3", "m4a", "wav"])


def _voice_url_from_message(message: dict[str, Any]) -> str | None:
    for key in ["media_url", "file_url", "url", "fileUrl", "mediaUrl", "downloadUrl", "contentUri", "contentUrl"]:
        value = message.get(key)
        if value:
            return str(value)

    raw = message.get("raw_audio_item")
    if isinstance(raw, dict):
        nested_msg = _message_from_audio_item(raw)
        if nested_msg:
            return str(nested_msg.get("media_url") or "")

    return None


async def _download_voice_bytes(message: dict[str, Any]) -> tuple[bytes, str, str]:
    """Скачивает голосовое по URL из Wazzup.

    Если URL подписанный — хватит обычного GET.
    Если Wazzup требует авторизацию — добавляем Bearer/API headers, но только если ключ есть.
    """
    url = _voice_url_from_message(message)
    if not url:
        raise ValueError("voice media url not found")

    settings = get_settings()
    api_key = (
        getattr(settings, "wazzup_api_key", "")
        or getattr(settings, "wazzup_token", "")
        or getattr(settings, "wazzup_access_token", "")
        or ""
    )

    headers: dict[str, str] = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
        headers["X-API-KEY"] = str(api_key)

    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0)) as client:
        response = await client.get(url, headers=headers, follow_redirects=True)
        response.raise_for_status()

    content_type = response.headers.get("content-type") or str(message.get("content_type") or "")
    filename = str(message.get("filename") or "")

    if not filename or "." not in filename:
        ext = mimetypes.guess_extension(content_type.split(";")[0].strip()) or ".ogg"
        filename = f"voice{ext}"

    return response.content, filename, content_type


async def _transcribe_voice_message(message: dict[str, Any]) -> str:
    """Распознаёт голосовое максимально совместимо со старым кодом.

    Сначала пробуем существующую функцию transcribe_wazzup_voice(message).
    Если она не подходит под формат Wazzup — скачиваем файл и отправляем bytes в Whisper/OpenAI.
    """
    try:
        transcript = await transcribe_wazzup_voice(message, language="ru")
        text = str(getattr(transcript, "text", "") or "")
        if text.strip():
            return text.strip()
    except Exception as exc:
        state.log_event(str(message.get("chat_id") or "voice"), "voice_transcribe_wazzup_error", {"error": str(exc)[:1000]})

    data, filename, _content_type = await _download_voice_bytes(message)
    transcript = await transcribe_bytes(data, filename=filename, language="ru")
    return str(getattr(transcript, "text", "") or "").strip()


async def _build_answer_for_message(message: dict[str, Any]) -> str:
    chat_id = str(message["chat_id"])
    phone = str(message.get("phone") or chat_id)
    kind = str(message.get("kind") or "text")

    # Вне рабочего времени бот полностью молчит.
    if not is_bot_work_time():
        state.log_event(chat_id, "silent_outside_work_time", {"kind": kind})
        return ""

    if kind == "voice" or _message_has_voice_url(message):
        try:
            transcript_text = await _transcribe_voice_message(message)
            user_text = voice_text_for_bot(transcript_text)
            state.log_event(chat_id, "voice_transcribed", {"text": transcript_text[:500]})
        except Exception as exc:
            state.log_event(chat_id, "voice_transcription_failed_fallback_doctor", {"error": str(exc)[:1000]})
            return _guard_answer(chat_id, _voice_fallback_answer())
    else:
        user_text = str(message.get("text") or "")

    answer = await handle_message(chat_id=chat_id, phone=phone, user_text=user_text)
    return _guard_answer(chat_id, answer)


async def _send_answer_parts(
    *,
    chat_id: str,
    answer: str,
    chat_type: str,
    channel_id: str | None,
) -> None:
    """Отправляет ответ частями через разделитель ---.

    Пустые части не отправляем. Каждую часть дополнительно прогоняем через guard,
    чтобы даже после split не ушло обращение по имени или лишняя фраза.
    """
    parts = [p.strip() for p in (answer or "").split("---") if p.strip()]
    for part in parts:
        safe_part = _guard_answer(chat_id, part)
        if not safe_part:
            continue
        await send_text(
            chat_id=chat_id,
            text=safe_part,
            chat_type=chat_type,
            channel_id=channel_id,
        )


async def _debounced_process_and_send(message: dict[str, Any]) -> None:
    """Обрабатывает входящее сообщение в фоне.

    Webhook отвечает Wazzup сразу, а бот отвечает после debounce-паузы.
    Если клиент отправил несколько сообщений подряд, они объединяются в один текст.
    """
    chat_id = str(message["chat_id"])
    chat_type = str(message.get("chat_type") or "whatsapp")
    channel_id = message.get("channel_id") or None
    kind = str(message.get("kind") or "text")

    try:
        if not is_bot_work_time():
            state.log_event(chat_id, "silent_outside_work_time", {"kind": kind})
            return

        settings = get_settings()
        wait_seconds = max(0, int(getattr(settings, "message_debounce_seconds", 0) or 0))

        if wait_seconds > 0 and kind != "voice":
            batch_id = uuid.uuid4().hex
            state.append_pending_message(chat_id, batch_id, str(message.get("text") or ""))
            await asyncio.sleep(wait_seconds)

            if state.latest_pending_batch_id(chat_id) != batch_id:
                return

            combined_text = state.pop_pending_messages(chat_id)
            if not combined_text:
                return

            message = dict(message)
            message["text"] = combined_text

        answer = await _build_answer_for_message(message)
        if not answer:
            return

        await _send_answer_parts(
            chat_id=chat_id,
            answer=answer,
            chat_type=chat_type,
            channel_id=channel_id,
        )

    except Exception as exc:
        state.log_event(chat_id, "background_processing_error", {"error": str(exc)[:1000]})
        try:
            if kind == "voice" or _message_has_voice_url(message):
                fallback_text = _voice_fallback_answer()
            else:
                fallback_text = "Передам Ваш вопрос администратору, она ответит Вам в ближайшее время."

            fallback = _guard_answer(chat_id, fallback_text)
            await send_text(
                chat_id=chat_id,
                chat_type=chat_type,
                channel_id=channel_id,
                text=fallback,
            )
        except Exception:
            pass


@app.post("/webhook/wazzup")
async def wazzup_webhook(request: Request, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    settings = get_settings()
    if settings.webhook_secret:
        expected = f"Bearer {settings.webhook_secret}"
        if authorization != expected:
            raise HTTPException(status_code=401, detail="Bad webhook secret")

    payload = await request.json()
    messages = extract_incoming_messages(payload)

    # voice_fallback_extractor:
    # Если текущий wazzup.extract_incoming_messages не распознал голосовое,
    # аккуратно достаём audio/media из raw payload. Для обычного текста ничего не меняется.
    try:
        extra_voice_messages = _extract_voice_messages_from_payload(payload)
        existing_keys = {str(m.get("message_key") or "") for m in messages}
        existing_voice_urls = {
            str(m.get("media_url") or m.get("file_url") or m.get("url") or "")
            for m in messages
        }
        for vm in extra_voice_messages:
            vm_key = str(vm.get("message_key") or "")
            vm_url = str(vm.get("media_url") or "")
            if (vm_key and vm_key in existing_keys) or (vm_url and vm_url in existing_voice_urls):
                continue
            messages.append(vm)
    except Exception as exc:
        state.log_event("system", "voice_payload_extract_error", {"error": str(exc)[:1000]})

    accepted = 0
    skipped = 0

    for message in messages:
        chat_id = str(message["chat_id"])
        message_key = str(message.get("message_key") or "")

        if message_key and state.is_processed_message(message_key):
            skipped += 1
            continue

        if message_key:
            state.mark_processed_message(message_key, chat_id)

        asyncio.create_task(_debounced_process_and_send(message))
        accepted += 1

    return {"ok": True, "accepted": accepted, "skipped": skipped}


@app.post("/debug/chat")
async def debug_chat(data: dict[str, Any]) -> dict[str, Any]:
    chat_id = str(data.get("chat_id") or "test")
    phone = str(data.get("phone") or "77011234567")
    text = str(data.get("text") or "")
    force = bool(data.get("force") or False)

    if not force and not is_bot_work_time():
        answer = ""
    else:
        raw_answer = await handle_message(chat_id=chat_id, phone=phone, user_text=text)
        answer = _guard_answer(chat_id, raw_answer)

    return {
        "answer": answer,
        "session": state.get_session(chat_id),
        "bot_work_time_now": is_bot_work_time(),
    }


@app.post("/debug/reset")
async def debug_reset(data: dict[str, Any]) -> dict[str, Any]:
    chat_id = str(data.get("chat_id") or "test")
    state.reset_session(chat_id)
    return {"ok": True}


@app.post("/debug/voice")
async def debug_voice(request: Request) -> dict[str, Any]:
    filename = request.headers.get("x-filename") or "voice.ogg"
    phone = request.headers.get("x-phone") or "77011234567"
    chat_id = request.headers.get("x-chat-id") or "test_voice"

    data = await request.body()
    transcript = await transcribe_bytes(data, filename=filename, language="ru")
    raw_answer = await handle_message(
        chat_id=chat_id,
        phone=phone,
        user_text=voice_text_for_bot(transcript.text),
    )
    answer = _guard_answer(chat_id, raw_answer)

    return {"transcript": transcript.text, "answer": answer}


@app.post("/debug/voice-file")
async def debug_voice_file(
    file: UploadFile = File(...),
    phone: str = "77011234567",
    chat_id: str = "test_voice_file",
) -> dict[str, Any]:
    transcript = await transcribe_upload(file, language="ru")
    raw_answer = await handle_message(
        chat_id=chat_id,
        phone=phone,
        user_text=voice_text_for_bot(transcript.text),
    )
    answer = _guard_answer(chat_id, raw_answer)

    return {
        "filename": transcript.filename,
        "content_type": transcript.content_type,
        "transcript": transcript.text,
        "answer": answer,
    }


@app.get("/debug/crm-check")
async def debug_crm_check(date: str = "2026-06-12"):
    """Direct CRM connectivity check from Railway.

    This endpoint does NOT send anything to WhatsApp.
    It only calls crm.check_slots(date) and returns the raw result or error.
    """
    try:
        import crm
        from config import get_settings

        settings = get_settings()
        data = await crm.check_slots(date)

        return {
            "ok": True,
            "date": date,
            "crm_base_url": getattr(settings, "crm_base_url", ""),
            "slots_count": len(data.get("slots", []) or []),
            "availability_count": len(data.get("availability", []) or []),
            "data": data,
        }
    except Exception as exc:
        return {
            "ok": False,
            "date": date,
            "error_type": type(exc).__name__,
            "error": str(exc),
        }

