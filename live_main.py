from __future__ import annotations

import asyncio
import json
from typing import Any

import main as neuro
import state
from repair_observer import dispatch_new_lead_turn


app = neuro.app


async def _observe_real_wazzup_payload(payload: dict[str, Any]) -> None:
    try:
        settings = neuro.get_settings()
        delay = max(1, int(getattr(settings, "message_debounce_seconds", 0) or 0) + 2)
        await asyncio.sleep(delay)
        raw_messages = neuro._raw_wazzup_messages(payload) or [neuro._primary_payload_message(payload)]
        for raw in raw_messages:
            if not isinstance(raw, dict):
                continue
            message = neuro._normalize_wazzup_message(payload, raw)
            if not message.get("is_incoming"):
                continue
            chat_id = str(message.get("chat_id") or "").strip()
            if not chat_id:
                continue
            session = state.get_session(chat_id)
            answer = str(session.get("last_sent_answer") or session.get("final_answer_preview") or "")
            dispatch_new_lead_turn(
                chat_id=chat_id,
                user_text=str(message.get("text") or ""),
                session=session,
                answer=answer,
            )
    except Exception:
        # This observer must never affect the live Wazzup path.
        return


@app.middleware("http")
async def claude_live_new_lead_observer(request, call_next):
    payload: dict[str, Any] | None = None
    if request.method.upper() == "POST" and request.url.path in set(neuro.WAZZUP_WEBHOOK_PATHS):
        try:
            body = await request.body()
            parsed = json.loads(body.decode("utf-8")) if body else {}
            payload = parsed if isinstance(parsed, dict) else None
        except Exception:
            payload = None

    response = await call_next(request)

    if payload:
        try:
            asyncio.create_task(_observe_real_wazzup_payload(payload))
        except RuntimeError:
            pass
    return response
