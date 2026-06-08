from fastapi import FastAPI, Header, HTTPException, Request, UploadFile, File
import asyncio
import uuid
import state
from dialog import handle_message
from wazzup import extract_incoming_messages, send_text
from voice import transcribe_wazzup_voice, transcribe_bytes, transcribe_upload, voice_text_for_bot
from config import get_settings
from schedule import is_bot_work_time, daytime_handoff_text

app = FastAPI(title="Neuro Balance Hybrid WhatsApp Booking Bot")


@app.get("/")
def root() -> dict:
    return {"ok": True, "service": "Neuro Balance WhatsApp Bot", "docs": "/docs"}


@app.on_event("startup")
def startup() -> None:
    state.init_db()


@app.get("/health")
def health() -> dict:
    return {"ok": True, "mode": "gpt4o-mini-night-only", "bot_work_time_now": is_bot_work_time()}


async def _build_answer_for_message(message: dict[str, str]) -> str:
    chat_id = message["chat_id"]
    phone = message.get("phone") or chat_id
    kind = message.get("kind") or "text"

    # Вне рабочего времени бот полностью молчит.
    if not is_bot_work_time():
        state.log_event(chat_id, "silent_outside_work_time", {"kind": kind})
        return ""

    if kind == "voice":
        transcript = await transcribe_wazzup_voice(message, language="ru")
        user_text = voice_text_for_bot(transcript.text)
    else:
        user_text = message.get("text") or ""

    return await handle_message(chat_id=chat_id, phone=phone, user_text=user_text)


async def _debounced_process_and_send(message: dict[str, str]) -> None:
    """Обрабатывает входящее сообщение в фоне.

    Важно: webhook отвечает CRM/Wazzup сразу, а не ждёт 30 секунд.
    Если клиент отправил несколько сообщений подряд, мы ждём паузу и обрабатываем их одним текстом.
    """
    chat_id = message["chat_id"]
    chat_type = message.get("chat_type") or "whatsapp"
    channel_id = message.get("channel_id") or None
    kind = message.get("kind") or "text"

    try:
        if not is_bot_work_time():
            state.log_event(chat_id, "silent_outside_work_time", {"kind": kind})
            return

        settings = get_settings()
        wait_seconds = max(0, int(getattr(settings, "message_debounce_seconds", 0) or 0))

        if wait_seconds > 0 and kind != "voice":
            batch_id = uuid.uuid4().hex
            state.append_pending_message(chat_id, batch_id, message.get("text") or "")
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
        for part in [p.strip() for p in answer.split("---") if p.strip()]:
            await send_text(chat_id=chat_id, text=part, chat_type=chat_type, channel_id=channel_id)
    except Exception as exc:
        state.log_event(chat_id, "background_processing_error", {"error": str(exc)})
        try:
            await send_text(
                chat_id=chat_id,
                chat_type=chat_type,
                channel_id=channel_id,
                text="Передам ваш вопрос координатору, она ответит вам в ближайшее время.",
            )
        except Exception:
            pass


@app.post("/webhook/wazzup")
async def wazzup_webhook(request: Request, authorization: str | None = Header(default=None)) -> dict:
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
        chat_id = message["chat_id"]
        message_key = message.get("message_key") or ""
        if message_key and state.is_processed_message(message_key):
            skipped += 1
            continue
        if message_key:
            state.mark_processed_message(message_key, chat_id)
        asyncio.create_task(_debounced_process_and_send(message))
        accepted += 1

    # Возвращаем ответ сразу. Сам бот ответит клиенту в фоне после debounce-паузы.
    return {"ok": True, "accepted": accepted, "skipped": skipped}


@app.post("/debug/chat")
async def debug_chat(data: dict) -> dict:
    chat_id = str(data.get("chat_id") or "test")
    phone = str(data.get("phone") or "77011234567")
    text = str(data.get("text") or "")
    force = bool(data.get("force") or False)
    if not force and not is_bot_work_time():
        answer = ""
    else:
        answer = await handle_message(chat_id=chat_id, phone=phone, user_text=text)
    return {"answer": answer, "session": state.get_session(chat_id), "bot_work_time_now": is_bot_work_time()}


@app.post("/debug/reset")
async def debug_reset(data: dict) -> dict:
    chat_id = str(data.get("chat_id") or "test")
    state.reset_session(chat_id)
    return {"ok": True}


@app.post("/debug/voice")
async def debug_voice(request: Request) -> dict:
    filename = request.headers.get("x-filename") or "voice.ogg"
    phone = request.headers.get("x-phone") or "77011234567"
    chat_id = request.headers.get("x-chat-id") or "test_voice"
    data = await request.body()
    transcript = await transcribe_bytes(data, filename=filename, language="ru")
    answer = await handle_message(chat_id=chat_id, phone=phone, user_text=voice_text_for_bot(transcript.text))
    return {"transcript": transcript.text, "answer": answer}


@app.post("/debug/voice-file")
async def debug_voice_file(
    file: UploadFile = File(...),
    phone: str = "77011234567",
    chat_id: str = "test_voice_file",
) -> dict:
    transcript = await transcribe_upload(file, language="ru")
    answer = await handle_message(chat_id=chat_id, phone=phone, user_text=voice_text_for_bot(transcript.text))
    return {
        "filename": transcript.filename,
        "content_type": transcript.content_type,
        "transcript": transcript.text,
        "answer": answer,
    }
