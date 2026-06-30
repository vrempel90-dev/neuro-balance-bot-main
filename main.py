from __future__ import annotations

import asyncio
import uuid
import mimetypes
import re
from typing import Any

import httpx

from fastapi import FastAPI, Header, HTTPException, Request, UploadFile, File

import state
from config import get_settings
from dialog import handle_message
from guards import GuardDecision, should_auto_reply
from ai import humanize_reply_with_openai
from schedule import astana_now, is_bot_work_time
from voice import transcribe_wazzup_voice, transcribe_bytes, transcribe_upload, voice_text_for_bot
from wazzup import extract_incoming_messages, is_audio_message_payload, send_text

try:
    from strict_prompt_guard import enforce_prompt_only
except Exception:
    def enforce_prompt_only(answer: str, session: dict[str, Any] | None = None) -> str:
        return answer or ""


app = FastAPI(title="Neuro Balance Hybrid WhatsApp Booking Bot")


def _preview(value: Any, limit: int = 120) -> str:
    return str(value or "").replace("\n", " ").strip()[:limit]


def _dialog_debug(session: dict[str, Any], answer: str = "") -> dict[str, Any]:
    decision = session.get("guard_decision") if isinstance(session.get("guard_decision"), dict) else {}
    return {
        "source": session.get("source") or "",
        "local_time": session.get("local_time") or astana_now().isoformat(),
        "bot_work_time_now": is_bot_work_time(),
        "working_hours_allowed": bool(session.get("working_hours_allowed", is_bot_work_time())),
        "openai_used": bool(session.get("openai_used")),
        "openai_model": session.get("openai_model") or "",
        "openai_skip_reason": session.get("openai_skip_reason") or "",
        "openai_guard_failed": bool(session.get("openai_guard_failed")),
        "openai_brain_used": bool(session.get("openai_brain_used")),
        "openai_brain_intent": session.get("openai_brain_intent") or "",
        "openai_brain_action": session.get("openai_brain_action") or "",
        "openai_brain_needs_python_tool": session.get("openai_brain_needs_python_tool") or "",
        "openai_brain_extracted": session.get("openai_brain_extracted") or {},
        "openai_brain_guard_failed": bool(session.get("openai_brain_guard_failed")),
        "openai_brain_guard_reason": session.get("openai_brain_guard_reason") or "",
        "openai_brain_skip_reason": session.get("openai_brain_skip_reason") or "",
        "openai_brain_fallback_used": bool(session.get("openai_brain_fallback_used")),
        "openai_brain_model": session.get("openai_brain_model") or getattr(get_settings(), "ai_brain_model", ""),
        "openai_brain_temperature": session.get("openai_brain_temperature") if session.get("openai_brain_temperature") is not None else getattr(get_settings(), "ai_brain_temperature", None),
        "openai_error_type": session.get("openai_error_type") or "",
        "openai_error_message_preview": session.get("openai_error_message_preview") or "",
        "openai_config_missing_detail": session.get("openai_config_missing_detail") or {},
        "openai_missing_keys": session.get("openai_missing_keys") or [],
        "openai_disabled_flags": session.get("openai_disabled_flags") or [],
        "humanize_skipped_because_brain_valid": bool(session.get("humanize_skipped_because_brain_valid")),
        "humanize_fallback_used": bool(session.get("humanize_fallback_used")),
        "llm_blocked": bool(session.get("llm_blocked")),
        "llm_repaired": bool(session.get("llm_repaired")),
        "repair_reason": session.get("repair_reason") or "",
        "repaired_step": session.get("repaired_step") or "",
        "base_answer_preview": session.get("base_answer_preview") or _preview(answer, 160),
        "final_answer_preview": session.get("final_answer_preview") or _preview(answer, 160),
        "gate_reason": session.get("gate_reason") or "",
        "no_reply_reason": session.get("no_reply_reason") or "",
        "crm_called": bool(session.get("crm_called")),
        "wazzup_send_called": bool(session.get("wazzup_send_called")),
        "should_send_wazzup": bool(decision.get("should_send_wazzup", False)),
        "answer_empty": not bool(answer),
        "step": session.get("step") or session.get("current_step") or "",
        "ai_muted": bool(session.get("ai_muted")),
        "manual_takeover": bool(session.get("manual_takeover") or session.get("manual_admin_intervention")),
        "ai_lead_started": bool(session.get("ai_lead_started")),
        "working_hours_bypassed_by_force": bool(session.get("working_hours_bypassed_by_force")),
        "state_before_step": session.get("state_before_step") or "",
        "state_after_step": session.get("state_after_step") or session.get("step") or "",
        "brain_allowed": bool(session.get("brain_allowed")),
        "brain_skip_reason": session.get("brain_skip_reason") or session.get("openai_brain_skip_reason") or "",
        "fallback_reason": session.get("fallback_reason") or "",
        "state_repaired": bool(session.get("state_repaired")),
        "state_repair_reason": session.get("state_repair_reason") or "",
        "state_before": session.get("state_before_step") or "",
        "state_after": session.get("state_after_step") or session.get("step") or "",
        "next_step_reason": session.get("state_repair_reason") or session.get("fallback_reason") or session.get("gate_reason") or "",
        "entity_extraction": session.get("openai_brain_extracted") or {},
        "faq_type": session.get("last_answered_faq_type") or "",
        "crm_result": session.get("crm_result") or session.get("appointment_status") or "",
        "booking_ready": bool(session.get("booking_ready")),
        "booking_confirmed": bool(session.get("booking_confirmed")),
        "final_repair_reason": session.get("repair_reason") or session.get("fallback_reason") or "",
        "answer_source": session.get("answer_source") or ("openai" if session.get("openai_used") else "python"),
        "last_slots_count": len(session.get("last_slots") or []),
        "selected_slot": session.get("selected_slot") or {},
        "selected_doctor_login": session.get("selected_doctor_login") or "",
        "selected_doctor_name": session.get("selected_doctor_name") or "",
    }


def _log_dialog_result(chat_id: str, phone: str, answer: str) -> None:
    session = state.get_session(chat_id)
    debug = _dialog_debug(session, answer)
    state.log_event(chat_id, "dialog_result", {
        "phone": phone,
        "answer_empty": debug["answer_empty"],
        "answer_preview": _preview(answer, 160),
        **{k: debug[k] for k in ("source", "local_time", "bot_work_time_now", "working_hours_allowed", "step", "gate_reason", "no_reply_reason", "ai_muted", "manual_takeover", "ai_lead_started", "openai_used", "openai_skip_reason")},
    })
    if not answer:
        state.log_event(chat_id, "bot_no_reply", {
            "phone": phone,
            **{k: debug[k] for k in ("no_reply_reason", "gate_reason", "step", "ai_muted", "manual_takeover")},
        })


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


_ALLOWED_HUMANIZE_STEPS = {"start", "complaint", "age", "contraindications", "date", "time", "name"}
_ALLOWED_HUMANIZE_GATE_REASONS = {"new_lead", "new_lead_like_message", "active_ai_lead", "active_conversation_reply"}

def _set_openai_debug(session: dict[str, Any], debug: dict[str, Any], base_answer: str, final_answer: str) -> None:
    session["openai_used"] = bool(debug.get("openai_used"))
    session["openai_model"] = str(debug.get("openai_model") or "")
    session["openai_skip_reason"] = str(debug.get("openai_skip_reason") or "")
    session["openai_guard_failed"] = bool(debug.get("openai_guard_failed"))
    session["openai_error_type"] = str(debug.get("openai_error_type") or "")
    session["openai_error_message_preview"] = str(debug.get("openai_error_message_preview") or "")
    session["openai_error_detail"] = debug.get("openai_error_detail") or {}
    session["openai_config_missing_detail"] = debug.get("openai_config_missing_detail") or {}
    session["openai_missing_keys"] = debug.get("openai_missing_keys") or []
    session["openai_disabled_flags"] = debug.get("openai_disabled_flags") or []
    if "humanize_fallback_used" in debug:
        session["humanize_fallback_used"] = bool(debug.get("humanize_fallback_used"))
    session["base_answer_preview"] = str(debug.get("base_answer_preview") or _preview(base_answer, 160))
    session["final_answer_preview"] = str(debug.get("final_answer_preview") or _preview(final_answer, 160))


def _humanize_skip_reason(session: dict[str, Any], answer: str, *, voice_ignored: bool = False) -> str:
    if not (answer or "").strip():
        return "empty_answer"
    if voice_ignored or session.get("last_ignored_message_type") in {"voice", "audio"}:
        return "handoff"
    step = str(session.get("step") or session.get("current_step") or "start")
    if step in {"booked", "confirmed", "done"} or session.get("booked") or session.get("ai_muted"):
        return "booked_or_muted"
    if session.get("manual_takeover") or session.get("manual_admin_intervention") or session.get("do_not_reply"):
        return "handoff"
    if session.get("refund_claim_admin_required") or session.get("gate_reason") == "refund_claim_admin_required":
        return "refund_or_claim"
    if session.get("old_chat_ai_disabled") or session.get("gate_reason") == "old_chat_ai_disabled":
        return "handoff"
    if step in {"stopped"} or session.get("hard_contraindication_stop") or session.get("contraindication_hard_stop"):
        return "hard_stop"
    if step not in _ALLOWED_HUMANIZE_STEPS:
        return "not_allowed_step"
    gate_reason = str(session.get("gate_reason") or "")
    if not (session.get("ai_lead_started") is True or gate_reason in _ALLOWED_HUMANIZE_GATE_REASONS):
        return "not_ai_lead"
    return ""


async def _maybe_humanize_answer(chat_id: str, user_text: str, base_answer: str, *, voice_ignored: bool = False) -> str:
    session = _get_session_safe(chat_id)
    session["chat_id"] = chat_id
    if session.get("openai_brain_used") and (base_answer or "").strip():
        session["humanize_skipped_because_brain_valid"] = True
        session["humanize_fallback_used"] = False
        debug = {
            "openai_used": True,
            "openai_model": session.get("openai_brain_model") or getattr(get_settings(), "ai_brain_model", ""),
            "openai_skip_reason": "",
            "openai_guard_failed": False,
            "base_answer_preview": _preview(base_answer, 160),
            "final_answer_preview": _preview(base_answer, 160),
            "humanize_fallback_used": False,
        }
        _set_openai_debug(session, debug, base_answer, base_answer)
        state.save_session(chat_id, session)
        state.log_event(chat_id, "humanize_skipped_because_brain_valid", {
            "chat_id": chat_id,
            "openai_brain_model": session.get("openai_brain_model") or getattr(get_settings(), "ai_brain_model", ""),
            "openai_brain_temperature": session.get("openai_brain_temperature") if session.get("openai_brain_temperature") is not None else getattr(get_settings(), "ai_brain_temperature", None),
            "openai_brain_used": True,
            "humanize_skipped_because_brain_valid": True,
            "humanize_fallback_used": False,
        })
        if not getattr(get_settings(), "openai_humanize_replies", True):
            state.log_event(chat_id, "humanize_skipped", {
                "chat_id": chat_id,
                "reason": "disabled",
                "step": session.get("step") or session.get("current_step") or "",
                "disabled_flags": ["OPENAI_HUMANIZE_REPLIES=false"],
            })
        return base_answer
    reason = _humanize_skip_reason(session, base_answer, voice_ignored=voice_ignored)
    if reason:
        debug = {
            "openai_used": False,
            "openai_model": getattr(get_settings(), "openai_model", ""),
            "openai_skip_reason": reason,
            "openai_guard_failed": False,
            "base_answer_preview": _preview(base_answer, 160),
            "final_answer_preview": _preview(base_answer, 160),
        }
        _set_openai_debug(session, debug, base_answer, base_answer)
        state.save_session(chat_id, session)
        state.log_event(chat_id, "openai_skipped", {"chat_id": chat_id, "reason": reason, "step": session.get("step") or session.get("current_step") or ""})
        return base_answer

    session["humanize_skipped_because_brain_valid"] = False
    session["humanize_fallback_used"] = True
    state.log_event(chat_id, "humanize_fallback_used", {"chat_id": chat_id, "reason": session.get("openai_brain_skip_reason") or session.get("openai_skip_reason") or "rule_based", "humanize_fallback_used": True})
    final_answer, debug = await humanize_reply_with_openai(base_answer=base_answer, user_text=user_text, session=session)
    debug["humanize_fallback_used"] = True
    _set_openai_debug(session, debug, base_answer, final_answer)
    state.save_session(chat_id, session)
    if not debug.get("openai_used"):
        if debug.get("openai_config_missing_detail"):
            state.log_event(chat_id, "openai_config_missing_detail", debug["openai_config_missing_detail"])
        disabled_flags = debug.get("openai_disabled_flags") or []
        missing_keys = debug.get("openai_missing_keys") or []
        payload = {"chat_id": chat_id, "reason": debug.get("openai_skip_reason") or "config_missing", "step": session.get("step") or session.get("current_step") or "", "missing_keys": missing_keys, "disabled_flags": disabled_flags}
        if disabled_flags == ["OPENAI_HUMANIZE_REPLIES=false"] and not missing_keys:
            state.log_event(chat_id, "humanize_skipped", {**payload, "reason": "disabled"})
        else:
            state.log_event(chat_id, "openai_skipped", payload)
    return final_answer



def _mark_working_hours_disabled(
    *,
    chat_id: str,
    phone: str = "",
    source: str,
    force: bool = False,
    kind: str = "text",
    text: str = "",
) -> None:
    """Persist and log the daytime silence decision before any AI/CRM path runs."""
    session = _get_session_safe(chat_id)
    session["source"] = source
    session["local_time"] = astana_now().isoformat()
    session["working_hours_allowed"] = False
    session["no_reply_reason"] = "working_hours_ai_disabled"
    session["openai_used"] = False
    session["openai_brain_used"] = False
    session["openai_brain_skip_reason"] = "working_hours_ai_disabled"
    session["working_hours_bypassed_by_force"] = False
    state.save_session(chat_id, session)
    state.log_event(chat_id, "working_hours_blocked", {
        "chat_id": chat_id,
        "phone": phone,
        "current_time_astana": astana_now().isoformat(),
        "source": source,
        "force": force,
        "kind": kind,
        "text_preview": _preview(text, 120),
    })


def _mark_bot_auto_reply_disabled(*, chat_id: str, phone: str = "", source: str, force: bool = False, kind: str = "text", text: str = "") -> None:
    session = _get_session_safe(chat_id)
    session["source"] = source
    session["local_time"] = astana_now().isoformat()
    session["working_hours_allowed"] = is_bot_work_time()
    session["no_reply_reason"] = "bot_auto_reply_disabled"
    session["openai_used"] = False
    session["openai_brain_used"] = False
    session["openai_brain_skip_reason"] = "bot_auto_reply_disabled"
    state.save_session(chat_id, session)
    state.log_event(chat_id, "bot_auto_reply_disabled", {
        "chat_id": chat_id, "phone": phone, "source": source, "force": force, "kind": kind,
        "text_preview": _preview(text, 120), "current_time_astana": astana_now().isoformat(),
    })



def _apply_guard_block(
    decision: GuardDecision,
    *,
    chat_id: str,
    phone: str = "",
    source: str,
    force: bool = False,
    kind: str = "text",
    text: str = "",
) -> None:
    session = _get_session_safe(chat_id)
    session["source"] = source
    session["local_time"] = astana_now().isoformat()
    session["working_hours_allowed"] = is_bot_work_time() or bool(force)
    session["no_reply_reason"] = decision.no_reply_reason
    session["openai_used"] = False
    session["openai_brain_used"] = False
    session["openai_brain_skip_reason"] = decision.no_reply_reason
    session["crm_called"] = False
    session["wazzup_send_called"] = False
    session["guard_decision"] = decision.to_dict()
    state.save_session(chat_id, session)
    event = "working_hours_blocked" if decision.no_reply_reason == "working_hours_ai_disabled" else decision.no_reply_reason
    state.log_event(chat_id, event or "auto_reply_blocked", {
        "chat_id": chat_id, "phone": phone, "source": source, "force": force, "kind": kind,
        "text_preview": _preview(text, 120), "current_time_astana": astana_now().isoformat(),
        "openai_used": False, "openai_brain_used": False, "crm_called": False, "wazzup_send_called": False,
    })


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

    state.log_event(chat_id, "incoming_message_received", {"phone": phone, "kind": kind, "source": str(message.get("source") or "wazzup"), "text_preview": _preview(message.get("text"), 120)})

    source = str(message.get("source") or "wazzup")
    if not bool(getattr(get_settings(), "bot_auto_reply_enabled", True)):
        _mark_bot_auto_reply_disabled(chat_id=chat_id, phone=phone, source=source, force=False, kind=kind, text=str(message.get("text") or ""))
        return ""
    if not is_bot_work_time():
        _mark_working_hours_disabled(chat_id=chat_id, phone=phone, source=source, force=False, kind=kind, text=str(message.get("text") or ""))
        return ""
    pre_session = _get_session_safe(chat_id)
    decision = should_auto_reply(message, pre_session, source=source, force=False, now=astana_now())
    # Tests and deployments patch main.is_bot_work_time as the public work-hours seam.
    if not decision.allowed and decision.no_reply_reason == "working_hours_ai_disabled" and is_bot_work_time():
        decision = GuardDecision(True, "", True, True, source in {"wazzup", "crm"})
    if not decision.allowed:
        _apply_guard_block(decision, chat_id=chat_id, phone=phone, source=source, force=False, kind=kind, text=str(message.get("text") or ""))
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

    session = _get_session_safe(chat_id)
    session["source"] = source
    session["local_time"] = astana_now().isoformat()
    session["working_hours_allowed"] = True
    session["guard_decision"] = decision.to_dict()
    session["crm_called"] = False
    session["wazzup_send_called"] = False
    state.save_session(chat_id, session)
    state.log_event(chat_id, "dialog_start", {"phone": phone, "text_preview": _preview(user_text, 120), "force": False, "source": message.get("source") or "wazzup"})
    base_answer = await handle_message(chat_id=chat_id, phone=phone, user_text=user_text)
    answer = await _maybe_humanize_answer(chat_id, user_text, base_answer)
    answer = _guard_answer(chat_id, answer)
    _log_dialog_result(chat_id, phone, answer)
    return answer


async def _send_answer_parts(
    *,
    chat_id: str,
    answer: str,
    chat_type: str,
    channel_id: str | None,
    phone: str = "",
) -> None:
    """Отправляет один dialog_result одним сообщением Wazzup."""
    safe_text = str(answer or "").replace("---", "\n")
    safe_text = re.sub(r"(?i)Вижу Ваш запрос|Ваш запрос принят", "", safe_text)
    safe_text = re.sub(r"\n{3,}", "\n\n", safe_text).strip()
    safe_text = _guard_answer(chat_id, safe_text)
    if not safe_text:
        return
    state.log_event(chat_id, "wazzup_send_attempt", {"phone": phone, "answer_preview": _preview(safe_text, 160)})
    try:
        decision_data = _get_session_safe(chat_id).get("guard_decision")
        if isinstance(decision_data, dict) and not bool(decision_data.get("should_send_wazzup", True)):
            state.log_event(chat_id, "wazzup_send_blocked", {"phone": phone, "reason": _get_session_safe(chat_id).get("no_reply_reason") or "send_guard"})
            return
        sess = _get_session_safe(chat_id)
        sess["wazzup_send_called"] = True
        state.save_session(chat_id, sess)
        result = await send_text(chat_id=chat_id, text=safe_text, chat_type=chat_type, channel_id=channel_id)
        state.log_event(chat_id, "wazzup_send_result", {"phone": phone, "ok": True, "status_code": result.get("status_code")})
    except Exception as exc:
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
        state.log_event(chat_id, "wazzup_send_result", {"phone": phone, "ok": False, "status_code": status_code, "error_preview": _preview(exc, 300)})
        raise


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
        session_after = _get_session_safe(chat_id)
        guard_decision = session_after.get("guard_decision") if isinstance(session_after.get("guard_decision"), dict) else {}
        if not answer or not bool(guard_decision.get("should_send_wazzup", True)):
            state.log_event(chat_id, "wazzup_send_blocked", {"phone": str(message.get("phone") or ""), "reason": session_after.get("no_reply_reason") or "empty_answer"})
            return

        await _send_answer_parts(
            chat_id=chat_id,
            answer=answer,
            chat_type=chat_type,
            channel_id=channel_id,
            phone=str(message.get("phone") or ""),
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


def _dig_any(obj: Any, path: str) -> Any:
    cur = obj
    for key in path.split("."):
        if isinstance(cur, dict) and key in cur:
            cur = cur[key]
        else:
            return None
    return cur


def _raw_wazzup_messages(payload: dict[str, Any]) -> list[dict[str, Any]]:
    raw = payload.get("messages") or payload.get("message") or []
    if isinstance(raw, dict):
        raw = [raw]
    return [m for m in raw if isinstance(m, dict)]



def _remember_ignored_voice_message(chat_id: str) -> None:
    session = _get_session_safe(chat_id)
    session["last_ignored_message_type"] = "voice"
    state.save_session(chat_id, session)


def _ignore_voice_message(chat_id: str, message_key: str, source: str) -> None:
    _remember_ignored_voice_message(chat_id)
    state.log_event(chat_id, "ignored_voice_message", {"message_key": message_key, "source": source})


def _looks_like_outgoing_message(msg: dict[str, Any]) -> bool:
    return bool(
        msg.get("isEcho") is True
        or msg.get("fromMe") is True
        or str(msg.get("direction") or msg.get("status") or "").lower() in {"out", "outgoing", "sent"}
        or str(_dig_any(msg, "message.direction") or "").lower() in {"out", "outgoing", "sent"}
    )


def _looks_like_api_bot_message(msg: dict[str, Any], session: dict[str, Any]) -> bool:
    source_text = " ".join(
        str(x or "")
        for x in [
            msg.get("source"), msg.get("sender"), msg.get("author"), msg.get("createdBy"),
            msg.get("from"), msg.get("type"), _dig_any(msg, "message.source"),
            _dig_any(msg, "message.sender"), _dig_any(msg, "message.author"),
        ]
    ).lower()
    if any(marker in source_text for marker in ["api", "bot", "integration", "webhook"]):
        return True
    if msg.get("fromApi") is True or msg.get("isApi") is True or _dig_any(msg, "message.fromApi") is True:
        return True

    text = str(
        msg.get("text")
        or msg.get("body")
        or msg.get("content")
        or _dig_any(msg, "message.text")
        or _dig_any(msg, "message.body")
        or ""
    ).strip()
    last_bot = str(session.get("last_assistant_answer") or session.get("last_bot_answer") or "").strip()
    return bool(text and last_bot and text == last_bot)


def _mark_manual_admin_interventions(payload: dict[str, Any]) -> None:
    for msg in _raw_wazzup_messages(payload):
        if not _looks_like_outgoing_message(msg):
            continue
        chat_id = str(msg.get("chatId") or msg.get("chat_id") or msg.get("from") or "").strip()
        if not chat_id:
            continue
        session = _get_session_safe(chat_id)
        if _looks_like_api_bot_message(msg, session):
            continue
        session["manual_admin_intervention"] = True
        session["manual_takeover"] = True
        session["ai_muted"] = True
        state.save_session(chat_id, session)
        state.log_event(chat_id, "manual_admin_intervention_detected", {"source": "wazzup_outgoing"})


@app.post("/webhook/wazzup")
async def wazzup_webhook(request: Request, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    settings = get_settings()
    if settings.webhook_secret:
        expected = f"Bearer {settings.webhook_secret}"
        if authorization != expected:
            raise HTTPException(status_code=401, detail="Bad webhook secret")

    payload = await request.json()
    _mark_manual_admin_interventions(payload)
    ignored_raw_message_keys: set[str] = set()
    ignored_raw_count = 0
    for raw_msg in _raw_wazzup_messages(payload):
        if not is_audio_message_payload(raw_msg):
            continue
        chat_id = str(raw_msg.get("chatId") or raw_msg.get("chat_id") or raw_msg.get("from") or "").strip()
        if not chat_id:
            continue
        message_key = str(raw_msg.get("id") or raw_msg.get("messageId") or raw_msg.get("message_id") or "")
        if message_key and state.is_processed_message(message_key):
            continue
        if message_key:
            state.mark_processed_message(message_key, chat_id)
            ignored_raw_message_keys.add(message_key)
        _ignore_voice_message(chat_id, message_key, "raw_payload")
        ignored_raw_count += 1

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

    accepted = ignored_raw_count
    skipped = 0

    for message in messages:
        chat_id = str(message["chat_id"])
        state.log_event(chat_id, "wazzup_received", {
            "phone": str(message.get("phone") or chat_id),
            "message_type": str(message.get("kind") or "text"),
            "has_text": bool(message.get("text")),
            "text_preview": _preview(message.get("text"), 120),
            "source": "wazzup",
        })
        message_key = str(message.get("message_key") or "")

        if message_key and (message_key in ignored_raw_message_keys or state.is_processed_message(message_key)):
            skipped += 1
            continue

        if is_audio_message_payload(message):
            if message_key:
                state.mark_processed_message(message_key, chat_id)
            _ignore_voice_message(chat_id, message_key, "extracted_message")
            accepted += 1
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

    state.log_event(chat_id, "incoming_message_received", {"phone": phone, "kind": "text", "source": "debug", "text_preview": _preview(text, 120), "force": force})

    pre_session = state.get_session(chat_id)
    if not force and not is_bot_work_time():
        _mark_working_hours_disabled(chat_id=chat_id, phone=phone, source="debug", force=force, kind="text", text=text)
        answer = ""
        session = state.get_session(chat_id)
        return {
            "answer": answer,
            "session": session,
            "bot_work_time_now": is_bot_work_time(),
            "last_bot_question_type": session.get("last_bot_question_type"),
            "inferred_context_action": session.get("inferred_context_action"),
            "debug": _dialog_debug(session, answer),
        }
    decision = should_auto_reply(text, pre_session, source="debug", force=force, now=astana_now())
    if not decision.allowed and decision.no_reply_reason == "working_hours_ai_disabled" and is_bot_work_time():
        decision = GuardDecision(True, "", True, True, False)
    if not decision.allowed:
        _apply_guard_block(decision, chat_id=chat_id, phone=phone, source="debug", force=force, kind="text", text=text)
        answer = ""
    else:
        if force:
            session = state.get_session(chat_id)
            for key in ("manual_admin_intervention", "manual_takeover", "ai_muted"):
                session[key] = False
            session["working_hours_bypassed_by_force"] = not is_bot_work_time()
            state.save_session(chat_id, session)
        session = state.get_session(chat_id)
        session["source"] = "debug"
        session["local_time"] = astana_now().isoformat()
        session["working_hours_allowed"] = True
        session["guard_decision"] = decision.to_dict()
        state.save_session(chat_id, session)
        state.log_event(chat_id, "dialog_start", {"phone": phone, "text_preview": _preview(text, 120), "force": force, "source": "debug"})
        raw_answer = await handle_message(chat_id=chat_id, phone=phone, user_text=text)
        answer = await _maybe_humanize_answer(chat_id, text, raw_answer)
        answer = _guard_answer(chat_id, answer)
        _log_dialog_result(chat_id, phone, answer)
    session = state.get_session(chat_id)

    return {
        "answer": answer,
        "session": session,
        "bot_work_time_now": is_bot_work_time(),
        "last_bot_question_type": session.get("last_bot_question_type"),
        "inferred_context_action": session.get("inferred_context_action"),
        "used_history_context": session.get("used_history_context"),
        "no_reply_reason": session.get("no_reply_reason"),
        "current_step": session.get("step"),
        "prior_complaint_text": session.get("prior_complaint_text") or session.get("complaint"),
        "openai_config_missing_detail": session.get("openai_config_missing_detail") or {},
        "openai_missing_keys": session.get("openai_missing_keys") or [],
        "openai_disabled_flags": session.get("openai_disabled_flags") or [],
        "debug": _dialog_debug(session, answer),
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
