"""Fail-closed final guard for appointment confirmation language.

No code path may tell a patient that an appointment exists unless the session
contains a CRM-backed confirmation flag. This protects the primary GPT agent
and the legacy fallback equally, including old helper functions that may format
a confirmation-looking sentence after an ambiguous CRM response.
"""
from __future__ import annotations

import re
from typing import Any, Callable


_CONFIRMATION_PATTERNS = (
    r"\bзапись\s+(?:подтверждена|оформлена|создана)\b",
    r"\bвы\s+(?:уже\s+)?записан(?:ы|а)?\b",
    r"\bя\s+вас\s+записал(?:а)?\b",
    r"\bвас\s+записал(?:и|а)?\b",
    r"\bжазылуыңыз\s+расталды\b",
    r"\bжазба\s+расталды\b",
    r"\bсіз\s+жазылдыңыз\b",
)


def _claims_confirmed_booking(answer: str) -> bool:
    text = str(answer or "").lower().replace("ё", "е")
    return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in _CONFIRMATION_PATTERNS)


def _crm_confirmation_present(session: dict[str, Any] | None) -> bool:
    session = session or {}
    return bool(
        session.get("booking_confirmed") is True
        or session.get("booking_visible_in_crm") is True
        or session.get("created_by_ai") is True and session.get("appointment_id")
    )


def enforce_confirmed_booking_only(
    answer: str,
    session: dict[str, Any] | None,
    base_guard: Callable[[str, dict[str, Any] | None], str],
) -> str:
    cleaned = base_guard(answer, session)
    if not cleaned or not _claims_confirmed_booking(cleaned):
        return cleaned
    if _crm_confirmation_present(session):
        return cleaned

    lang = str((session or {}).get("language") or "ru")
    if lang == "kk":
        return (
            "Жазылу әлі CRM-де расталған жоқ. Қате ақпарат бермеу үшін "
            "жазбаны расталды деп айта алмаймын 🌿 Әкімші тексеріп береді."
        )
    return (
        "Запись пока не подтверждена в CRM. Чтобы не вводить Вас в заблуждение, "
        "не буду говорить, что запись оформлена 🌿 Администратор проверит её вручную."
    )
