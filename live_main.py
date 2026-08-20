from __future__ import annotations

from typing import Any

import main as neuro
import state
from repair_observer import dispatch_new_lead_turn


_original_process_wazzup_message = neuro._process_wazzup_message


async def _process_wazzup_message_with_claude_observer(
    request: Any,
    payload: dict[str, Any],
    raw_msg: dict[str, Any],
    parse_meta: dict[str, Any],
    *,
    send_enabled: bool = True,
) -> dict[str, Any]:
    """Run the unchanged production Wazzup processor, then observe its final CRM state.

    The observer is strictly post-processing and fire-and-forget. Any observer
    failure is swallowed so it can never alter the patient response or Wazzup
    webhook result.
    """
    result = await _original_process_wazzup_message(
        request,
        payload,
        raw_msg,
        parse_meta,
        send_enabled=send_enabled,
    )
    try:
        message = neuro._normalize_wazzup_message(payload, raw_msg)
        if not message.get("is_incoming"):
            return result
        chat_id = str(message.get("chat_id") or "").strip()
        if not chat_id:
            return result
        session = state.get_session(chat_id)
        answer = str((result or {}).get("answer") or session.get("last_sent_answer") or session.get("final_answer_preview") or "")
        dispatch_new_lead_turn(
            chat_id=chat_id,
            user_text=str(message.get("text") or ""),
            session=session,
            answer=answer,
        )
    except Exception:
        pass
    return result


# handle_wazzup_webhook resolves this module-global function at runtime, so all
# existing routes/contracts stay identical while the post-processing observer
# gets the exact completed real Wazzup turn.
neuro._process_wazzup_message = _process_wazzup_message_with_claude_observer

app = neuro.app
