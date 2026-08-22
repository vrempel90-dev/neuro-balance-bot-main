from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Awaitable, Callable

import dialog


ShowSlots = Callable[[str, dict[str, Any], str], Awaitable[str]]


def _parse_iso_date(value: str) -> date | None:
    try:
        return date.fromisoformat(str(value or "").strip())
    except (TypeError, ValueError):
        return None


def is_procedure_weekend(date_iso: str) -> bool:
    """Return True for Saturday/Sunday clinic procedure days."""
    parsed = _parse_iso_date(date_iso)
    return bool(parsed is not None and parsed.weekday() >= 5)


def next_weekday(date_iso: str) -> str:
    """Return the nearest Monday-Friday date on or after ``date_iso``."""
    parsed = _parse_iso_date(date_iso)
    if parsed is None:
        return str(date_iso or "")
    while parsed.weekday() >= 5:
        parsed += timedelta(days=1)
    return parsed.isoformat()


def _next_calendar_weekday(value: date) -> date:
    candidate = value + timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)
    return candidate


def _weekend_prefix(session: dict[str, Any], offered_date: str) -> str:
    lang = str(session.get("language") or "ru")
    human = dialog._format_booking_date_human(offered_date, lang)
    if lang == "kk":
        return (
            "Сенбі және жексенбі — процедуралық күндер. "
            f"Сондықтан консультацияға жақын жұмыс күніне — {human} — жаза аламыз 🌿"
        )
    return (
        "В субботу и воскресенье у нас процедурные дни. "
        f"Поэтому на консультацию можем записать Вас в ближайший рабочий день — {human} 🌿"
    )


async def _show_weekday_slots_for_weekend(
    original: ShowSlots,
    chat_id: str,
    session: dict[str, Any],
    requested_date_iso: str,
) -> str:
    requested = _parse_iso_date(requested_date_iso)
    if requested is None or requested.weekday() < 5:
        return await original(chat_id, session, requested_date_iso)

    session["weekend_procedure_day_requested"] = requested_date_iso
    candidate = _parse_iso_date(next_weekday(requested_date_iso))
    if candidate is None:
        return await original(chat_id, session, requested_date_iso)

    # Search only a small deterministic weekday window. Every candidate still
    # goes through the existing CRM check, reserve-slot filter and doctor guard.
    # We never invent availability or bypass the canonical booking path.
    last_answer = ""
    for _ in range(5):
        candidate_iso = candidate.isoformat()
        last_answer = await original(chat_id, session, candidate_iso)

        if session.get("manual_takeover") or session.get("escalated"):
            return _weekend_prefix(session, candidate_iso) + "\n\n" + last_answer

        if session.get("last_slots"):
            session["weekend_redirect_date"] = candidate_iso
            session["weekend_redirect_found_slots"] = True
            return _weekend_prefix(session, candidate_iso) + "\n\n" + last_answer

        candidate = _next_calendar_weekday(candidate)

    session["weekend_redirect_found_slots"] = False
    session["step"] = "date"
    if str(session.get("language") or "ru") == "kk":
        return (
            "Сенбі және жексенбі — процедуралық күндер. Жақын жұмыс күндеріндегі "
            "бос уақыттарды тексердім, әзірге бос терезе көрінбейді 🌿 "
            "Басқа ыңғайлы күнді жазыңызшы — актуалды кестені тексеремін."
        )
    return (
        "В субботу и воскресенье у нас процедурные дни. Проверила ближайшие "
        "рабочие дни — свободных окошек пока не вижу 🌿 Подскажите другой "
        "удобный день, и я проверю актуальное расписание."
    )


def install_weekend_booking_policy() -> None:
    """Wrap the canonical slot presenter once; leave weekday behavior unchanged."""
    current = dialog._show_slots
    if getattr(current, "_neuro_weekend_booking_policy", False):
        return

    async def guarded_show_slots(chat_id: str, session: dict[str, Any], date_iso: str) -> str:
        if not is_procedure_weekend(date_iso):
            return await current(chat_id, session, date_iso)
        return await _show_weekday_slots_for_weekend(current, chat_id, session, date_iso)

    guarded_show_slots._neuro_weekend_booking_policy = True  # type: ignore[attr-defined]
    guarded_show_slots._neuro_weekend_booking_original = current  # type: ignore[attr-defined]
    dialog._show_slots = guarded_show_slots
