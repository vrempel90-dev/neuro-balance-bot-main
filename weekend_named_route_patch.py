from __future__ import annotations

import dialog
import state


def install_named_weekend_route() -> None:
    """Route explicit weekend booking dates through canonical weekend slot policy."""
    current = dialog.handle_message
    if getattr(current, "_neuro_named_weekend_route", False):
        return

    async def guarded(chat_id: str, phone: str, user_text: str) -> str:
        session = state.get_session(chat_id)
        if isinstance(session, dict) and str(session.get("step") or "") in {"date", "preferred_time"}:
            date_iso = dialog._parse_date(user_text)
            if (
                date_iso
                and dialog._is_new_patient_consultation(session)
                and dialog._mentions_weekend_day(user_text)
                and dialog._is_weekend_date(date_iso)
            ):
                answer = await dialog._show_slots(chat_id, session, date_iso)
                if dialog._has_video_procedure_question(user_text):
                    answer = dialog._video_procedure_answer(session) + "\n\n" + answer
                return dialog._finalize(chat_id, session, answer)
        return await current(chat_id=chat_id, phone=phone, user_text=user_text)

    guarded._neuro_named_weekend_route = True  # type: ignore[attr-defined]
    guarded._neuro_named_weekend_original = current  # type: ignore[attr-defined]
    dialog.handle_message = guarded
