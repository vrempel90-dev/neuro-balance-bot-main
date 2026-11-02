"""Conservative subject tracking for a clinic conversation.

The LLM resolves ambiguous messages. This module protects stored facts when an
explicitly different patient is mentioned and never treats a WhatsApp sender as
proof of a relative's CRM identity.
"""
from __future__ import annotations

import re
from typing import Any

# This is only an unambiguous hint layer; model/tool extraction remains needed
# for indirect expressions, typos and mixed Russian/Kazakh.
_RELATIVES = {
    "мама": r"(?:мам[аыуое]|матер[иью]|анам(?:ды|а|ның|е)?)",
    "папа": r"(?:пап[аыуое]|отц[ауеом]|әкем(?:ді|е|нің)?)",
    "муж": r"(?:муж[ауем]?|супруг[ауеом]?|күйеуім(?:ді|е|нің)?)",
    "жена": r"(?:жен[аыуое]|супруг[аеуы]|әйелім(?:ді|е|нің)?)",
    "сын": r"(?:сын[ауеом]?|ұлым(?:ды|а|ның)?)",
    "дочь": r"(?:доч[ьиерью]|дочк[аеуы]|қызыма|қызымды|қызым)",
    "бабушка": r"(?:бабушк[аеуы]|әжем(?:ді|е|нің)?)",
    "дедушка": r"(?:дедушк[аеуы]|атам(?:ды|а|ның)?)",
    "брат": r"(?:брат[ауеом]?|ағам(?:ды|а|ның)?|інім(?:ді|е|нің)?)",
    "сестра": r"(?:сестр[аеуы]|әпкем(?:ді|е|нің)?)",
    "ребёнок": r"(?:реб[её]нк[аеуы]|реб[её]нок|балам(?:ды|а|ның)?)",
}
_CUE_BEFORE = r"(?:запис\w*|для|за|у|про|обследовать|показать|врач\w*|при[её]м|жаздыр\w*|көрсет\w*)"
_CUE_AFTER = r"(?:болит|болеет|лет|год\w*|жас(?:та)?|ауыр\w*|запис\w*|при[её]м|к\s+врачу)"
_SELF = re.compile(
    r"\b(?:для\s+себя|себя\s+запис|меня\s+запис|"
    r"хочу\s+записаться\s+(?:сам|сама)|"
    r"я\s+(?:сам|сама)\s+хочу|"
    r"мне\s+(?:нужн\w*\s+)?(?:на\s+при[её]м|к\s+врачу)|"
    r"у\s+меня\s+болит|өзім\s+үшін)\b",
    re.IGNORECASE,
)

# All fields belong to one subject; they must not survive switching to another.
_PATIENT_KEYS = (
    "patient_name", "age", "complaint", "service", "can_help",
    "profile_status", "complaint_gate", "irrelevant",
    "contraindications_ok", "contraindications_raw",
    "contraindications_verdict", "preferred_date",
    "preferred_doctor_login", "preferred_doctor_name",
    "pending_doctor_date_from", "pending_doctor_time_preference",
    "selected_doctor_login", "selected_doctor_name",
    "selected_date", "selected_time", "last_slots",
    "crm_offered_slots", "doctor_options", "crm_active_appointments",
    "last_required_step", "last_required_question",
    "last_required_question_count", "time_preference",
)
_PATIENT_FACT_KEYS = {"patient_name", "patient_relation", "complaint", "age"}


def explicit_subject(text: str) -> str | None:
    """Return normalized relation, 'self', or None when not unambiguous."""
    value = re.sub(r"\s+", " ", str(text or "").casefold().replace("ё", "е"))
    matches: list[str] = []
    for relation, forms in _RELATIVES.items():
        token = rf"\b(?:{forms})\b"
        # The subject may precede or follow an explicit booking/symptom cue.
        before = rf"{_CUE_BEFORE}[^.!?]{{0,48}}{token}"
        after = rf"{token}[^.!?]{{0,28}}(?:{_CUE_AFTER}|\\b\\d{{1,3}}\\b)"
        if re.search(before, value) or re.search(after, value):
            matches.append(relation)
    if len(matches) == 1 and not _SELF.search(value):
        return matches[0]
    if not matches and _SELF.search(value):
        return "self"
    return None


def explicit_sender_name(text: str) -> str:
    """Only capture a clearly self-introduced sender name, not the patient."""
    value = str(text or "").strip()
    match = re.search(
        r"(?:меня\s+зовут|мо[её]\s+имя|менің\s+атым)\s+"
        r"([А-Яа-яЁёӘәҒғҚқҢңӨөҰұҮүҺһІіA-Za-z]{2,40})\b",
        value, flags=re.IGNORECASE,
    )
    return match.group(1) if match else ""


def set_subject(session: dict[str, Any], subject: str) -> bool:
    """Switch patient context; retain transport, sender and admission state.

    A change invalidates previous clinical gates and CRM slot/appointment facts.
    No automatic claim that a relative has been identified or is a NEW patient.
    """
    normalized = str(subject or "").strip().casefold()
    if not normalized:
        return False
    new_relation = "" if normalized == "self" else normalized
    current_relation = str(session.get("patient_relation") or "").strip().casefold()
    current_subject = str(session.get("patient_subject") or ("relative" if current_relation else "self"))
    new_subject = "relative" if new_relation else "self"
    if new_relation == current_relation and current_subject == new_subject:
        return False

    for key in _PATIENT_KEYS:
        session.pop(key, None)
    session.pop("tool_history", None)
    session.pop("booking_idempotency_key", None)
    facts = session.get("known_user_facts")
    if isinstance(facts, dict):
        for key in _PATIENT_FACT_KEYS:
            facts.pop(key, None)
    session["patient_relation"] = new_relation
    session["patient_subject"] = new_subject
    session["booking_for_self"] = not bool(new_relation)
    session["patient_identity_status"] = "unverified" if new_relation else "sender_only"
    return True
