"""Deterministic outbound checks for CRM claims and clinic contact facts."""
from __future__ import annotations

import re
from urllib.parse import urlsplit

import clinic_info

_URL = re.compile(r"https?://[^\s<>\]\[)]+", re.IGNORECASE)
_BOOKED = re.compile(
    r"(?:\bвы\s+(?:успешно\s+)?записан(?:ы|а)?\b|"
    r"\bвас\s+записал(?:и|а)\b|"
    r"\bзапись\s+(?:создана|подтверждена|оформлена)\b|"
    r"\bсіз\s+жазылдыңыз\b|\bжазылуыңыз\s+расталды\b)",
    re.IGNORECASE,
)
_CANCELLED = re.compile(
    r"\bзапись\s+отменен[аы]\b|\bмы\s+отменили\s+(?:вашу\s+)?запись\b",
    re.IGNORECASE,
)
_RESCHEDULED = re.compile(
    r"\bзапись\s+перенесен[аы]\b|\bмы\s+перенесли\s+(?:вашу\s+)?запись\b",
    re.IGNORECASE,
)
_ADDRESS = re.compile(
    r"(?:\bадрес\b|\bмекенжай\b|\bулица\b|\bул\.\s*|"
    r"\bпроспект\b|\bкабанбай\s+батыр[аы]?\b)",
    re.IGNORECASE,
)
_DIGIT = re.compile(r"\d")


def _urls_in(text: str) -> set[str]:
    return {m.group(0).rstrip('.,;!?"\'') for m in _URL.finditer(text or "")}


def _normalized(url: str) -> tuple[str, str]:
    parts = urlsplit(url.strip())
    return parts.netloc.lower().removeprefix("www."), parts.path.rstrip("/").lower()


def approved_urls() -> set[tuple[str, str]]:
    """URLs from approved text templates only, no model-generated links."""
    sources = [*clinic_info.CLINIC_INFO_TEMPLATES.values(),
               *clinic_info.CLINIC_INFO_TEMPLATES_KK.values()]
    allowed = set()
    for template in sources:
        allowed.update({_normalized(u) for u in _urls_in(template)})
    return allowed


def violation(answer: str, result: object = None) -> str:
    """Return a safe category without logging or repeating patient content."""
    if not answer:
        return ""
    permitted_urls = approved_urls()
    for url in _urls_in(answer):
        if _normalized(url) not in permitted_urls:
            return "unapproved_url"
    booked = bool(result and getattr(result, "booked", False))
    tool_results = getattr(result, "tool_results", None) or []
    cancel_confirmed = any(isinstance(x, dict) and x.get("cancel_success") is True
                           for x in tool_results)
    reschedule_confirmed = any(isinstance(x, dict) and x.get("reschedule_success") is True
                               for x in tool_results)
    if _BOOKED.search(answer) and not booked:
        return "unconfirmed_booking_claim"
    if _CANCELLED.search(answer) and not cancel_confirmed:
        return "unconfirmed_cancellation_claim"
    if _RESCHEDULED.search(answer) and not reschedule_confirmed:
        return "unconfirmed_reschedule_claim"

    # When a physical address is asserted, it must contain the exact approved
    # street and building number. Do not reject a simple invitation to ask.
    low = answer.lower()
    if _ADDRESS.search(answer) and _DIGIT.search(answer):
        if any(x in low for x in ("адрес:", "наш адрес", "мекенжай:", "улица", "ул.", "проспект", "кабанбай")):
            approved_address_ru = "кабанбай батыра 28"
            approved_address_kk = "қабанбай батыр 28"
            if approved_address_ru not in low and approved_address_kk not in low:
                return "unapproved_address"
    return ""


def repeated_question(answer: str, previous: str, user_text: str) -> bool:
    """Escalate instead of trapping a patient in an identical question loop."""
    if "?" not in (answer or "") or not previous or not user_text:
        return False
    if re.search(r"\\b(?:повтор\\w*|ещё раз|снова спроси|қайталашы)\\b",
                 user_text, flags=re.IGNORECASE):
        return False
    normalize = lambda text: re.sub(r"\\s+", " ", text.casefold()).strip()
    return normalize(answer) == normalize(previous)
