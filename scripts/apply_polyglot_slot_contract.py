from __future__ import annotations

from pathlib import Path


PATH = Path("dialog.py")
MARKER = "def _same_slot_identity("


def replace_exact(text: str, old: str, new: str, label: str, expected: int = 1) -> str:
    count = text.count(old)
    if count != expected:
        raise RuntimeError(f"{label}: expected {expected} matches, got {count}")
    return text.replace(old, new)


def main() -> None:
    text = PATH.read_text(encoding="utf-8")
    if MARKER in text:
        print("slot contract already applied")
        return

    old_time = '''def _slot_time(slot: dict[str, Any]) -> str:
    return str(slot.get("time") or slot.get("timeStart") or slot.get("time_start") or "")
'''
    new_time = old_time + '''

def _same_slot_identity(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """Compare the stable CRM identity of two slot payloads, independent of key shape."""
    if not isinstance(left, dict) or not isinstance(right, dict):
        return False

    def normalize_time(value: str) -> str:
        raw = str(value or "").strip()
        match = re.fullmatch(r"(\\d{1,2}):(\\d{2})(?::\\d{2})?", raw)
        if not match:
            return raw
        return f"{int(match.group(1)):02d}:{match.group(2)}"

    left_login = _slot_doctor_login(left).strip().lower()
    right_login = _slot_doctor_login(right).strip().lower()
    left_date = _slot_date(left).strip()
    right_date = _slot_date(right).strip()
    left_time = normalize_time(_slot_time(left))
    right_time = normalize_time(_slot_time(right))
    return bool(
        left_login
        and right_login
        and left_date
        and right_date
        and left_time
        and right_time
        and left_login == right_login
        and left_date == right_date
        and left_time == right_time
    )


def _slot_in_offered_set(slot: dict[str, Any], offered: list[Any]) -> bool:
    return any(_same_slot_identity(existing, slot) for existing in offered if isinstance(existing, dict))
'''
    text = replace_exact(text, old_time, new_time, "slot identity helper")

    old_membership = 'if last_slots and not any(existing == slot for existing in last_slots if isinstance(existing, dict)):'
    new_membership = 'if last_slots and not _slot_in_offered_set(slot, last_slots):'
    text = replace_exact(text, old_membership, new_membership, "offered-set membership", expected=2)

    # A rejected selection must not leave stale denormalized time behind.
    old_remember_reject = '''    if last_slots and not _slot_in_offered_set(slot, last_slots):
        session.pop("selected_slot", None)
        session["slot_selection_rejected_reason"] = "not_in_session_last_slots"
        return
'''
    new_remember_reject = '''    if last_slots and not _slot_in_offered_set(slot, last_slots):
        session.pop("selected_slot", None)
        session["selected_time"] = ""
        session["slot_selection_rejected_reason"] = "not_in_session_last_slots"
        return
'''
    text = replace_exact(text, old_remember_reject, new_remember_reject, "remember stale slot cleanup")

    old_book_reject = '''    if last_slots and not _slot_in_offered_set(slot, last_slots):
        session.pop("selected_slot", None)
        session["step"] = "time" if last_slots else "date"
        _safe_log(chat_id, "booking_payload_blocked_not_from_last_slots", {"chat_id": chat_id, "step": session.get("step") or "", "slots_count": len(last_slots) if isinstance(last_slots, list) else 0})
        return _mandatory_step_prompt(session, session["step"])
'''
    new_book_reject = '''    if last_slots and not _slot_in_offered_set(slot, last_slots):
        session.pop("selected_slot", None)
        session["selected_time"] = ""
        session["booking_ready"] = False
        session["step"] = "time" if last_slots else "date"
        _safe_log(chat_id, "booking_payload_blocked_not_from_last_slots", {"chat_id": chat_id, "step": session.get("step") or "", "slots_count": len(last_slots) if isinstance(last_slots, list) else 0})
        return _mandatory_step_prompt(session, session["step"])
'''
    text = replace_exact(text, old_book_reject, new_book_reject, "booking stale slot cleanup")

    old_legacy = '''    if not last_slots:
        _safe_log(chat_id, "booking_payload_legacy_selected_slot_without_last_slots", {"chat_id": chat_id, "step": session.get("step") or ""})

    if session.get("contraindications_ok") is True and not session.get("contraindications_verdict"):
'''
    new_legacy = '''    if not last_slots:
        # Compatibility-safe Polyglot invariant: a legacy in-flight session may
        # have preserved selected_slot while losing last_slots. Revalidate the
        # exact doctor/date/time against current CRM availability before booking.
        slot_date = _slot_date(slot).strip()
        slot_login = _slot_doctor_login(slot).strip()
        try:
            data = await crm.check_slots(slot_date, doctor_login=(slot_login or None))
            if isinstance(data, dict):
                data.setdefault("date", slot_date)
            refreshed_slots = _format_slots(data if isinstance(data, dict) else {}, max_count=1000)
        except Exception as exc:
            session["booking_ready"] = False
            session["crm_called"] = False
            session["booking_confirmed"] = False
            session["manual_takeover"] = True
            session["escalated"] = True
            session["ai_muted"] = True
            session["handoff_reason"] = "legacy_slot_revalidation_failed"
            bot_tools.escalate_to_human(session, "legacy_slot_revalidation_failed")
            _safe_log(chat_id, "booking_payload_legacy_slot_revalidation_failed", {
                "chat_id": chat_id,
                "date": slot_date,
                "doctor_login": slot_login,
                "error": str(exc)[:300],
            })
            return _crm_slots_unavailable_answer(session)

        matched_slot = next(
            (candidate for candidate in refreshed_slots if _same_slot_identity(candidate, slot)),
            None,
        )
        if matched_slot is None:
            session["booking_ready"] = False
            session["crm_called"] = False
            session["booking_confirmed"] = False
            session.pop("selected_slot", None)
            session["selected_time"] = ""
            session["last_slots"] = refreshed_slots[:5]
            session["step"] = "time" if session["last_slots"] else "date"
            _safe_log(chat_id, "booking_payload_legacy_slot_stale", {
                "chat_id": chat_id,
                "date": slot_date,
                "doctor_login": slot_login,
                "requested_time": _slot_time(slot),
                "current_slots_count": len(refreshed_slots),
            })
            return _mandatory_step_prompt(session, session["step"])

        slot = matched_slot
        session["selected_slot"] = matched_slot
        session["selected_doctor_login"] = _slot_doctor_login(matched_slot)
        session["selected_doctor_name"] = _slot_doctor_name(matched_slot)
        session["selected_date"] = _slot_date(matched_slot)
        session["selected_time"] = _slot_time(matched_slot)
        session["last_slots"] = [matched_slot]
        session["legacy_slot_revalidated"] = True
        _safe_log(chat_id, "booking_payload_legacy_slot_revalidated", {
            "chat_id": chat_id,
            "date": _slot_date(matched_slot),
            "doctor_login": _slot_doctor_login(matched_slot),
            "time": _slot_time(matched_slot),
        })

    if session.get("contraindications_ok") is True and not session.get("contraindications_verdict"):
'''
    text = replace_exact(text, old_legacy, new_legacy, "legacy slot revalidation")

    compile(text, str(PATH), "exec")
    PATH.write_text(text, encoding="utf-8")
    print("Polyglot slot contract applied")


if __name__ == "__main__":
    main()
