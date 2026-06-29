# FINAL GOLD LIVE ADMIN LOGIC
# Принцип:
# 1) Сначала определяем намерение клиента.
# 2) Учитываем состояние диалога.
# 3) Не запускаем анкету, если клиент подтвердил визит/поблагодарил/спросил вопрос.
# 4) Если не уверены — уточняем намерение, а не придумываем.
# 5) Имя спрашиваем только в конце, после выбора слота.
#
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import bot_tools
import crm
import state
from ai import run_openai_dialog_brain
from config import get_settings

try:
    from phone import sanitize_kz_phone
except Exception:
    def sanitize_kz_phone(phone: str) -> str:
        digits = re.sub(r"\D+", "", phone or "")
        if digits.startswith("8") and len(digits) == 11:
            digits = "7" + digits[1:]
        return digits

try:
    from language_guard import detect_language as detect_message_language
except Exception:
    detect_message_language = None

try:
    from config import get_settings
except Exception:
    get_settings = None


OPENAI_BRAIN_ALLOWED_STEPS = {"start", "complaint", "age", "contraindications", "date", "time", "select_slot", "name"}
OPENAI_BRAIN_MULTI_ENTITY_STEPS = {"age", "contraindications", "date"}
OPENAI_BRAIN_ALLOWED_GATES = {"new_lead", "new_lead_like_message", "active_ai_lead", "active_conversation_reply"}
APPROVED_CONTRA_TERMS = {
    "кардиостимулятор", "дефибриллятор", "инсулиновая помпа", "помпа",
    "кохлеар", "кохлеарный имплант", "тромбофлебит", "тромбоз",
    "свертываем", "свёртываем", "онколог", "онкология", "рак",
    "подозрение на онкологию", "эпилеп", "судорог", "судороги",
    "декомпенсированный сахарный диабет", "декомпенсированн", "тиреотоксикоз",
    "беремен", "беременность", "температур", "орви", "грипп", "острая инфекц",
    "тяжелые проблемы с сердцем", "тяжёлые проблемы с сердцем",
    "тяжелые проблемы с дыханием", "тяжёлые проблемы с дыханием",
    "тяжелое психическое", "тяжёлое психическое",
    "коляск", "костыл", "лежач", "ограниченная подвижность",
}

UNKNOWN_CONTRA_SAFE_ANSWER_RU = (
    "Спасибо, поняла. Этого пункта нет в нашем основном чек-листе противопоказаний, "
    "но чтобы не ошибиться по медицинской части, передам информацию администратору "
    "для уточнения 🌿\n\nПодскажите, пожалуйста, других противопоказаний из списка нет?"
)


@dataclass
class RepairResult:
    answer: str
    step: str
    reason: str
    event: str = "llm_invalid_response_repaired"
    answer_empty: bool = False


def _reset_llm_repair_debug(session: dict[str, Any]) -> None:
    session["llm_blocked"] = False
    session["llm_repaired"] = False
    session["repair_reason"] = ""
    session["repaired_step"] = ""


def _reset_openai_brain_debug(session: dict[str, Any]) -> None:
    session["openai_brain_used"] = False
    session["openai_brain_intent"] = ""
    session["openai_brain_action"] = ""
    session["openai_brain_needs_python_tool"] = ""
    session["openai_brain_extracted"] = {}
    session["openai_brain_guard_failed"] = False
    session["openai_brain_guard_reason"] = ""
    session["openai_brain_skip_reason"] = ""
    session["openai_brain_fallback_used"] = False
    session["openai_brain_model"] = ""
    session["openai_brain_temperature"] = None
    session["openai_error_type"] = ""
    session["openai_error_message_preview"] = ""
    session["openai_error_detail"] = {}
    session["openai_config_missing_detail"] = {}
    session["openai_missing_keys"] = []
    session["openai_disabled_flags"] = []
    session["humanize_skipped_because_brain_valid"] = False
    session["humanize_fallback_used"] = False
    _reset_llm_repair_debug(session)


def _apply_openai_brain_debug(session: dict[str, Any], debug: dict[str, Any]) -> None:
    for key in (
        "openai_brain_used", "openai_brain_action", "openai_brain_needs_python_tool",
        "openai_brain_intent", "openai_brain_extracted", "openai_brain_guard_failed", "openai_brain_guard_reason",
        "openai_brain_skip_reason", "openai_brain_fallback_used", "openai_brain_model", "openai_brain_temperature",
        "openai_error_type", "openai_error_message_preview", "openai_error_detail",
        "openai_config_missing_detail", "openai_missing_keys", "openai_disabled_flags",
    ):
        if key in debug:
            session[key] = debug[key]


def _is_active_new_ai_request(session: dict[str, Any]) -> bool:
    step = str(session.get("step") or session.get("current_step") or "start")
    if step in {"booked", "confirmed", "done", "appointment_confirmed", "escalated", "stopped"} or session.get("booked"):
        return False
    if session.get("manual_takeover") or session.get("manual_admin_intervention") or session.get("ai_muted") or session.get("do_not_reply") or session.get("escalated"):
        return False
    if session.get("refund_claim_admin_required") or session.get("gate_reason") == "refund_claim_admin_required":
        return False
    if session.get("old_chat_ai_disabled") or session.get("old_chat") or session.get("gate_reason") == "old_chat_ai_disabled":
        return False
    if session.get("last_ignored_message_type") in {"voice", "audio"} or session.get("voice_ignored") or session.get("last_message_type") in {"voice", "audio"}:
        return False
    return session.get("ai_lead_started") is True or session.get("gate_reason") in OPENAI_BRAIN_ALLOWED_GATES


def build_safe_answer_for_current_state(session: dict[str, Any], user_text: str = "") -> tuple[str, str, dict[str, Any]]:
    """Central non-LLM fallback for every active state where silence is unsafe."""
    updates: dict[str, Any] = {}
    current_step = str(session.get("step") or "start")
    if session.get("booked"):
        updates["ai_muted"] = True
        session["ai_muted"] = True
        return "", "booked", updates

    if session.get("selected_time") and not session.get("patient_name"):
        session["step"] = "name"
        updates["step"] = "name"
        answer = _ask_name(session)
        _safe_log(str(session.get("chat_id") or "system"), "safe_answer_built", {"reason": "selected_time_without_name", "step": "name", "answer_preview": answer[:160]})
        return answer, "name", updates

    if current_step in {"time", "select_slot"} and session.get("last_slots"):
        session["step"] = "time"
        updates["step"] = "time"
        answer = _slot_times_answer(session)
        _safe_log(str(session.get("chat_id") or "system"), "safe_answer_built", {"reason": "time_unselected", "step": "time", "answer_preview": answer[:160]})
        return answer, "time", updates

    if current_step == "name" and session.get("selected_slot") and session.get("complaint"):
        session["step"] = "name"
        updates["step"] = "name"
        answer = _ask_name(session)
        _safe_log(str(session.get("chat_id") or "system"), "safe_answer_built", {"reason": "name_unknown", "step": "name", "answer_preview": answer[:160]})
        return answer, "name", updates

    if current_step == "contraindications":
        session["questionnaire_step"] = "contra"
        updates["questionnaire_step"] = "contra"
        answer = _ask_contra(session)
        _safe_log(str(session.get("chat_id") or "system"), "safe_answer_built", {"reason": "contraindications_not_passed", "step": "contraindications", "answer_preview": answer[:160]})
        return answer, "contraindications", updates

    if not session.get("complaint"):
        if _has_complaint(user_text) or _has_medical_complaint_text(user_text):
            complaint = (user_text or "").strip()
            session["complaint"] = complaint
            updates["complaint"] = complaint
            _record_complaint_tool(session, complaint, is_in_profile=True)
            session["step"] = "age"
            updates["step"] = "age"
            answer = _profile_confirm_and_ask_age(session, complaint)
            _safe_log(str(session.get("chat_id") or "system"), "safe_answer_built", {"reason": "profile_complaint_detected", "step": "age", "answer_preview": answer[:160]})
            return answer, "age", updates
        session["step"] = "complaint"
        updates["step"] = "complaint"
        answer = "Подскажите, пожалуйста, что Вас беспокоит?"
        _safe_log(str(session.get("chat_id") or "system"), "safe_answer_built", {"reason": "complaint_unknown", "step": "complaint", "answer_preview": answer[:160]})
        return answer, "complaint", updates

    if not session.get("age"):
        session["step"] = "age"
        updates["step"] = "age"
        answer = _ask_age(session)
        if _has_complaint(str(session.get("complaint") or user_text)) or _has_medical_complaint_text(str(session.get("complaint") or user_text)):
            answer = _profile_confirm_and_ask_age(session, str(session.get("complaint") or user_text))
        _safe_log(str(session.get("chat_id") or "system"), "safe_answer_built", {"reason": "age_unknown", "step": "age", "answer_preview": answer[:160]})
        return answer, "age", updates

    if session.get("contraindications_ok") is not True:
        session["step"] = "contraindications"
        session["questionnaire_step"] = "contra"
        updates.update({"step": "contraindications", "questionnaire_step": "contra"})
        answer = _ask_contra(session)
        _safe_log(str(session.get("chat_id") or "system"), "safe_answer_built", {"reason": "contraindications_not_passed", "step": "contraindications", "answer_preview": answer[:160]})
        return answer, "contraindications", updates

    if not session.get("preferred_date") and not session.get("last_slots"):
        session["step"] = "date"
        session["questionnaire_step"] = "date"
        updates.update({"step": "date", "questionnaire_step": "date"})
        answer = _ask_date(session)
        _safe_log(str(session.get("chat_id") or "system"), "safe_answer_built", {"reason": "date_unknown", "step": "date", "answer_preview": answer[:160]})
        return answer, "date", updates

    session["step"] = "complaint"
    updates["step"] = "complaint"
    answer = "Подскажите, пожалуйста, что Вас беспокоит?"
    _safe_log(str(session.get("chat_id") or "system"), "safe_answer_built", {"reason": "unknown_state", "step": "complaint", "answer_preview": answer[:160]})
    return answer, "complaint", updates




def _has_required_data_for_step(session: dict[str, Any], step: str) -> bool:
    step = str(step or "")
    if step == "age":
        try:
            return int(session.get("age") or 0) > 0
        except (TypeError, ValueError):
            return False
    if step == "contraindications":
        return session.get("contraindications_ok") is True
    if step in {"date", "preferred_time"}:
        return bool(session.get("preferred_date") or session.get("selected_date")) and not (session.get("crm_availability_empty") or session.get("crm_availability_error"))
    if step in {"time", "select_slot"}:
        return bool(session.get("selected_time"))
    if step == "name":
        return bool(session.get("patient_name"))
    if step == "complaint":
        return bool(session.get("complaint"))
    return False


def _next_required_step_after_collected_data(session: dict[str, Any]) -> str:
    try:
        age_ok = int(session.get("age") or 0) > 0
    except (TypeError, ValueError):
        age_ok = False
    if not session.get("complaint"):
        return "complaint"
    if not age_ok:
        return "age"
    if session.get("contraindications_ok") is not True:
        return "contraindications"
    if not (session.get("preferred_date") or session.get("selected_date")) or session.get("crm_availability_empty") or session.get("crm_availability_error"):
        return "date"
    if not session.get("selected_time"):
        return "time"
    if not session.get("patient_name"):
        return "name"
    return "book"


def _repair_forbidden_required_question(chat_id: str, session: dict[str, Any], answer: str) -> str:
    qtype = _classify_bot_question(answer)
    if not qtype:
        return answer
    step = "select_slot" if qtype == "time" else qtype
    if not _has_required_data_for_step(session, step):
        if session.get("last_required_step") == ("time" if step == "select_slot" else step) and int(session.get("last_required_question_count") or 0) >= 2:
            session["llm_blocked"] = True
            session["llm_repaired"] = True
            session["repair_reason"] = f"repeat_required_step_{step}_limit"
            session["step"] = "escalated"
            session["escalated"] = True
            _safe_log(chat_id, "repeat_required_question_limit_reached", {"blocked_step": step, "answer_preview": answer[:180]})
            return _tr(
                session,
                "Чтобы не повторяться и не запутать Вас, передам диалог администратору — он уточнит данные и поможет с записью 🌿",
                "Қайталамау және шатастырмау үшін диалогты әкімшіге жіберемін — ол деректерді нақтылап, жазылуға көмектеседі 🌿",
            )
        return answer
    next_step = _next_required_step_after_collected_data(session)
    session["llm_blocked"] = True
    session["llm_repaired"] = True
    session["repair_reason"] = f"repeat_required_step_{step}_blocked"
    _safe_log(chat_id, "repeat_required_question_blocked", {"blocked_step": step, "next_step": next_step, "answer_preview": answer[:180]})
    if next_step == "book":
        return ""
    if next_step == "time":
        session["step"] = "time"
        session["questionnaire_step"] = "time"
        return _mandatory_step_prompt(session, "time")
    session["step"] = next_step
    if next_step in {"date", "name", "contraindications"}:
        session["questionnaire_step"] = "contra" if next_step == "contraindications" else next_step
    return _mandatory_step_prompt(session, next_step)


def _repair_state_consistency(session: dict[str, Any], user_text: str = "") -> tuple[bool, str]:
    before = str(session.get("step") or "start")
    reason = ""
    if _repair_bad_patient_name(session):
        reason = "bad_patient_name_cleared"
    if before in {"escalated", "stopped", "done", "booked"}:
        if session.get("booked"):
            session["ai_muted"] = True
        session["state_repaired"] = False
        session["state_repair_reason"] = ""
        return False, ""
    if session.get("booked"):
        session["ai_muted"] = True
        return (before != "booked" or not session.get("ai_muted")), "booked_ai_muted"
    if reason:
        pass
    elif session.get("last_slots") and before in {"time", "select_slot"} and not session.get("selected_slot"):
        session["step"] = "time"; reason = ""  # consistent active slot-picking state
    elif (session.get("complaint") or _has_complaint(user_text)) and (_profile_status(user_text) == "profile" or session.get("profile_status") == "profile") and before in {"start", "complaint"} and not session.get("age"):
        if not session.get("complaint") and user_text:
            session["complaint"] = user_text
            _record_complaint_tool(session, user_text, is_in_profile=True)
        session["step"] = "age"; reason = "complaint_profile_without_age"
    elif before == "age" and session.get("age"):
        next_step = _next_required_step_after_collected_data(session)
        session["step"] = next_step if next_step != "book" else before; reason = "age_already_collected"
    elif before == "contraindications" and session.get("contraindications_ok") is True:
        session["step"] = "date"; session["questionnaire_step"] = "date"; reason = "contraindications_already_ok"
    elif before in {"age", "contraindications"} and session.get("age") and session.get("contraindications_ok") is None:
        session["step"] = "contraindications"; session["questionnaire_step"] = "contra"; reason = "age_without_contraindications"
    elif before in {"date", "preferred_time"} and session.get("contraindications_ok") is True and not session.get("preferred_date") and not session.get("last_slots"):
        session["step"] = "date"; session["questionnaire_step"] = "date"; reason = "contraindications_without_date"
    elif session.get("last_slots") and before not in {"time", "select_slot"} and not session.get("selected_slot"):
        session["step"] = "time"; reason = "slots_without_selected_slot"
    elif before in {"time", "select_slot"} and session.get("selected_time") and not session.get("patient_name"):
        session["step"] = "name"; session["questionnaire_step"] = "name"; reason = "selected_time_without_name"
    elif before == "name" and session.get("patient_name"):
        reason = "name_already_collected"
    elif before == "name" and (session.get("selected_slot") or session.get("selected_time")) and session.get("complaint") and not session.get("patient_name"):
        session["step"] = "name"; session["questionnaire_step"] = "name"; reason = "slot_without_name"
    repaired = bool(reason)
    if repaired:
        session["state_repaired"] = True
        session["state_repair_reason"] = reason
        _safe_log(str(session.get("chat_id") or "system"), "state_inconsistency_repaired", {"from_step": before, "to_step": session.get("step"), "reason": reason})
    else:
        session["state_repaired"] = False
        session["state_repair_reason"] = ""
    return repaired, reason


def _repair_unknown_current_step(session: dict[str, Any], user_text: str = "") -> tuple[str, str]:
    answer, step, _ = build_safe_answer_for_current_state(session, user_text)
    return step, answer


def _cleanup_contraindications_after_ok(session: dict[str, Any]) -> None:
    """Once contraindications are cleared, never let state point back to that gate."""
    if session.get("contraindications_ok") is not True:
        return
    if session.get("step") == "contraindications":
        session["step"] = "date"
        session["questionnaire_step"] = "date"
    if session.get("last_required_step") == "contraindications":
        session["last_required_step"] = ""
        session["last_required_question"] = ""
        session["last_required_question_count"] = 0
    if session.get("pending_step_after_faq") == "contraindications":
        session["pending_step_after_faq"] = ""


def _answer_contains_contraindications_question(answer: str) -> bool:
    low = _low(answer)
    if not low:
        return False
    contra = "противопоказ" in low or "қарсы көрсет" in low or "карсы корсет" in low
    checklist_terms = [
        "кардиостимулятор", "дефибриллятор", "инсулинов", "кохлеар", "беремен",
        "онколог", "эпилеп", "судорог", "тромбоз", "свёртываем", "свертываем",
        "тиреотоксикоз", "ограниченной подвиж", "коляска", "костыли",
    ]
    asks = "?" in low or any(x in low for x in ["нет", "уточ", "подскаж", "жоқ", "жок"])
    return (contra and asks) or sum(1 for term in checklist_terms if term in low) >= 2


def _repair_after_contraindications_ok(session: dict[str, Any]) -> tuple[str, str]:
    """Controller-owned continuation when Brain tries to reopen contraindications."""
    session["llm_blocked"] = True
    session["llm_repaired"] = True
    session["repair_reason"] = "contraindications_already_ok"
    if session.get("last_slots") and not session.get("selected_slot"):
        session["step"] = "time"
        session["questionnaire_step"] = "time"
        answer = "Какое окошко Вам удобно?"
    elif session.get("selected_slot") and not session.get("patient_name"):
        session["step"] = "name"
        session["questionnaire_step"] = "name"
        answer = "Подскажите, пожалуйста, Ваше имя для записи."
    elif not session.get("preferred_date"):
        session["step"] = "date"
        session["questionnaire_step"] = "date"
        answer = "Отлично 🌿 На какой день Вам удобно прийти?"
    else:
        session["step"] = "date"
        session["questionnaire_step"] = "date"
        answer = "На какой день Вам удобно прийти?"
    _cleanup_contraindications_after_ok(session)
    return str(session.get("step") or "date"), answer


def _apply_brain_contraindications_clear(
    session: dict[str, Any],
    user_text: str,
    decision: dict[str, Any],
) -> None:
    """Persist Brain-confirmed contraindication clearance before guards/repairs."""
    extracted = decision.get("extracted") if isinstance(decision.get("extracted"), dict) else {}
    if extracted.get("contraindications_clear") is not True:
        return
    _accept_no_contraindications(session, user_text or "нет")
    session["last_required_step"] = ""
    session["last_required_question"] = ""
    session["pending_step_after_faq"] = ""
    session["questionnaire_step"] = ""
    if str(session.get("step") or "") == "contraindications":
        session["step"] = "date"


def repair_invalid_llm_response(reason: str, session: dict[str, Any], user_text: str, attempted_reply: str, llm_decision: dict[str, Any]) -> RepairResult:
    """Block an invalid LLM decision and return a safe controller-owned answer."""
    normalized = {
        "slots_without_crm_tool": "slot_hallucination",
        "select_slot_without_last_slots": "slot_hallucination",
        "asked_name_too_early": "asked_name_too_early",
        "ask_name_without_slot": "asked_name_too_early",
        "extracted_name_before_slot": "asked_name_too_early",
        "ask_date_before_contra": "date_before_contraindications",
        "offered_date_before_contra": "date_before_contraindications",
        "show_slots_multi_entity_guard": "date_before_contraindications",
        "llm_attempted_booking": "book_without_selected_slot",
        "book_without_slot_or_name": "book_without_selected_slot",
        "llm_unknown_contraindication_blocked": "contraindication_false_hard_stop",
        "contra_term_question_not_hard_stop": "contraindication_false_hard_stop",
        "contra_question_missing": "contraindication_false_hard_stop",
    }.get(reason, reason or "unknown_invalid_llm")

    step_before = str(session.get("step") or "start")
    extracted = llm_decision.get("extracted") if isinstance(llm_decision.get("extracted"), dict) else {}
    if extracted.get("age") and not session.get("age"):
        try:
            session["age"] = int(extracted.get("age"))
        except Exception:
            pass
    if normalized == "contraindications_already_ok":
        repaired_step, answer = _repair_after_contraindications_ok(session)
        event = "llm_contraindications_already_ok_repaired"
    elif normalized == "need_date_after_contraindications_clear":
        _cleanup_contraindications_after_ok(session)
        session["step"] = "date"
        session["questionnaire_step"] = "date"
        answer = "Отлично 🌿 На какой день Вам удобно прийти?"
        event = "llm_need_date_after_contraindications_clear_repaired"
    elif normalized == "slot_hallucination":
        session["last_slots"] = []
        session["selected_slot"] = None
        session["step"] = "date"
        answer = "На выбранный день свободных окошек не нашла 🌿 Подскажите, пожалуйста, другой удобный день — проверю актуальное расписание."
        event = "llm_slot_hallucination_repaired"
    elif normalized == "doctor_hallucination":
        answer = "Врач зависит от выбранного дня и актуального расписания. Когда подберём свободное окошко, я покажу варианты по расписанию 🌿 Подскажите, пожалуйста, какой день Вам удобен?"
        event = "llm_doctor_hallucination_repaired"
    elif normalized == "duration_hallucination":
        answer = "Длительность зависит от назначенной процедуры и плана врача. На первичном приёме врач объяснит, сколько по времени это займёт именно в Вашем случае 🌿"
        if step_before == "time":
            answer += "\n\nКакое время из вариантов выше Вам удобно?"
        elif step_before == "contraindications":
            answer += "\n\nПодскажите, пожалуйста, противопоказаний из списка нет?"
        event = "llm_duration_hallucination_repaired"
    elif normalized == "contraindication_false_hard_stop":
        session["step"] = "contraindications"
        session["hard_stop"] = False
        session["hard_contraindication_stop"] = False
        session["contraindication_hard_stop"] = False
        session["manual_takeover"] = False
        answer = "Я уточняю это только для безопасности перед записью. Подскажите, пожалуйста, по списку противопоказаний ничего нет?"
        event = "llm_contraindication_hard_stop_repaired"
    elif normalized == "asked_name_too_early":
        if not session.get("complaint"):
            session["step"] = "complaint"; answer = "Подскажите, пожалуйста, что Вас беспокоит?"
        elif not session.get("age"):
            session["step"] = "age"; answer = "Сколько Вам лет?"
        elif session.get("contraindications_ok") is not True:
            session["step"] = "contraindications"; session["questionnaire_step"] = "contra"; answer = "Перед записью уточню важный момент по безопасности 🌿 Противопоказаний из списка нет?"
        else:
            session["step"] = "date"; session["questionnaire_step"] = "date"; answer = "На какой день Вам удобно прийти?"
        event = "llm_name_too_early_repaired"
    elif normalized == "date_before_contraindications":
        session["step"] = "contraindications"
        session["questionnaire_step"] = "contra"
        answer = "Перед тем как подобрать окошко, уточню важный момент по безопасности 🌿 Противопоказаний из списка нет?"
        event = "llm_date_before_contraindications_repaired"
    elif normalized == "book_without_selected_slot":
        session["step"] = "date"
        session["questionnaire_step"] = "date"
        answer = "Сначала нужно выбрать удобное окошко из актуального расписания 🌿 На какой день Вам удобно прийти?"
        event = "llm_book_without_selected_slot_repaired"
    else:
        normalized = "unknown_invalid_llm"
        _, answer = _repair_unknown_current_step(session, user_text)
        event = "llm_invalid_response_repaired"

    session["llm_blocked"] = True
    session["llm_repaired"] = True
    session["repair_reason"] = normalized
    session["repaired_step"] = str(session.get("step") or step_before)
    return RepairResult(answer=answer, step=session["repaired_step"], reason=normalized, event=event, answer_empty=False)


def repair_empty_active_reply(session: dict[str, Any], user_text: str, reason: str = "empty_active_reply") -> RepairResult:
    """Repair forbidden silence for an active new lead using controller-owned prompts."""
    step, answer = _repair_unknown_current_step(session, user_text)
    session["llm_blocked"] = True
    session["llm_repaired"] = True
    session["repair_reason"] = reason
    session["repaired_step"] = step
    session["no_reply_reason"] = ""
    return RepairResult(answer=answer, step=step, reason=reason, event="empty_active_reply_repaired", answer_empty=False)


def _log_llm_repair(chat_id: str, result: RepairResult, attempted_reply: str = "") -> None:
    payload = {
        "chat_id": chat_id,
        "llm_response_blocked": True,
        "llm_response_repaired": True,
        "repair_reason": result.reason,
        "repaired_step": result.step,
        "repaired_answer_preview": result.answer[:180],
        "attempted_answer_preview": (attempted_reply or "")[:180],
    }
    _safe_log(chat_id, "llm_response_blocked", payload)
    _safe_log(chat_id, "llm_response_repaired", payload)
    if result.reason == "slot_hallucination":
        _safe_log(chat_id, "llm_slot_hallucination_blocked", payload)
    _safe_log(chat_id, result.event, payload)


def _openai_brain_skip_reason(session: dict[str, Any], text: str) -> str:
    step = str(session.get("step") or session.get("current_step") or "start")
    if not (text or "").strip():
        return "empty_answer"
    if step in {"booked", "confirmed", "done", "appointment_confirmed", "escalated", "stopped"} or session.get("booked"):
        return "booked_or_handoff"
    if session.get("manual_takeover") or session.get("manual_admin_intervention") or session.get("ai_muted") or session.get("do_not_reply") or session.get("escalated"):
        return "manual_or_muted"
    if step == "name" and session.get("selected_slot"):
        return "python_owned_booking"
    if session.get("refund_claim_admin_required") or session.get("gate_reason") == "refund_claim_admin_required":
        return "refund_or_claim"
    if session.get("old_chat_ai_disabled") or session.get("old_chat") or session.get("gate_reason") == "old_chat_ai_disabled":
        return "old_chat_ai_disabled"
    if session.get("last_ignored_message_type") in {"voice", "audio"} or session.get("voice_ignored") or session.get("last_message_type") in {"voice", "audio"}:
        return "voice_or_audio"
    if session.get("hard_contraindication_stop") or session.get("contraindication_hard_stop"):
        return "hard_contraindication_stop"
    active_new_lead = session.get("ai_lead_started") is True or session.get("gate_reason") == "new_lead"
    if not active_new_lead:
        return "not_ai_lead"
    if step not in OPENAI_BRAIN_ALLOWED_STEPS:
        return "not_allowed_step"
    return ""


def validate_openai_dialog_decision(decision: dict, session: dict, user_text: str) -> tuple[bool, str]:
    action = str(decision.get("action") or "")
    tool = str(decision.get("needs_python_tool") or "none")
    extracted = decision.get("extracted") if isinstance(decision.get("extracted"), dict) else {}
    reply = str(decision.get("reply") or "")
    step = str(session.get("step") or "start")
    next_step = str(decision.get("next_step") or "")
    if session.get("contraindications_ok") is True:
        if next_step == "contraindications" or action == "ask_contraindications" or _answer_contains_contraindications_question(reply):
            return False, "contraindications_already_ok"
    if action == "show_slots" and tool != "check_slots":
        return False, "slots_without_crm_tool"
    if tool == "check_slots" and action != "show_slots":
        return False, "crm_tool_action_mismatch"
    if action == "select_slot" and not (session.get("last_slots") or []):
        return False, "select_slot_without_last_slots"
    if next_step == "booked" or tool == "book_appointment":
        return False, "llm_attempted_booking"
    if extracted.get("patient_name") and not session.get("selected_slot"):
        return False, "extracted_name_before_slot"
    red_flags = extracted.get("contraindication_red_flags")
    if isinstance(red_flags, list) and red_flags:
        bad = [str(x).lower() for x in red_flags if not any(term in str(x).lower() for term in APPROVED_CONTRA_TERMS)]
        if bad:
            return False, "unapproved_contraindication_terms"
        return False, "contraindication_red_flags"
    if extracted.get("contraindication_confirmed") and _looks_like_contra_term_question(_low(user_text)):
        return False, "contra_term_question_not_hard_stop"
    safety = decision.get("safety") if isinstance(decision.get("safety"), dict) else {}
    if action == "stop_contraindication" and not _contra_has_hard_stop(user_text):
        return False, "llm_unknown_contraindication_blocked"
    if _llm_claims_contraindication_stop(decision) and not _contra_has_hard_stop(user_text):
        return False, "llm_unknown_contraindication_blocked"
    if safety.get("unsafe_medical_claim") or safety.get("tries_to_book_without_rules"):
        return False, "unsafe_decision"
    if session.get("booked") or session.get("manual_takeover") or session.get("ai_muted") or session.get("refund_claim_admin_required") or session.get("old_chat_ai_disabled"):
        return False, "protected_flow"
    if action == "ask_name" and not session.get("selected_slot"):
        return False, "ask_name_without_slot"
    if action == "ask_date" and session.get("contraindications_ok") is not True and extracted.get("contraindications_clear") is not True and not _text_confirms_no_contra(session, user_text):
        return False, "ask_date_before_contra"
    if action == "show_slots" and not extracted.get("preferred_date_text"):
        if session.get("contraindications_ok") is True or extracted.get("contraindications_clear") is True:
            return False, "need_date_after_contraindications_clear"
        return False, "show_slots_without_date"
    if action == "show_slots" and (
        step not in OPENAI_BRAIN_MULTI_ENTITY_STEPS
        or session.get("ai_lead_started") is not True
        or (session.get("contraindications_ok") is not True and extracted.get("contraindications_clear") is not True and not _text_confirms_no_contra(session, user_text))
    ):
        return False, "show_slots_multi_entity_guard"
    if tool == "book_appointment" and not (session.get("selected_slot") and (session.get("patient_name") or extracted.get("patient_name"))):
        return False, "book_without_slot_or_name"
    low_reply = _low(reply)
    if any(bad in low_reply for bad in ["гарантируем", "точно вылечим", "100%", "полностью вылечим"]):
        return False, "unsafe_promise"
    if action == "ask_name" or any(x in low_reply for x in ["как вас зовут", "ваше имя", "имя для записи"]):
        if not session.get("selected_slot"):
            return False, "asked_name_too_early"
    if (
        re.search(r"\b\d+\s*(?:минут|час|часа|часов)\b", low_reply)
        or any(x in low_reply for x in ["около часа", "полчаса", "пол часа", "30 минут", "40 минут", "60 минут"])
    ) and any(x in low_reply for x in ["длитель", "процедур", "займ", "идет", "идёт"]):
        return False, "duration_hallucination"
    if any(x in _low(user_text) for x in ["доктор", "врач", "кто принимает", "какой специалист"]) and any(x in low_reply for x in ["доктор", "врач"]) and not (session.get("last_slots") or []):
        return False, "doctor_hallucination"
    if not (session.get("last_slots") or []) and (_TIME_PATTERN.search(reply) or any(p in low_reply for p in _FORBIDDEN_EMPTY_SLOT_PHRASES)):
        return False, "slot_hallucination"
    if action in {"ask_date", "show_slots"} and session.get("contraindications_ok") is not True and extracted.get("contraindications_clear") is not True and not _text_confirms_no_contra(session, user_text):
        return False, "offered_date_before_contra"
    if step == "contraindications" and session.get("contraindications_ok") is not True and action not in {"ask_date", "stop_contraindication", "handoff_admin", "fallback_rule_based", "no_reply", "answer_faq_and_continue"}:
        if "противопоказ" not in low_reply and "қарсы" not in low_reply:
            return False, "contra_question_missing"
    return True, ""


async def _try_openai_dialog_brain(chat_id: str, phone: str, session: dict[str, Any], text: str) -> str | None:
    settings = get_settings()
    brain_log_fields = {
        "openai_brain_model": getattr(settings, "ai_brain_model", ""),
        "openai_brain_temperature": getattr(settings, "ai_brain_temperature", None),
        "openai_brain_used": False,
        "humanize_skipped_because_brain_valid": False,
        "humanize_fallback_used": False,
    }
    session["openai_brain_model"] = brain_log_fields["openai_brain_model"]
    session["openai_brain_temperature"] = brain_log_fields["openai_brain_temperature"]
    reason = _openai_brain_skip_reason(session, text)
    session["brain_allowed"] = not bool(reason)
    session["brain_skip_reason"] = reason
    _safe_log(chat_id, "brain_allowed_decision", {"chat_id": chat_id, "step": session.get("step") or "start", "brain_allowed": not bool(reason), "brain_skip_reason": reason})
    if reason:
        session["openai_brain_skip_reason"] = reason
        _safe_log(chat_id, "brain_skipped", {"chat_id": chat_id, "reason": reason, "step": session.get("step") or "start"})
        _safe_log(chat_id, "openai_brain_skipped", {"chat_id": chat_id, "reason": reason, "step": session.get("step") or "start", "action": "", "needs_python_tool": "", "guard_failed": False, "guard_reason": "", "fallback_reason": reason, "extracted_preview": {}, **brain_log_fields})
        if _is_active_new_ai_request(session) and reason in {"not_allowed_step", "guard_failed", "empty_answer"}:
            repair_reason = "not_allowed_step_on_active_lead" if reason == "not_allowed_step" else "brain_skipped_unexpectedly"
            repair = repair_empty_active_reply(session, text, "empty_active_reply")
            session["fallback_reason"] = repair_reason
            _safe_log(chat_id, "fallback_used", {"chat_id": chat_id, "reason": repair_reason, "step": repair.step})
            _log_llm_repair(chat_id, repair, "")
            return _finalize(chat_id, session, repair.answer)
        return None
    _safe_log(chat_id, "openai_brain_called", {"chat_id": chat_id, "step": session.get("step") or "start", "action": "", "needs_python_tool": "", "guard_failed": False, "guard_reason": "", "extracted_preview": {}, **brain_log_fields})
    decision, debug = await run_openai_dialog_brain(user_text=text, session={**session, "chat_id": chat_id}, recent_history=_recent_history_for_brain(chat_id, session), available_slots=session.get("last_slots") or None)
    _apply_openai_brain_debug(session, debug)
    if decision.get("intent"):
        session["openai_brain_intent"] = decision.get("intent")
    _safe_log(chat_id, "openai_brain_decision", {"chat_id": chat_id, "step": session.get("step") or "start", "intent": decision.get("intent"), "action": decision.get("action"), "needs_python_tool": decision.get("needs_python_tool"), "guard_failed": False, "guard_reason": "", "extracted_preview": {k: v for k, v in (decision.get("extracted") or {}).items() if v not in (None, "", [], {})}, "openai_brain_model": session.get("openai_brain_model") or brain_log_fields["openai_brain_model"], "openai_brain_temperature": session.get("openai_brain_temperature"), "openai_brain_used": bool(debug.get("openai_brain_used")), "humanize_skipped_because_brain_valid": False, "humanize_fallback_used": bool(debug.get("openai_brain_fallback_used"))})
    if decision.get("action") == "fallback_rule_based":
        session["openai_brain_fallback_used"] = True
        _safe_log(chat_id, "openai_brain_fallback_rule_based", {"chat_id": chat_id, "step": session.get("step") or "start", "action": decision.get("action"), "needs_python_tool": decision.get("needs_python_tool"), "guard_failed": False, "guard_reason": "", "fallback_reason": debug.get("openai_brain_skip_reason") or "fallback", "extracted_preview": decision.get("extracted") or {}})
        if _is_active_new_ai_request(session) and debug.get("openai_brain_skip_reason") in {"empty_reply", "openai_error"}:
            rr = "openai_error" if debug.get("openai_brain_skip_reason") == "openai_error" else "empty_active_reply"
            if rr == "openai_error":
                _safe_log(chat_id, "brain_error", {"chat_id": chat_id, "step": session.get("step") or "start", "error": session.get("openai_error_message_preview") or "openai_error"})
            repair = repair_empty_active_reply(session, text, rr)
            session["fallback_reason"] = rr
            _safe_log(chat_id, "fallback_used", {"chat_id": chat_id, "reason": rr, "step": repair.step})
            _log_llm_repair(chat_id, repair, str(decision.get("reply") or ""))
            return _finalize(chat_id, session, repair.answer)
        if _is_active_new_ai_request(session) and debug.get("openai_brain_skip_reason") in {"invalid_json", "invalid_next_step", "invalid_action", "invalid_tool"}:
            repair = repair_invalid_llm_response("unknown_invalid_llm", session, text, str(decision.get("reply") or ""), decision)
            session["fallback_reason"] = debug.get("openai_brain_skip_reason") or "invalid_llm_json"
            _safe_log(chat_id, "fallback_used", {"chat_id": chat_id, "reason": session["fallback_reason"], "step": repair.step})
            _log_llm_repair(chat_id, repair, str(decision.get("reply") or ""))
            return _finalize(chat_id, session, repair.answer)
        return None
    _apply_brain_contraindications_clear(session, text, decision)
    ok, guard_reason = validate_openai_dialog_decision(decision, session, text)
    if not ok:
        session["openai_brain_guard_failed"] = True
        session["openai_brain_guard_reason"] = guard_reason
        session["openai_brain_fallback_used"] = True
        event_name = "llm_unknown_contraindication_blocked" if guard_reason == "llm_unknown_contraindication_blocked" else "openai_brain_guard_failed"
        _safe_log(chat_id, event_name, {"chat_id": chat_id, "step": session.get("step") or "start", "action": decision.get("action"), "needs_python_tool": decision.get("needs_python_tool"), "guard_failed": True, "guard_reason": guard_reason, "fallback_reason": guard_reason, "extracted_preview": {k: v for k, v in (decision.get("extracted") or {}).items() if v not in (None, "", [], {})}})
        if _is_active_new_ai_request(session):
            repair = repair_invalid_llm_response(guard_reason, session, text, str(decision.get("reply") or ""), decision)
            _log_llm_repair(chat_id, repair, str(decision.get("reply") or ""))
            return _finalize(chat_id, session, repair.answer)
        return None
    action = decision.get("action")
    extracted = decision.get("extracted") or {}
    if extracted.get("symptom_duration"):
        session["symptom_duration"] = str(extracted.get("symptom_duration") or "")
        facts = session.get("known_user_facts") if isinstance(session.get("known_user_facts"), dict) else {}
        facts["symptom_duration"] = session["symptom_duration"]
        session["known_user_facts"] = facts
    if decision.get("intent"):
        session["last_user_intent"] = str(decision.get("intent") or "")
    if extracted.get("time_preference"):
        session["time_preference"] = str(extracted.get("time_preference") or "")
    elif "не рано" in _low(text):
        session["time_preference"] = "не рано"
    reply = str(decision.get("reply") or "").strip()
    if action == "no_reply":
        session["openai_used"] = False
        return _no_reply(chat_id, session, "openai_brain_no_reply")
    if action == "ask_age":
        if extracted.get("complaint"):
            session["complaint"] = extracted.get("complaint")
            _record_complaint_tool(session, str(extracted.get("complaint")), is_in_profile=True)
        session["step"] = "age"
        return _finalize(chat_id, session, reply or _ask_age(session))
    if action == "ask_contraindications":
        if extracted.get("age"):
            session["age"] = int(extracted.get("age"))
        session["step"] = "contraindications"
        session["questionnaire_step"] = "contra"
        return _finalize(chat_id, session, reply or _ask_contra(session))
    if action == "ask_date":
        if extracted.get("contraindications_clear") is True or _text_confirms_no_contra(session, text):
            _accept_no_contraindications(session, text or "нет")
        session["step"] = "date"
        session["questionnaire_step"] = "date"
        return _finalize(chat_id, session, reply or _ask_date(session))
    if action == "show_slots" or decision.get("needs_python_tool") == "check_slots":
        if extracted.get("age"):
            try:
                session["age"] = int(extracted.get("age"))
            except Exception:
                pass
        if extracted.get("contraindications_clear") is True or _text_confirms_no_contra(session, text):
            _accept_no_contraindications(session, text or "нет")
            if extracted.get("contraindications_clear") is True:
                session["contraindications_raw"] = _extract_no_contra_raw(text)
        if session.get("contraindications_ok") is not True:
            session["step"] = "contraindications"
            session["questionnaire_step"] = "contra"
            return _finalize(chat_id, session, _ask_contra(session))
        date_text = str(extracted.get("preferred_date_text") or text)
        if date_text.strip():
            session["preferred_date_text"] = date_text.strip()
        date_iso = _parse_date(date_text) or _parse_date(text)
        if not date_iso:
            session["step"] = "date"
            return _finalize(chat_id, session, (reply + "\n\n" if reply else "") + _ask_date(session))
        if any(p in _low(text) for p in TIME_PREFERENCE_WORDS):
            session["time_preference"] = next((p for p in TIME_PREFERENCE_WORDS if p in _low(text)), "")
        slots_answer = await _show_slots(chat_id, session, date_iso)
        if session.get("step") != "time" or not (session.get("last_slots") or []):
            return _finalize(chat_id, session, slots_answer)
        return _finalize(chat_id, session, (reply + "\n\n" if reply else "") + slots_answer)
    if action == "select_slot":
        slots = session.get("last_slots") or []
        if not _explicit_slot_selection_text(text, slots):
            session.pop("slot_choice", None)
            session.pop("selected_time", None)
            session.pop("selected_slot", None)
            session["step"] = "time"
            return _finalize(chat_id, session, _slot_times_answer(session))
        choice = extracted.get("slot_choice")
        slot = None
        if isinstance(choice, int) and 1 <= choice <= len(slots):
            slot = slots[choice - 1]
        slot = slot or _select_slot(text, slots)
        if not slot:
            return _finalize(chat_id, session, _mandatory_step_prompt(session, "time"))
        _remember_selected_slot(session, slot)
        session["step"] = "name"
        session["questionnaire_step"] = "name"
        ask = _ask_name(session)
        return _finalize(chat_id, session, reply + ("\n\n" + ask if ask not in reply else ""))
    if action == "ask_name":
        session["step"] = "name"
        return _finalize(chat_id, session, reply or _ask_name(session))
    if action == "answer_faq_and_continue":
        pending = str(session.get("pending_step_after_faq") or session.get("last_required_step") or session.get("step") or "complaint")
        if session.get("contraindications_ok") is True and pending == "contraindications":
            _, prompt = _repair_after_contraindications_ok(session)
            pending = str(session.get("step") or "date")
            session["pending_step_after_faq"] = ""
            return _finalize(chat_id, session, reply + ("\n\n" + prompt if reply else prompt))
        if pending in {"time", "select_slot"} and session.get("last_slots"):
            session["step"] = "time"
        elif pending in {"complaint", "age", "contraindications", "date", "name"}:
            session["step"] = pending
        session["pending_step_after_faq"] = pending
        return _finalize(chat_id, session, reply + ("\n\n" + _mandatory_step_prompt(session, pending) if reply else ""))
    if action == "stop_contraindication":
        if not _contra_has_hard_stop(text):
            _safe_log(chat_id, "llm_unknown_contraindication_blocked", {"chat_id": chat_id, "step": session.get("step") or "start", "answer_preview": reply[:180]})
            session["contraindications_ok"] = False
            session["contraindications_verdict"] = "admin_contact"
            session["step"] = "contraindications"
            return _finalize(chat_id, session, _unknown_contra_safe_answer(session))
        session["step"] = "stopped"
        session["escalated"] = True
        return _finalize(chat_id, session, reply or _stop_booking_text(session, "contra"))
    if action == "handoff_admin":
        session["step"] = "escalated"
        session["manual_takeover"] = True
        session["escalated"] = True
        return _finalize(chat_id, session, reply or _crm_fallback_answer(session))
    return None


def _multi_entity_safe_date_text(text: str) -> str:
    low = _low(text)
    replacements = {
        "понеддельник": "понедельник",
        "понеделник": "понедельник",
        "понедельникк": "понедельник",
        "пандельник": "понедельник",
    }
    for bad, good in replacements.items():
        low = low.replace(bad, good)
    if "понедельник" in low:
        return "в понедельник"
    if "послезавтра" in low:
        return "послезавтра"
    if "завтра" in low:
        return "завтра"
    return text


def _has_multi_entity_safe_date(text: str) -> bool:
    low = _low(text)
    return any(p in low for p in [
        "понеддельник", "понеделник", "понедельникк", "пандельник",
        "понедельник", "завтра", "послезавтра",
    ])


async def _try_python_multi_entity_fallback(chat_id: str, session: dict[str, Any], text: str) -> str | None:
    step = str(session.get("step") or "start")
    if step not in {"age", "contraindications"}:
        return None
    age = _extract_age(text, step="age") or (int(session["age"]) if str(session.get("age") or "").isdigit() else None)
    if not age:
        return None
    if not _text_confirms_no_contra(session, text):
        return None
    if _contra_has_hard_stop(text) or not _has_multi_entity_safe_date(text):
        return None
    date_text = _multi_entity_safe_date_text(text)
    date_iso = _parse_date(date_text) or _parse_date(text)
    if not date_iso:
        return None

    session["age"] = age
    _accept_no_contraindications(session, text or "нет")
    session["contraindications_raw"] = _extract_no_contra_raw(text)
    if "не рано" in _low(text):
        session["time_preference"] = "не рано"
    session["openai_brain_fallback_used"] = True
    session["openai_brain_skip_reason"] = session.get("openai_brain_skip_reason") or "python_multi_entity_fallback"
    _safe_log(chat_id, "openai_brain_fallback_rule_based", {
        "chat_id": chat_id,
        "step": step,
        "action": "show_slots",
        "needs_python_tool": "check_slots",
        "guard_failed": False,
        "guard_reason": "",
        "fallback_reason": session["openai_brain_skip_reason"],
        "extracted_preview": {
            "age": age,
            "contraindications_clear": True,
            "preferred_date_text": date_text,
            "time_preference": session.get("time_preference") or "",
        },
    })
    return _finalize(chat_id, session, await _show_slots(chat_id, session, date_iso))

# ============================================================
# Neuro Balance dialog.py
# Safe controller:
# complaint -> age -> contraindications -> date -> time -> name -> booking
# ============================================================

KZ_MARKERS = [
    "сәлем", "салем", "қалай", "менің", "меним", "атым",
    "белім", "белим", "ауырады", "ауыр", "аурат", "ертең", "ертен",
    "бүгін", "бугин", "жоқ", "жок", "joq", "ия", "иә",
    "қарсы", "карсы", "көрсетілім", "корсетилим",
    "жазыл", "жазылғым", "келеді", "жас", "жастамын",
    "аяқ", "аяг", "аяғым", "аягым", "аяғымнан", "аягымнан",
    "қол", "кол", "омыртқа", "омыртка", "жарық", "жарык",
    "оң", "он ая", "сол аяқ", "сол ая", "қысқа", "кыска",
    "емдей", "емдей аласыз", "емдей аласыздар", "аласыздар ма",
]

RU_MARKERS = [
    "здравствуйте", "привет", "добрый", "хочу", "записаться", "консультац",
    "болит", "боль", "спина", "поясница", "шея", "грыжа", "протрузия",
    "сколько", "стоит", "адрес", "завтра", "сегодня", "лет",
]

BOOKING_WORDS = [
    "запис", "консультац", "прием", "приём", "акци", "50%",
    "жазыл", "қабылдау", "кабылдау",
]

COMPLAINT_WORDS = [
    "болит", "боль", "ноет", "тянет", "ломит", "хрустит", "онем",
    "мурашк", "защем", "грыж", "грыжа", "протруз", "протрузия",
    "отдает", "отдаёт", "стреляет", "прострел", "нога", "ногу", "ноге",
    "рука", "руку", "руке", "бедро", "таз", "лопат", "ребр",
    "спина", "спине", "спину", "поясниц", "шея", "шей", "сустав",
    "колен", "плеч", "голова", "артроз", "артрит", "остеохонд", "травм",
    "ауырады", "ауырып", "ауыр", "аурат", "қатты", "катты", "береді", "береди",
    "belim", "белім", "белим", "бел", "арқа", "аркам", "арқам",
    "аяқ", "аяққа", "аяк", "аякка", "аяғым", "аягым",
    "мойын", "мойным", "буын", "тізе", "тизе", "иық", "иык", "қол", "кол",
    "парез",
    "парез стопы",
    "стопа",
    "стопы",
    "после операции",
    "операции",
    "операция",
    "прооперировали",
    "шов",
    "реабилитация",
    "реабилитацию",
    "оналту",
    "табан",
    "ота",
]

NO_COMPLAINT_WORDS = [
    "ничего", "ничего не беспокоит", "не беспокоит", "ничего не болит",
    "просто консультация", "просто осмотр", "профилактика",
    "профилактический осмотр", "для профилактики", "не знаю",
    "ештеңе", "ештене", "мазаламайды", "ауырмайды",
    "білмеймін", "билмеймин", "жай консультация", "профилактика үшін",
]

PRICE_WORDS = [
    "сколько стоит", "стоимость", "цена", "прайс", "қанша тұрады",
    "канша турады", "бағасы", "багасы", "стоить",
]

ADDRESS_WORDS = [
    "адрес", "адресс", "где вы", "где находитесь", "вы в астане", "2gis", "2 гис",
    "куда обращаться", "куда прийти", "как пройти", "куда ехать",
    "куда приехать",
    "мекенжай", "қайда", "кайда", "астанада",
]

SCHEDULE_WORDS = [
    "график", "режим", "работаете", "расписание", "сенбі", "жексенбі",
    "кесте", "жұмыс", "жумыс",
]

MRI_WORDS = [
    "мрт", "кт", "рентген", "узи", "анализ", "снимок", "снимки",
    "диагностика", "диагностик", "томография",
]


METHOD_WORDS = [
    "что за методика", "какая методика", "методика", "методы лечения", "как лечите",
    "чем лечите", "какое лечение", "что делаете", "безоперацион", "без операции",
    "операциясыз",
]

DOCTOR_WORDS = [
    "у вас врачи", "врачи или как", "врач или как", "консультацию проводит врач",
    "кто консультирует", "кто смотрит", "врач смотрит", "доктор", "доктора",
    "а врачи кто", "врачи кто", "имена врачей", "имена их", "какой врач",
    "к кому запишете", "как зовут врача", "как зовут врачей", "имя врача",
]

COURSE_DURATION_WORDS = [
    "сколько дней будет всего", "сколько дней", "сколько процедур", "сколько длится курс",
    "длительность курса", "курс сколько", "сколько сеансов", "сколько лечение длится",
]

INSTALLMENT_WORDS = ["рассрочка", "каспи ред", "kaspi red", "kaspi", "кредит"]
REFUND_WORDS = ["возврат", "предоплат", "претенз", "жалоба", "отдел забот", "асем"]
PHONE_CALL_WORDS = ["позвоните", "перезвоните", "можете позвонить", "звонок", "позвонить мне"]
RETURNING_PATIENT_WORDS = ["был у вас", "была у вас", "были у вас", "лечился у вас", "лечилась у вас", "приходил", "приходила раньше", "повторно"]
OTHER_CITY_WORDS = ["я из ", "не из астаны", "костанай", "караганда", "кокшетау", "алматы", "павлодар", "семей", "усть-каменогорск", "шымкент"]
TOO_EXPENSIVE_WORDS = ["дорого", "нет денег", "не по карману", "дороговато"]
WILL_THINK_WORDS = ["подумаю", "посоветуюсь", "изучу", "если что напишу"]
HELPS_WORDS = ["поможет", "помогает", "гарантия", "правда лечит", "эффективно"]
IMMOBILITY_WORDS = ["коляска", "костыли", "лежит", "не ходит", "тяжело ходить", "ходунки"]



# ============================================================
# Profile classifier
# Профиль клиники: спина, позвоночник, суставы, мышцы, неврология,
# реабилитация после операции/травм, парезы, онемение, нарушение походки.
# Не профиль: стоматология, ЛОР, глаза, кожа, живот/ЖКТ, сердце/скорая,
# гинекология/урология, инфекция/температура, психиатрия, чистая косметология.
# ============================================================

PROFILE_COMPLAINT_WORDS = [
    # позвоночник / спина
    "спина", "спине", "спину", "поясниц", "пояснич", "крестец", "копчик",
    "шея", "шей", "воротников", "лопат", "межлопат",
    "позвоноч", "омыртқа", "омыртка", "арқа", "арка", "белім", "белим",

    # диагнозы опорно-двигательного аппарата
    "грыж", "грыжа", "протруз", "остеохонд", "сколиоз", "кифоз",
    "лордоз", "радикул", "ишиас", "невралг", "защем", "защим",
    "артроз", "артрит", "коксартроз", "гонартроз", "остеоартроз",
    "плоскостоп", "пяточная шпора", "шпора", "плантар",

    # суставы / мышцы / связки
    "сустав", "колен", "плеч", "локт", "кисть", "запяст", "тазобед",
    "бедро", "голен", "стоп", "стопа", "стопы", "табан",
    "мышц", "мышца", "связк", "сухожил", "растяж", "вывих",

    # неврология / симптомы
    "онем", "немеет", "мурашк", "прострел", "стреляет", "отдает", "отдаёт",
    "тянет", "ноет", "ломит", "хрустит", "судорог", "спазм",
    "парез", "паралич", "слабость в ног", "слабость в рук",
    "нарушение походки", "хром", "координац", "вестибул",

    # реабилитация / после операций
    "после операции", "послеоперац", "операции", "операция",
    "прооперировали", "реабилитац", "реабилитация", "восстановлен",
    "после травмы", "травм", "перелом", "ушиб",

    # казахский
    "ауырады", "ауыр", "ауырсын", "аяқ", "аяк", "қол", "кол",
    "мойын", "буын", "тізе", "тизе", "иық", "иык",
    "оң аяқ", "сол аяқ", "қысқа", "кыска", "оналту",
]

NON_PROFILE_COMPLAINT_WORDS = [
    # зубы / стоматология
    "зуб", "зубы", "десна", "кариес", "стоматолог", "тіс", "тис",

    # ЛОР
    "горло", "ангина", "насморк", "кашель", "ухо", "уши", "отит",
    "гайморит", "нос", "лор", "құлақ", "кулак", "тамақ", "тамак",

    # глаза
    "глаз", "глаза", "зрение", "офтальм", "конъюнктив", "көз", "коз",

    # кожа / косметология
    "кожа", "сыпь", "прыщ", "акне", "дермат", "экзема", "псориаз",
    "родинка", "бородав", "аллергия на коже", "бетім", "бет", "тері", "тери",

    # ЖКТ / живот
    "живот", "желуд", "кишеч", "понос", "диар", "рвота", "тошнит",
    "печень", "желчный", "гастрит", "аппендиц", "іш", "асқазан", "асказан",

    # сердце / сосуды / скорая
    "сердце", "сердц", "сердеч", "давление", "гипертони", "инфаркт", "стенокард",
    "боль в груди", "грудь сжимает", "одышка", "тромб", "варикоз",
    "жүрек", "журек", "қан қысым", "кан кысым",

    # урология / гинекология / беременность
    "почки", "моч", "цистит", "простата", "уролог", "гинеколог",
    "месячные", "беремен", "беременность", "жүктілік", "жукцилик",

    # инфекции / высокая температура
    "температура", "лихорад", "грипп", "ковид", "covid", "инфекц",
    "пневмони", "бронхит", "астма", "қызу", "кызу",

    # психиатрия / зависимости
    "депресс", "паничес", "тревога", "психиатр", "нарколог", "алкогол",

    # эндокринология без невро/суставной жалобы
    "щитовид", "сахарный диабет", "диабет", "эндокрин",

    # экстренное
    "инсульт", "потеря сознания", "обморок", "кровотеч", "судороги сейчас",
    "не чувствую половину тела",
]

# Дополнительная карта болезней/диагнозов.
# Если пациент пишет профильную болезнь — продолжаем сценарий записи.
# Если болезнь не профильная — не ведём в запись и передаём администратору/профильному врачу.
PROFILE_DISEASE_WORDS = [
    'заболевания суставов', 'болезни суставов', 'суставная боль', 'боль в суставах', 'болят суставы', 'суставы болят', 'коленный сустав', 'плечевой сустав', 'локтевой сустав', 'тазобедренный сустав', 'голеностопный сустав', 'сустав кисти', 'суставы', 'сустав', 'буын', 'буындар', 'буыным', 'буындарым', 'протрузиях', 'грыжах', 'защемлении нервов', 'боли в спине', 'боли в шее', 'болях в спине', 'болях в шее',

    # позвоночник / диски / осанка
    "межпозвоночная грыжа", "грыжа диска", "грыжа позвоночника", "межпозвонковая грыжа",
    "протрузия", "протрузии", "экструзия", "секвестр", "секвестрированная грыжа",
    "остеохондроз", "спондилез", "спондилез", "спондилоартроз", "спондилолистез",
    "сколиоз", "кифоз", "лордоз", "радикулит", "радикулопатия", "дорсопатия",
    "защемление нерва", "ущемление нерва", "седалищный нерв", "ишиас",
    "стеноз позвоночного канала", "нестабильность позвонков",

    # суставы / опорно-двигательный аппарат
    "артроз", "артрит", "остеоартроз", "коксартроз", "гонартроз",
    "периартрит", "плечелопаточный периартрит", "бурсит", "тендинит",
    "пяточная шпора", "плантарный фасциит", "плоскостопие",
    "контрактура", "тугоподвижность", "нарушение осанки",

    # неврология / симптомы
    "невралгия", "невропатия", "нейропатия", "онемение", "мурашки",
    "парез", "парез стопы", "слабость в ноге", "слабость в руке",
    "нарушение походки", "хромота", "прострел", "люмбаго", "цервикалгия",
    "люмбалгия", "люмбоишиалгия",

    # реабилитация
    "после операции", "послеоперационная реабилитация", "реабилитация после операции",
    "после травмы", "после перелома", "восстановление после травмы",
    "восстановление после операции",

    # казахский / транслит
    "омыртқа жарығы", "омыртка жарыгы", "грыжа бар", "протрузия бар",
    "бел грыжасы", "белде грыжа", "мойын грыжасы", "арқа ауырады",
    "белім ауырады", "белим ауырады", "аяғым ұйиды", "аягым уйиды",
    "аяққа береді", "аякка береди", "оналту",
]

NON_PROFILE_DISEASE_WORDS = [
    # стоматология
    "кариес", "пульпит", "периодонтит", "флюс", "зубная боль", "болит зуб",
    "десна болит", "стоматит",

    # ЛОР / дыхательные
    "ангина", "тонзиллит", "фарингит", "ларингит", "гайморит", "синусит",
    "отит", "насморк", "кашель", "бронхит", "пневмония", "астма",

    # глаза
    "конъюнктивит", "катаракта", "глаукома", "миопия", "близорукость",
    "ухудшение зрения", "болит глаз",

    # ЖКТ
    "гастрит", "язва желудка", "панкреатит", "холецистит", "аппендицит",
    "колит", "диарея", "понос", "рвота", "тошнота", "болит живот",
    "геморрой",

    # сердце / сосуды / экстренное
    "инфаркт", "стенокардия", "аритмия", "тахикардия", "гипертония",
    "высокое давление", "варикоз", "тромбоз", "инсульт", "обморок",
    "потеря сознания", "боль в груди",

    # урология / гинекология / беременность
    "цистит", "пиелонефрит", "камни в почках", "простатит", "аденома простаты",
    "гинекология", "миома", "киста яичника", "эндометриоз", "беременность",

    # кожа / аллергия / инфекция
    "дерматит", "экзема", "псориаз", "акне", "прыщи", "сыпь", "крапивница",
    "аллергия", "температура", "грипп", "ковид", "covid", "инфекция",

    # эндокринология
    "диабет", "сахарный диабет", "щитовидка", "гипотиреоз", "гипертиреоз",

    # психиатрия/наркология
    "депрессия", "паническая атака", "тревожность", "алкоголизм", "наркомания",

    # казахский
    "тіс ауырады", "тис ауырады", "іш ауырады", "иш ауырады", "асқазан",
    "жүрек", "журек", "қысым", "кысым", "көз", "коз", "құлақ", "кулак",
]

UNCLEAR_DISEASE_WORDS = [
    "диагноз", "болезнь", "заболевание", "лечите", "емдей", "емдейсіздер",
    "емдей аласыз", "емдей аласыздар", "можно лечить", "лечите ли",
]

CANCEL_WORDS = [
    "отмен", "не приду", "не смогу", "перенес", "перенести", "поменять время",
    "басқа уақыт", "ауыстыр", "келмеймін", "келе алмаймын",
]

LOOKUP_WORDS = [
    "я уже записан", "я уже записана", "уже записан", "уже записана",
    "у меня запись", "моя запись", "мою запись", "когда я записан",
    "когда у меня запись", "напомните", "на какое время", "во сколько",
    "проверить запись", "посмотреть запись", "жазылдым", "жазбам", "қашан",
]

NO_CONTRA_WORDS = [
    "нет", "нету", "не было", "не имеется", "противопоказаний нет", "нет противопоказаний",
    "ничего нет", "по списку ничего нет", "нет такого ничего", "нет такого", "все чисто", "всё чисто", "чисто",
    "все нормально", "всё нормально", "нормально", "ничего такого нет", "ничего из этого нет",
    "нет ничего из перечисленного", "ничего из перечисленного нет", "того что перечислено нет",
    "из перечисленного ничего нет", "из того что вы перечислили ничего нет", "этого нет",
    "того, что перечислено, этого нет", "то что перечислено этого нет", "то что перечислено, этого нет",
    "по всем нет", "все нет", "всё нет",
    "жоқ", "жок", "joq", "jok", "қарсы көрсетілім жоқ", "карсы корсетилим жок",
]

REFUSAL_OR_IRRITATION_WORDS = [
    "я уже от вас ничего не хочу", "ничего не хочу", "не хочу", "отстаньте", "хватит писать",
    "вы надоели", "устал", "устала", "одно и то же пишите", "одно и тоже пишите", "спокойной ночи",
    "не надо", "не интересно", "больше не пишите",
]

YES_WORDS = [
    "да", "есть", "бар", "иә", "ия", "есть противопоказ", "имеется",
]

HARD_CONTRA_WORDS = [
    "кардиостимулятор", "кардиостемулятор", "дефибриллятор",
    "кохлеар", "инсулиновая помпа", "помпа",
    "тромбофлебит", "тромбоз", "тромб", "свертываем", "свёртываем",
    "беремен", "беременность", "жүктілік", "жукцилик",
    "онколог", "онкология", "рак", "подозрение на онколог",
    "эпилеп", "эпилепсия", "судорог", "судороги",
    "декомпенсирован", "тиреотоксикоз",
    "температура", "орви", "грипп", "острая инфек",
    "тяжелые проблемы с сердцем", "тяжёлые проблемы с сердцем",
    "тяжелые проблемы с дыханием", "тяжёлые проблемы с дыханием",
    "тяжелое психическое", "тяжёлое психическое",
    "коляск", "костыл", "костыли", "лежач", "ограниченная подвижность", "ограниченной подвижностью",
    "мүгедек арба", "арбамен", "таяқ", "балдақ",
]

NAME_BANNED_WORDS = set(
    "да нет ок окей хорошо приду буду завтра сегодня ертең бугин бүгін жок жоқ хочу записаться консультация болит боль спасибо спс благодарю понял поняла".split()
)

SERVICE_POLITE_NAME_PHRASES = {"спасибо", "спс", "благодарю", "ок", "окей", "хорошо", "поняла", "понял"}
BAD_PATIENT_NAME_VALUES = {"спасибо", "ок", "окей", "хорошо", "понял", "поняла"}


def _is_service_polite_phrase(text: str) -> bool:
    cleaned = re.sub(r"[\s.!?,🙏🌿❤️❤]+", "", _low(text or ""))
    return cleaned in {re.sub(r"[\s.!?,🙏🌿❤️❤]+", "", phrase) for phrase in SERVICE_POLITE_NAME_PHRASES}


def _repair_bad_patient_name(session: dict[str, Any]) -> bool:
    name = str(session.get("patient_name") or "")
    if _low(name) in BAD_PATIENT_NAME_VALUES:
        session["patient_name"] = ""
        return True
    return False


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def _normalize_typos(text: str) -> str:
    """Нормализует частые опечатки RU/KZ, чтобы бот понимал живые сообщения.

    Важно: это не меняет текст ответа пациенту, только помогает распознать смысл.
    """
    s = _clean(text).lower().replace("ё", "е")

    # Частые казахские буквы, которые пишут русскими/латиницей.
    char_map = {
        "і": "и",
        "ұ": "у",
        "ү": "у",
        "қ": "к",
        "ң": "н",
        "ғ": "г",
        "ә": "а",
        "ө": "о",
        "һ": "х",
    }
    for a, b in char_map.items():
        s = s.replace(a, b)

    # Убираем лишние повторы букв: "болииит" -> "болиит" (мягко, не ломая слова).
    s = re.sub(r"([а-яa-z])\1{2,}", r"\1\1", s)

    replacements = {
        # запись / отказ / благодарность
        "спс": "спасибо",
        "спосибо": "спасибо",
        "спасиба": "спасибо",
        "рахметт": "рахмет",
        "ракмет": "рахмет",
        "рахмед": "рахмет",
        "океи": "окей",
        "окэй": "окей",
        "ненадо": "не надо",
        "ни надо": "не надо",
        "не нада": "не надо",
        "не нодо": "не надо",
        "перидумал": "передумал",
        "передумола": "передумала",

        # профильные жалобы RU
        "грыжа": "грыжа",
        "грижа": "грыжа",
        "грыжы": "грыжа",
        "грыж": "грыжа",
        "протрузи": "протрузия",
        "пратрузия": "протрузия",
        "пратрузи": "протрузия",
        "астеохондроз": "остеохондроз",
        "остихондроз": "остеохондроз",
        "остеахондроз": "остеохондроз",
        "остехондроз": "остеохондроз",
        "скалеоз": "сколиоз",
        "сколиес": "сколиоз",
        "радиколит": "радикулит",
        "защимление": "защемление",
        "защемления": "защемление",
        "онемела": "онемение",
        "нимеет": "немеет",
        "немиет": "немеет",
        "больт": "болит",
        "балит": "болит",
        "балеет": "болит",
        "пояснитса": "поясница",
        "поесница": "поясница",
        "поясницца": "поясница",
        "позвоночнек": "позвоночник",
        "суставв": "сустав",
        "суставыы": "суставы",
        "калено": "колено",
        "каленка": "колено",
        "плеччо": "плечо",
        "шее": "шея",
        "шеии": "шея",

        # профильные жалобы KZ/транслит
        "белим": "бел",
        "белым": "бел",
        "белиме": "бел",
        "белім": "бел",
        "ауырады": "ауырады",
        "аурады": "ауырады",
        "аурыйды": "ауырады",
        "ауырып": "ауырады",
        "аягым": "аяк",
        "аяғым": "аяк",
        "аякка": "аякка",
        "аяққа": "аякка",
        "тартылады": "тартылады",
        "тартылад": "тартылады",
        "тартлады": "тартылады",
        "грижасы": "грыжа",
        "грыжасы": "грыжа",
        "грижа": "грыжа",
        "мойным": "мойын",
        "мойыным": "мойын",
        "мойынм": "мойын",
        "буыным": "буын",
        "буындарым": "буын",
        "тизем": "тизе",
        "тізем": "тизе",

        # противопоказания
        "кардио стимулятор": "кардиостимулятор",
        "кардио-стимулятор": "кардиостимулятор",
        "кардистимулятор": "кардиостимулятор",
        "кардиостемулятор": "кардиостимулятор",
        "онка": "онкология",
        "онко": "онкология",
        "онколгия": "онкология",
        "беременна": "беременность",
        "биременна": "беременность",
        "эпилепсия": "эпилепсия",
        "эпилепссия": "эпилепсия",
        "метал": "металл",
        "металлл": "металл",
        "противопаказ": "противопоказ",
        "противопокоз": "противопоказ",
        "противопоказаниев": "противопоказаний",
        "ограниченой": "ограниченной",
        "коляска": "коляска",
        "каляска": "коляска",
        "кастыли": "костыли",

        # даты / время
        "севодня": "сегодня",
        "сегодна": "сегодня",
        "завтро": "завтра",
        "завтар": "завтра",
        "послезавтро": "послезавтра",
        "понеделник": "понедельник",
        "панедельник": "понедельник",
        "вторнек": "вторник",
        "среду": "среда",
        "четвирг": "четверг",
        "пятнитса": "пятница",
        "субота": "суббота",
        "васкресенье": "воскресенье",
        "следущ": "следующ",
        "следуюший": "следующий",
        "след неделе": "следующей неделе",
        "отпускга": "отпускға",
        "отпуска": "отпуск",
        "сенябре": "сентябре",
        "сентебре": "сентябре",

        # МРТ / диагностика
        "мртт": "мрт",
        "мрt": "мрт",
        "мртга": "мртға",
        "мрт тусу": "мрт түсу",
        "мрт тус": "мрт түсу",
        "снимка": "снимок",
        "снимкии": "снимки",
        "рентгенн": "рентген",
        "диогностика": "диагностика",
    }

    # Сначала точные фразы, потом отдельные слова.
    for wrong, right in replacements.items():
        s = s.replace(wrong, right)

    live_typos = {
        "хачу": "хочу",
        "хочу записатся": "хочу записаться",
        "хочу записатса": "хочу записаться",
        "хочу записаца": "хочу записаться",
        "записатся": "записаться",
        "записатса": "записаться",
        "записаца": "записаться",
        "кансультация": "консультация",
        "консультацы": "консультация",
        "спосибо": "спасибо",
        "спасиба": "спасибо",
        "рахмед": "рахмет",
        "ракмет": "рахмет",
        "отмините": "отмените",
        "атмените": "отмените",
        "отменити": "отмените",
        "атмена": "отмена",
        "ни приду": "не приду",
        "не прийду": "не приду",
        "не смогу придти": "не смогу прийти",
        "напомнити": "напомните",
        "напамните": "напомните",
        "времья": "время",
    }
    for wrong, right in live_typos.items():
        s = s.replace(wrong, right)

    # Нормализуем пробелы.
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _low(text: str) -> str:
    return _normalize_typos(text)


def _has_negative_visit_intent(text: str) -> bool:
    low = _low(_strip_quoted_bot_text(text))
    if not low:
        return False

    negative_phrases = [
        "не приду", "не прийду", "не буду", "не смогу прийти", "не смогу приехать",
        "не получится прийти", "не получается прийти", "я не приду", "я не буду",
        "келмеймін", "келе алмаймын", "бармаймын",
    ]
    return any(p in low for p in negative_phrases)


def _word_distance_one(a: str, b: str) -> bool:
    """Очень лёгкая проверка опечатки в 1 символ для слов длиной от 5.

    Без внешних библиотек, чтобы не ломать деплой.
    """
    if len(a) < 5 or len(b) < 5:
        return False
    if abs(len(a) - len(b)) > 1:
        return False

    if a == b:
        return True

    # equal length: one substitution
    if len(a) == len(b):
        diff = sum(1 for x, y in zip(a, b) if x != y)
        return diff <= 1

    # one insertion/deletion
    if len(a) > len(b):
        a, b = b, a

    i = j = diff = 0
    while i < len(a) and j < len(b):
        if a[i] == b[j]:
            i += 1
            j += 1
        else:
            diff += 1
            if diff > 1:
                return False
            j += 1

    return True


def _has_any(text: str, words: list[str]) -> bool:
    low = _low(text)
    if any(w in low for w in words):
        return True

    tokens = re.findall(r"[a-zа-яәғқңөұүһі]+", low)
    for token in tokens:
        for w in words:
            # Проверяем только однословные маркеры, чтобы не ловить ложные совпадения.
            if " " in w or len(w) < 5:
                continue
            if _word_distance_one(token, _low(w)):
                return True

    return False
def _has_mri_question(text: str) -> bool:
    low = _low(text)
    if not low:
        return False

    # Диагностику проверяем аккуратно, чтобы имя "Виктор" не срабатывало как "КТ".
    # Короткие маркеры "кт", "мрт", "узи" — только отдельным словом.
    short_markers = ["мрт", "мрt", "кт", "узи"]
    for marker in short_markers:
        if re.search(rf"(?<![а-яa-z]){re.escape(marker)}(?![а-яa-z])", low):
            return True

    phrase_markers = [
        "мрт түсу", "мрт тусу", "кт түсу", "кт тусу",
        "рентгенге түсу", "рентгенге тусу",
        "снимок", "снимки", "снимка", "снимкаға", "снимкага",
        "рентген", "томография", "диагностика", "диагностик",
        "обследование", "тексеру", "тексеріс", "тексерис",
        "сурет", "суретке", "мртға", "мртга",
    ]

    return any(w in low for w in phrase_markers)

def _explicit_language_request(text: str) -> str | None:
    low = _low(text)
    if not low:
        return None

    # Клиент явно просит язык.
    ru_patterns = [
        "на русском", "по русски", "по-русски", "русский язык",
        "пишите на русском", "говорите на русском", "можно на русском",
    ]
    kk_patterns = [
        "қазақша", "казакша", "на казахском", "по казахски", "по-казахски",
        "қазақ тілінде", "казахский язык", "пишите на казахском",
    ]

    if any(p in low for p in ru_patterns):
        return "ru"
    if any(p in low for p in kk_patterns):
        return "kk"
    return None


def _detect_lang(text: str, session: dict[str, Any]) -> str:
    """Определяет язык без скачков туда-сюда.

    Правило:
    - первый осмысленный язык диалога фиксируется;
    - короткие ответы типа "жоқ/рахмет/37 жаста" не переключают язык;
    - смешанные сообщения не переключают язык;
    - смена языка только если клиент явно попросил: "пишите на казахском/русском".
    """
    current = session.get("language") or "ru"
    low = _low(text)
    text_stripped = (text or "").strip()

    # Если язык уже зафиксирован — держим его.
    # Явная просьба сменить язык обрабатывается в handle_message через _explicit_language_request.
    if session.get("language_locked") and current in ("ru", "kk"):
        return current

    step_now = session.get("step") or "start"
    if session.get("language") in ("ru", "kk") and step_now not in ("start", "", None):
        return current

    short_answer = bool(
        re.fullmatch(
            r"\s*(?:\d{1,3}\s*(?:жаста|жас|лет|года|год)?|жоқ|жок|ия|иә|жақсы|жаксы|рахмет|спасибо|ок|окей|нет|да)\s*[.!?🙏🌿]*\s*",
            low,
        )
    )
    if short_answer and current in ("ru", "kk"):
        return current

    has_kz_letters = bool(re.search(r"[әғқңөұүһіӘҒҚҢӨҰҮҺІ]", text_stripped))
    has_kz_words = any(w in low for w in KZ_MARKERS) or bool(
        re.search(r"(емдей\s+аласыз|емдей\s+аласыздар|аласыздар\s+ма)", low)
    )
    has_ru = any(w in low for w in RU_MARKERS)

    # Смешанный текст: держим текущий язык.
    if has_ru and (has_kz_letters or has_kz_words) and current in ("ru", "kk"):
        return current

    if has_kz_letters or has_kz_words:
        return "kk"

    if has_ru:
        return "ru"

    if detect_message_language:
        try:
            detected = detect_message_language(text, current)
            if detected in ("ru", "kk"):
                return detected
        except Exception:
            pass

    return current if current in ("ru", "kk") else "ru"
def _tr(session_or_lang: dict[str, Any] | str, ru: str, kk: str) -> str:
    if isinstance(session_or_lang, dict):
        lang = session_or_lang.get("language") or "ru"
    else:
        lang = session_or_lang or "ru"
    return kk if lang == "kk" else ru


def _clinic_info_template(session: dict[str, Any], topic: str) -> str | None:
    """Return old-bot operator template through the Python get_clinic_info tool."""
    return bot_tools.get_clinic_info(session, topic)


def _price_short_text(session: dict[str, Any]) -> str:
    return _tr(
        session,
        "Первичный приём в нашей клинике — 5 000 тг 🌿",
        "Біздің клиникада алғашқы қабылдау — 5 000 тг 🌿",
    )


def _price_answer(text: str, session: dict[str, Any]) -> str:
    course = any(w in _low(text) for w in ["курс", "лечение", "лечения", "емдеу", "ем"])
    template = _clinic_info_template(session, "price_course" if course else "price_first_visit")
    if template:
        if course and "5 000" not in template:
            return _price_short_text(session) + "\n\nСтоимость курса подбирается индивидуально. " + template
        return template
    base = _price_short_text(session)
    if not course:
        return base
    return _tr(
        session,
        base + " Стоимость курса врач сможет рассчитать после осмотра, потому что всё зависит от диагноза, состояния и количества процедур.",
        base + " Емдеу курсының құнын дәрігер алғашқы қараудан кейін ғана есептей алады, себебі бәрі диагнозға, жағдайға және процедура санына байланысты.",
    )


def _address_answer(session: dict[str, Any]) -> str:
    session["last_clinic_info_topic"] = "address"
    bot_tools.mark_tool(session, "get_clinic_info", topic="address")
    return _tr(
        session,
        "Мы находимся по адресу Кабанбай батыра 28, внутренний двор, подъезд 3. Заезд со стороны Кунаева, после ворот поверните направо 📍\n\n2ГИС: https://2gis.kz/astana/inside/9570784863354265/firm/70000001105992248?m=71.416112%2C51.134091%2F16",
        "Біздің мекенжай: Қабанбай батыр 28, ішкі аула, 3-кіреберіс. Кунаев жағынан кіріп, қақпадан кейін оңға бұрылыңыз 📍\n\n2ГИС: https://2gis.kz/astana/inside/9570784863354265/firm/70000001105992248?m=71.416112%2C51.134091%2F16",
    )


def _schedule_answer(session: dict[str, Any]) -> str:
    return _clinic_info_template(session, "schedule") or _tr(
        session,
        "Работаем по предварительной записи 🌿 Напишите удобный день — я проверю свободное время.",
        "Алдын ала жазылу бойынша жұмыс істейміз 🌿 Ыңғайлы күнді жазыңыз — бос уақытты тексеремін.",
    )


def _prepend_price_if_needed(text: str, session: dict[str, Any], answer: str) -> str:
    if _has_any(text, PRICE_WORDS):
        price = _price_answer(text, session)
        if price not in answer:
            return price + "\n\n" + answer
    return answer


def _is_relative_new_booking_request(text: str) -> bool:
    low = _low(text)

    relatives = [
        "маму", "мама", "папу", "папа", "отца", "отец", "сына", "сын",
        "дочь", "дочку", "дочери", "мужа", "жену", "брата", "сестру",
        "анамды", "анама", "әкемді", "әкем", "баламды", "балам",
        "ұлымды", "қызымды", "жолдасымды", "ағамды", "інімді", "әпкемді",
    ]

    booking_words = [
        "записать", "записаться", "запишите", "хочу записать", "хочу записаться",
        "жазу", "жазғым", "жазыл", "жазып", "жазайын",
    ]

    existing_words = [
        "уже записан", "уже записана", "я записан", "я записана", "был записан",
        "была записана", "бұрын жазылған", "жазылған едім",
        "отмените", "отменить", "перенести", "перенесите",
        "басқа уақыт", "ауыстыру", "ауыстырыңыз",
    ]

    return (
        any(w in low for w in relatives)
        and any(w in low for w in booking_words)
        and any(w in low for w in existing_words)
    )


def _relative_dual_task_answer(session: dict[str, Any], text: str) -> str:
    low = _low(text)

    details = []
    age_match = _extract_age(text, step="age")
    if age_match:
        details.append(f"возраст: {age_match}")

    if any(w in low for w in ["шея", "мойын"]):
        details.append("жалоба: шея")
    elif any(w in low for w in ["спина", "поясница", "бел"]):
        details.append("жалоба: спина/поясница")
    elif any(w in low for w in ["колено", "тізе"]):
        details.append("жалоба: колено")
    elif any(w in low for w in ["нога", "аяқ"]):
        details.append("жалоба: нога")

    if _contra_is_clear_no(text):
        details.append("противопоказаний нет")
    elif _contra_has_hard_stop(text):
        details.append("есть важные ограничения — передать врачу")

    if any(w in low for w in ["завтра", "ертең", "ертен"]):
        details.append("желательная дата: завтра")

    details_text_ru = ""
    details_text_kk = ""
    if details:
        joined = "; ".join(details)
        details_text_ru = f"\n\nПо новой записи передам данные: {joined}."
        details_text_kk = f"\n\nЖаңа жазба бойынша мәліметтерді жіберемін: {joined}."

    return _tr(
        session,
        "Поняла Вас. Передам администратору, чтобы он проверил Вашу запись и помог отменить или перенести её 🌿\n\nТакже администратор отдельно поможет записать родственника на консультацию.",
        "Түсіндім. Әкімшіге жіберемін, ол Сіздің жазбаңызды тексеріп, тоқтатуға немесе ауыстыруға көмектеседі 🌿\n\nСонымен қатар әкімші туысыңызды консультацияға бөлек жазуға көмектеседі.",
    ) + _tr(session, details_text_ru, details_text_kk)


def _safe_save(chat_id: str, session: dict[str, Any]) -> None:
    try:
        state.save_session(chat_id, session)
    except Exception:
        pass


def _safe_add_message(chat_id: str, role: str, text: str) -> None:
    try:
        state.add_message(chat_id, role, text)
    except Exception:
        pass


def _safe_log(chat_id: str, event: str, payload: dict[str, Any]) -> None:
    try:
        state.log_event(chat_id, event, payload)
    except Exception:
        pass



def _profile_status(text: str) -> str:
    """Возвращает: profile / non_profile / unclear / none.

    Правило:
    - профильная болезнь/жалоба -> продолжаем запись;
    - не профильная болезнь -> не ведём в запись, передаём администратору;
    - если непонятно -> уточняем жалобу.
    """
    low = _low(text)
    if not low:
        return "none"

    has_profile = any(w in low for w in PROFILE_COMPLAINT_WORDS) or any(w in low for w in PROFILE_DISEASE_WORDS)

    def has_non_profile_word(word: str) -> bool:
        if word == "нос":
            return re.search(r"(?<![а-яa-z])нос(?![а-яa-z])", low) is not None
        return word in low

    has_non_profile = any(has_non_profile_word(w) for w in NON_PROFILE_COMPLAINT_WORDS) or any(w in low for w in NON_PROFILE_DISEASE_WORDS)

    # Экстренные/явно чужие направления не ведём в запись, даже если есть слово "боль".
    emergency_or_foreign = [
        "инфаркт", "инсульт", "потеря сознания", "обморок",
        "кровотечение", "боль в груди", "сильная одышка",
        "аппендицит", "температура 39", "температура 40",
    ]
    if any(w in low for w in emergency_or_foreign):
        return "non_profile"

    # Если есть профильная жалоба + сопутствующий диагноз, ведём как профиль.
    # Например: "диабет, но немеет нога" — профильная жалоба есть.
    if has_profile:
        return "profile"

    if has_non_profile:
        return "non_profile"

    # Если пациент спрашивает "лечите ли ..." без понятной болезни/симптома — уточняем.
    if any(w in low for w in UNCLEAR_DISEASE_WORDS):
        return "unclear"

    return "none"

def _appointment_request_answer(session: dict[str, Any], text: str) -> str | None:
    low = _low(text)
    if not low:
        return None

    has_intent = _has_booking_intent(text) or "хочу" in low
    if not has_intent:
        return None

    if "консультац" in low:
        return _tr(
            session,
            "Да, можно записаться на консультацию 🌿 Подскажите, пожалуйста, что Вас беспокоит?",
            "Иә, консультацияға жазылуға болады 🌿 Айтыңызшы, Сізді не мазалайды?",
        )
    if "диагностик" in low:
        return _tr(
            session,
            "Да, можно записаться на диагностику 🌿 Подскажите, пожалуйста, что Вас беспокоит?",
            "Иә, диагностикаға жазылуға болады 🌿 Айтыңызшы, Сізді не мазалайды?",
        )
    if any(w in low for w in ("приём", "прием", "осмотр")):
        return _tr(
            session,
            "Да, можно записаться на приём 🌿 Подскажите, пожалуйста, что Вас беспокоит?",
            "Иә, қабылдауға жазылуға болады 🌿 Айтыңызшы, Сізді не мазалайды?",
        )

    return None

def _record_complaint_tool(session: dict[str, Any], complaint: str, *, is_in_profile: bool) -> None:
    bot_tools.record_chief_complaint(session, complaint, is_in_profile=is_in_profile)


def _mark_irrelevant_tool(session: dict[str, Any], reason: str = "non_profile") -> None:
    bot_tools.mark_irrelevant(session, reason)
    bot_tools.escalate_to_human(session, reason)


def _non_profile_answer(session: dict[str, Any], text: str) -> str:
    return _tr(
        session,
        "Понимаю Вас 🌿 К сожалению, этим направлением наша клиника не занимается. Мы специализируемся на болях в спине и шее, грыжах, протрузиях, защемлении нервов и заболеваниях суставов. По Вашему вопросу лучше обратиться к профильному специалисту.",
        "Түсіндім 🌿 Өкінішке қарай, бұл бағытпен біздің клиника айналыспайды. Біз арқа/мойын ауруы, грыжа, протрузия, нерв қысылуы және буын ауруларына маманданамыз. Бұл сұрақ бойынша профильді маманға жүгінген дұрыс.",
    )
def _unclear_profile_answer(session: dict[str, Any], text: str) -> str:
    return _tr(
        session,
        "Поняла Вас 🌿 Чтобы точно сориентировать, подскажите, пожалуйста: беспокоит спина, шея, суставы, онемение или боль, которая отдаёт в руку/ногу?",
        "Түсіндім 🌿 Дәл бағыттау үшін нақтылап жазыңызшы: арқа, мойын, буын, ұю немесе қолға/аяққа берілетін ауырсыну мазалай ма?",
    )
def _has_medical_complaint_text(text: str) -> bool:
    # Медицинская жалоба есть, если классификатор понял профиль/не профиль/неясную болезнь.
    # Отдельно оставляем старые базовые слова через COMPLAINT_WORDS.
    status = _profile_status(text)
    if status in ("profile", "non_profile", "unclear"):
        return True
    return _has_any(text, COMPLAINT_WORDS)


def _is_no_contra_answer(text: str) -> bool:
    low = _low(text)
    if not low:
        return False

    # Explicit answers meaning "no contraindications".
    explicit_no = [
        "нет", "нету", "не имеется", "нет ничего", "ничего нет", "нет такого ничего", "нет такого", "противопоказаний нет", "противопаказаний нет", "противопокозаний нет",
        "нет противопоказаний", "ограничений нет", "не противопоказаний",
        "все чисто", "всё чисто", "чисто", "все нормально", "всё нормально", "нормально",
        "ничего такого нет", "ничего из этого нет", "нет ничего из перечисленного",
        "ничего из перечисленного нет", "из того что вы перечислили ничего нет",
        "из того что вы перечислили ничего такого нет", "того что перечислено нет",
        "то что перечислено этого нет", "то что перечислено, этого нет",
        "этого нет", "по списку ничего нет", "нет, ничего такого нет",
        "по всем нет", "все нет", "всё нет",
        "жоқ", "жок", "қарсы көрсетілім жоқ", "карсы корсетилим жок",
        "қарсы көрсетілімдер жоқ", "карсы корсетилимдер жок",
        "жоқ!", "жок!",
    ]

    compact = re.sub(r"[\s.!?,🙏🌿❤️❤]+", "", low)
    for phrase in explicit_no:
        if compact == re.sub(r"[\s.!?,🙏🌿❤️❤]+", "", phrase):
            return True

    if "противопоказ" in low and any(x in low for x in ["нет", "нету", "жоқ", "жок"]):
        return True
    if ("қарсы" in low or "карсы" in low) and any(x in low for x in ["жоқ", "жок", "нет"]):
        return True

    return False


def _is_thanks_or_ok(text: str) -> bool:
    low = _low(text)
    if not low:
        return False

    final_words = [
        "спасибо", "спс", "благодарю", "хорошо", "ок", "окей", "понял", "поняла",
        "рахмет", "жақсы", "жаксы", "түсіндім", "тусиндим",
    ]

    cleaned = re.sub(r"[\s.!?,🙏🌿❤️❤]+", "", low)

    for w in final_words:
        normalized = re.sub(r"[\s.!?,🙏🌿❤️❤]+", "", w)
        if cleaned == normalized:
            return True

    # ВАЖНО:
    # Нельзя делать `"ок" in low`, потому что слово "беспокоить" содержит "ок".
    # Для коротких фраз разрешаем только отдельные слова.
    if len(low) <= 40:
        for w in final_words:
            pattern = rf"(?<![a-zа-яәғқңөұүһі]){re.escape(w)}(?![a-zа-яәғқңөұүһі])"
            if re.search(pattern, low):
                return True

    return False


def _is_refuse_booking(text: str) -> bool:
    """Пациент отказался продолжать запись/консультацию.

    Важно: не путать с отменой уже существующей записи — отмена обрабатывается отдельно через CRM.
    """
    low = _low(text)
    if not low:
        return False

    phrases = [
        "не надо", "ненадо", "не нужно", "не хочу", "не буду", "не интересно",
        "передумал", "передумала", "отказываюсь", "не хочу записываться",
        "не буду записываться", "пока не надо", "потом напишу", "позже напишу",
        "потом обращусь", "пока откажусь", "запись не нужна", "консультация не нужна",
        "не записывайте", "не записывать", "давайте не будем", "оставим",
        "не актуально", "уже не актуально", "сам свяжусь", "сама свяжусь",
        "керек емес", "қажет емес", "кажет емес", "жазылмаймын",
        "бас тартамын", "кейін жазамын", "кейин жазамын", "қазір керек емес",
        "казир керек емес",
    ]

    cleaned = re.sub(r"[\s.!?,🙏🌿❤️❤-]+", "", low)
    for phrase in phrases:
        if phrase in low or cleaned == re.sub(r"[\s.!?,🙏🌿❤️❤-]+", "", phrase):
            return True

    return False


def _refuse_booking_answer(session: dict[str, Any]) -> str:
    return _tr(
        session,
        "Хорошо, спасибо, что написали 🌿 Если позже понадобится консультация — напишите нам, мы сориентируем и поможем подобрать удобное время.",
        "Жақсы, жазғаныңызға рақмет 🌿 Кейін консультация қажет болса, бізге жазыңыз — бағыттап, ыңғайлы уақыт таңдауға көмектесеміз.",
    )


def _strip_quoted_bot_text(text: str) -> str:
    """Убирает из входящего текста цитаты предыдущих сообщений бота.

    В Wazzup пользователь может ответить реплаем. Иногда в payload попадает
    процитированный текст бота + реальный ответ клиента. Нам нужен реальный ответ.
    """
    if not text:
        return text

    lines = [line.strip() for line in str(text).splitlines() if line.strip()]
    if not lines:
        return text

    bot_quote_patterns = [
        "спасибо. на какой день вам удобно прийти",
        "на какой день вам удобно прийти",
        "подскажите, пожалуйста, что вас беспокоит",
        "перед записью нужно подтвердить",
        "қаи күн ыңғайлы",
        "қай күн ыңғайлы",
        "сізді не мазалайды",
        "сейчас не получается проверить свободные окошки",
    ]

    cleaned_lines: list[str] = []
    for line in lines:
        low = _low(line)
        if any(p in low for p in bot_quote_patterns):
            continue
        if low in ("phone", "api", "admin", "api · admin", "api · null"):
            continue
        cleaned_lines.append(line)

    return "\n".join(cleaned_lines).strip() or text


def _is_visit_confirmation_reply(text: str) -> bool:
    low = _low(_strip_quoted_bot_text(text))
    if not low:
        return False

    if _has_negative_visit_intent(low):
        return False

    confirm_words = [
        "буду", "приду", "подойду", "приеду", "буду завтра", "подойду завтра",
        "да буду", "хорошо буду", "ок буду", "буду в", "подойду в", "приду в",
        "келемін", "келемин", "барамын", "барам", "барамын ертең", "ертең барамын",
        "иә барамын", "ия барамын", "жақсы барамын", "жаксы барамын",
    ]

    # Подтверждение часто содержит время: "буду в 18.00", "буду 18:00".
    has_time = bool(re.search(r"\b([01]?\d|2[0-3])[:.]\d{2}\b|\b([01]?\d|2[0-3])\s*(?:час|ч|:00)?\b", low))
    has_confirm = any(w in low for w in confirm_words)

    # Не считаем жалобы подтверждением.
    if _has_complaint(low) or _has_medical_complaint_text(low):
        return False

    return has_confirm or (has_time and any(w in low for w in ["буд", "прид", "подойд", "кел", "бар"]))


def _visit_confirmation_answer(session: dict[str, Any]) -> str:
    return _tr(
        session,
        "Хорошо, спасибо, будем ждать Вас 🌿",
        "Жақсы, рақмет, Сізді күтеміз 🌿",
    )


def _is_time_question_without_date(text: str) -> bool:
    low = _low(_strip_quoted_bot_text(text))
    if not low:
        return False

    patterns = [
        "за какое время", "на какое время", "какое время", "какие времена",
        "во сколько", "когда можно", "в какое время", "какое окно", "какие окна",
        "подсказать чтобы записаться", "подскажите время", "есть время",
        "какое свободное время", "свободное время",
        "қай уақыт", "кай уакыт", "сағат нешеде", "сагат нешеде",
        "бос уақыт", "бос уакыт", "қай уақытта", "кай уакытта",
    ]

    return any(p in low for p in patterns) and not _parse_date(text)


def _time_question_without_date_answer(session: dict[str, Any]) -> str:
    return _tr(
        session,
        "Могу подсказать свободное время 🌿 Напишите, пожалуйста, на какой день Вам удобно прийти — я сразу проверю окошки в расписании.",
        "Бос уақыттарды қарап бере аламын 🌿 Қай күні келгеніңіз ыңғайлы екенін жазыңызшы — кестеден бос уақыттарды бірден тексеремін.",
    )


def _is_later_month_or_self_schedule(text: str) -> bool:
    low = _low(_strip_quoted_bot_text(text))
    if not low:
        return False

    later_words = [
        "потом", "позже", "позже сам", "позже сама", "сам выберу", "сама выберу",
        "сам напишу", "сама напишу", "когда смогу", "как смогу",
        "только в сентябре", "в сентябре", "сентябрь", "сентябре",
        "в августе", "август", "в июле", "июль", "через месяц", "через пару месяцев",
        "после каникул", "каникулы", "сейчас сижу с внуками",
        "кейін", "кейин", "өзім жазамын", "озим жазамын", "өзім таңдаймын", "озим тандаймын",
        "қыркүйек", "кыркүйек", "қыркүйекте", "кыркуйекте",
        "тамыз", "шілде", "шилде", "бір айдан кейін", "бир айдан кейин",
    ]

    # Если пациент всё-таки назвал конкретную дату/день недели — не закрываем, пусть CRM покажет слоты.
    if _parse_date(text):
        return False

    return any(w in low for w in later_words)


def _is_vacation_later_visit(text: str) -> bool:
    low = _low(_strip_quoted_bot_text(text))
    if not low:
        return False

    vacation_words = [
        "отпуск", "отпускға", "отпускка", "отпуска",
        "демалыс", "демалысқа", "демалыска",
        "демалысқа шық", "демалыска шык", "шығамын", "шыгамын",
    ]
    visit_later_words = [
        "сонда", "баруға", "баруга", "барып", "көрінуге", "коринуге",
        "болама", "бола ма", "келейін", "келейин", "приду", "приеду",
        "когда смогу", "как смогу", "потом прийти", "позже прийти",
    ]

    return any(w in low for w in vacation_words) and any(w in low for w in visit_later_words)


def _vacation_later_visit_answer(session: dict[str, Any]) -> str:
    return _tr(
        session,
        "Хорошо, будем ждать Вас в любое удобное время 🌿 Спасибо за обращение, всего доброго!",
        "Жақсы, Сізді өзіңізге ыңғайлы уақытта күтеміз 🌿 Хабарласқаныңызға рақмет, сау болыңыз!",
    )


def _is_unknown_date_answer(text: str) -> bool:
    low = _low(_strip_quoted_bot_text(text))
    if not low:
        return False

    patterns = [
        "пока не знаю", "не знаю", "позже", "потом", "напишу позже",
        "когда дома буду", "когда буду дома", "с работы приду",
        "после работы", "на работе", "я пока на работа", "я пока на работе",
        "уточню", "надо подумать", "пока не могу сказать",
        "в отпуск", "отпуск", "когда выйду в отпуск", "после отпуска",
        "как освобожусь", "когда освобожусь",
        "кейін", "кейин", "білмеймін", "билмеймин", "үйге келгенде", "уйге келгенде",
        "демалыс", "отпуска", "отпускға", "отпускка", "демалысқа", "демалыска",
        "демалысқа шыққанда", "демалыска шыкканда", "қашан босаймын", "кашан босаймын",
    ]
    return any(p in low for p in patterns)


def _is_tentative_date_answer(text: str) -> bool:
    low = _low(_strip_quoted_bot_text(text))
    if not low:
        return False
    return any(p in low for p in [
        "следующ", "недел", "вторник", "сред", "четверг", "пятниц", "понедельник",
        "может вторник", "может среда", "на следующей неделе",
        "келесі апта", "сейсенбі", "сәрсенбі", "бейсенбі", "жұма",
    ])


def _is_doctor_can_treat_question(text: str) -> bool:
    low = _low(_strip_quoted_bot_text(text))
    return any(p in low for p in [
        "сможет леч", "сможете леч", "лечите это", "можно лечить",
        "этот сможет", "это сможет", "по фото", "по снимку", "по документу",
        "осы емдей", "емдей аласыз",
    ])


def _has_document_or_image_context(text: str) -> bool:
    low = _low(_strip_quoted_bot_text(text))
    if not low:
        return False

    short_markers = ["мрт", "кт"]
    if any(re.search(rf"(?<![а-яa-z]){marker}(?![а-яa-z])", low) for marker in short_markers):
        return True

    return any(p in low for p in [
        "фото", "документ", "снимок", "снимк", "рентген", "заключение",
        "заключен", "анализ", "сурет", "құжат", "кужат",
    ])


def _is_non_surgical_treatment_question(text: str) -> bool:
    low = _low(_strip_quoted_bot_text(text))
    if not low:
        return False

    return any(p in low for p in [
        "без операции", "безоперацион", "операциясыз",
    ]) and any(p in low for p in [
        "можно", "леч", "бола", "емде",
    ])


def _has_specific_profile_context(text: str) -> bool:
    low = _low(_strip_quoted_bot_text(text))
    if not low:
        return False

    excluded = {"операция", "операции", "операциясыз"}
    profile_words = [w for w in PROFILE_COMPLAINT_WORDS if w not in excluded]
    disease_words = [w for w in PROFILE_DISEASE_WORDS if "операц" not in w]
    return any(w in low for w in profile_words) or any(w in low for w in disease_words)


def _non_surgical_general_answer(session: dict[str, Any]) -> str:
    return _tr(
        session,
        "Да, в нашей клинике применяются безоперационные методы лечения 🌿 Но подойдёт ли такой вариант именно Вам, врач сможет сказать после первичного осмотра и оценки состояния.\n\nПодскажите, пожалуйста, что Вас беспокоит: спина, шея, суставы, грыжа, протрузия или боль отдаёт в руку/ногу?",
        "Иә, біздің клиникада операциясыз емдеу әдістері қолданылады 🌿 Бірақ бұл әдіс Сізге нақты сәйкес келе ме — дәрігер алғашқы қараудан кейін жағдайыңызды бағалап айтады.\n\nНақты не мазалайды: арқа, мойын, буын, грыжа/протрузия немесе ауырсыну қолға/аяққа беріле ме?",
    )


def _non_surgical_profile_context(text: str) -> str:
    low = _low(text)
    if any(p in low for p in ["спин", "поясниц", "арқа", "арка", "белім", "белим"]):
        return "По спине"
    if any(p in low for p in ["ше", "мойын"]):
        return "По шее"
    if any(p in low for p in ["сустав", "буын", "колен", "тізе", "тизе"]):
        return "По суставам"
    if any(p in low for p in ["грыж", "протруз"]):
        return "По грыже/протрузии"
    return "По Вашей жалобе"


def _non_surgical_profile_answer(session: dict[str, Any], text: str) -> str:
    return _tr(
        session,
        f"Да, в нашей клинике применяются безоперационные методы лечения 🌿 {_non_surgical_profile_context(text)} — это наш профиль. Подскажите, пожалуйста, сколько Вам лет?",
        "Иә, біздің клиникада операциясыз емдеу әдістері қолданылады 🌿 Бұл біздің бағыт. Жасыңыз нешеде?",
    )


def _document_non_surgical_answer(session: dict[str, Any]) -> str:
    return _tr(
        session,
        "По фото/снимку или документам точно обещать лечение не будем, чтобы не вводить Вас в заблуждение 🌿 Врач сможет сориентировать после очного осмотра и оценки состояния. Передам информацию врачу, он ответит Вам в ближайшее время.",
        "Фото/снимок немесе құжат бойынша нақты емдеуге уәде бермейміз, себебі алдымен дәрігер қарап, жағдайды бағалауы керек 🌿 Ақпаратты дәрігерге жіберемін, ол жақын уақытта жауап береді.",
    )


def _avoid_repeating_same_question(answer: str, session: dict[str, Any]) -> str:
    """Не повторять один и тот же вопрос подряд, если пользователь уже дал ответ/отложил.

    Это не заменяет антидубль webhook, но защищает диалог от зацикливания.
    """
    if not answer:
        return answer

    last = _clean(str(session.get("last_bot_answer") or ""))
    current = _clean(answer)

    if not last or last != current:
        return answer

    # Если ответ полностью совпал с прошлым, даём мягкую альтернативу.
    if "на какой день" in _low(current) or "қай күн" in _low(current):
        return _tr(
            session,
            "Когда определитесь с удобным днём и временем — напишите сюда, я продолжу запись 🌿",
            "Қай күн мен уақыт ыңғайлы екенін анықтағанда осында жазыңыз, жазылуды жалғастырамын 🌿",
        )

    if "что вас беспокоит" in _low(current) or "сізді не мазалайды" in _low(current):
        return _tr(
            session,
            "Опишите, пожалуйста, жалобу одним сообщением: что болит или что беспокоит 🌿",
            "Шағымыңызды бір хабарламада жазыңызшы: қай жеріңіз ауырады немесе не мазалайды 🌿",
        )

    return answer


def _remove_name_addressing(answer: str, session: dict[str, Any]) -> str:
    """Жёсткая защита: бот никогда не обращается к пациенту по имени/нику.

    Имя можно собрать и передать в CRM, но в исходящих сообщениях запрещено:
    - "Здравствуйте, Айжан!"
    - "Добрый день, Сергей!"
    - "Уважаемый(-ая) Съемка!"
    - "Спасибо, Арман!"
    - "Арман, подскажите..."
    """
    if not answer:
        return answer

    cleaned = str(answer)

    # Убираем универсальные обращения по имени/нику в начале любой строки.
    # Это ловит имена из Wazzup/CRM даже если они не сохранены в session.
    patterns = [
        r"(?im)^\s*Здравствуйте,\s*[^!\n]{2,80}!\s*",
        r"(?im)^\s*Добрый день,\s*[^!\n]{2,80}!\s*",
        r"(?im)^\s*Доброе утро,\s*[^!\n]{2,80}!\s*",
        r"(?im)^\s*Добрый вечер,\s*[^!\n]{2,80}!\s*",
        r"(?im)^\s*Уважаемый\(-ая\)\s*[^!\n]{1,80}!\s*",
        r"(?im)^\s*Уважаемый\s*[^!\n]{1,80}!\s*",
        r"(?im)^\s*Уважаемая\s*[^!\n]{1,80}!\s*",
    ]
    for pattern in patterns:
        cleaned = re.sub(pattern, "", cleaned)

    # Если конкретное имя/ник лежит в session — убираем персональное обращение.
    possible_names = [
        session.get("patient_name"),
        session.get("name"),
        session.get("client_name"),
        session.get("patientName"),
        session.get("contact_name"),
        session.get("contactName"),
        session.get("wazzup_name"),
        session.get("wazzupName"),
    ]

    for name in possible_names:
        if not name:
            continue
        n = str(name).strip()
        if not n:
            continue
        cleaned = re.sub(rf"(?im)^\s*{re.escape(n)}\s*,\s*", "", cleaned)
        cleaned = re.sub(rf"(?im)^\s*Здравствуйте,\s*{re.escape(n)}!\s*", "Здравствуйте! ", cleaned)
        cleaned = re.sub(rf"(?im)^\s*Добрый день,\s*{re.escape(n)}!\s*", "Добрый день! ", cleaned)
        cleaned = re.sub(rf"(?im)^\s*Уважаемый\(-ая\)\s*{re.escape(n)}!\s*", "", cleaned)

    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    if not cleaned:
        cleaned = _tr(session, "Спасибо, принято 🌿", "Рақмет, қабылданды 🌿")
    return cleaned


def _strict_trim_extra(answer: str, session: dict[str, Any]) -> str:
    """Мягкая защита от лишней отсебятины.

    Обязательные вопросы сценария не обрезаем.
    """
    if not answer:
        return answer

    low = _low(answer)

    allow_long = any(x in low for x in [
        "перед записью нужно подтвердить",
        "перед записью мне нужно уточнить",
        "противопоказан",
        "ваш визит в neuro balance",
        "запись подтверждена",
        "отлично, запись оформлена",
        "2гис",
        "2gis",
        "кардиостимулятор",
        "процесс записи останавливаю",
        "этим направлением",
        "сколько вам лет",
        "стоимость курса",
        "первичный приём",
        "первичный прием",
        "это наша специализация",
        "я сориентирую",
        "это окошко можем закрепить",
        "перед записью обязательно уточню",
        "здравствуйте! да, это наша специализация",
        "сәлеметсіз бе! иә",
        "біздің клиниканың бағыты",
        "жасыңыз нешеде",
        "жасыныз нешеде",
        "на какой день вам удобно",
        "безоперационные методы лечения",
        "магнитотерапия",
        "консультацию проводит врач",
        "количество дней и процедур",
        "процедурные дни",
        "қай күн ыңғайлы",
        "кай кун ыңгайлы",
        "какое вам удобно",
        "қайсысы ыңғайлы",
        "кайсысы ыңгайлы",
        "для оформления записи",
        "противопоказаний нет",
        "қарсы көрсетілімдер жоқ",
        "жазбаны рәсімдеу",
        "в стоимость уже входит",
        "местоположение в 2gis",
        "каспи red",
        "отдел забот",
        "у нас нет филиалов",
        "современные методы",
        "европейском аппарате",
        "подбирается индивидуально",
        "снимок заранее делать не обязательно",
        "график приёма",
    ])

    if allow_long:
        return answer.strip()

    banned_fragments = [
        "мы команда профессионалов",
        "гарантируем результат",
        "обязательно вылечим",
        "лучшие специалисты",
        "не переживайте, мы вас вылечим",
    ]

    cleaned = answer
    for fragment in banned_fragments:
        cleaned = re.sub(re.escape(fragment), "", cleaned, flags=re.I)

    sentences = re.split(r"(?<=[.!?])\s+", cleaned.strip())
    if len(sentences) > 2:
        cleaned = " ".join(sentences[:2]).strip()

    return cleaned.strip()

_TIME_PATTERN = re.compile(r"\b(?:[01]?\d|2[0-3])[:.][0-5]\d\b")
DATE_TIME_NAME_BLOCK_WORDS = (
    "сегодня", "завтра", "послезавтра", "понедельник", "вторник", "среда",
    "понеддельник", "понеделник", "пандельник",
    "среду", "четверг", "пятница", "пятницу", "суббота", "субботу",
    "воскресенье", "утром", "днём", "днем", "после обеда", "вечером",
    "не рано", "попозже", "пораньше", "часов",
)
TIME_PREFERENCE_WORDS = ("после обеда", "вечером", "не рано", "попозже", "пораньше", "утром", "днем", "днём")
_FORBIDDEN_EMPTY_SLOT_PHRASES = (
    "есть свободные слоты",
    "есть такие свободные",
    "показываю свободные слоты",
    "свободные слоты",
    "выберите подходящий",
    "выберите удобный",
)


def _contains_date_time_preference(text: str) -> bool:
    low = _low(text)
    if _TIME_PATTERN.search(low) or re.search(r"\b(?:[01]?\d|2[0-3])\s*(?:час|ч)\b", low):
        return True
    return any(w in low for w in DATE_TIME_NAME_BLOCK_WORDS)


def _slot_times_answer(session: dict[str, Any]) -> str:
    slots = session.get("last_slots") or []
    times = [_slot_time(slot) for slot in slots if isinstance(slot, dict) and _slot_time(slot)]
    times = list(dict.fromkeys(times))
    if not times:
        return _tr(session, "Какое время Вам удобно?", "Қай уақыт ыңғайлы?")
    joined = ", ".join(times[:-1]) + (" и " + times[-1] if len(times) > 1 else times[0])
    pref = str(session.get("time_preference") or "").strip()
    if pref:
        return _tr(
            session,
            f"{pref.capitalize()} есть {joined} 🌿 Какое время Вам удобно?",
            f"{joined} уақыттары бар 🌿 Қай уақыт ыңғайлы?",
        )
    return _tr(
        session,
        f"Есть такие свободные окошки: {joined} 🌿 Какое время Вам удобно?",
        f"Таңдалған күнге бос уақыттар бар: {joined} 🌿 Қай уақыт ыңғайлы?",
    )


def _cleanup_final_wazzup_text(answer: str) -> str:
    text = str(answer or "")
    text = text.replace("---", "\n")
    for phrase in ("Вижу Ваш запрос", "Ваш запрос принят"):
        text = re.sub(re.escape(phrase), "", text, flags=re.I)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip(" \n.,")


def _validate_final_fact_answer(chat_id: str, session: dict[str, Any], answer: str) -> str:
    """Python/CRM remains the only source of truth for clinic facts and slots."""
    text = _clean(answer)
    low = _low(text)
    slots = session.get("last_slots") or []
    step = _low(str(session.get("step") or ""))
    crm_empty = session.get("crm_availability_empty") is True
    slot_display_context = step in {"date", "preferred_time", "time", "select_slot"} and not session.get("selected_slot") and not session.get("booked")
    has_time = bool(_TIME_PATTERN.search(text))
    forbidden_phrase = any(p in low for p in _FORBIDDEN_EMPTY_SLOT_PHRASES)
    if (crm_empty or (slot_display_context and not slots)) and (has_time or forbidden_phrase):
        _safe_log(chat_id, "llm_slot_hallucination_blocked", {"chat_id": chat_id, "date": session.get("preferred_date") or "", "step": session.get("step") or "", "slots_count": len(slots) if isinstance(slots, list) else 0})
        session["last_slots"] = []
        session["step"] = "date"
        return _no_slots_text(session)
    price_mentions = re.findall(r"\b(?:[1-9]\d{0,2}\s?000|[1-9]\d{3,})\s*(?:тг|тенге|₸)", low)
    allowed_prices = {"5 000", "5000", "2 000", "2000", "3 000", "3000"}
    if price_mentions and any(re.sub(r"\D+", "", mention) not in {re.sub(r'\D+', '', p) for p in allowed_prices} for mention in price_mentions):
        _safe_log(chat_id, "llm_price_fact_blocked", {"chat_id": chat_id, "step": session.get("step") or "", "answer_preview": text[:180]})
        return _tr(
            session,
            "Первичный приём — 5 000 тг. Стоимость курса врач рассчитывает только после осмотра 🌿",
            "Алғашқы қабылдау — 5 000 тг. Ем курсының құнын дәрігер тек қараудан кейін есептейді 🌿",
        )
    if "кабанбай" in low and "28" not in low:
        _safe_log(chat_id, "llm_address_fact_blocked", {"chat_id": chat_id, "step": session.get("step") or "", "answer_preview": text[:180]})
        return _tr(
            session,
            "Адрес: Астана, Кабанбай батыра 28, внутренний двор, подъезд 3. Заезд со стороны Кунаева, после ворот направо.",
            "Мекенжай: Астана, Қабанбай батыр 28, ішкі аула, 3-подъезд. Қонаев жағынан кіріп, шлагбаумнан кейін оңға бұрыласыз.",
        )
    return text


def _finalize(chat_id: str, session: dict[str, Any], answer: str) -> str:
    session["chat_id"] = chat_id
    before_step = str(session.get("step") or "start")
    answer = _clean(answer)
    if str(session.get("step") or "") == "complaint" and ("что Вас беспокоит" in answer or "не мазалайды" in answer):
        repaired, repair_reason = False, ""
        session["state_repaired"] = False
        session["state_repair_reason"] = ""
    else:
        repaired, repair_reason = _repair_state_consistency(session, str(session.get("last_user_text") or ""))
    if repaired and _is_active_new_ai_request(session) and not answer:
        answer, _, _ = build_safe_answer_for_current_state(session, str(session.get("last_user_text") or ""))
        session["fallback_reason"] = "state_inconsistency_repaired"
    if session.get("contraindications_ok") is True and (str(session.get("step") or "") == "contraindications" or _answer_contains_contraindications_question(answer)):
        repaired_step, repaired_answer = _repair_after_contraindications_ok(session)
        if not _answer_contains_contraindications_question(answer):
            answer = answer
        else:
            answer = repaired_answer
        _safe_log(chat_id, "contraindications_ok_final_guard_repaired", {"chat_id": chat_id, "repaired_step": repaired_step, "answer_preview": answer[:180]})
    _cleanup_contraindications_after_ok(session)
    answer = _repair_forbidden_required_question(chat_id, session, answer)
    answer = _validate_final_fact_answer(chat_id, session, answer)
    if session.get("last_slots") and not session.get("selected_time"):
        session["step"] = "time"
        if not answer or "на какой день" in _low(answer) or "сейчас посмотр" in _low(answer):
            answer = _slot_times_answer(session)
    if str(session.get("step") or "") == "time" and session.get("last_slots") and not session.get("selected_time") and not str(answer or "").strip():
        answer = _slot_times_answer(session)
    if not (session.get("step") == "booked" and session.get("booking_confirmed") is True):
        answer = _remove_name_addressing(answer, session)
    answer = _strict_trim_extra(answer, session)
    answer = _cleanup_final_wazzup_text(answer)
    # final_contraindications_repair:
    # Последний барьер перед сохранением/возвратом ответа. Он должен перебивать
    # OpenAI/Brain/fallback/старые шаблоны и запрещать длинный чек-лист в обычном
    # booking flow, если пациент явно не попросил список противопоказаний.
    if session.get("contraindications_ok") is True:
        if str(session.get("step") or "") == "contraindications" or "противопоказ" in _low(answer):
            _safe_log(chat_id, "contraindications_ok_final_answer_repaired", {"chat_id": chat_id, "step": session.get("step") or "", "answer_preview": answer[:180]})
            session["step"] = "date"
            session["questionnaire_step"] = "date"
            answer = _ask_date(session)
    elif str(session.get("step") or "") == "contraindications" and not _contra_details_question(str(session.get("last_user_text") or "")):
        safe_contra_answer = _ask_contra(session)
        if answer != safe_contra_answer:
            _safe_log(chat_id, "contraindications_checklist_final_repaired", {"chat_id": chat_id, "answer_preview": answer[:180]})
        answer = safe_contra_answer
    if not answer and str(session.get("step") or "") == "time" and _is_active_new_ai_request(session):
        session["repair_reason"] = "empty_answer_time_recovered"
        session["fallback_reason"] = "empty_answer_time_recovered"
        _safe_log(chat_id, "empty_answer_time_recovered", {"chat_id": chat_id, "from_step": before_step, "preferred_date": session.get("preferred_date") or "", "slots_count": len(session.get("last_slots") or [])})
        answer = _slot_times_answer(session) if session.get("last_slots") else _tr(
            session,
            "Сейчас уточню свободные окошки и напишу Вам варианты 🌿",
            "Қазір бос уақыттарды нақтылап, Сізге нұсқаларын жазамын 🌿",
        )
    if not answer and _is_active_new_ai_request(session):
        repair = repair_empty_active_reply(session, str(session.get("last_user_text") or ""), "empty_active_reply")
        session["fallback_reason"] = "empty_active_reply"
        _safe_log(chat_id, "empty_active_reply_repaired", {"chat_id": chat_id, "from_step": before_step, "to_step": repair.step, "state_repair_reason": repair_reason})
        _safe_log(chat_id, "fallback_used", {"chat_id": chat_id, "reason": "empty_active_reply", "step": repair.step})
        answer = repair.answer
    elif not answer:
        if session.get("complaint") and not session.get("age"):
            answer = _ask_age(session)
        else:
            answer = _tr(
                session,
                "Подскажите, пожалуйста, что Вас беспокоит? 🌿",
                "Сізді не мазалайды? 🌿",
            )

    # duplicate_answer_guard: на одно входящее сообщение — один ответ.
    # Если новый текст полностью совпадает с последним ответом бота, молчим,
    # чтобы Wazzup не получал одинаковые сообщения подряд.
    last_answer = _clean(str(session.get("last_assistant_answer") or session.get("last_bot_answer") or ""))
    if last_answer and _low(last_answer) == _low(answer):
        _safe_save(chat_id, session)
        return ""

    session["state_before_step"] = before_step
    session["state_after_step"] = session.get("step") or ""
    if session.get("step") != "escalated":
        _safe_log(chat_id, "final_state_after_decision", {"chat_id": chat_id, "state_before_step": before_step, "state_after_step": session.get("step") or "", "answer_empty": not bool(answer), "fallback_reason": session.get("fallback_reason") or "", "state_repaired": bool(session.get("state_repaired")), "state_repair_reason": session.get("state_repair_reason") or ""})

    session["last_assistant_answer"] = answer
    _remember_required_question(session, answer)
    _safe_save(chat_id, session)
    _safe_add_message(chat_id, "assistant", answer)
    return answer


def _recent_history_for_brain(chat_id: str, session: dict[str, Any]) -> list[dict[str, Any]]:
    """Build compact context-first history for the LLM Dialog Brain."""
    raw = state.get_history(chat_id, limit=24) if hasattr(state, "get_history") else []
    items: list[dict[str, Any]] = []
    for item in raw[-20:]:
        text = str(item.get("text") or item.get("content") or "").strip()
        role = str(item.get("role") or "").strip()
        if not text or role not in {"user", "assistant", "bot", "admin", "human", "operator", "manager"}:
            continue
        row: dict[str, Any] = {"role": "assistant" if role == "bot" else role, "text": text}
        if role in {"assistant", "bot", "admin"}:
            row["step"] = session.get("last_required_step") or session.get("step") or ""
        items.append(row)
    return items


def _remember_required_question(session: dict[str, Any], answer: str) -> None:
    qtype = _classify_bot_question(answer)
    step_by_type = {
        "complaint": "complaint",
        "age": "age",
        "contraindications": "contraindications",
        "date": "date",
        "time": "time",
        "name": "name",
    }
    if qtype == "contraindications" and session.get("contraindications_ok") is True:
        _cleanup_contraindications_after_ok(session)
    elif qtype in step_by_type:
        new_step = step_by_type[qtype]
        if session.get("last_required_step") == new_step:
            session["last_required_question_count"] = int(session.get("last_required_question_count") or 0) + 1
        else:
            session["last_required_question_count"] = 1
        session["last_required_question"] = answer
        session["last_required_step"] = new_step
        session["pending_step_after_faq"] = ""
    session["conversation_turns_count"] = int(session.get("conversation_turns_count") or 0) + 1
    facts = session.get("known_user_facts") if isinstance(session.get("known_user_facts"), dict) else {}
    for key in ("complaint", "age", "contraindications_ok", "preferred_date", "selected_slot", "patient_name", "symptom_duration"):
        val = session.get(key)
        if val not in (None, "", [], {}):
            facts[key] = val
    session["known_user_facts"] = facts
    important = []
    for key in ("complaint", "age", "contraindications_ok", "preferred_date", "selected_slot"):
        if key in facts:
            important.append(f"{key}={facts[key]}")
    if important:
        session["dialog_summary"] = "; ".join(important)[-1200:]


def _no_reply(chat_id: str, session: dict[str, Any], reason: str = "") -> str:
    """Сохраняет состояние, но ничего не отправляет пациенту.

    Нужно после завершения записи: если пациент пишет "хорошо/спасибо/ок",
    бот не должен дублировать подтверждение и не должен запускать анкету заново.
    """
    session["last_assistant_answer"] = session.get("last_assistant_answer", "")
    if reason:
        session["no_reply_reason"] = reason
    _safe_save(chat_id, session)
    return ""


def _classify_bot_question(text: str) -> str:
    low = _low(text)
    if not low:
        return "unknown"
    if any(x in low for x in ["из какого города", "какого города", "қай қаладан", "кай каладан"]):
        return "city"
    if any(x in low for x in ["планируете приехать в астану", "сможете приехать в астану", "астанаға кел", "астанага кел"]):
        return "astana_visit"
    if any(x in low for x in ["что вас беспокоит", "чем можем помочь", "не мазалай", "мәселе мазалай", "меселе мазалай"]):
        return "complaint"
    if any(x in low for x in ["сколько вам лет", "жасыңыз", "жасыныз", "қанша жаста", "канша жаста"]):
        return "age"
    if any(x in low for x in ["противопоказ", "қарсы көрсет", "карсы корсет", "кардиостимулятор", "противопоказаний нет"]):
        return "contraindications"
    if any(x in low for x in ["какой день", "на какой день", "қай күн", "кай кун", "удобный день"]):
        return "date"
    if any(x in low for x in ["ваше имя", "атыңыз", "атыныз", "имя для оформления"]):
        return "name"
    if any(x in low for x in ["какое время", "қайсысы ыңғайлы", "кайсысы ынгайлы", "вариант", "окошк"]):
        return "time"
    if _has_mri_question(low) or any(x in low for x in ["сним", "мрт", "түсірілім", "тусирилим"]):
        return "mri"
    if _has_any(low, PRICE_WORDS):
        return "price"
    if _has_any(low, ADDRESS_WORDS):
        return "address"
    if _has_any(low, SCHEDULE_WORDS):
        return "schedule"
    if any(x in low for x in ["уже запис", "имеющейся записи", "бұрынғы жазба", "бурынгы жазба"]):
        return "existing_appointment"
    return "unknown"


def _has_active_conversation_context(session: dict[str, Any]) -> bool:
    """Detect an already-started lead funnel, including short replies to bot/admin questions."""
    step = _low(str(session.get("step") or ""))
    if step in {"complaint", "age", "contraindications", "date", "time", "name"}:
        return True
    if session.get("ai_lead_started") is True:
        return True
    qtype = str(session.get("last_bot_question_type") or "")
    if qtype and qtype != "unknown":
        return True
    if session.get("complaint") or session.get("age") or session.get("last_slots"):
        return True
    if session.get("asked_city") or session.get("asked_complaint"):
        return True
    last_answer = str(session.get("last_bot_answer") or session.get("last_assistant_answer") or "")
    return bool(last_answer and _classify_bot_question(last_answer) != "unknown")


def _is_reply_to_active_question(session: dict[str, Any], text: str) -> bool:
    """Return True for non-empty patient replies while the funnel is waiting for an answer."""
    if not _clean(text):
        return False
    step = _low(str(session.get("step") or ""))
    if step in {"complaint", "age", "contraindications", "date", "time", "name"}:
        return True
    qtype = str(session.get("last_bot_question_type") or "")
    if qtype and qtype != "unknown":
        return True
    last_answer = str(session.get("last_bot_answer") or session.get("last_assistant_answer") or "")
    return bool(last_answer and _classify_bot_question(last_answer) != "unknown")


def _message_role(item: dict[str, Any]) -> str:
    return _low(str(item.get("role") or item.get("type") or ""))


def _build_conversation_context(chat_id: str, session: dict[str, Any], text: str) -> dict[str, Any]:
    """Lightweight history layer: infer what the short incoming reply answers."""
    history = state.get_history(chat_id, limit=20)
    last_bot_question = ""
    last_user_message = ""
    last_admin_message = ""
    prior_complaint_text = str(session.get("complaint") or "").strip()
    has_prior_contra_question = False
    awaiting_complaint_answer = False
    for item in history:
        role = _message_role(item)
        content = str(item.get("content") or "").strip()
        if not content:
            continue
        if role in ("assistant", "bot"):
            last_bot_question = content
            classified = _classify_bot_question(content)
            awaiting_complaint_answer = classified == "complaint"
            if classified == "contraindications":
                has_prior_contra_question = True
        elif role in ("admin", "human", "operator", "manager"):
            last_admin_message = content
            qtype = _classify_bot_question(content)
            if qtype != "unknown":
                last_bot_question = content
            awaiting_complaint_answer = qtype == "complaint"
            if qtype == "contraindications":
                has_prior_contra_question = True
        elif role == "user":
            if content != text:
                last_user_message = content
                if (
                    not prior_complaint_text
                    and (
                        ((_has_complaint(content) or _has_medical_complaint_text(content)) and _profile_status(content) != "non_profile")
                        or awaiting_complaint_answer
                    )
                ):
                    prior_complaint_text = content
            awaiting_complaint_answer = False

    last_bot_question_type = _classify_bot_question(last_bot_question)
    short_reply = len(re.findall(r"[a-zа-яәіңғүұқөһ0-9]+", _low(text))) <= 4
    likely_reply = short_reply and last_bot_question_type != "unknown"
    should_continue = bool(
        prior_complaint_text
        or session.get("age")
        or session.get("contraindications_ok") is not None
        or session.get("last_slots")
        or (session.get("step") or "start") not in ("start", "", None)
    )
    ctx = {
        "last_bot_question": last_bot_question,
        "last_bot_question_type": last_bot_question_type,
        "has_prior_complaint": bool(prior_complaint_text),
        "prior_complaint_text": prior_complaint_text,
        "has_prior_age": bool(session.get("age")),
        "has_prior_contra_question": has_prior_contra_question,
        "has_prior_slots": bool(session.get("last_slots")),
        "last_user_message": last_user_message,
        "last_admin_message": last_admin_message,
        "was_manual_admin_recent": bool(last_admin_message or session.get("manual_admin_intervention") or session.get("manual_takeover")),
        "should_continue_existing_flow": should_continue,
        "likely_reply_to_previous_question": likely_reply,
        "inferred_context_action": "",
        "used_history_context": False,
    }
    return ctx


def _apply_conversation_context(session: dict[str, Any], ctx: dict[str, Any], text: str) -> None:
    qtype = str(ctx.get("last_bot_question_type") or "unknown")
    if ctx.get("has_prior_complaint") and not session.get("complaint"):
        session["complaint"] = ctx.get("prior_complaint_text") or ""
        if session["complaint"]:
            _record_complaint_tool(session, session["complaint"], is_in_profile=True)
    if not ctx.get("likely_reply_to_previous_question"):
        return
    if qtype == "age":
        session["step"] = "age"
        ctx["inferred_context_action"] = "answer_age"
        ctx["used_history_context"] = True
    elif qtype == "contraindications":
        session["step"] = "contraindications"
        ctx["inferred_context_action"] = "answer_contraindications"
        ctx["used_history_context"] = True
    elif qtype == "date":
        session["step"] = "date"
        ctx["inferred_context_action"] = "answer_date"
        ctx["used_history_context"] = True
    elif qtype == "time" and session.get("last_slots"):
        session["step"] = "time"
        ctx["inferred_context_action"] = "select_slot"
        ctx["used_history_context"] = True
    elif qtype == "name":
        session["step"] = "name"
        ctx["inferred_context_action"] = "answer_name"
        ctx["used_history_context"] = True
    elif qtype == "mri" and ctx.get("has_prior_complaint"):
        session["mri_answer"] = text
        session["had_mri"] = any(w in _low(text) for w in ["да", "иа", "ия", "иә", "yes", "болды"])
        session["step"] = "age" if not session.get("age") else "contraindications"
        ctx["inferred_context_action"] = "answer_mri_continue_booking"
        ctx["used_history_context"] = True


def _last_answer_was_info(session: dict[str, Any]) -> bool:
    last = _low(str(session.get("last_assistant_answer") or session.get("last_bot_answer") or ""))
    if not last:
        return False

    info_markers = [
        "адрес", "2гис", "2gis", "кабанбай", "кунаева", "қабанбай", "мекенжай",
        "стоимость", "цена", "приём", "прием", "5000", "5 000", "бағасы", "багасы",
        "график", "режим", "работаем", "выходной", "дүйсенбі", "понедельник",
        "instagram", "tiktok", "тик ток", "инстаграм",
        "находимся", "мы находимся", "орналасқан",
    ]
    return any(marker in last for marker in info_markers)


def _extract_age(text: str, step: str = "") -> int | None:
    low = _low(text)

    # Не путать время с возрастом: "10:30" не возраст.
    if re.search(r"\b\d{1,2}[:.]\d{2}\b", low):
        return None

    # Прямые формы возраста RU/KZ.
    patterns = [
        r"\bмне\s*(\d{1,2})\s*(?:лет|года|год)?\b",
        r"\bмен\s*(\d{1,2})\s*(?:жастамын|жаста|жас)?\b",
        r"\b(\d{1,2})\s*(?:лет|года|год|жас|жастамын|жаста)\b",
    ]
    for pat in patterns:
        m = re.search(pat, low)
        if m:
            age = int(m.group(1))
            if 1 <= age <= 99:
                return age

    # Не путать длительность боли с возрастом: "3 день болит" не возраст.
    if re.search(r"\b\d{1,2}\s*(день|дня|дней|недел|неделя|месяц|месяцев|сутки)\b", low):
        return None

    # Если мы явно ждём возраст — можно принять просто число.
    nums = re.findall(r"\b(\d{1,2})\b", low)
    if nums and step == "age":
        age = int(nums[0])
        if 1 <= age <= 99:
            return age

    return None


def _age_stop_text(age: int, session: dict[str, Any]) -> str:
    if age < 16:
        return _stop_booking_text(session, "under_16")
    if 16 <= age < 18:
        return _tr(
            session,
            "Так как Вам нет 18 лет, на консультацию нужно прийти с родителем или законным представителем 🌿",
            "18 жасқа толмағандықтан, консультацияға ата-анаңызбен немесе заңды өкіліңізбен келу керек 🌿",
        )
    if age > 75:
        return _stop_booking_text(session, "over_75")
    return ""


def _has_booking_intent(text: str) -> bool:
    return _has_any(text, BOOKING_WORDS)


def _has_complaint(text: str) -> bool:
    return _has_any(text, COMPLAINT_WORDS)


def _has_no_complaint(text: str) -> bool:
    return _has_any(text, NO_COMPLAINT_WORDS)


def _is_positive_confirm(text: str) -> bool:
    low = _low(text)
    return any(w in low for w in [
        "да", "хочу", "запишите", "можно", "ок", "окей", "давайте",
        "иә", "ия", "жаз", "жазылы", "болады", "келісемін", "келисемин",
    ])


def _is_greeting_only(text: str) -> bool:
    low = _low(text)
    words = re.sub(r"[^\wа-яА-ЯәіңғүұқөһӘІҢҒҮҰҚӨҺ]+", " ", low).split()
    return bool(words) and len(words) <= 3 and any(w in words for w in ["здравствуйте", "привет", "салем", "сәлем", "доброе"])


def _parse_date(text: str) -> str | None:
    low = _low(text)
    for typo in ("понеддельник", "понеделник", "понедельникк", "пандельник"):
        low = low.replace(typo, "понедельник")
    today = (datetime.now(timezone.utc) + timedelta(hours=5)).date()

    if any(w in low for w in ["сегодня", "бүгін", "бугин"]):
        return today.isoformat()
    if "послезавтра" in low:
        return (today + timedelta(days=2)).isoformat()
    if any(w in low for w in ["завтра", "ертең", "ертен"]):
        return (today + timedelta(days=1)).isoformat()

    weekdays = {
        "понедельник": 0, "в понедельник": 0, "дүйсенбі": 0, "дуйсенби": 0,
        "вторник": 1, "во вторник": 1, "сейсенбі": 1, "сейсенби": 1,
        "среда": 2, "среду": 2, "в среду": 2, "сәрсенбі": 2, "сарсенби": 2,
        "четверг": 3, "в четверг": 3, "бейсенбі": 3, "бейсенби": 3,
        "пятница": 4, "пятницу": 4, "в пятницу": 4, "жұма": 4, "жума": 4,
        "суббота": 5, "субботу": 5, "в субботу": 5, "сенбі": 5, "сенби": 5,
        "воскресенье": 6, "в воскресенье": 6, "жексенбі": 6, "жексенби": 6,
    }

    has_next_week = any(p in low for p in [
        "следующ", "на следующей неделе", "келесі апта", "келеси апта",
    ])

    for name, wd in weekdays.items():
        if name in low:
            delta = (wd - today.weekday()) % 7
            if delta == 0:
                delta = 7

            # Если человек явно пишет "на следующей неделе в понедельник",
            # не передаём координатору, а считаем дату и показываем слоты.
            # Если ближайший такой день ещё на этой неделе, переносим на неделю вперёд.
            if has_next_week and delta < 7:
                delta += 7

            return (today + timedelta(days=delta)).isoformat()

    m = re.search(r"\b(\d{1,2})[.\-/](\d{1,2})(?:[.\-/](\d{2,4}))?\b", low)
    if m:
        d, mo, y = m.groups()
        year = int(y) if y else today.year
        if year < 100:
            year += 2000
        try:
            return datetime(year, int(mo), int(d)).date().isoformat()
        except ValueError:
            return None

    return None

def _time_from_text(text: str) -> str | None:
    m = re.search(r"\b([01]?\d|2[0-3])[:./\-\s]+([0-5]\d)\b", text or "")
    if m:
        return f"{int(m.group(1)):02d}:{m.group(2)}"

    m = re.search(r"\b([8-9]|1\d|2[0-3])\s*(?:час|ч|:00)?\b", _low(text))
    if m:
        return f"{int(m.group(1)):02d}:00"

    return None


def _format_slots(slots_data: dict[str, Any], max_count: int = 5) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []

    # CRM format: {"availability":[{"doctorLogin": "...", "doctorName": "...", "date": "...", "availableSlots":["09:00"]}]}
    for item in slots_data.get("availability", []) or []:
        doctor_login = item.get("doctorLogin") or item.get("doctor_login") or ""
        doctor_name = item.get("doctorName") or item.get("doctor_name") or "Врач клиники"
        date = item.get("date") or ""
        for time_start in item.get("availableSlots", []) or item.get("slots", []) or []:
            if isinstance(time_start, dict):
                time_start = time_start.get("timeStart") or time_start.get("time") or ""
            if not time_start:
                continue
            result.append({
                "doctorLogin": str(doctor_login),
                "doctorName": str(doctor_name),
                "date": str(date),
                "timeStart": str(time_start),
                "doctor_login": str(doctor_login),
                "doctor_name": str(doctor_name),
                "time": str(time_start),
            })
            if len(result) >= max_count:
                return result

    # Fallback format: {"slots":[...]}
    for item in slots_data.get("slots", []) or []:
        if isinstance(item, str):
            result.append({"doctorLogin": "", "doctorName": "Врач клиники", "date": "", "timeStart": item, "doctor_login": "", "doctor_name": "Врач клиники", "time": item})
        elif isinstance(item, dict):
            doctor_login = str(item.get("doctorLogin") or item.get("doctor_login") or "")
            doctor_name = str(item.get("doctorName") or item.get("doctor_name") or "Врач клиники")
            time_start = str(item.get("timeStart") or item.get("time_start") or item.get("time") or "")
            result.append({
                "doctorLogin": doctor_login,
                "doctorName": doctor_name,
                "date": str(item.get("date") or ""),
                "timeStart": time_start,
                "doctor_login": doctor_login,
                "doctor_name": doctor_name,
                "time": time_start,
            })
        if len(result) >= max_count:
            return result

    return result


def _slots_text(slots: list[dict[str, str]], lang: str) -> str:
    lines = []
    for i, slot in enumerate(slots, 1):
        date = _slot_date(slot)
        time = _slot_time(slot)
        doctor = _slot_doctor_name(slot) or "Врач клиники"
        if lang == "kk":
            lines.append(f"{i}) {date} {time} — {doctor}")
        else:
            lines.append(f"{i}) {date} в {time} — {doctor}")
    return "\n".join(lines)


def _slot_doctor_login(slot: dict[str, Any]) -> str:
    return str(slot.get("doctor_login") or slot.get("doctorLogin") or "")


def _slot_doctor_name(slot: dict[str, Any]) -> str:
    return str(slot.get("doctor_name") or slot.get("doctorName") or "")


def _slot_date(slot: dict[str, Any]) -> str:
    return str(slot.get("date") or slot.get("preferred_date") or "")


def _slot_time(slot: dict[str, Any]) -> str:
    return str(slot.get("time") or slot.get("timeStart") or slot.get("time_start") or "")


def _selected_slot_from_session(session: dict[str, Any]) -> dict[str, Any]:
    """Build a CRM booking slot from denormalized selected_* fields."""
    return {
        "doctorLogin": session.get("selected_doctor_login") or "",
        "doctorName": session.get("selected_doctor_name") or "",
        "date": session.get("selected_date") or session.get("preferred_date") or "",
        "timeStart": session.get("selected_time") or "",
        "doctor_login": session.get("selected_doctor_login") or "",
        "doctor_name": session.get("selected_doctor_name") or "",
        "time": session.get("selected_time") or "",
    }


def _booking_ready(session: dict[str, Any], phone: str = "") -> bool:
    """Return True when all required final booking fields are present."""
    try:
        age_ok = int(session.get("age") or 0) > 0
    except (TypeError, ValueError):
        age_ok = False
    normalized_phone = sanitize_kz_phone(phone or session.get("phone") or "")
    return bool(
        session.get("patient_name")
        and normalized_phone
        and session.get("complaint")
        and age_ok
        and session.get("contraindications_ok") is True
        and session.get("selected_date")
        and session.get("selected_time")
        and (session.get("selected_doctor_login") or session.get("selected_doctor_name"))
    )


def _select_slot(text: str, slots: list[dict[str, str]]) -> dict[str, str] | None:
    low = _low(text)

    m = re.search(r"\b([1-9])\b", low)
    if m:
        idx = int(m.group(1)) - 1
        if 0 <= idx < len(slots):
            return slots[idx]

    ordinal_indexes = {
        "первый": 0,
        "первая": 0,
        "первое": 0,
        "первую": 0,
        "первого": 0,
        "второй": 1,
        "вторая": 1,
        "второе": 1,
        "вторую": 1,
        "второго": 1,
        "третий": 2,
        "третья": 2,
        "третье": 2,
        "третью": 2,
        "третьего": 2,
    }
    for word, idx in ordinal_indexes.items():
        if re.search(rf"\b{re.escape(word)}\b", low) and idx < len(slots):
            return slots[idx]

    t = _time_from_text(text)
    if t:
        for slot in slots:
            if _slot_time(slot) == t:
                return slot

    return None


def _explicit_slot_selection_text(text: str, slots: list[dict[str, str]]) -> bool:
    low = _low(text)
    if _time_from_text(text):
        return any(_slot_time(slot) == _time_from_text(text) for slot in slots)
    return bool(
        re.search(r"\b([1-9])\s*(?:вариант|окно|окошко)\b", low)
        or any(w in low for w in ("первый", "первая", "первое", "первую", "второй", "вторая", "второе", "вторую", "третий", "третья", "третье", "третью", "пятое окно", "5 вариант"))
    )


def _remember_selected_slot(session: dict[str, Any], slot: dict[str, Any]) -> None:
    """Persist the exact CRM slot payload and denormalized booking fields."""
    last_slots = session.get("last_slots") or []
    if last_slots and not any(existing == slot for existing in last_slots if isinstance(existing, dict)):
        session.pop("selected_slot", None)
        session["slot_selection_rejected_reason"] = "not_in_session_last_slots"
        return
    if session.get("contraindications_ok") is True and not session.get("contraindications_verdict"):
        # Keep in-flight sessions created before the tool-gate markers bookable
        # as soon as the patient chooses a concrete slot.
        bot_tools.verify_contraindications(session, bot_tools.CONTRA_PROCEED, str(session.get("contraindications_raw") or "нет"))
    if session.get("complaint") and not session.get("complaint_gate"):
        _record_complaint_tool(session, str(session.get("complaint") or ""), is_in_profile=True)

    session["selected_slot"] = slot
    session["selected_doctor_login"] = _slot_doctor_login(slot)
    session["selected_doctor_name"] = _slot_doctor_name(slot)
    session["selected_date"] = _slot_date(slot) or session.get("preferred_date")
    session["selected_time"] = _slot_time(slot)


def _looks_like_name(text: str) -> bool:
    clean = _clean(text)
    low = _low(clean)
    if not clean or len(clean) > 60:
        return False
    if any(ch.isdigit() for ch in clean):
        return False
    if low in NAME_BANNED_WORDS:
        return False
    if _has_any(low, BOOKING_WORDS + COMPLAINT_WORDS + PRICE_WORDS + ADDRESS_WORDS):
        return False
    return bool(re.match(r"^[A-Za-zА-Яа-яӘәҒғҚқҢңӨөҰұҮүҺһІі\-\s]{2,}$", clean))


def _extract_name(text: str) -> str:
    clean = _clean(text)
    if _contains_date_time_preference(clean):
        return ""

    patterns = [
        r"\bменя\s+зовут\s+([A-Za-zА-Яа-яӘәҒғҚқҢңӨөҰұҮүҺһІі\-\s]{2,50})",
        r"\bзовут\s+([A-Za-zА-Яа-яӘәҒғҚқҢңӨөҰұҮүҺһІі\-\s]{2,50})",
        r"\bмо[её]\s+имя\s+([A-Za-zА-Яа-яӘәҒғҚқҢңӨөҰұҮүҺһІі\-\s]{2,50})",
        r"\bатым\s+([A-Za-zА-Яа-яӘәҒғҚқҢңӨөҰұҮүҺһІі\-\s]{2,50})",
        r"\bменің\s+атым\s+([A-Za-zА-Яа-яӘәҒғҚқҢңӨөҰұҮүҺһІі\-\s]{2,50})",
    ]
    low = clean.lower()
    for p in patterns:
        m = re.search(p, low, flags=re.I)
        if m:
            name = _clean(m.group(1)).strip(" .,!?:;")
            return name.title() if _looks_like_name(name) else ""

    return clean.title() if _looks_like_name(clean) else ""


def _method_answer(session: dict[str, Any]) -> str:
    template = _clinic_info_template(session, "methods")
    if template:
        return "В клинике применяются безоперационные методы лечения: магнитотерапия, лазерная терапия, УВТ, PRP, иглотерапия и ЛФК 🌿\n\n" + template
    return _tr(
        session,
        "В клинике применяются безоперационные методы лечения боли в спине, шее, грыж, протрузий и суставов: магнитотерапия, лазерная терапия, ударно-волновая терапия, PRP, иглотерапия и ЛФК 🌿",
        "Клиникада арқа, мойын, грыжа, протрузия және буын ауруларын емдеудің операциясыз әдістері қолданылады: магнитотерапия, лазерлік терапия, соққы-толқынды терапия, PRP, инетерапия және ЕДШ 🌿",
    )


def _doctor_answer(session: dict[str, Any], chat_id: str = "") -> str:
    step = _low(str(session.get("step") or ""))
    slots = session.get("last_slots") or []
    if step in {"date", "time", "select_slot"}:
        names: list[str] = []
        for slot in slots if isinstance(slots, list) else []:
            if not isinstance(slot, dict):
                continue
            name = _slot_doctor_name(slot).strip()
            if name and name not in names and name.lower() != "врач клиники":
                names.append(name)
        if names:
            _safe_log(chat_id or str(session.get("chat_id") or "system"), "doctor_names_from_slots", {"chat_id": chat_id or str(session.get("chat_id") or ""), "date": session.get("preferred_date") or "", "step": step, "slots_count": len(slots)})
            return _tr(
                session,
                "По актуальным свободным окошкам доступны специалисты: " + ", ".join(names) + " 🌿 Выберите, пожалуйста, удобное время из вариантов выше.",
                "Актуалды бос уақыттар бойынша мамандар: " + ", ".join(names) + " 🌿 Жоғарыдағы ыңғайлы уақытты таңдаңызшы.",
            )
        _safe_log(chat_id or str(session.get("chat_id") or "system"), "doctor_names_unavailable_without_slots", {"chat_id": chat_id or str(session.get("chat_id") or ""), "date": session.get("preferred_date") or "", "step": step, "slots_count": 0})
        return _tr(
            session,
            "Врач зависит от выбранного дня и доступного расписания. Когда подберём свободное окошко, я покажу варианты по актуальному расписанию 🌿 Подскажите, пожалуйста, какой день Вам удобен?",
            "Дәрігер таңдалған күнге және актуалды кестеге байланысты. Бос уақытты таңдағанда актуалды кесте бойынша нұсқаларды көрсетемін 🌿 Қай күн ыңғайлы?",
        )
    return _tr(
        session,
        "Да, консультацию проводит врач 🌿 Он осматривает, оценивает состояние и подбирает индивидуальный план лечения.",
        "Иә, консультацияны дәрігер жүргізеді 🌿 Ол қарап, жағдайды бағалап, жеке ем жоспарын таңдайды.",
    )


def _course_duration_answer(session: dict[str, Any]) -> str:
    return _tr(
        session,
        "Количество дней и процедур врач сможет определить только после первичного осмотра 🌿 Всё зависит от диагноза, состояния и индивидуального плана лечения.",
        "Күндер мен процедуралар санын дәрігер алғашқы қараудан кейін ғана анықтай алады 🌿 Бәрі диагнозға, жағдайға және жеке ем жоспарына байланысты.",
    )


def _clinic_answer(text: str, session: dict[str, Any]) -> str | None:
    if _has_any(text, RETURNING_PATIENT_WORDS):
        bot_tools.escalate_to_human(session, "returning_patient")
        return _clinic_info_template(session, "returning_patient")
    if _has_any(text, REFUND_WORDS):
        bot_tools.escalate_to_human(session, "refund_or_complaint")
        return _clinic_info_template(session, "refund_complaint")
    if _has_any(text, PHONE_CALL_WORDS):
        session["phone_call_requested"] = True
        return _clinic_info_template(session, "phone_call_request")
    if _has_any(text, OTHER_CITY_WORDS):
        return _clinic_info_template(session, "other_city")
    if _has_any(text, IMMOBILITY_WORDS):
        bot_tools.escalate_to_human(session, "immobility")
        return _clinic_info_template(session, "immobility_refuse")
    if _has_any(text, TOO_EXPENSIVE_WORDS):
        session["objection_handled"] = True
        return _clinic_info_template(session, "objection_too_expensive")
    if _has_any(text, WILL_THINK_WORDS):
        session["objection_handled"] = True
        return _clinic_info_template(session, "objection_will_think")
    if _has_any(text, HELPS_WORDS):
        return _clinic_info_template(session, "helps_question")
    if _has_any(text, INSTALLMENT_WORDS):
        return _clinic_info_template(session, "installment")
    if _has_any(text, METHOD_WORDS):
        return _method_answer(session)
    if _has_any(text, DOCTOR_WORDS):
        return _doctor_answer(session)
    if _has_any(text, COURSE_DURATION_WORDS):
        return _course_duration_answer(session)
    if _has_any(text, PRICE_WORDS):
        return _price_answer(text, session)
    if _has_any(text, ADDRESS_WORDS):
        return _address_answer(session)
    if _has_any(text, SCHEDULE_WORDS):
        return _schedule_answer(session)
    if _has_mri_question(text):
        return _clinic_info_template(session, "mri_needed") or _tr(
            session,
            "Снимки и МРТ заранее делать не обязательно 🌿 На первичном осмотре врач сам посмотрит Ваше состояние и, если потребуется, назначит МРТ/КТ или другое обследование.",
            "Снимок немесе МРТ-ны алдын ала жасау міндетті емес 🌿 Алғашқы қаралу кезінде дәрігер жағдайыңызды өзі қарап, қажет болса МРТ/КТ немесе басқа тексеріс тағайындайды.",
        )
    return None


def _mri_answer_in_flow(session: dict[str, Any]) -> str:
    return _clinic_info_template(session, "mri_needed") or _tr(
        session,
        "Снимки и МРТ заранее делать не обязательно 🌿 На первичном осмотре врач сам посмотрит Ваше состояние и, если потребуется, назначит МРТ/КТ или другое обследование.",
        "Снимок немесе МРТ-ны алдын ала жасау міндетті емес 🌿 Алғашқы қаралу кезінде дәрігер жағдайыңызды өзі қарап, қажет болса МРТ/КТ немесе басқа тексеріс тағайындайды.",
    )


def _ask_complaint(session: dict[str, Any]) -> str:
    return _tr(
        session,
        "Здравствуйте! Подскажите, пожалуйста, что Вас беспокоит? Я сориентирую, относится ли это к профилю нашей клиники 🌿",
        "Сәлеметсіз бе! Нақты не мазалайды? Бұл біздің клиниканың бағытына жата ма — соны айтып, бағыттаймын 🌿",
    )


def _ask_age(session: dict[str, Any]) -> str:
    return _tr(
        session,
        "Подскажите, пожалуйста, сколько Вам лет?",
        "Жасыңыз нешеде?",
    )
def _human_profile_age_answer(session: dict[str, Any], text: str) -> str:
    low = _low(text)

    if any(w in low for w in ["поясниц", "пояснич", "бел"]):
        ru = "Поняла Вас. Поясничная боль — по нашему направлению 🌿\nПодскажите, пожалуйста, сколько Вам лет?"
        kk = "Түсіндім. Бел ауыруы біздің бағытқа жатады 🌿\nЖасыңыз нешеде?"
    elif any(w in low for w in ["спина", "спине", "спину", "арқа", "арка"]):
        ru = "Поняла, спина беспокоит. Это наш профиль 🌿\nПодскажите, пожалуйста, сколько Вам лет?"
        kk = "Түсіндім, арқаңыз мазалап тұр екен. Бұл біздің бағыт 🌿\nЖасыңыз нешеде?"
    elif any(w in low for w in ["протруз", "грыж", "грыжа"]):
        ru = "Поняла. Протрузии и грыжи относятся к нашему профилю 🌿\nПодскажите, пожалуйста, сколько Вам лет?"
        kk = "Түсіндім. Протрузия мен грыжа біздің бағытқа жатады 🌿\nЖасыңыз нешеде?"
    elif any(w in low for w in ["шея", "шей", "мойын"]):
        ru = "Поняла, шея беспокоит. Это по нашему направлению 🌿\nПодскажите, пожалуйста, сколько Вам лет?"
        kk = "Түсіндім, мойын мазалап тұр екен. Бұл біздің бағыт 🌿\nЖасыңыз нешеде?"
    elif any(w in low for w in ["онем", "немеет", "рук", "нога", "ногу", "аяқ", "аяк"]):
        ru = "Поняла Вас. Онемение или боль, которая отдаёт в руку/ногу, относится к нашему профилю 🌿\nПодскажите, пожалуйста, сколько Вам лет?"
        kk = "Түсіндім. Қолға/аяққа берілетін ауырсыну немесе ұю біздің бағытқа жатады 🌿\nЖасыңыз нешеде?"
    elif any(w in low for w in ["сустав", "колен", "плеч", "локт", "тазобед", "буын", "тізе", "тизе"]):
        ru = "Поняла Вас. По суставам тоже принимаем 🌿\nПодскажите, пожалуйста, сколько Вам лет?"
        kk = "Түсіндім. Буын бойынша да қабылдаймыз 🌿\nЖасыңыз нешеде?"
    else:
        ru = "Поняла Вас. Это относится к нашему профилю 🌿\nПодскажите, пожалуйста, сколько Вам лет?"
        kk = "Түсіндім. Бұл біздің клиниканың бағытына жатады 🌿\nЖасыңыз нешеде?"

    return _tr(session, ru, kk)


def _profile_confirm_and_ask_age(session: dict[str, Any], text: str = "") -> str:
    return _human_profile_age_answer(session, text or str(session.get("complaint") or ""))
def _profile_confirm_next_step(session: dict[str, Any]) -> str:
    # Если возраст уже был написан раньше, не спрашиваем его повторно.
    if session.get("age"):
        return _tr(
            session,
            "Да, это относится к нашему профилю 🌿",
            "Иә, бұл біздің клиниканың бағытына жатады 🌿",
        ) + "\n\n" + _ask_contra(session)

    return _profile_confirm_and_ask_age(session, str(session.get("complaint") or ""))



def _has_leg_radiation_profile(text: str) -> bool:
    low = _low(text)
    patterns = [
        "отдаёт в ногу", "отдает в ногу", "боль отдаёт в ногу", "боль отдает в ногу",
        "тянет ногу", "немеет нога", "в ногу стреляет", "стреляет в ногу",
        "отдаёт на ногу", "отдает на ногу",
    ]
    return any(p in low for p in patterns)

def _ask_age_contextual(session: dict[str, Any], text: str) -> str:
    parts_ru: list[str] = []
    parts_kk: list[str] = []

    if _has_any(text, PRICE_WORDS):
        parts_ru.append("Приём в нашей клинике — 5 000 тг 🌿")
        parts_kk.append("Біздің клиникада алғашқы қабылдау — 5 000 тг 🌿")

    if _has_mri_question(text):
        parts_ru.append("Снимки и МРТ заранее делать не обязательно 🌿 На первичном осмотре врач сам посмотрит Ваше состояние и, если потребуется, назначит обследование.")
        parts_kk.append("Снимок немесе МРТ-ны алдын ала жасау міндетті емес 🌿 Алғашқы қаралу кезінде дәрігер қажет болса тексеріс тағайындайды.")

    profile_answer = _human_profile_age_answer(session, text)
    parts_ru.append(profile_answer)
    parts_kk.append(profile_answer)

    return _tr(session, "\n\n".join(parts_ru), "\n\n".join(parts_kk))
def _senior_contra_intro(session: dict[str, Any]) -> str:
    return _stop_booking_text(session, "over_75")


def _ask_contra(session: dict[str, Any]) -> str:
    return _tr(
        session,
        "Перед записью уточню для безопасности 🌿 Есть ли у Вас какие-нибудь противопоказания?",
        "Жазбас бұрын қауіпсіздік үшін нақтылайын 🌿 Сізде қандай да бір Қарсы көрсетілімдер бар ма?",
    )


def _contra_details_question(text: str) -> bool:
    low = _low(text)
    return any(
        phrase in low
        for phrase in (
            "какие противопоказания",
            "какие именно",
            "перечислите",
            "что входит",
            "что входит в противопоказания",
            "список противопоказаний",
            "какие есть противопоказания",
        )
    )


def _contra_detailed_list(session: dict[str, Any]) -> str:
    sent_count = int(session.get("contraindications_checklist_sent_count") or 0)
    session["contraindications_checklist_sent_count"] = sent_count + 1
    return _tr(
        session,
        'Основные противопоказания: кардиостимулятор/дефибриллятор, инсулиновая помпа, кохлеарный имплант, беременность, онкология или подозрение на неё, металл в зоне лечения, эпилепсия/судороги, тромбоз или нарушения свёртываемости, декомпенсированный диабет/тиреотоксикоз, температура/ОРВИ/острая инфекция, тяжёлые проблемы с сердцем, дыханием или психическим состоянием. Также приём не проводится пациентам младше 16 или старше 75 лет и при ограниченной подвижности — коляска, костыли.\n\nЕсть ли у Вас что-то из этого?',
        'Негізгі қарсы көрсетілімдер: кардиостимулятор/дефибриллятор, инсулин помпасы, кохлеарлық имплант, жүктілік, онкология немесе оған күдік, емдеу аймағында металл, эпилепсия/судорога, тромбоз немесе қан ұюының бұзылысы, декомпенсацияланған диабет/тиреотоксикоз, қызу/ЖРВИ/жедел инфекция, жүрек, тыныс алу немесе психикалық жағдай бойынша ауыр мәселе. Сондай-ақ 16 жасқа дейінгі, 75 жастан асқан және қозғалысы шектеулі пациенттерге — коляска, костыли — қабылдау жүргізілмейді.\n\nСізде осының біреуі бар ма?',
    )


def _ask_date(session: dict[str, Any]) -> str:
    return _tr(
        session,
        "Отлично, спасибо 🌿 На какой день Вам удобно прийти?",
        "Жақсы, рақмет 🌿 Қай күн ыңғайлы?",
    )


def _ask_name(session: dict[str, Any]) -> str:
    return _tr(
        session,
        "Хорошо, это окошко можем закрепить за Вами 🌿 Подскажите, пожалуйста, Ваше имя для оформления записи.",
        "Жақсы, бұл уақытты Сізге бекіте аламыз 🌿 Жазбаны рәсімдеу үшін атыңызды жазыңызшы.",
    )


def _format_booking_date_human(date_iso: str, lang: str = "ru") -> str:
    try:
        dt = datetime.fromisoformat(str(date_iso)).date()
    except Exception:
        return str(date_iso or "")
    if lang == "kk":
        months = [
            "қаңтар", "ақпан", "наурыз", "сәуір", "мамыр", "маусым",
            "шілде", "тамыз", "қыркүйек", "қазан", "қараша", "желтоқсан",
        ]
        return f"{dt.day} {months[dt.month - 1]}"
    months = [
        "января", "февраля", "марта", "апреля", "мая", "июня",
        "июля", "августа", "сентября", "октября", "ноября", "декабря",
    ]
    return f"{dt.day} {months[dt.month - 1]}"


def _booking_success_answer(session: dict[str, Any], booked: dict[str, Any], slot: dict[str, Any]) -> str:
    patient_name = str(session.get("patient_name") or "Пациент").strip()
    date = booked.get("date") or _slot_date(slot) or session.get("selected_date") or session.get("preferred_date") or ""
    time_start = booked.get("timeStart") or booked.get("time_start") or _slot_time(slot) or session.get("selected_time") or ""
    doctor = booked.get("doctorName") or booked.get("doctor_name") or _slot_doctor_name(slot) or session.get("selected_doctor_name") or "врачу клиники"
    date_human = _format_booking_date_human(date, session.get("language") or "ru")
    return _tr(
        session,
        (
            "Отлично, запись оформлена 🌿\n\n"
            f"{patient_name}, записала Вас — ждём Вас {date_human} в {time_start} к врачу {doctor}.\n\n"
            "Адрес: Кабанбай батыра 28, внутренний двор, подъезд 3.\n"
            "Заезд со стороны Кунаева, после ворот поверните направо.\n\n"
            "2ГИС: https://2gis.kz/astana/inside/9570784863354265/firm/70000001105992248?m=71.416112%2C51.134091%2F16"
        ),
        (
            f"{patient_name}, Сізді {date_human} күні сағат {time_start} дәрігер {doctor} қабылдауына жаздым 🌿\n\n"
            "Мекенжай: Қабанбай батыр 28, ішкі аула, 3-подъезд. Күтеміз!"
        ),
    )


def _booking_failed_answer(session: dict[str, Any]) -> str:
    patient_name = str(session.get("patient_name") or "Пациент").strip()
    return _tr(
        session,
        f"{patient_name}, данные для записи собрала 🌿 Сейчас передам администратору, чтобы он подтвердил запись.",
        f"{patient_name}, жазылуға қажет деректерді жинадым 🌿 Қазір әкімшіге жіберемін, ол жазбаны растайды.",
    )


def _no_slots_text(session: dict[str, Any]) -> str:
    return _tr(
        session,
        "На выбранный день свободных окошек не нашла 🌿 Подскажите, пожалуйста, другой удобный день — проверю актуальное расписание.",
        "Таңдалған күнге бос уақыт таппадым 🌿 Басқа ыңғайлы күнді жазыңызшы — актуалды кестені тексеремін.",
    )


def _has_video_procedure_question(text: str) -> bool:
    low = _low(text).replace("ё", "е")
    compact = re.sub(r"[^\w\s]", " ", low)
    compact = re.sub(r"\s+", " ", compact).strip()
    patterns = [
        "так же будет",
        "так же делают",
        "как на видео",
        "как в видео",
        "точно так делают",
        "это будут делать",
        "все как на видео",
        "процедура такая же",
        "процедуры как на видео",
        "в инстаграме",
        "в instagram",
        "осылай болады ма",
        "видеодагыдай болады ма",
        "видеодағыдай болады ма",
    ]
    return any(pattern in compact for pattern in patterns)


def _video_procedure_answer(session: dict[str, Any]) -> str:
    return _tr(
        session,
        "Да, процедуры проходят по методике клиники, примерно как показано в видео 🌿 Но точный план врач подбирает после осмотра, потому что всё зависит от Вашего состояния.",
        "Иә, процедуралар клиника әдістемесі бойынша, видеода көрсетілгендей форматта өтуі мүмкін 🌿 Бірақ нақты ем жоспарын дәрігер алғашқы қараудан кейін жағдайыңызға қарай таңдайды.",
    )


async def _show_slots(chat_id: str, session: dict[str, Any], date_iso: str) -> str:
    session["preferred_date"] = date_iso
    lang = session.get("language") or "ru"

    try:
        max_slots = 5
        if get_settings:
            try:
                max_slots = int(getattr(get_settings(), "max_slots_to_show", 5) or 5)
            except Exception:
                pass

        data = await crm.check_slots(date_iso)
        slots = _format_slots(data, max_count=max_slots)
    except Exception as exc:
        _safe_log(chat_id, "crm_check_slots_error", {"error": str(exc)[:500]})
        session["step"] = "escalated"
        session["manual_takeover"] = True
        session["escalated"] = True
        return _crm_slots_unavailable_answer(session)

    if not slots:
        session["last_slots"] = []
        session.pop("selected_slot", None)
        session["step"] = "date"
        session["crm_availability_empty"] = True
        _safe_log(chat_id, "crm_slots_empty", {"chat_id": chat_id, "date": date_iso, "step": session.get("step") or "date", "slots_count": 0})
        return _no_slots_text(session)

    session["crm_availability_empty"] = False
    session["last_slots"] = slots
    session["step"] = "time"
    if session.get("time_preference"):
        return _slot_times_answer(session)
    return _tr(
        session,
        "Есть такие свободные окошки:\n" + _slots_text(slots, lang) + "\n\nКакое время Вам удобно?",
        "Таңдалған күнге бос уақыттар бар:\n" + _slots_text(slots, lang) + "\n\nҚай уақыт ыңғайлы?",
    )


def _crm_slots_unavailable_answer(session: dict[str, Any]) -> str:
    return _tr(
        session,
        "Сейчас не вижу свободные окошки по системе. Передам администратору, чтобы он помог подобрать время 🌿",
        "Қазір жүйеден бос уақыттарды көре алмай тұрмын. Уақыт таңдауға көмектесу үшін әкімшіге жіберемін 🌿",
    )


async def _recover_empty_time_slots(chat_id: str, session: dict[str, Any], text: str) -> str | None:
    """Recover time-step sessions whose CRM slots were lost between messages."""
    if str(session.get("step") or "") not in {"time", "select_slot"}:
        return None
    if session.get("last_slots") or session.get("selected_time"):
        return None
    preferred_date = str(session.get("preferred_date") or session.get("selected_date") or "").strip()
    if not preferred_date:
        return None

    wanted_time = _time_from_text(text)
    try:
        max_slots = 5
        if get_settings:
            try:
                max_slots = int(getattr(get_settings(), "max_slots_to_show", 5) or 5)
            except Exception:
                pass
        data = await crm.check_slots(preferred_date)
        slots = _format_slots(data, max_count=max_slots)
    except Exception as exc:
        _safe_log(chat_id, "crm_check_slots_error", {"error": str(exc)[:500], "recovery": "empty_time_slots"})
        session["step"] = "escalated"
        session["manual_takeover"] = True
        session["escalated"] = True
        return _crm_slots_unavailable_answer(session)

    if not slots:
        _safe_log(chat_id, "crm_slots_empty_time_recovery", {"chat_id": chat_id, "date": preferred_date})
        session["step"] = "escalated"
        session["manual_takeover"] = True
        session["escalated"] = True
        return _crm_slots_unavailable_answer(session)

    session["preferred_date"] = preferred_date
    session["last_slots"] = slots
    session["step"] = "time"
    session["crm_availability_empty"] = False

    if wanted_time:
        slot = next((slot for slot in slots if _slot_time(slot) == wanted_time), None)
        if slot:
            _remember_selected_slot(session, slot)
            session["step"] = "name"
            session["questionnaire_step"] = "name"
            return _ask_name(session)
        return _slot_times_answer(session)

    return _slot_times_answer(session)




async def _refresh_slots_after_book_conflict(chat_id: str, session: dict[str, Any], date_iso: str) -> str:
    try:
        crm.clear_slots_cache(date_iso)
    except Exception:
        pass

    prefix = _tr(
        session,
        "К сожалению, это окошко уже заняли 🌿 Сейчас покажу актуальные свободные варианты.",
        "Өкінішке қарай, бұл уақытты жаңа ғана алып қойды 🌿 Қазір өзекті бос уақыттарды көрсетемін.",
    )
    session.pop("selected_slot", None)
    session.pop("selected_time", None)
    session["step"] = "time"
    refreshed = await _show_slots(chat_id, session, date_iso)
    session["step"] = "time"
    return prefix + "\n\n" + refreshed

async def _book(chat_id: str, session: dict[str, Any], phone: str) -> str:
    normalized_phone = sanitize_kz_phone(phone or session.get("phone") or "")
    slot = session.get("selected_slot") or {}
    if not slot and (
        session.get("selected_date")
        and session.get("selected_time")
        and (session.get("selected_doctor_login") or session.get("selected_doctor_name"))
    ):
        slot = _selected_slot_from_session(session)
        session["selected_slot"] = slot

    if not normalized_phone:
        session["step"] = "phone"
        return _tr(
            session,
            "Не вижу номер телефона. Напишите, пожалуйста, Ваш номер в формате 77001234567.",
            "Телефон нөмірі көрінбей тұр. Нөміріңізді 77001234567 форматында жазыңызшы.",
        )

    if not slot:
        session["step"] = "date"
        return _ask_date(session)
    last_slots = session.get("last_slots") or []
    if last_slots and not any(existing == slot for existing in last_slots if isinstance(existing, dict)):
        session.pop("selected_slot", None)
        session["step"] = "time" if last_slots else "date"
        _safe_log(chat_id, "booking_payload_blocked_not_from_last_slots", {"chat_id": chat_id, "step": session.get("step") or "", "slots_count": len(last_slots) if isinstance(last_slots, list) else 0})
        return _mandatory_step_prompt(session, session["step"])
    if not last_slots:
        _safe_log(chat_id, "booking_payload_legacy_selected_slot_without_last_slots", {"chat_id": chat_id, "step": session.get("step") or ""})

    if session.get("contraindications_ok") is True and not session.get("contraindications_verdict"):
        # Backward-compatible migration for existing sessions created before tool gates.
        bot_tools.verify_contraindications(session, bot_tools.CONTRA_PROCEED, str(session.get("contraindications_raw") or "нет"))
    if session.get("complaint") and not session.get("complaint_gate"):
        # Existing in-flight Python sessions had complaint text but no old-bot marker yet.
        _record_complaint_tool(session, str(session.get("complaint") or ""), is_in_profile=True)

    gate_ok, gate_reason = bot_tools.booking_gate_status(session)
    if not gate_ok:
        if gate_reason == "complaint":
            session["step"] = "complaint"
            return _tr(
                session,
                "Перед записью уточню, что именно Вас беспокоит — так врач сможет корректно подготовиться 🌿",
                "Жазбас бұрын нақты не мазалайтынын анықтайын — дәрігер дұрыс дайындала алады 🌿",
            )
        if gate_reason == "contra_refuse":
            session["step"] = "stopped"
            return _stop_booking_text(session, "contra")
        session["step"] = "contraindications"
        return _ask_contra(session)

    bot_tools.mark_tool(session, "book_appointment", gate="passed")

    try:
        booked = await crm.book_appointment(
            patient_name=session.get("patient_name") or "Пациент",
            phone=normalized_phone,
            doctor_login=_slot_doctor_login(slot),
            doctor_name=_slot_doctor_name(slot) or None,
            date=_slot_date(slot) or session.get("preferred_date"),
            time_start=_slot_time(slot),
            notes=(
                f"Жалоба: {session.get('complaint') or ''}; "
                f"возраст: {session.get('age') or ''}; "
                f"противопоказания/ограничения: {session.get('contraindications_raw') or ''}; "
                f"важно для врача: {'да' if session.get('doctor_note_required') else 'нет'}"
            ),
        )
        session["booked"] = True
        session["appointment"] = booked
        session["step"] = "booked"
        session["appointment_status"] = "booked"
        session["ai_muted"] = True
        session["manual_takeover"] = False
        session["booking_confirmed"] = True
        session["no_reply_reason"] = ""
        session["status"] = "booked"
        session["crm_status"] = "Записан"
        _safe_log(chat_id, "crm_booking_success", {"appointment": booked, "doctor_login": _slot_doctor_login(slot), "date": _slot_date(slot), "time_start": _slot_time(slot)})

        return _booking_success_answer(session, booked, slot)
    except crm.CRMResponseError as exc:
        log_payload = {
            "error": str(exc)[:500],
            "status_code": exc.status_code,
            "response_text": exc.response_text[:2000],
            "response_json": exc.data,
            "code": exc.code,
            "gate": bot_tools.booking_gate_status(session),
            "doctor_login": _slot_doctor_login(slot),
            "date": _slot_date(slot) or session.get("preferred_date"),
            "time_start": _slot_time(slot),
            "selected_slot": slot,
        }
        _safe_log(chat_id, "crm_booking_failed", log_payload)

        session["step"] = "escalated"
        session["escalated"] = True
        session["manual_takeover"] = True
        session["ai_muted"] = True
        session["handoff_reason"] = f"crm_book_{exc.status_code}"
        bot_tools.escalate_to_human(session, session["handoff_reason"])
        session["step"] = "escalated"
        return _booking_failed_answer(session)
    except Exception as exc:
        _safe_log(
            chat_id,
            "crm_booking_failed",
            {
                "error": str(exc)[:500],
                "gate": bot_tools.booking_gate_status(session),
                "doctor_login": _slot_doctor_login(slot),
                "date": _slot_date(slot) or session.get("preferred_date"),
                "time_start": _slot_time(slot),
                "selected_slot": slot,
            },
        )
        session["step"] = "escalated"
        session["escalated"] = True
        session["manual_takeover"] = True
        session["ai_muted"] = True
        bot_tools.escalate_to_human(session, "crm_book_exception")
        session["step"] = "escalated"
        return _booking_failed_answer(session)


async def _handle_existing_lookup(chat_id: str, phone: str, session: dict[str, Any], text: str = "") -> str:
    normalized = sanitize_kz_phone(phone or session.get("phone") or "") or phone
    try:
        lookup = await crm.patient_lookup(normalized)
        session["patient_lookup"] = lookup
        session["patient_lookup_done"] = True

        appt = None
        if isinstance(lookup, dict) and lookup.get("hasActiveAppointment"):
            raw = lookup.get("lastAppointment") or lookup.get("appointment") or {}
            if isinstance(raw, dict) and raw:
                appt = raw

        if appt:
            date = appt.get("date") or appt.get("appointmentDate") or ""
            time = appt.get("timeStart") or appt.get("time_start") or appt.get("time") or ""
            doctor = appt.get("doctorName") or appt.get("doctor_name") or ""
            session["step"] = "done"
            if date and time:
                return _tr(session, f"Вы уже записаны на {date} в {time} к {doctor or 'врачу клиники'} 🌿", f"Сіз {date} күні {time} уақытқа {doctor or 'клиника дәрігеріне'} жазылғансыз 🌿")
            details = ", ".join(str(x) for x in [date, time, doctor] if x) or "активная запись"
            return _tr(session, f"Вы записаны: {details} 🌿", f"Сіз жазылғансыз: {details} 🌿")
    except Exception as exc:
        _safe_log(chat_id, "patient_lookup_error", {"error": str(exc)[:500]})

    low = _low(text)
    wants_move = _is_cancel(text) or any(w in low for w in [
        "перенести", "перенес", "поменять время", "на завтра", "завтра",
        "ауыстыр", "басқа уақыт", "ертең", "ертен"
    ])
    has_contra_note = _contra_has_hard_stop(text) or any(w in low for w in ["кардиостимулятор", "кардиостимулятор бар"])

    session["step"] = "escalated"
    session["escalated"] = True

    if wants_move:
        answer = _tr(
            session,
            "Поняла Вас. Передам администратору, чтобы он проверил Вашу запись, напомнил дату и время и помог перенести на удобное время 🌿",
            "Түсіндім. Әкімшіге жіберемін, ол жазбаңызды тексеріп, күні мен уақытын еске салып, ыңғайлы уақытқа ауыстыруға көмектеседі 🌿",
        )
    else:
        answer = _tr(
            session,
            "Поняла Вас. Я передам администратору, чтобы он проверил Вашу запись и напомнил дату и время 🌿",
            "Түсіндім. Жазбаңызды тексеріп, күні мен уақытын еске салу үшін әкімшіге жіберемін 🌿",
        )

    if has_contra_note:
        answer += "\n\n" + _tr(
            session,
            "Информацию про кардиостимулятор обязательно передадим врачу.",
            "Кардиостимулятор туралы ақпаратты дәрігерге міндетті түрде жеткіземіз.",
        )

    return answer

def _wants_existing_lookup(text: str) -> bool:
    low = _low(text)
    if any(w in low for w in LOOKUP_WORDS):
        return True
    if any(w in low for w in ["на какое число", "на какую дату", "какое число записали", "какую дату записали", "на какое время", "когда запись"]):
        return True
    has_existing = any(w in low for w in ["уже", "моя", "мою", "у меня", "менің", "меним"])
    has_record = any(w in low for w in ["запис", "запись", "жазыл", "жазба"])
    has_time = any(w in low for w in ["когда", "время", "дат", "во сколько", "напом", "қашан", "уақыт"])
    return has_existing and has_record and has_time


def _is_cancel(text: str) -> bool:
    return _has_any(text, CANCEL_WORDS)


def _is_reschedule_request(text: str) -> bool:
    low = _low(text)
    return any(w in low for w in [
        "перенес", "перенести", "перенесите", "поменять время", "изменить время",
        "на другое время", "на другую дату", "басқа уақыт", "баска уакыт",
        "ауыстыр", "ауыстыру", "ауыстырыңыз",
    ])


def _is_cancel_direct_request(text: str) -> bool:
    low = _low(text)
    return any(w in low for w in [
        "отмен", "уберите запись", "снимите запись", "я не приду",
        "не приду", "не смогу прийти", "не смогу приехать",
        "келмеймін", "келе алмаймын", "тоқтат", "токтат",
    ])


async def _handle_cancel_appointment(chat_id: str, phone: str, session: dict[str, Any], text: str) -> str:
    """Автоматически отменяет активную запись через CRM.

    CRM умеет отменять последнюю активную запись по телефону даже без appointmentId.
    Если lookup не сработал — всё равно пробуем cancel по телефону.
    """
    normalized = sanitize_kz_phone(phone or session.get("phone") or "") or (phone or session.get("phone") or "")
    normalized = _clean(str(normalized))

    if not normalized:
        session["step"] = "phone"
        return _tr(
            session,
            "Для отмены записи напишите, пожалуйста, номер телефона, на который оформляли запись.",
            "Жазбаны тоқтату үшін жазба рәсімделген телефон нөмірін жазыңызшы.",
        )

    appointment_id = None

    # Сначала пробуем найти активную запись, чтобы отменить точнее.
    try:
        lookup = await crm.patient_lookup(normalized)
        session["patient_lookup"] = lookup

        if isinstance(lookup, dict):
            appt = lookup.get("lastAppointment") or lookup.get("appointment") or None
            if isinstance(appt, dict):
                appointment_id = appt.get("id") or appt.get("appointmentId")

            # Если lookup точно ответил, что активной записи нет — не вызываем cancel вслепую.
            if lookup.get("hasActiveAppointment") is False:
                session["step"] = "escalated"
                session["escalated"] = True
                return _tr(
                    session,
                    "Сейчас не нашла активную запись автоматически. Передам администратору, чтобы он проверил и отменил вручную 🌿",
                    "Қазір белсенді жазбаны автоматты түрде таба алмадым. Әкімшіге жіберемін, ол тексеріп, қолмен тоқтатады 🌿",
                )
    except Exception as exc:
        # Lookup может быть недоступен/залимичен — всё равно пробуем cancel по телефону.
        _safe_log(chat_id, "patient_lookup_before_cancel_error", {"error": str(exc)[:500]})

    try:
        result = await crm.cancel_appointment(
            phone=normalized,
            appointment_id=appointment_id,
            reason=f"отмена через бота: {text[:200]}",
        )
        session["cancel_result"] = result
        session["step"] = "done"
        session["status"] = "cancelled"
        session["cancelled"] = True
        session["escalated"] = False

        if isinstance(result, dict) and result.get("alreadyCancelled"):
            return _tr(
                session,
                "Эта запись уже была отменена. Будем рады Вам в другой раз🌿",
                "Бұл жазба бұрын тоқтатылған. Сізді басқа уақытта күтеміз🌿",
            )

        return _tr(
            session,
            "Запись отменили. Хорошо, будем рады Вам в другой раз🌿",
            "Жазба тоқтатылды. Жақсы, Сізді басқа уақытта күтеміз🌿",
        )
    except Exception as exc:
        _safe_log(chat_id, "crm_cancel_error", {"error": str(exc)[:500]})
        session["step"] = "escalated"
        session["escalated"] = True
        return _tr(
            session,
            "Передам администратору, чтобы он проверил запись и помог с отменой 🌿",
            "Әкімшіге жіберемін, ол жазбаны тексеріп, тоқтатуға көмектеседі 🌿",
        )


def _llm_claims_contraindication_stop(decision: dict[str, Any]) -> bool:
    extracted = decision.get("extracted") if isinstance(decision.get("extracted"), dict) else {}
    safety = decision.get("safety") if isinstance(decision.get("safety"), dict) else {}
    reply = _low(str(decision.get("reply") or ""))
    return (
        bool(safety.get("hard_stop"))
        or bool(extracted.get("contraindication_confirmed"))
        or any(phrase in reply for phrase in [
            "является противопоказанием",
            "процесс записи останавливаю",
            "не можем записать из-за противопоказания",
            "не можем вас записать из-за противопоказания",
        ])
    )


def _unknown_contra_safe_answer(session: dict[str, Any]) -> str:
    return _tr(
        session,
        UNKNOWN_CONTRA_SAFE_ANSWER_RU,
        "Рақмет, түсіндім. Бұл тармақ біздің негізгі қарсы көрсетілімдер чек-листінде жоқ, бірақ медициналық жағынан қателеспеу үшін ақпаратты әкімшіге нақтылауға жіберемін 🌿\n\nТізімдегі басқа қарсы көрсетілімдер жоқ па?",
    )


def _contra_has_hard_stop(text: str) -> bool:
    low = _low(text)
    if _looks_like_contra_term_question(low):
        return False

    # Отрицания считаем только если "нет/жоқ" стоит рядом с конкретным словом.
    direct_neg_patterns = [
        r"(?:нет|нету|жоқ|жок)\s+(?:у\s+меня\s+)?{word}",
        r"{word}\s+(?:у\s+меня\s+)?(?:нет|нету|жоқ|жок)",
        r"{word}\s+жоқ",
        r"{word}\s+жок",
    ]

    for word in HARD_CONTRA_WORDS:
        w = re.escape(word)
        if not re.search(w, low):
            continue

        negated = False
        for pat in direct_neg_patterns:
            if re.search(pat.format(word=w), low):
                negated = True
                break
        if negated:
            continue

        # Hard stop only when patient clearly confirms that the contraindication
        # applies to them, not when they ask what a term means.
        if len(low.split()) <= 3:
            return True
        explicit_confirm = (
            re.search(rf"\b(?:у\s+меня|у\s+нас|мне|я|да|есть|имеется|беременна|беременен|беременность)\b[^?.!]*{w}", low)
            or re.search(rf"{w}[^?.!]*\b(?:есть|имеется|бар)\b", low)
            or (word.startswith("беремен") and re.search(r"\b(?:беременна|я\s+беременна|беременность\s+есть)\b", low))
        )
        if explicit_confirm:
            return True

    return False


def _looks_like_contra_term_question(low: str) -> bool:
    return bool(re.search(r"\b(?:что\s+такое|что\s+значит|это\s+что|что\s+это|объясните|расскажите)\b", low)) and any(
        word in low for word in HARD_CONTRA_WORDS
    )


def _contra_term_answer(text: str, session: dict[str, Any]) -> str | None:
    low = _low(text)
    if not _looks_like_contra_term_question(low):
        return None
    if "кохлеар" in low or "имплант" in low:
        explanation = "Кохлеарный имплант — это электронное устройство для слуха, его ставят хирургически при выраженной потере слуха."
    elif "тромб" in low:
        explanation = "Тромбоз — это состояние, когда в сосуде образуется сгусток крови."
    elif "помп" in low:
        explanation = "Инсулиновая помпа — это устройство, которое подаёт инсулин пациентам с диабетом."
    elif "металл" in low or "метал" in low:
        explanation = "Металл в зоне лечения — это пластины, винты, штифты или другие металлические конструкции именно в области, где планируется процедура."
    else:
        explanation = "Это один из пунктов противопоказаний, который важен для безопасности перед записью."
    return _tr(
        session,
        f"{explanation}\n\nПодскажите, пожалуйста, у Вас этого нет?",
        f"{explanation}\n\nСізде осы жоқ па?",
    )


def _age_block_reason(age: int | None) -> str | None:
    if age is None:
        return None
    if age < 16:
        return "under_16"
    if age > 75:
        return "over_75"
    return None


def _stop_booking_text(session: dict[str, Any], reason: str = "contra") -> str:
    if reason == "under_16":
        return _tr(
            session,
            "К сожалению, по правилам клиники приём не проводится пациентам младше 16 лет. Запись оформить не смогу. Передам информацию врачу, он свяжется с Вами завтра 🌿",
            "Өкінішке қарай, клиника ережесі бойынша 16 жасқа дейінгі пациенттерге қабылдау жүргізілмейді. Жазба рәсімдей алмаймын. Ақпаратты дәрігерге жіберемін, ол ертең Сізбен байланысады 🌿",
        )
    if reason == "over_75":
        return _tr(
            session,
            "Спасибо, что уточнили 🌿 По правилам клиники пациентам старше 75 лет запись автоматически не оформляется. Это противопоказание для автоматической записи. Передам информацию врачу, он свяжется с Вами завтра.",
            "Нақтылағаныңызға рақмет 🌿 Клиника ережесі бойынша 75 жастан асқан пациенттерге жазба автоматты түрде рәсімделмейді. Бұл автоматты жазбаға қарсы көрсетілім. Ақпаратты дәрігерге жіберемін, ол ертең Сізбен байланысады.",
        )

    return _tr(
        session,
        "Спасибо, что уточнили. Это противопоказание для записи в нашей клинике. Процесс записи останавливаю. Передам информацию врачу, он свяжется с Вами завтра 🌿",
        "Нақтылағаныңызға рақмет. Бұл біздің клиникада жазылуға қарсы көрсетілім. Жазылу процесін тоқтатамын. Ақпаратты дәрігерге жіберемін, ол ертең Сізбен байланысады 🌿",
    )
def _contra_is_clear_no(text: str) -> bool:
    low = _low(text)
    return any(w == low or w in low for w in NO_CONTRA_WORDS)


def _contra_clear_allowed_context(session: dict[str, Any], text: str) -> bool:
    low = _low(text)
    return (
        "противопоказ" in low
        or str(session.get("step") or session.get("current_step") or "") == "contraindications"
        or str(session.get("last_required_step") or "") == "contraindications"
        or str(session.get("last_bot_question_type") or "") == "contraindications"
    )


def _text_confirms_no_contra(session: dict[str, Any], text: str) -> bool:
    if _contra_clear_allowed_context(session, text):
        return _is_no_contra_answer(text) or _contra_is_clear_no(text)
    step = str(session.get("step") or session.get("current_step") or "")
    if step == "age" and _extract_age(text, step="age") and _has_multi_entity_safe_date(text):
        return _is_no_contra_answer(text) or _contra_is_clear_no(text)
    return False


def _extract_no_contra_raw(text: str) -> str:
    low = _low(text)
    for phrase in sorted(NO_CONTRA_WORDS, key=len, reverse=True):
        if phrase in low:
            return phrase
    if "противопоказ" in low and any(x in low for x in ["нет", "нету", "не имеется"]):
        return "противопоказаний нет"
    return (text or "нет").strip() or "нет"


def _mandatory_step_prompt(session: dict[str, Any], step: str) -> str:
    if _has_required_data_for_step(session, step):
        next_step = _next_required_step_after_collected_data(session)
        if next_step != step and next_step != "book":
            return _mandatory_step_prompt(session, next_step)
        if next_step == "book":
            return ""
    if step == "age":
        return _ask_age(session)
    if step == "contraindications":
        return _ask_contra(session)
    if step in ("date", "preferred_time"):
        return _ask_date(session)
    if step in ("time", "select_slot"):
        if session.get("last_slots"):
            return _slot_times_answer(session)
        return _tr(session, "Какое время Вам удобно?", "Қай уақыт ыңғайлы?")
    if step == "name":
        return _ask_name(session)
    return _clarify_intent_answer(session)


def _faq_answer(text: str, session: dict[str, Any]) -> str | None:
    if _has_any(text, METHOD_WORDS):
        return _method_answer(session)
    if _has_any(text, DOCTOR_WORDS):
        return _doctor_answer(session)
    if _has_any(text, COURSE_DURATION_WORDS):
        return _course_duration_answer(session)
    if _has_any(text, PRICE_WORDS):
        return _price_answer(text, session)
    if _has_mri_question(text):
        return _mri_answer_in_flow(session)
    if _has_any(text, ADDRESS_WORDS):
        return _address_answer(session)
    if _has_any(text, SCHEDULE_WORDS):
        return _schedule_answer(session)
    return None


def _faq_answer_then_resume(text: str, session: dict[str, Any], step: str) -> str | None:
    info = _faq_answer(text, session)
    if not info:
        return None
    return info + "\n\n" + _mandatory_step_prompt(session, step)


def _medical_risk_question(text: str) -> bool:
    low = _low(text)
    return any(p in low for p in ("это опасно", "опасно?", "что это может быть", "грыжа шморля"))


def _medical_risk_answer_then_resume(session: dict[str, Any], step: str) -> str:
    return _tr(
        session,
        "По переписке точно оценить нельзя. Лучше, чтобы врач посмотрел очно 🌿",
        "Хат арқылы нақты бағалау мүмкін емес. Дәрігердің көзбе-көз қарағаны дұрыс 🌿",
    ) + "\n\n" + _mandatory_step_prompt(session, step)


def _active_flow_resume_step(session: dict[str, Any]) -> str:
    step = str(session.get("step") or "start")
    if step in {"date", "preferred_time", "time", "select_slot", "name"}:
        return "time" if step == "select_slot" else step
    if session.get("last_slots") and not session.get("selected_time"):
        return "time"
    if session.get("selected_time") and not session.get("patient_name"):
        return "name"
    if session.get("contraindications_ok") is True and not (session.get("preferred_date") or session.get("selected_date")):
        return "date"
    return step


def _address_answer_then_optional_resume(session: dict[str, Any]) -> str:
    base = _address_answer(session)
    step = _active_flow_resume_step(session)
    if step in {"date", "time", "name"}:
        prompt = _mandatory_step_prompt(session, step)
        return base + (" " + prompt if prompt else "")
    return base


def _accept_no_contraindications(session: dict[str, Any], text: str) -> None:
    raw = (text or "нет").strip() or "нет"
    session["contraindications_ok"] = True
    session["contraindications_raw"] = raw
    session["contraindications_verdict"] = "proceed"
    bot_tools.verify_contraindications(session, bot_tools.CONTRA_PROCEED, raw)
    _cleanup_contraindications_after_ok(session)




def _is_contra_clear_hotfix_phrase(text: str) -> bool:
    low = _low(text)
    compact = re.sub(r"[\s.!?,🙏🌿❤️❤]+", "", low)
    phrases = [
        "противопоказаний нет", "нет противопоказаний", "по списку ничего нет",
        "ничего из перечисленного нет", "того что перечислено нет",
        "то что перечислено этого нет", "то что перечислено, этого нет",
        "из того что вы перечислили ничего нет", "из того что вы перечислили ничего такого нет",
        "нет, ничего такого нет", "этого нет", "ничего нет", "ничего такого нет",
        "всё чисто", "все чисто", "нету", "не имеется",
    ]
    return any(compact == re.sub(r"[\s.!?,🙏🌿❤️❤]+", "", phrase) for phrase in phrases)

def _is_refusal_or_irritation(text: str) -> bool:
    low = _low(text)
    return bool(low and any(phrase in low for phrase in REFUSAL_OR_IRRITATION_WORDS))


def _is_instagram_detail_request(text: str) -> bool:
    low = _low(text)
    has_link = "instagram.com" in low or "instagr.am" in low or "instagram" in low or "инстаграм" in low
    asks_detail = any(x in low for x in ["можно узнать", "подробнее", "что это", "интересует", "об этом", "про это"])
    return has_link and asks_detail


def _instagram_detail_answer(session: dict[str, Any]) -> str:
    return _tr(
        session,
        "Да, конечно 🌿 Подскажите, пожалуйста, что именно заинтересовало в посте — боль, процедура или запись на консультацию?",
        "Иә, әрине 🌿 Постта нақты не қызықтырды — ауырсыну, процедура ма, әлде консультацияға жазылу ма?",
    )


def _contra_reference_question(text: str) -> bool:
    low = _low(text)
    return "противопоказ" in low and any(x in low for x in ["где", "написано", "писали", "было", "выше", "список"])


def _contra_repeated_block_answer(session: dict[str, Any]) -> str:
    session["contraindications_repeated_blocked"] = True
    _safe_log(str(session.get("chat_id") or "system"), "contraindications_repeated_blocked", {"sent_count": int(session.get("contraindications_checklist_sent_count") or 0)})
    return _tr(
        session,
        "Я выше перечислил основные противопоказания. Если ничего из этого нет, напишите, пожалуйста: противопоказаний нет.",
        "Жоғарыда негізгі қарсы көрсетілімдерді жаздым. Егер ешқайсысы болмаса: қарсы көрсетілімдер жоқ деп жазыңызшы.",
    )


def _accept_no_contra_and_advance(chat_id: str, session: dict[str, Any], text: str) -> str:
    _accept_no_contraindications(session, text or "нет")
    session["step"] = "date"
    session["questionnaire_step"] = "date"
    _safe_log(chat_id, "contraindications_clear_detected", {"text_preview": (text or "")[:180]})
    if session.get("preferred_date"):
        return ""
    return _tr(session, "Отлично 🌿 На какой день Вам удобно прийти?", "Жақсы 🌿 Қай күн ыңғайлы?")

def _after_booking_admin_answer(text: str, session: dict[str, Any]) -> str:
    info = _faq_answer(text, session)
    if info:
        return info
    return _tr(
        session,
        "Чтобы не подсказать неверно, передам Ваш вопрос администратору/врачу — с Вами свяжутся 🌿",
        "Қате ақпарат бермеу үшін сұрағыңызды әкімшіге/дәрігерге жіберемін — Сізбен байланысады 🌿",
    )


def _crm_fallback_answer(session: dict[str, Any]) -> str:
    return _tr(
        session,
        "Вижу Ваш запрос 🌿 Сейчас передам администратору, чтобы он проверил данные и связался с Вами.",
        "Сұрағыңызды көріп тұрмын 🌿 Қазір әкімшіге жіберемін, ол деректерді тексеріп, Сізбен байланысады.",
    )

async def _continue_after_collected_age(chat_id: str, session: dict[str, Any], text: str, age: int) -> str:
    """Продолжение сценария, если возраст уже есть в этом же сообщении."""
    age_reason = _age_block_reason(age)
    if age_reason:
        session["contraindications_raw"] = text
        session["contraindications_ok"] = False
        if age_reason == "over_75":
            session["contraindications_verdict"] = "admin_contact"
            session["step"] = "escalated"
            session["escalated"] = True
        else:
            session["contraindications_verdict"] = "stop"
            session["step"] = "stopped"
        session["escalated"] = True
        return _prepend_price_if_needed(text, session, _stop_booking_text(session, age_reason))

    if _contra_has_hard_stop(text):
        bot_tools.verify_contraindications(session, bot_tools.CONTRA_REFUSE, text)
        session["step"] = "stopped"
        session["escalated"] = True
        return _prepend_price_if_needed(text, session, _stop_booking_text(session, "contra"))

    # Если пациент сразу написал, что противопоказаний нет — не спрашиваем это повторно.
    if _contra_is_clear_no(text):
        _accept_no_contraindications(session, text)

        date_iso = _parse_date(text)
        if date_iso:
            slots_answer = await _show_slots(chat_id, session, date_iso)
            return _prepend_price_if_needed(text, session, slots_answer)

        session["step"] = "date"
        return _ask_date(session)

    # Если противопоказания ещё не ясны — задаём обязательный вопрос.
    session["step"] = "contraindications"
    session["questionnaire_step"] = "contra"

    if age > 74:
        session["senior_patient"] = True
        return _senior_contra_intro(session)

    stop = _age_stop_text(age, session)
    if age < 18:
        session["minor_parent_required"] = True
        return stop + "\n\n" + _ask_contra(session)

    return _ask_contra(session)


def _has_active_context(session: dict[str, Any]) -> bool:
    return bool(
        session.get("complaint")
        or session.get("age")
        or session.get("contraindications_ok")
        or session.get("preferred_date")
        or session.get("selected_time")
        or session.get("selected_slot")
        or session.get("booked")
        or session.get("patient_name")
        or session.get("appointment")
    )


def _is_question_mark_only(text: str) -> bool:
    return _low(text).strip() in ("?", "??", "???", "？")


def _is_too_vague_to_start(text: str) -> bool:
    low = _low(_strip_quoted_bot_text(text))
    if not low:
        return False

    # Эти фразы сами по себе не являются заявкой и не должны запускать анкету.
    vague = [
        "добрый вечер", "добрый день", "здравствуйте", "привет",
        "салем", "сәлем", "ассалаумағалейкум", "ассалаумагалейкум",
        "можно вопрос", "вопрос", "уточнить", "хотел спросить", "хотела спросить",
        "а можно", "подскажите", "скажите пожалуйста",
    ]

    if low in vague:
        return True

    tokens = re.findall(r"[a-zа-яәғқңөұүһі0-9]+", low)
    if len(tokens) <= 2 and not (
        _has_complaint(low)
        or _has_medical_complaint_text(low)
        or _has_booking_intent(low)
        or _is_visit_confirmation_reply(low)
        or _wants_existing_lookup(low)
        or _is_cancel(low)
        or _has_mri_question(low)
        or _has_any(low, PRICE_WORDS)
        or _has_any(low, ADDRESS_WORDS)
        or _has_any(low, SCHEDULE_WORDS)
        or _is_thanks_or_ok(low)
    ):
        return True

    return False


def _clarify_intent_answer(session: dict[str, Any]) -> str:
    return _tr(
        session,
        "Подскажите, пожалуйста, чем можем помочь: хотите записаться на консультацию или уточняете по уже имеющейся записи? 🌿",
        "Нақтылап жіберіңізші: консультацияға жазылғыңыз келе ме, әлде бұрынғы жазбаңыз бойынша сұрап тұрсыз ба? 🌿",
    )


async def _safe_intent_router(chat_id: str, phone: str, session: dict[str, Any], text: str) -> str | None:
    """Главный роутер намерений живого администратора.

    Смысл: прежде чем запускать сценарий записи, определяем, что человек реально хочет.
    Если не уверены — уточняем намерение, а не начинаем анкету заново.
    """
    step = session.get("step") or "start"

    if not text:
        return _tr(session, "Здравствуйте! Напишите, пожалуйста, чем можем помочь 🌿", "Сәлеметсіз бе! Қалай көмектесе аламыз? 🌿")

    # 1. Простые завершающие сообщения не запускают сценарий.
    if _is_thanks_or_ok(text):
        if step in ("start", "complaint", "date", "done", "booked", "stopped", "escalated") or not _has_active_context(session):
            return _no_reply(chat_id, session)

    # 2. Подтверждение уже назначенного визита.
    if _is_visit_confirmation_reply(text):
        session["step"] = "done"
        session["status"] = "visit_confirmed"
        session["visit_confirmed"] = True
        return _visit_confirmation_answer(session)

    # 3. Отмена / перенос / уже записан — это не новая анкета.
    if _wants_existing_lookup(text):
        return await _handle_existing_lookup(chat_id, phone, session, text)

    if _is_cancel(text):
        if _is_reschedule_request(text) and not _is_cancel_direct_request(text):
            session["step"] = "escalated"
            session["escalated"] = True
            return _tr(
                session,
                "Поняла Вас 🌿 Передам администратору, чтобы он проверил Вашу запись и помог перенести её на удобное время.",
                "Түсіндім 🌿 Әкімшіге жіберемін, ол жазбаңызды тексеріп, ыңғайлы уақытқа ауыстыруға көмектеседі.",
            )
        return await _handle_cancel_appointment(chat_id, phone, session, text)

    # critical_step_guard:
    # Если бот уже ждёт имя для оформления записи, любое нормальное короткое сообщение
    # считаем именем и отдаём в основной сценарий. FAQ/МРТ/КТ/адрес не должны перехватить имя.
    if step == "name":
        if _is_question_mark_only(text):
            return _ask_name(session)
        return None

    # 4. Отказ, позже, отпуск, сентябрь, сам выберу.
    if _is_refuse_booking(text) and not _is_cancel_direct_request(text):
        session["step"] = "stopped"
        session["status"] = "refused"
        session["refused_booking"] = True
        return _refuse_booking_answer(session)

    if _is_vacation_later_visit(text) or _is_later_month_or_self_schedule(text):
        session["step"] = "stopped"
        session["status"] = "waiting_patient_later"
        session["waiting_for_date"] = True
        return _vacation_later_visit_answer(session)

    # 5. Вопросы, которые админ должен обработать без запуска анкеты.
    # Вопрос про время без выбранной даты.
    if _is_time_question_without_date(text) and not _parse_date(text):
        session["step"] = "date"
        session["waiting_for_date"] = True
        return _time_question_without_date_answer(session)

    # Просто "?" — не повторяем тот же вопрос.
    if _is_question_mark_only(text):
        if step == "date":
            return _tr(
                session,
                "Чтобы проверить свободное время, напишите, пожалуйста, удобный день — например: завтра, в понедельник или 23 июня 🌿",
                "Бос уақытты тексеру үшін ыңғайлы күнді жазыңызшы — мысалы: ертең, дүйсенбі немесе 23 маусым 🌿",
            )
        if step == "contraindications":
            return _ask_contra(session)
        return _clarify_intent_answer(session)

    if step in ("start", "complaint", "", None) and _appointment_request_answer(session, text):
        return None

    # МРТ/снимки/КТ — отдельный вопрос, не старт новой анкеты.
    # Но явную просьбу записаться на диагностику/консультацию/приём обрабатывает основной сценарий.
    if (
        _has_mri_question(text)
        and not (_has_complaint(text) or _has_medical_complaint_text(text))
        and not _appointment_request_answer(session, text)
    ):
        return _mri_answer_in_flow(session)

    info = _clinic_answer(text, session)
    if info and not (_has_complaint(text) or _has_medical_complaint_text(text)):
        session["last_info_answer"] = True
        if _has_any(text, METHOD_WORDS) and step in ("start", "", None):
            session["step"] = "complaint"
            return info + "\n\n" + _tr(session, "Чтобы подсказать точнее, напишите, пожалуйста, что Вас беспокоит?", "Дәлірек бағыттау үшін не мазалайтынын жазыңызшы?")
        # После остальных инфо-вопросов не ставим жёстко "complaint", чтобы "спасибо" не запустило анкету.
        if step in ("start", "", None) and not session.get("escalated"):
            session["step"] = "start"
        return info

    # 6. Если сообщение слишком vague, не начинаем анкету вслепую.
    if step in ("start", "", None) and _is_too_vague_to_start(text):
        session["step"] = "start"
        return _clarify_intent_answer(session)

    return None


def _is_ai_muted(session: dict[str, Any]) -> bool:
    return bool(
        session.get("manual_admin_intervention")
        or session.get("manual_takeover")
        or session.get("ai_muted")
    )


def _is_booked_or_confirmed_session(session: dict[str, Any]) -> bool:
    """Return True when a session contains any reliable booking marker."""
    if _low(str(session.get("step") or "")) in {
        "booked", "done", "confirmed", "appointment_confirmed",
    }:
        return True
    if _low(str(session.get("appointment_status") or "")) in {
        "booked", "confirmed", "active",
    }:
        return True
    if any(session.get(key) for key in (
        "existing_appointment", "appointment", "booked_date", "booked_time",
        "appointment_date", "appointment_time", "booking_id", "appointment_id",
    )):
        return True

    booking_result = session.get("booking_result")
    if isinstance(booking_result, dict) and (
        booking_result.get("success") is True
        or booking_result.get("ok") is True
        or _low(str(booking_result.get("status") or "")) in {"success", "booked", "confirmed"}
    ):
        return True

    selected_slot = session.get("selected_slot")
    patient_name = session.get("patient_name")
    booking_state = " ".join(str(session.get(key) or "") for key in (
        "booking_status", "status", "crm_status",
    )).lower()
    return bool(
        selected_slot
        and patient_name
        and any(marker in booking_state for marker in ("success", "booked", "confirmed", "записан"))
    )


def _is_new_lead_text(text: str) -> bool:
    """Recognise explicit booking intent or a clinic-profile complaint."""
    low = _low(text)
    if not low or low in {
        "да", "нет", "ок", "окей", "подтверждаю", "спасибо", "хорошо",
        "ия", "иә", "жоқ", "рахмет", "рақмет",
    }:
        return False
    lead_markers = (
        "запис", "консультац", "диагност", "приём", "прием", "осмотр",
        "жазыл", "қабылдау", "кабылдау",
    )
    return any(marker in low for marker in lead_markers) or _profile_status(text) == "profile"


def _is_new_lead_like_message(text: str) -> bool:
    """Return True for greetings, clinic questions, and profile requests worth starting AI."""
    low = _low(text)
    if not low or low in {
        "да", "нет", "ок", "окей", "подтверждаю", "спасибо", "хорошо",
        "ия", "иә", "жоқ", "рахмет", "рақмет",
    }:
        return False

    lead_like_markers = (
        # greetings
        "здравствуйте", "добрый день", "добрый вечер", "қайырлы кеш",
        "кайырлы кеш", "сәлеметсіз бе", "салеметсиз бе", "привет",
        # booking / consultation intent
        "хочу записаться", "можно записаться", "на консультацию", "на диагностику",
        "прием", "приём", "записаться на прием", "записаться на приём",
        # profile complaints
        "спина болит", "поясница", "шея", "грыжа", "протрузия",
        "отдаёт в ногу", "отдает в ногу", "отдаёт в руку", "отдает в руку",
        "сустав", "онемение", "защемление", "радикулит",
        # clinic questions
        "адрес", "адресс", "где вы находитесь", "где находитесь", "цена",
        "сколько стоит", "график", "режим", "мрт", "снимки",
        "без операции", "как на видео",
    )
    return any(marker in low for marker in lead_like_markers) or _profile_status(text) == "profile"


def _is_refund_or_claim_issue(text: str) -> bool:
    """Recognise payment, installment, refund and formal-claim admin cases."""
    low = _low(text)
    markers = (
        "возврат", "рассроч", "отмена рассрочки", "каспи ред", "kaspi red",
        "каспи", "kaspi", "кредит", "заявление", "претенз", "жалоба",
        "отдел забот", "отмена платежа", "отмена кредита", "деньги вернуть",
        "верните деньги", "вернут деньги", "оплата", "оплатил", "оплатила",
        "лечение на рассрочку", "когда будет отмена", "решите вопрос",
        "мама проходила лечение", "родственник проходил лечение",
    )
    return any(marker in low for marker in markers)


def _should_ai_handle_new_lead(session: dict[str, Any], text: str) -> tuple[bool, str]:
    if _is_refund_or_claim_issue(text):
        return False, "refund_claim_admin_required"
    if _is_booked_or_confirmed_session(session):
        return False, "booked_session_ai_disabled"
    if session.get("ai_muted") or session.get("manual_takeover") or session.get("do_not_reply"):
        return False, "manual_takeover"
    is_old_chat = (
        session.get("old_chat") is True
        or session.get("imported") is True
        or session.get("existing_chat") is True
        or _low(str(session.get("source") or "")) == "old"
        or _low(str(session.get("lead_source") or "")) == "old_chat"
    )
    has_human_admin_history = bool(
        session.get("manual_admin_intervention")
        or session.get("manual_takeover")
        or session.get("last_admin_message")
        or session.get("has_admin_history")
        or session.get("was_manual_admin_recent")
    )
    if is_old_chat and has_human_admin_history:
        return False, "old_chat_ai_disabled"

    if _has_active_conversation_context(session) or _is_reply_to_active_question(session, text):
        return True, "active_conversation_reply"

    step = _low(str(session.get("step") or "start"))
    if session.get("ai_lead_started") is True and step in {
        "start", "complaint", "age", "contraindications", "date",
        "preferred_time", "time", "select_slot", "name", "phone",
    }:
        return True, "active_ai_lead"
    if _is_new_lead_text(text):
        return True, "new_lead"
    if _is_new_lead_like_message(text):
        return True, "new_lead_like_message"
    return False, "not_new_lead"


def _is_new_patient_consultation(session: dict[str, Any]) -> bool:
    if session.get("existing_patient") or session.get("is_existing_patient") or session.get("procedure_patient"):
        return False
    visit_type = _low(str(session.get("visit_type") or session.get("appointment_type") or ""))
    if any(w in visit_type for w in ["процед", "повтор", "existing", "procedure"]):
        return False
    return True


def _mentions_weekend_day(text: str) -> bool:
    low = _low(text)
    return any(w in low for w in ["суббот", "воскрес", "сенбі", "сенби", "жексенбі", "жексенби"])


def _is_weekend_date(date_iso: str) -> bool:
    try:
        return datetime.fromisoformat(date_iso).date().weekday() >= 5
    except Exception:
        return False


def _weekend_primary_block_answer(session: dict[str, Any]) -> str:
    return _tr(
        session,
        "В субботу и воскресенье у нас процедурные дни 🌿 Первичных пациентов на консультацию записываем в будние дни. Давайте подберём ближайший удобный день на неделе?",
        "Сенбі және жексенбі бізде процедуралық күндер 🌿 Алғашқы консультацияға жаңа пациенттерді жұмыс күндері жазамыз. Апта ішінен ыңғайлы күн таңдайық?",
    )


async def handle_message(chat_id: str, phone: str, user_text: str) -> str:
    """Главная функция, которую вызывает main.py.

    main.py ожидает именно такую сигнатуру:
    await handle_message(chat_id=chat_id, phone=phone, user_text=text)
    """
    text = _clean(user_text)
    text = _strip_quoted_bot_text(text)
    text = _clean(text)
    _safe_add_message(chat_id, "user", text)

    session = state.get_session(chat_id)
    if not isinstance(session, dict):
        session = {}

    session["phone"] = phone or session.get("phone") or ""
    session["chat_id"] = chat_id
    session["last_user_text"] = text
    session["state_before_step"] = session.get("step") or "start"
    session["openai_used"] = False
    session["openai_model"] = ""
    session["openai_skip_reason"] = ""
    session["openai_guard_failed"] = False
    _reset_openai_brain_debug(session)
    session.pop("base_answer_preview", None)
    session.pop("final_answer_preview", None)
    session["language"] = _detect_lang(text, session)
    _repair_bad_patient_name(session)
    _safe_log(chat_id, "state_before_decision", {"chat_id": chat_id, "step": session.get("step") or "start", "current_step": session.get("current_step") or "", "ai_lead_started": bool(session.get("ai_lead_started")), "gate_reason": session.get("gate_reason") or ""})
    if (session.get("ai_muted") or session.get("manual_takeover") or session.get("manual_admin_intervention")) and not (session.get("old_chat") or session.get("imported") or session.get("existing_chat")):
        session["ai_muted"] = True
        session["manual_takeover"] = True
        session["openai_brain_skip_reason"] = "manual_takeover"
        _safe_log(chat_id, "ai_muted_no_reply", {"chat_id": chat_id, "reason": "manual_takeover"})
        return _no_reply(chat_id, session, "manual_takeover")
    if _is_refusal_or_irritation(text):
        session["ai_muted"] = True
        session["manual_takeover"] = True
        session["escalated"] = True
        session["step"] = "escalated"
        session["no_reply_reason"] = "user_refused_or_irritated"
        session["openai_brain_skip_reason"] = "user_refused_or_irritated"
        _safe_log(chat_id, "user_refused_or_irritated", {"chat_id": chat_id, "text_preview": text[:180]})
        return _finalize(chat_id, session, "Понимаю, извините за повторные сообщения. Передаю диалог администратору, больше не буду беспокоить.")
    if _is_instagram_detail_request(text):
        session["step"] = "complaint"
        session["instagram_detail_request"] = True
        session["gate_reason"] = "new_lead_like_message"
        session["ai_lead_started"] = True
        return _finalize(chat_id, session, _instagram_detail_answer(session))
    if _is_booked_or_confirmed_session(session):
        session["gate_reason"] = "booked_session_ai_disabled"
        return _no_reply(chat_id, session, "booked_session_ai_disabled")
    early_step = str(session.get("step") or "start")
    if _has_any(text, ADDRESS_WORDS) and not (_parse_date(text) or _contains_date_time_preference(text)) and not (early_step in {"time", "select_slot"} and _select_slot(text, session.get("last_slots") or [])):
        session.setdefault("gate_reason", "new_lead_like_message")
        session["ai_lead_started"] = True
        return _finalize(chat_id, session, _address_answer_then_optional_resume(session))
    if (session.get("step") == "contraindications" or session.get("last_required_step") == "contraindications") and _is_contra_clear_hotfix_phrase(text):
        answer = _accept_no_contra_and_advance(chat_id, session, text)
        faq_info = _faq_answer(text, session)
        if faq_info and answer:
            answer = faq_info + "\n\n" + answer
        return _finalize(chat_id, session, answer)
    if (session.get("step") == "escalated" or session.get("escalated")) and not session.get("booked"):
        session["no_reply_reason"] = "escalated_ai_disabled"
        _reset_openai_brain_debug(session)
        session["openai_brain_skip_reason"] = "escalated_ai_disabled"
        return _no_reply(chat_id, session, "escalated_ai_disabled")
    can_handle, reason = _should_ai_handle_new_lead(session, text)
    session["gate_reason"] = reason
    session["active_conversation_detected"] = bool(
        _has_active_conversation_context(session) or _is_reply_to_active_question(session, text)
    )
    if session.get("last_bot_question_type") in (None, ""):
        session["last_bot_question_type"] = _classify_bot_question(
            str(session.get("last_bot_answer") or session.get("last_assistant_answer") or "")
        )
    if not can_handle:
        session["no_reply_reason"] = reason
        if reason in {
            "booked_session_ai_disabled",
            "manual_takeover",
            "refund_claim_admin_required",
            "old_chat_ai_disabled",
        }:
            session["ai_muted"] = True
        if reason == "refund_claim_admin_required":
            session["manual_takeover"] = True
            session["escalated"] = True
        _safe_save(chat_id, session)
        if reason == "refund_claim_admin_required":
            answer = _tr(
                session,
                "Понимаю Вас. Вопрос по возврату/рассрочке передам ответственному администратору, чтобы проверили информацию и связались с Вами 🌿",
                "Түсіндім. Қайтарым/бөліп төлеу бойынша сұрақты жауапты әкімшіге жіберемін, ақпаратты тексеріп, Сізбен байланысады 🌿",
            )
            session["last_assistant_answer"] = answer
            _safe_save(chat_id, session)
            _safe_add_message(chat_id, "assistant", answer)
            return answer
        return ""

    if reason in {"new_lead", "new_lead_like_message", "active_conversation_reply", "active_ai_lead"}:
        session.pop("no_reply_reason", None)

    if reason in {"new_lead", "new_lead_like_message"}:
        session["ai_lead_started"] = True
        session["lead_source"] = reason
        session["ai_started_at"] = datetime.now(timezone.utc).isoformat()

    context = _build_conversation_context(chat_id, session, text)
    _apply_conversation_context(session, context, text)
    context_qtype = context.get("last_bot_question_type", "unknown")
    if context_qtype == "unknown":
        context_qtype = _classify_bot_question(
            str(session.get("last_bot_answer") or session.get("last_assistant_answer") or "")
        )
    session["last_bot_question_type"] = context_qtype
    session["inferred_context_action"] = context.get("inferred_context_action", "")
    session["used_history_context"] = bool(context.get("used_history_context"))
    session["prior_complaint_text"] = context.get("prior_complaint_text", "")
    session["current_step"] = session.get("step") or "start"
    session["no_reply_reason"] = ""

    # human_takeover_guard: если живой админ уже вмешался, AI молчит и не продолжает старый сценарий.
    if _is_ai_muted(session):
        session["ai_muted"] = True
        session["manual_takeover"] = True
        reason = "thanks/manual_takeover" if _is_thanks_or_ok(text) else "manual_takeover"
        return _no_reply(chat_id, session, reason)

    # language_lock_guard:
    # Фиксируем язык диалога, чтобы бот не прыгал RU/KZ от коротких ответов.
    # Сменить язык можно только явной просьбой клиента.
    if not session.get("language_locked") and text and not _is_thanks_or_ok(text):
        session["language_locked"] = True

    # thanks_after_info_guard:
    # Если пациент поблагодарил после адреса/цены/графика, не начинаем анкету заново.
    # В WhatsApp это должно выглядеть как молчание живого админа, а не как новый сценарий.
    if _is_thanks_or_ok(text) and _last_answer_was_info(session):
        return _no_reply(chat_id, session, "thanks/info")

    # no_duplicate_after_booking_guard:
    # После успешной записи короткие ответы "хорошо/спасибо/ок" не требуют ответа.
    # Так бот не дублирует подтверждение записи и не запускает сценарий заново.
    current_step = session.get("step") or "start"
    if (current_step in ("done", "booked") or session.get("booked")) and _is_thanks_or_ok(text):
        return _no_reply(chat_id, session, "thanks/done")

    # explicit_language_switch_guard:
    # Если клиент просит отвечать на другом языке — переключаем язык и подтверждаем.
    lang_request = _explicit_language_request(text)
    if lang_request:
        session["language"] = lang_request
        session["language_locked"] = True
        return _finalize(
            chat_id,
            session,
            _tr(session, "Хорошо, буду отвечать на русском 🌿", "Жақсы, қазақша жауап беремін 🌿"),
        )

    # state_machine_first_guard:
    # После языкового режима сначала уважаем текущее состояние диалога.
    # Intent-router запускается только после обязательных шагов, чтобы не перехватывать
    # возраст/противопоказания/дату/время/имя и не начинать анкету заново.
    step = session.get("step") or "start"

    if step == "contraindications" and session.get("contraindications_ok") is True:
        session["step"] = "date"
        session["questionnaire_step"] = "date"
        step = "date"
    if step == "age" and session.get("age"):
        session["step"] = _next_required_step_after_collected_data(session)
        step = session["step"]
    if session.get("last_slots") and not session.get("selected_time") and step not in {"booked", "done", "escalated", "stopped"}:
        session["step"] = "time"
        step = "time"
    if session.get("selected_time") and not session.get("patient_name") and step not in {"booked", "done", "escalated", "stopped"}:
        session["step"] = "name"
        step = "name"
    if _has_any(text, ADDRESS_WORDS) and step in {"time", "select_slot"}:
        slot = _select_slot(text, session.get("last_slots") or [])
        if slot and _explicit_slot_selection_text(text, session.get("last_slots") or []):
            _remember_selected_slot(session, slot)
            session["step"] = "name"
            step = "name"
            return _finalize(chat_id, session, _address_answer(session) + " " + _ask_name(session))
    if _has_any(text, ADDRESS_WORDS) and not (_parse_date(text) or _contains_date_time_preference(text)):
        return _finalize(chat_id, session, _address_answer_then_optional_resume(session))
    if step in {"date", "preferred_time", "time", "select_slot", "name"} and _medical_risk_question(text):
        return _finalize(chat_id, session, _medical_risk_answer_then_resume(session, step))
    if step in {"time", "select_slot"}:
        slot = _select_slot(text, session.get("last_slots") or [])
        info = _faq_answer(text, session)
        if slot and info and _explicit_slot_selection_text(text, session.get("last_slots") or []):
            _remember_selected_slot(session, slot)
            session["step"] = "name"
            step = "name"
            return _finalize(chat_id, session, info + " " + _ask_name(session))
    if step in {"date", "preferred_time", "time", "select_slot", "name"}:
        faq_resume = None if (_has_any(text, ADDRESS_WORDS) and (_parse_date(text) or _contains_date_time_preference(text))) else _faq_answer_then_resume(text, session, step)
        if faq_resume:
            return _finalize(chat_id, session, faq_resume)
        step = session.get("step") or step
    if step in ("time", "select_slot") and session.get("selected_time") and not session.get("patient_name"):
        session["step"] = "name"
        session["questionnaire_step"] = "name"
        return _finalize(chat_id, session, _ask_name(session))
    recovered_time_answer = await _recover_empty_time_slots(chat_id, session, text)
    if recovered_time_answer is not None:
        return _finalize(chat_id, session, recovered_time_answer)
    if step == "name" and session.get("patient_name") and _booking_ready(session, phone):
        answer = await _book(chat_id, session, phone)
        return _finalize(chat_id, session, answer)

    if step in ("start", "complaint") and session.get("last_bot_question_type") == "city" and text:
        session["city"] = text
        if "астан" not in _low(text):
            session["last_bot_question_type"] = "astana_visit"
            return _finalize(
                chat_id,
                session,
                _tr(
                    session,
                    "Поняла Вас 🌿 Вы планируете приехать в Астану на консультацию?",
                    "Түсіндім 🌿 Консультацияға Астанаға келуді жоспарлап отырсыз ба?",
                ),
            )

    if step == "complaint" and _is_greeting_only(text) and not session.get("complaint"):
        return _finalize(
            chat_id,
            session,
            _tr(
                session,
                "Доброе утро 🌿 Подскажите, пожалуйста, что Вас беспокоит?",
                "Қайырлы таң 🌿 Нақты не мазалайды?",
            ),
        )

    if step in ("date", "preferred_time", "time", "select_slot") and _has_any(text, DOCTOR_WORDS):
        return _finalize(chat_id, session, _doctor_answer(session, chat_id))

    if step == "contraindications" and not _is_no_contra_answer(text) and not _contra_is_clear_no(text) and not _contra_has_hard_stop(text) and not _contra_term_answer(text, session) and not _faq_answer(text, session):
        if _has_complaint(text) or _has_medical_complaint_text(text):
            session["contraindications_raw"] = text
            session["contraindications_ok"] = False
            session["contraindications_verdict"] = "admin_contact"
            session["step"] = "contraindications"
            _safe_log(chat_id, "llm_unknown_contraindication_blocked", {"chat_id": chat_id, "step": "contraindications", "patient_text": text[:180]})
            return _finalize(chat_id, session, _unknown_contra_safe_answer(session))

    # Final booking step guard: if a patient provides their name after a slot
    # was already selected and all required booking fields are present, CRM
    # booking must be attempted before any AI/admin fallback can answer.
    name_from_text = _extract_name(text)
    if step == "name" and _is_service_polite_phrase(text):
        session["patient_name"] = ""
        return _finalize(chat_id, session, "Подскажите, пожалуйста, Ваше имя для записи.")

    if step == "name" and name_from_text and not session.get("patient_name"):
        session["patient_name"] = name_from_text
        if _booking_ready(session, phone):
            return _finalize(chat_id, session, await _book(chat_id, session, phone))

    brain_answer = await _try_openai_dialog_brain(chat_id, phone, session, text)
    if brain_answer is not None:
        session["openai_used"] = True
        session["openai_skip_reason"] = ""
        return brain_answer

    fallback_answer = await _try_python_multi_entity_fallback(chat_id, session, text)
    if fallback_answer is not None:
        session["openai_used"] = False
        session["openai_skip_reason"] = "openai_brain_fallback_rule_based"
        return fallback_answer

    if _contra_has_hard_stop(text) and step not in ("done", "booked", "stopped"):
        bot_tools.verify_contraindications(session, bot_tools.CONTRA_REFUSE, text)
        session["step"] = "stopped"
        session["escalated"] = True
        return _finalize(chat_id, session, _stop_booking_text(session, "contra"))

    if step in ("done", "booked") or session.get("booked"):
        if _is_cancel(text):
            answer = await _handle_cancel_appointment(chat_id, phone, session, text)
            return _finalize(chat_id, session, answer)
        if _wants_existing_lookup(text):
            answer = await _handle_existing_lookup(chat_id, phone, session, text)
            return _finalize(chat_id, session, answer)
        if _is_visit_confirmation_reply(text):
            session["status"] = "visit_confirmed"
            session["visit_confirmed"] = True
            return _finalize(chat_id, session, _visit_confirmation_answer(session))
        if _is_thanks_or_ok(text):
            return _no_reply(chat_id, session)
        return _finalize(chat_id, session, _after_booking_admin_answer(text, session))

    if step in ("age", "contraindications", "date", "preferred_time", "time", "select_slot", "name"):
        if _is_thanks_or_ok(text) and step in ("date", "preferred_time"):
            return _no_reply(chat_id, session)
        if _is_thanks_or_ok(text) and step in ("time", "select_slot"):
            session["patient_name"] = "" if _is_service_polite_phrase(text) else session.get("patient_name", "")
            return _finalize(chat_id, session, _slot_times_answer(session))
        faq_info = _doctor_answer(session, chat_id) if _has_any(text, DOCTOR_WORDS) else _faq_answer(text, session)

        if step in ("time", "select_slot") and faq_info:
            slots = session.get("last_slots") or []
            slot = _select_slot(text, slots)
            if slot:
                _remember_selected_slot(session, slot)
                session["step"] = "name"
                session["questionnaire_step"] = "name"
                return _finalize(chat_id, session, faq_info + "\n\n" + _ask_name(session))

        if step == "age" and faq_info:
            inline_age = _extract_age(text, step="age")
            if inline_age:
                session["age"] = inline_age
                after_age = await _continue_after_collected_age(chat_id, session, text, inline_age)
                return _finalize(chat_id, session, faq_info + "\n\n" + after_age)
            return _finalize(chat_id, session, faq_info + "\n\n" + _mandatory_step_prompt(session, step))

        if step == "contraindications" and faq_info and (_is_no_contra_answer(text) or _contra_is_clear_no(text)):
            _accept_no_contraindications(session, text)
            session["step"] = "date"
            session["questionnaire_step"] = "date"
            return _finalize(chat_id, session, faq_info + "\n\n" + _ask_date(session))

        if faq_info:
            return _finalize(chat_id, session, faq_info + "\n\n" + _mandatory_step_prompt(session, step))

        if step == "age":
            if any(p in _low(text) for p in ["не знаю", "позже", "потом", "уточню", "білмеймін", "билмеймин"]):
                return _finalize(
                    chat_id,
                    session,
                    _tr(
                        session,
                        "Хорошо 🌿 Для записи возраст всё равно понадобится. Когда сможете — напишите, пожалуйста, возраст пациента.",
                        "Жақсы 🌿 Жазылу үшін жас бәрібір қажет болады. Мүмкін болғанда пациенттің жасын жазыңызшы.",
                    ),
                )
            age = _extract_age(text, step="age")
            if not age:
                return _finalize(chat_id, session, _ask_age(session))
            session["age"] = age
            stop = _age_stop_text(age, session)
            if age < 16:
                session["contraindications_ok"] = False
                session["contraindications_verdict"] = "stop"
                session["step"] = "stopped"
                return _finalize(chat_id, session, stop)
            if age > 75:
                session["contraindications_ok"] = False
                session["contraindications_verdict"] = "admin_contact"
                session["step"] = "escalated"
                session["escalated"] = True
                return _finalize(chat_id, session, stop)
            if age < 18:
                session["minor_parent_required"] = True
            session["step"] = "contraindications"
            session["questionnaire_step"] = "contra"
            answer = (stop + "\n\n" if age < 18 else "") + _ask_contra(session)
            return _finalize(chat_id, session, answer)

        if step == "contraindications":
            if _contra_details_question(text):
                return _finalize(chat_id, session, _contra_detailed_list(session))
            if _contra_reference_question(text) and int(session.get("contraindications_checklist_sent_count") or 0) >= 1:
                return _finalize(chat_id, session, _contra_repeated_block_answer(session))
            term_answer = _contra_term_answer(text, session)
            if term_answer:
                session["step"] = "contraindications"
                session["questionnaire_step"] = "contra"
                return _finalize(chat_id, session, term_answer)
            if _is_no_contra_answer(text) or _contra_is_clear_no(text):
                return _finalize(chat_id, session, _accept_no_contra_and_advance(chat_id, session, text or "нет"))
            if _contra_has_hard_stop(text):
                bot_tools.verify_contraindications(session, bot_tools.CONTRA_REFUSE, text)
                session["step"] = "stopped"
                return _finalize(chat_id, session, _stop_booking_text(session, "contra"))
            if (_has_complaint(text) or _has_medical_complaint_text(text)):
                return _finalize(chat_id, session, _ask_contra(session))
            if any(w in _low(text) for w in YES_WORDS) or _low(text) in {"не знаю", "возможно", "наверное"}:
                session["contraindications_ok"] = False
                session["contraindications_verdict"] = "need_details"
                session["contraindications_need_details_asked"] = True
                answer = _tr(session, "Поняла. Подскажите, пожалуйста, какие именно противопоказания есть?", "Түсіндім. Қандай қарсы көрсетілім бар екенін нақтылап жазыңызшы.")
                return _finalize(chat_id, session, answer)
            if session.get("contraindications_need_details_asked"):
                session["escalated"] = True
                session["manual_takeover"] = True
                session["step"] = "escalated"
                return _finalize(chat_id, session, _tr(session, "Чтобы не ошибиться, передам администратору для уточнения 🌿", "Қателеспеу үшін әкімшіге нақтылауға жіберемін 🌿"))
            return _finalize(chat_id, session, _ask_contra(session))

        if step in ("date", "preferred_time"):
            date_iso = _parse_date(text)
            if not date_iso:
                if _has_video_procedure_question(text):
                    return _finalize(chat_id, session, _video_procedure_answer(session) + "\n\n" + _ask_date(session))
                return _finalize(chat_id, session, _ask_date(session))
            if _is_new_patient_consultation(session) and _mentions_weekend_day(text) and _is_weekend_date(date_iso):
                session["step"] = "date"
                return _finalize(chat_id, session, _weekend_primary_block_answer(session))
            if any(p in _low(text) for p in TIME_PREFERENCE_WORDS):
                session["time_preference"] = next((p for p in TIME_PREFERENCE_WORDS if p in _low(text)), "")
                session["preferred_date_text"] = text
            answer = await _show_slots(chat_id, session, date_iso)
            if _has_video_procedure_question(text):
                answer = _video_procedure_answer(session) + "\n\n" + answer
            return _finalize(chat_id, session, answer)

        if step in ("time", "select_slot"):
            slots = session.get("last_slots") or []
            slot = _select_slot(text, slots)
            if not slot:
                if _has_video_procedure_question(text):
                    return _finalize(
                        chat_id,
                        session,
                        _video_procedure_answer(session)
                        + "\n\n"
                        + _tr(session, "Какое время из вариантов выше Вам удобно?", "Жоғарыдағы уақыттардың қайсысы ыңғайлы?"),
                    )
                return _finalize(chat_id, session, _mandatory_step_prompt(session, "time"))
            _remember_selected_slot(session, slot)
            session["step"] = "name"
            session["questionnaire_step"] = "name"
            answer = _ask_name(session)
            if _has_video_procedure_question(text):
                answer = _video_procedure_answer(session) + "\n\n" + answer
            return _finalize(chat_id, session, answer)

        if step == "name":
            if _is_service_polite_phrase(text):
                session["patient_name"] = ""
                return _finalize(chat_id, session, "Подскажите, пожалуйста, Ваше имя для записи.")
            name = _extract_name(text)
            if not name:
                return _finalize(chat_id, session, _ask_name(session))
            session["patient_name"] = name
            return _finalize(chat_id, session, await _book(chat_id, session, phone))

    # universal_intent_router:
    # Сначала понимаем намерение клиента, потом запускаем сценарий записи.
    routed_answer = await _safe_intent_router(chat_id, phone, session, text)
    if routed_answer is not None:
        if routed_answer == "":
            return routed_answer
        return _finalize(chat_id, session, routed_answer)

    # visit_confirmation_guard:
    # Если пациент отвечает на напоминание о существующей записи
    # ("буду", "подойду", "буду в 18:00"), не начинаем новую анкету.
    if _is_visit_confirmation_reply(text):
        session["step"] = "done"
        session["status"] = "visit_confirmed"
        session["visit_confirmed"] = True
        return _finalize(chat_id, session, _visit_confirmation_answer(session))

    # 0.5) Две задачи в одном сообщении:
    # "я уже записан/отмените/перенести" + "маму/папу/сына хочу записать".
    # Не запускаем обычную анкету, чтобы не перепутать пациентов.
    if _is_relative_new_booking_request(text):
        session["step"] = "escalated"
        session["escalated"] = True
        answer = _relative_dual_task_answer(session, text)
        return _finalize(chat_id, session, answer)

    if not text:
        return _finalize(chat_id, session, _tr(session, "Напишите, пожалуйста, что Вас беспокоит 🌿", "Сізді не мазалайды? 🌿"))

    # standalone_thanks_guard:
    # Если человек просто написал "спасибо/рахмет/ок" без активной записи/жалобы,
    # не начинаем анкету заново. В WhatsApp это должно выглядеть как молчание.
    current_step_for_thanks = session.get("step") or "start"
    has_active_booking_context = bool(
        session.get("complaint")
        or session.get("age")
        or session.get("preferred_date")
        or session.get("selected_time")
        or session.get("booked")
        or session.get("patient_name")
    )
    if _is_thanks_or_ok(text) and not has_active_booking_context and current_step_for_thanks in ("start", "complaint", "", None):
        session["step"] = "start"
        return _no_reply(chat_id, session)

    # refusal_guard:
    # Если пациент во время диалога отказался от записи ("не надо", "не хочу", "потом"),
    # останавливаем сценарий и не задаём дальше вопросы анкеты.
    # Отмена уже существующей записи обрабатывается ниже отдельным CRM-блоком.
    if _is_refuse_booking(text) and not _is_cancel_direct_request(text) and not _wants_existing_lookup(text):
        session["step"] = "stopped"
        session["status"] = "refused"
        session["refused_booking"] = True
        session["escalated"] = False
        return _finalize(chat_id, session, _refuse_booking_answer(session))

    # 1) Уже записан / напомнить запись — не запускаем новую запись.
    if _wants_existing_lookup(text):
        answer = await _handle_existing_lookup(chat_id, phone, session, text)
        return _finalize(chat_id, session, answer)

    # 2) Отмена/перенос — не запускаем новую запись.
    if _is_cancel(text):
        # Если пациент просит именно перенос, не отменяем запись автоматически.
        # Перенос требует выбора новой даты/времени, поэтому безопаснее передать админу.
        if _is_reschedule_request(text) and not _is_cancel_direct_request(text):
            session["step"] = "escalated"
            session["escalated"] = True
            answer = _tr(
                session,
                "Поняла Вас 🌿 Передам администратору, чтобы он проверил Вашу запись и помог перенести её на удобное время.",
                "Түсіндім 🌿 Әкімшіге жіберемін, ол жазбаңызды тексеріп, ыңғайлы уақытқа ауыстыруға көмектеседі.",
            )
            return _finalize(chat_id, session, answer)

        answer = await _handle_cancel_appointment(chat_id, phone, session, text)
        return _finalize(chat_id, session, answer)

    # 2.5) Суперсложный сценарий: жалоба + возраст + противопоказания/дата в одном сообщении.
    # Важно: этот блок стоит ДО FAQ и ДО обычной анкеты.
    # Примеры:
    # "Мне 78 лет, болит спина, противопоказаний нет"
    # "Мне 45, грыжа поясницы, но есть кардиостимулятор"
    # "Мен 62 жастамын, белім ауырады, ертең келуге бола ма?"
    inline_age = _extract_age(text, step="age")
    if (
        inline_age
        and (_has_complaint(text) or _has_medical_complaint_text(text))
        and session.get("contraindications_ok") is not True
        and not session.get("contraindications_verdict")
    ):
        session["complaint"] = session.get("complaint") or text
        session["age"] = inline_age
        answer = await _continue_after_collected_age(chat_id, session, text, inline_age)
        return _finalize(chat_id, session, answer)

    # 2.6) Вопросы про безоперационное лечение.
    # Без контекста фото/МРТ/документов отвечаем продающе, но без гарантий.
    # Если клиент просит оценить по снимкам/документам — передаём врачу и не продолжаем запись.
    if _is_non_surgical_treatment_question(text):
        if _has_document_or_image_context(text):
            session["step"] = "escalated"
            session["escalated"] = True
            session["handoff_to_doctor"] = True
            session["handoff_reason"] = "document_non_surgical_question"
            return _finalize(chat_id, session, _document_non_surgical_answer(session))

        if _has_specific_profile_context(text):
            _record_complaint_tool(session, text, is_in_profile=True)
            session["step"] = "age"
            return _finalize(chat_id, session, _non_surgical_profile_answer(session, text))

        session["step"] = "complaint"
        session["escalated"] = False
        return _finalize(chat_id, session, _non_surgical_general_answer(session))

    # 3) Типовые вопросы.
    # Если в сообщении есть жалоба, жалоба важнее FAQ.
    # Например "Белім ауырады, похоже протрузия" нельзя ошибочно трактовать как вопрос про УЗИ.
    diagnostic_booking_request = _has_booking_intent(text) and _has_mri_question(text)
    info = None if ((_has_complaint(text) or _has_medical_complaint_text(text)) or diagnostic_booking_request) else _clinic_answer(text, session)
    if info and not session.get("complaint"):
        session["step"] = "complaint"
        if _has_any(text, METHOD_WORDS):
            info = info + "\n\n" + _tr(session, "Чтобы подсказать точнее, напишите, пожалуйста, что Вас беспокоит?", "Дәлірек бағыттау үшін не мазалайтынын жазыңызшы?")
        return _finalize(chat_id, session, info)

    step = session.get("step") or "start"

    # Если на любом этапе до выбора времени пациент написал явно не профильную болезнь,
    # прекращаем автоматическую запись и передаём администратору.
    current_profile_status = _profile_status(text)
    if step not in ("done", "escalated", "stopped") and current_profile_status == "non_profile":
        _record_complaint_tool(session, text, is_in_profile=False)
        _mark_irrelevant_tool(session, "non_profile")
        return _finalize(chat_id, session, _non_profile_answer(session, text))

    # later_during_contra_guard:
    # Если пациент ещё на этапе противопоказаний пишет, что сможет только позже
    # ("только в сентябре", "сам выберу время", "потом приеду"),
    # мягко завершаем диалог и НЕ спрашиваем потом дату после "нет/спасибо".
    if step == "contraindications" and _is_later_month_or_self_schedule(text):
        session["step"] = "stopped"
        session["status"] = "waiting_patient_later"
        session["waiting_for_date"] = True
        session["contraindications_pending"] = True
        return _finalize(chat_id, session, _vacation_later_visit_answer(session))

    # contra_no_answer_final_guard:
    # "Нет/Жоқ" на этапе противопоказаний = противопоказаний нет.
    if step == "contraindications" and _is_no_contra_answer(text):
        _accept_no_contraindications(session, text)

        if session.get("waiting_for_date") or session.get("status") == "waiting_patient_later":
            session["step"] = "stopped"
            session["status"] = "waiting_patient_later"
            return _no_reply(chat_id, session)

        session["step"] = "date"
        session["questionnaire_step"] = "date"
        faq_info = _faq_answer(text, session)
        answer = faq_info + "\n\n" + _ask_date(session) if faq_info else _ask_date(session)
        return _finalize(chat_id, session, answer)

    # doctor_can_treat_question_guard:
    # Если пациент отправил фото/документ и спрашивает "это сможет лечить?",
    # не обещаем лечение и не повторяем вопрос про дату.
    if _is_doctor_can_treat_question(text):
        session["step"] = "escalated"
        session["escalated"] = True
        session["handoff_reason"] = "doctor_can_treat_question"
        return _finalize(
            chat_id,
            session,
            _tr(
                session,
                "По фото/документу точно не буду обещать лечение, чтобы не ввести Вас в заблуждение. Передам вопрос координатору клиники, он уточнит у врача и свяжется с Вами 🌿",
                "Фото/құжат бойынша емді нақты уәде ете алмаймын, қате бағыт бергім келмейді. Сұрағыңызды клиника координаторына жіберемін, ол дәрігерден нақтылап, Сізбен байланысады 🌿",
            ),
        )

    # waiting_date_thanks_guard:
    # Если пациент уже сказал, что напишет дату позже, на "спасибо/рахмет/ок"
    # не повторяем вопрос и не запускаем сценарий заново.
    if step == "date" and session.get("waiting_for_date") and _is_thanks_or_ok(text):
        return _no_reply(chat_id, session)

    # date_plain_thanks_guard:
    # Если бот спросил дату, а пациент ответил только "спасибо/рахмет/ок",
    # не повторяем вопрос "На какой день удобно?".
    if step == "date" and _is_thanks_or_ok(text):
        session["waiting_for_date"] = True
        return _no_reply(chat_id, session)

    # date_question_mark_guard:
    # Если пациент ответил только "?", не повторяем тот же вопрос дословно.
    if step == "date" and _low(text).strip() in ("?", "??", "???"):
        session["waiting_for_date"] = True
        return _finalize(
            chat_id,
            session,
            _tr(
                session,
                "Чтобы проверить свободное время, напишите, пожалуйста, удобный день — например: завтра, в понедельник или 23 июня 🌿",
                "Бос уақытты тексеру үшін ыңғайлы күнді жазыңызшы — мысалы: ертең, дүйсенбі немесе 23 маусым 🌿",
            ),
        )

    # time_without_date_guard:
    # Если пациент спрашивает "на какое время можно?", но день ещё не выбран,
    # не повторяем одну и ту же фразу, а объясняем, что сначала нужен день.
    if step == "date" and _is_time_question_without_date(text):
        session["waiting_for_date"] = True
        return _finalize(chat_id, session, _time_question_without_date_answer(session))

    # mri_question_during_date_guard:
    # Если пациент на этапе выбора даты спрашивает про МРТ/снимок,
    # отвечаем на вопрос и не повторяем "қай күн ыңғайлы?".
    if step == "date" and _has_mri_question(text):
        session["waiting_for_date"] = True
        return _finalize(chat_id, session, _mri_answer_in_flow(session))

    # vacation_later_visit_guard:
    # Если пациент пишет, что позже/в отпуске/в другом месяце сам придёт на консультацию,
    # не повторяем вопрос про дату, а мягко завершаем диалог.
    if step == "date" and (_is_vacation_later_visit(text) or _is_later_month_or_self_schedule(text)):
        session["step"] = "stopped"
        session["status"] = "waiting_patient_later"
        session["waiting_for_date"] = True
        return _finalize(chat_id, session, _vacation_later_visit_answer(session))

    # unknown_date_answer_guard:
    # Если пациент пока не знает день/время, не повторяем один и тот же вопрос.
    if step == "date" and _is_unknown_date_answer(text):
        session["step"] = "date"
        session["waiting_for_date"] = True
        return _finalize(
            chat_id,
            session,
            _tr(
                session,
                "Да, конечно, можно 🌿 Когда будете знать удобный день и время — напишите сюда, я проверю свободные окошки и помогу с записью.",
                "Иә, әрине болады 🌿 Сізге ыңғайлы күн мен уақыт белгілі болғанда осында жазыңыз — бос уақыттарды қарап, жазылуға көмектесемін.",
            ),
        )

    # tentative_date_answer_guard:
    # Если пациент пишет неопределённо: "на следующей неделе, может вторник/среда",
    # фиксируем пожелание и не спамим вопросом повторно.
    if step == "date" and _is_tentative_date_answer(text) and not _parse_date(text):
        session["step"] = "escalated"
        session["escalated"] = True
        session["handoff_reason"] = "tentative_date"
        return _finalize(
            chat_id,
            session,
            _tr(
                session,
                "Спасибо, зафиксировала пожелание по дню. Так как точное время пока не выбрано, передам заявку координатору клиники — он свяжется с Вами и закрепит удобное время вручную 🌿",
                "Рақмет, күн бойынша қалауыңызды белгіледім. Нақты уақыт әлі таңдалмағандықтан, өтінімді клиника координаторына жіберемін — ол Сізбен байланысып, ыңғайлы уақытты қолмен бекітеді 🌿",
            ),
        )

    # visit_confirmed_thanks_guard:
    if session.get("visit_confirmed") and _is_thanks_or_ok(text):
        return _no_reply(chat_id, session)

    # post_done_new_booking_guard_final:
    # После завершения/передачи не начинаем новый сценарий от коротких сообщений,
    # но если человек явно снова просит записаться с новой жалобой — начинаем аккуратно.
    if step in ("done", "booked", "stopped", "escalated") and _has_booking_intent(text) and _profile_status(text) == "profile":
        session.clear()
        session["phone"] = phone or ""
        session["language"] = _detect_lang(text, session)
        _repair_bad_patient_name(session)
        session["language_locked"] = True
        _record_complaint_tool(session, text, is_in_profile=True)
        session["step"] = "age"
        return _finalize(
            chat_id,
            session,
            _profile_confirm_next_step(session),
        )

    # profile_classifier_guard:
    # Сначала определяем, относится ли жалоба к профилю клиники.
    # Если не профиль — не ведём в запись и не обещаем лечение.
    profile_status = _profile_status(text)
    if step in ("start", "complaint") and profile_status == "non_profile":
        _record_complaint_tool(session, text, is_in_profile=False)
        _mark_irrelevant_tool(session, "non_profile")
        return _finalize(chat_id, session, _non_profile_answer(session, text))

    if step in ("start", "complaint") and profile_status == "unclear":
        session["step"] = "complaint"
        session["profile_status"] = "unclear"
        return _finalize(chat_id, session, _unclear_profile_answer(session, text))

    appointment_answer = _appointment_request_answer(session, text)
    if step in ("start", "complaint") and appointment_answer and not (_has_complaint(text) or _has_medical_complaint_text(text)):
        session["step"] = "complaint"
        return _finalize(chat_id, session, appointment_answer)

    # escalated_repeat_guard_v2:
    # Если ранее запрос был не профильный, но пациент уточнил профильную жалобу
    # по спине/шее/суставам — продолжаем обычную запись.
    if step == "escalated" and session.get("profile_status") == "non_profile" and _profile_status(text) == "profile":
        _record_complaint_tool(session, text, is_in_profile=True)
        session["escalated"] = False
        session["step"] = "age"
        return _finalize(chat_id, session, _profile_confirm_and_ask_age(session, text))

    # После передачи координатору не задаём повторно вопросы анкеты.
    if step == "escalated":
        if _is_thanks_or_ok(text):
            return _finalize(chat_id, session, _tr(session, "Спасибо 🌿", "Рақмет 🌿"))
        if _is_unknown_date_answer(text):
            return _finalize(
                chat_id,
                session,
                _tr(
                    session,
                    "Хорошо, когда определитесь — напишите сюда. Координатор клиники также сможет связаться с Вами и закрепить удобное время 🌿",
                    "Жақсы, анықтаған кезде осында жазыңыз. Клиника координаторы да Сізбен байланысып, ыңғайлы уақытты бекіте алады 🌿",
                ),
            )

    # handoff_already_done_thanks_guard:
    # После передачи координатору/администратору не запускаем сценарий заново
    # на короткие ответы "спасибо/ок/хорошо".
    if step in ("done", "booked") and _is_thanks_or_ok(text):
        return _no_reply(chat_id, session)

    if step in ("escalated", "stopped") and _is_thanks_or_ok(text):
        return _finalize(chat_id, session, _tr(session, "Спасибо 🌿", "Рақмет 🌿"))

    # Если ранее пациент написал неопределённо ("на следующей неделе"),
    # а потом уточнил конкретный день ("в понедельник") — продолжаем запись
    # и показываем реальные слоты CRM, а не передаём координатору.
    if step == "escalated" and session.get("handoff_reason") == "tentative_date":
        date_iso = _parse_date(text)
        if date_iso:
            session["step"] = "time"
            session["escalated"] = False
            session["handoff_reason"] = ""
            return _finalize(chat_id, session, await _show_slots(chat_id, session, date_iso))

    # Если координатор уже закрепляет время вручную по другой причине,
    # новые уточнения по дню/времени не должны запускать повторную заявку.
    if step == "escalated" and (_parse_date(text) or _has_time_hint(text) or _has_booking_intent(text)):
        return _finalize(
            chat_id,
            session,
            _tr(
                session,
                "Спасибо, зафиксировала пожелание по времени. Координатор клиники свяжется с Вами и закрепит удобное время вручную 🌿",
                "Рақмет, уақыт бойынша қалауыңызды белгіледім. Клиника координаторы Сізбен байланысып, ыңғайлы уақытты қолмен бекітеді 🌿",
            ),
        )

    # complaint_already_given_guard:
    # Если пациент сразу написал профильную жалобу ("парез стопы", "после операции", "болит..."),
    # не спрашиваем "что беспокоит" повторно.
    if step in ("start", "complaint") and not _is_no_contra_answer(text) and _profile_status(text) == "profile":
        _record_complaint_tool(session, text, is_in_profile=True)
        if session.get("age"):
            session["step"] = "contra"
            answer = _profile_confirm_next_step(session)
        else:
            session["step"] = "age"
            answer = _ask_age_contextual(session, text)
        return _finalize(chat_id, session, answer)


    # 4) Если пациент прислал возраст внутри любого сообщения — сохраняем,
    # но НЕ перескакиваем противопоказания.
    age = _extract_age(text, step="age" if step == "age" else "")
    if age and not session.get("age"):
        session["age"] = age

    # ЖЁСТКИЙ ГЕЙТ: если жалоба уже есть и пациент прислал возраст,
    # нельзя перейти к дате, пока не закрыты противопоказания.
    if age and session.get("complaint") and session.get("contraindications_ok") is not True and not session.get("contraindications_verdict"):
        answer = await _continue_after_collected_age(chat_id, session, text, age)
        return _finalize(chat_id, session, answer)

    # complaint_thanks_guard:
    # Если бот уже спросил "что беспокоит?", а клиент ответил только "спасибо/рахмет",
    # не повторяем вопрос и не пушим анкету.
    if (session.get("step") or "start") == "complaint" and _is_thanks_or_ok(text) and not session.get("complaint"):
        session["step"] = "start"
        return _no_reply(chat_id, session)

    # 5) Старт / выясняем жалобу.
    if step in ("start", "", None):
        if _has_no_complaint(text):
            count = int(session.get("no_complaint_count") or 0) + 1
            session["no_complaint_count"] = count
            session["step"] = "complaint_no_confirm"
            answer = _tr(
                session,
                "Поняла 🌿 Если конкретной жалобы нет, можно прийти на первичную консультацию для профилактического осмотра. Хотите записаться на консультацию?",
                "Түсіндім 🌿 Егер нақты шағым болмаса, профилактикалық қаралу үшін алғашқы консультацияға келуге болады. Консультацияға жазылайын ба?",
            )
            return _finalize(chat_id, session, answer)

        if (_has_complaint(text) or _has_medical_complaint_text(text)) and _profile_status(text) != "non_profile":
            _record_complaint_tool(session, text, is_in_profile=True)

            # Если возраст уже есть в этом же сообщении — не спрашиваем его повторно.
            # Сразу проверяем противопоказания/дату.
            if session.get("age"):
                answer = await _continue_after_collected_age(chat_id, session, text, int(session["age"]))
                return _finalize(chat_id, session, answer)

            session["step"] = "age"
            return _finalize(chat_id, session, _ask_age_contextual(session, text))

        if _has_booking_intent(text) or _is_greeting_only(text):
            session["step"] = "complaint"
            if _has_mri_question(text):
                answer = _tr(
                    session,
                    "Здравствуйте! Да, поможем с записью на первичную консультацию/диагностику 🌿\nПодскажите, пожалуйста, что именно Вас беспокоит?",
                    "Сәлеметсіз бе! Иә, алғашқы консультацияға/диагностикаға жазылуға көмектесеміз 🌿\nСізді нақты не мазалайды?",
                )
            else:
                answer = _tr(
                    session,
                    "Здравствуйте! Да, можно записаться на консультацию по акции 🌿\nПодскажите, пожалуйста, что Вас беспокоит?",
                    "Сәлеметсіз бе! Иә, акция бойынша консультацияға жазылуға болады 🌿\nСізді не мазалайды?",
                )
            return _finalize(chat_id, session, answer)

        session["step"] = "complaint"
        return _finalize(chat_id, session, _ask_complaint(session))

    if step == "complaint":
        if _has_no_complaint(text):
            count = int(session.get("no_complaint_count") or 0) + 1
            session["no_complaint_count"] = count
            if count == 1:
                session["step"] = "complaint_no_confirm"
                answer = _tr(
                    session,
                    "Поняла 🌿 Если конкретной жалобы нет, можно прийти на первичную консультацию для профилактического осмотра. Хотите записаться на консультацию?",
                    "Түсіндім 🌿 Егер нақты шағым болмаса, профилактикалық қаралу үшін алғашқы консультацияға келуге болады. Консультацияға жазылайын ба?",
                )
                return _finalize(chat_id, session, answer)
            session["step"] = "escalated"
            session["escalated"] = True
            return _finalize(chat_id, session, _tr(session, "Поняла Вас 🌿 Передам администратору, чтобы он помог с записью и подсказал, какая консультация подойдёт.", "Түсіндім 🌿 Әкімшіге жіберемін, ол жазылуға көмектесіп, қандай консультация қолайлы екенін айтады."))

        if _profile_status(text) == "non_profile":
            _record_complaint_tool(session, text, is_in_profile=False)
            _mark_irrelevant_tool(session, "non_profile")
            return _finalize(chat_id, session, _non_profile_answer(session, text))

        if _profile_status(text) == "unclear":
            session["step"] = "complaint"
            session["profile_status"] = "unclear"
            return _finalize(chat_id, session, _unclear_profile_answer(session, text))

        if not (_has_complaint(text) or _has_medical_complaint_text(text)):
            if _has_booking_intent(text) or _is_greeting_only(text):
                return _finalize(chat_id, session, _ask_complaint(session))
            return _finalize(chat_id, session, _ask_complaint(session))

        _record_complaint_tool(session, text, is_in_profile=True)
        if session.get("age"):
            session["step"] = "contra"
            return _finalize(chat_id, session, _profile_confirm_next_step(session))
        session["step"] = "age"
        return _finalize(chat_id, session, _ask_age_contextual(session, text))

    if step == "complaint_no_confirm":
        if _has_no_complaint(text):
            session["step"] = "escalated"
            session["escalated"] = True
            return _finalize(chat_id, session, _tr(session, "Поняла Вас 🌿 Передам администратору, чтобы он помог с записью и подсказал, какая консультация подойдёт.", "Түсіндім 🌿 Әкімшіге жіберемін, ол жазылуға көмектесіп, қандай консультация қолайлы екенін айтады."))
        if _is_positive_confirm(text) or _has_booking_intent(text):
            _record_complaint_tool(session, "Профилактическая консультация, без конкретной жалобы", is_in_profile=True)
            session["step"] = "age"
            return _finalize(chat_id, session, _tr(session, "Хорошо 🌿 Подскажите, пожалуйста, сколько Вам лет?", "Жақсы 🌿 Жасыңыз нешеде?"))
        if (_has_complaint(text) or _has_medical_complaint_text(text)) and _profile_status(text) != "non_profile":
            _record_complaint_tool(session, text, is_in_profile=True)

            # Если возраст уже есть в этом же сообщении — не спрашиваем его повторно.
            # Сразу проверяем противопоказания/дату.
            if session.get("age"):
                answer = await _continue_after_collected_age(chat_id, session, text, int(session["age"]))
                return _finalize(chat_id, session, answer)

            session["step"] = "age"
            return _finalize(chat_id, session, _ask_age_contextual(session, text))
        session["step"] = "escalated"
        session["escalated"] = True
        return _finalize(chat_id, session, _tr(session, "Поняла Вас 🌿 Передам администратору, чтобы он помог сориентироваться.", "Түсіндім 🌿 Әкімшіге жіберемін, ол нақтылап көмектеседі."))

    # 6) Возраст: после возраста ВСЕГДА спрашиваем противопоказания.
    if step == "age":
        # age_unknown_answer_guard:
        # Если пациент не готов назвать возраст, не перескакиваем и не путаемся.
        if any(p in _low(text) for p in ["не знаю", "позже", "потом", "уточню", "білмеймін", "билмеймин"]):
            return _finalize(
                chat_id,
                session,
                _tr(
                    session,
                    "Хорошо 🌿 Для записи возраст всё равно понадобится. Когда сможете — напишите, пожалуйста, возраст пациента.",
                    "Жақсы 🌿 Жазылу үшін жас бәрібір қажет болады. Мүмкін болғанда пациенттің жасын жазыңызшы.",
                ),
            )

        age = _extract_age(text, step="age")
        if not age:
            return _finalize(chat_id, session, _tr(session, "Подскажите, пожалуйста, сколько Вам лет?", "Жасыңыз нешеде?"))

        session["age"] = age
        stop = _age_stop_text(age, session)
        if age < 16:
            session["contraindications_ok"] = False
            session["contraindications_verdict"] = "stop"
            session["step"] = "stopped"
            return _finalize(chat_id, session, stop)

        if age > 75:
            session["contraindications_ok"] = False
            session["contraindications_verdict"] = "admin_contact"
            session["step"] = "escalated"
            session["escalated"] = True
            return _finalize(chat_id, session, stop)

        # 16–18: не стоп, но только с родителем/законным представителем.
        if age < 18:
            session["minor_parent_required"] = True
            session["step"] = "contraindications"
            answer = stop + "\n\n" + _ask_contra(session)
            return _finalize(chat_id, session, answer)

        session["step"] = "contraindications"
        session["questionnaire_step"] = "contra"
        return _finalize(chat_id, session, _ask_contra(session))

    # 7) Противопоказания — обязательный гейт перед датой.
    if step == "contraindications":
        # contra_no_answer_direct_date_guard:
        # "Нет" / "Жоқ!" на вопрос о противопоказаниях = противопоказаний нет,
        # дальше спрашиваем дату, а не возвращаемся к жалобе.
        if _is_no_contra_answer(text):
            answer = _accept_no_contra_and_advance(chat_id, session, text or "нет")
            faq_info = _faq_answer(text, session)
            answer = faq_info + "\n\n" + answer if faq_info and answer else answer
            return _finalize(chat_id, session, answer)

        session["contraindications_raw"] = text

        if _contra_is_clear_no(text):
            _accept_no_contraindications(session, text)
            session["step"] = "date"
            return _finalize(chat_id, session, _ask_date(session))

        if _contra_has_hard_stop(text):
            session["contraindications_ok"] = False
            session["contraindications_verdict"] = "stop"
            session["step"] = "stopped"
            return _finalize(chat_id, session, _stop_booking_text(session, "contra"))

        # Если пациент написал симптомы вместо ответа по противопоказаниям — не считаем это противопоказанием.
        if (_has_complaint(text) or _has_medical_complaint_text(text)):
            session["contraindications_ok"] = False
            session["contraindications_verdict"] = "admin_contact"
            session["step"] = "contraindications"
            _safe_log(chat_id, "llm_unknown_contraindication_blocked", {"chat_id": chat_id, "step": "contraindications", "patient_text": text[:180]})
            return _finalize(chat_id, session, _unknown_contra_safe_answer(session))

        # Если пациент написал просто "есть/да/не знаю" без деталей — запись не продолжаем.
        if any(w in _low(text) for w in YES_WORDS) or _low(text) in {"не знаю", "возможно", "наверное"}:
            session["contraindications_ok"] = False
            session["contraindications_verdict"] = "need_details"
            session["step"] = "contraindications"
            session["contraindications_need_details_asked"] = True
            answer = _tr(session, "Поняла. Подскажите, пожалуйста, какие именно противопоказания есть?", "Түсіндім. Қандай қарсы көрсетілім бар екенін нақтылап жазыңызшы.")
            return _finalize(chat_id, session, answer)

        if session.get("contraindications_need_details_asked"):
            session["escalated"] = True
            session["manual_takeover"] = True
            session["step"] = "escalated"
            return _finalize(chat_id, session, _tr(session, "Чтобы не ошибиться, передам администратору для уточнения 🌿", "Қателеспеу үшін әкімшіге нақтылауға жіберемін 🌿"))

        return _finalize(chat_id, session, _ask_contra(session))

    # 8) Дата.
    if step in ("date", "preferred_time"):
        date_iso = _parse_date(text)
        if not date_iso:
            if _has_video_procedure_question(text):
                return _finalize(chat_id, session, _video_procedure_answer(session) + "\n\n" + _ask_date(session))
            return _finalize(chat_id, session, _ask_date(session))

        if _is_new_patient_consultation(session) and _mentions_weekend_day(text) and _is_weekend_date(date_iso):
            session["step"] = "date"
            return _finalize(chat_id, session, _weekend_primary_block_answer(session))

        if any(p in _low(text) for p in TIME_PREFERENCE_WORDS):
            session["time_preference"] = next((p for p in TIME_PREFERENCE_WORDS if p in _low(text)), "")
            session["preferred_date_text"] = text
        answer = await _show_slots(chat_id, session, date_iso)
        if _has_video_procedure_question(text):
            answer = _video_procedure_answer(session) + "\n\n" + answer
        return _finalize(chat_id, session, answer)

    # 9) Выбор времени.
    if step in ("time", "select_slot"):
        slots = session.get("last_slots") or []
        slot = _select_slot(text, slots)
        if not slot:
            if _has_video_procedure_question(text):
                return _finalize(
                    chat_id,
                    session,
                    _video_procedure_answer(session)
                    + "\n\n"
                    + _tr(session, "Какое время из вариантов выше Вам удобно?", "Жоғарыдағы уақыттардың қайсысы ыңғайлы?"),
                )
            return _finalize(
                chat_id,
                session,
                _tr(session, "Какое время из вариантов выше Вам удобно?", "Жоғарыдағы уақыттардың қайсысы ыңғайлы?"),
            )

        _remember_selected_slot(session, slot)
        session["step"] = "name"
        session["questionnaire_step"] = "name"
        answer = _ask_name(session)
        if _has_video_procedure_question(text):
            answer = _video_procedure_answer(session) + "\n\n" + answer
        return _finalize(chat_id, session, answer)

    # 10) Имя.
    if step == "name":
        name = _extract_name(text)
        if not name:
            return _finalize(chat_id, session, _ask_name(session))

        session["patient_name"] = name
        answer = await _book(chat_id, session, phone)
        return _finalize(chat_id, session, answer)

    # 11) После записи короткие сообщения не запускают новую анкету.
    if step == "done" or session.get("booked"):
        if _is_cancel(text):
            answer = await _handle_cancel_appointment(chat_id, phone, session, text)
            return _finalize(chat_id, session, answer)

        if _is_thanks_or_ok(text):
            return _no_reply(chat_id, session)

        return _finalize(
            chat_id,
            session,
            _tr(
                session,
                "Ваша запись уже оформлена 🌿 Если нужно отменить или перенести — напишите, пожалуйста.",
                "Сіздің жазбаңыз рәсімделген 🌿 Егер тоқтату немесе ауыстыру қажет болса, жазыңыз.",
            ),
        )

    # 11.5) Запись остановлена из-за противопоказаний/возраста или отказа пациента.
    if step == "stopped":
        if session.get("refused_booking"):
            if _is_thanks_or_ok(text) or _is_refuse_booking(text):
                return _no_reply(chat_id, session)
            if _has_booking_intent(text) or _profile_status(text) == "profile":
                session["step"] = "complaint"
                session["refused_booking"] = False
                return _finalize(chat_id, session, _ask_complaint(session))
            return _no_reply(chat_id, session)

        return _finalize(chat_id, session, _stop_booking_text(session, "contra"))

    # 12) Если состояние непонятное — безопасно продолжаем с ближайшего обязательного шага.
    if not session.get("complaint"):
        session["step"] = "complaint"
        return _finalize(chat_id, session, _ask_complaint(session))

    if not session.get("age"):
        session["step"] = "age"
        return _finalize(chat_id, session, _tr(session, "Подскажите, пожалуйста, сколько Вам лет?", "Жасыңыз нешеде?"))

    if session.get("selected_time") and not session.get("patient_name"):
        session["step"] = "name"
        return _finalize(chat_id, session, _ask_name(session))

    if session.get("contraindications_ok") is not True:
        session["step"] = "contraindications"
        return _finalize(chat_id, session, _ask_contra(session))

    session["step"] = "date"
    return _finalize(chat_id, session, _ask_date(session))
