from __future__ import annotations

import asyncio
import uuid
from typing import Any

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


async def _build_answer_for_message(message: dict[str, Any]) -> str:
    chat_id = str(message["chat_id"])
    phone = str(message.get("phone") or chat_id)
    kind = str(message.get("kind") or "text")

    # Вне рабочего времени бот полностью молчит.
    if not is_bot_work_time():
        state.log_event(chat_id, "silent_outside_work_time", {"kind": kind})
        return ""

    if kind == "voice":
        transcript = await transcribe_wazzup_voice(message, language="ru")
        user_text = voice_text_for_bot(transcript.text)
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
            fallback = _guard_answer(
                chat_id,
                "Передам Ваш вопрос администратору, она ответит Вам в ближайшее время.",
            )
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
