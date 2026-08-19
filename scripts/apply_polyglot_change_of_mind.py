from __future__ import annotations

from pathlib import Path

PATH = Path("dialog.py")
MARKER = "def _reset_slot_choice_for_change("


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected exactly one match, got {count}")
    return text.replace(old, new, 1)


def main() -> None:
    text = PATH.read_text(encoding="utf-8")
    if MARKER in text:
        print("change-of-mind migration already applied")
        return

    anchor = '''def _looks_like_name(text: str) -> bool:
'''
    helpers = '''def _reset_slot_choice_for_change(session: dict[str, Any], *, clear_offered: bool) -> None:
    """Clear only slot-choice state when the patient changes date/time.

    Clinical gates and complaint context remain intact; no questionnaire restart.
    """
    session.pop("selected_slot", None)
    session["selected_date"] = ""
    session["selected_time"] = ""
    session["selected_doctor_login"] = ""
    session["selected_doctor_name"] = ""
    session["booking_ready"] = False
    session["slot_selection_rejected_reason"] = ""
    if clear_offered:
        session["last_slots"] = []
        session["preferred_date"] = ""
        session["preferred_date_text"] = ""


def _wants_another_booking_day(text: str) -> bool:
    low = _low(text)
    return any(marker in low for marker in (
        "другой день", "другую дату", "другая дата", "на другой день",
        "не этот день", "день не подходит", "дата не подходит",
        "басқа күн", "баска кун", "басқа күнге", "баска кунге",
        "басқа дата", "баска дата", "күнді ауыстыр", "кунди ауыстыр",
    ))


def _wants_another_booking_time(text: str) -> bool:
    low = _low(text)
    return any(marker in low for marker in (
        "другое время", "другой вариант", "другое окошко", "другое окно",
        "не это время", "время не подходит", "это время не подходит",
        "басқа уақыт", "баска уакыт", "басқа уақытқа", "баска уакытка",
        "уақытты ауыстыр", "уакытты ауыстыр",
    ))


''' + anchor
    text = replace_once(text, anchor, helpers, "change-of-mind helpers")

    old_time = '''        if step in ("time", "select_slot"):
            slots = session.get("last_slots") or []
            slot = _select_slot(text, slots)
            if not slot:
                if _has_video_procedure_question(text):
                    return _finalize(
                        chat_id,
                        session,
                        _video_procedure_answer(session)
                        + "\\n\\n"
                        + _tr(session, "Какое время из вариантов выше Вам удобно?", "Жоғарыдағы уақыттардың қайсысы ыңғайлы?"),
                    )
                return _finalize(chat_id, session, _mandatory_step_prompt(session, "time"))
            _remember_selected_slot(session, slot)
            session["step"] = "name"
            session["questionnaire_step"] = "name"
            answer = _ask_name(session)
            if _has_video_procedure_question(text):
                answer = _video_procedure_answer(session) + "\\n\\n" + answer
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
'''

    new_time = '''        if step in ("time", "select_slot"):
            # Polyglot change-of-mind: an explicit new date wins over the old
            # offered slots. Reuse the normal CRM slot path instead of forcing
            # the patient to finish a stale choice.
            changed_date = _parse_date(text)
            if changed_date:
                _reset_slot_choice_for_change(session, clear_offered=True)
                answer = await _show_slots(chat_id, session, changed_date)
                return _finalize(chat_id, session, answer)
            if _wants_another_booking_day(text):
                _reset_slot_choice_for_change(session, clear_offered=True)
                session["step"] = "date"
                session["questionnaire_step"] = "date"
                return _finalize(chat_id, session, _ask_date(session))

            slots = session.get("last_slots") or []
            slot = _select_slot(text, slots)
            if not slot:
                if _has_video_procedure_question(text):
                    return _finalize(
                        chat_id,
                        session,
                        _video_procedure_answer(session)
                        + "\\n\\n"
                        + _tr(session, "Какое время из вариантов выше Вам удобно?", "Жоғарыдағы уақыттардың қайсысы ыңғайлы?"),
                    )
                return _finalize(chat_id, session, _mandatory_step_prompt(session, "time"))
            _remember_selected_slot(session, slot)
            session["step"] = "name"
            session["questionnaire_step"] = "name"
            answer = _ask_name(session)
            if _has_video_procedure_question(text):
                answer = _video_procedure_answer(session) + "\\n\\n" + answer
            return _finalize(chat_id, session, answer)

        if step == "name":
            # The patient may change their mind after selecting a slot but before
            # giving a name. Never interpret such a message as a name or book the
            # stale slot.
            changed_date = _parse_date(text)
            if changed_date:
                _reset_slot_choice_for_change(session, clear_offered=True)
                session["patient_name"] = ""
                answer = await _show_slots(chat_id, session, changed_date)
                return _finalize(chat_id, session, answer)
            if _wants_another_booking_day(text):
                _reset_slot_choice_for_change(session, clear_offered=True)
                session["patient_name"] = ""
                session["step"] = "date"
                session["questionnaire_step"] = "date"
                return _finalize(chat_id, session, _ask_date(session))

            offered = session.get("last_slots") or []
            replacement_slot = _select_slot(text, offered)
            current_slot = session.get("selected_slot") if isinstance(session.get("selected_slot"), dict) else None
            if replacement_slot and (current_slot is None or not _same_slot_identity(replacement_slot, current_slot)):
                _remember_selected_slot(session, replacement_slot)
                session["patient_name"] = ""
                session["step"] = "name"
                session["questionnaire_step"] = "name"
                return _finalize(chat_id, session, _ask_name(session))
            if _wants_another_booking_time(text):
                _reset_slot_choice_for_change(session, clear_offered=False)
                session["patient_name"] = ""
                session["step"] = "time"
                session["questionnaire_step"] = "time"
                return _finalize(chat_id, session, _mandatory_step_prompt(session, "time"))

            if _is_service_polite_phrase(text):
                session["patient_name"] = ""
                return _finalize(chat_id, session, "Подскажите, пожалуйста, Ваше имя для записи.")
            name = _extract_name(text)
            if not name:
                return _finalize(chat_id, session, _ask_name(session))
            session["patient_name"] = name
            return _finalize(chat_id, session, await _book(chat_id, session, phone))
'''
    text = replace_once(text, old_time, new_time, "time/name change-of-mind routing")

    compile(text, str(PATH), "exec")
    PATH.write_text(text, encoding="utf-8")
    print("Polyglot change-of-mind migration applied")


if __name__ == "__main__":
    main()
