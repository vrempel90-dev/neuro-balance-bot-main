from __future__ import annotations

from pathlib import Path

PATH = Path("dialog.py")
MARKER = "async def _continue_post_booking_reschedule("


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected exactly one match, got {count}")
    return text.replace(old, new, 1)


def main() -> None:
    text = PATH.read_text(encoding="utf-8")
    if MARKER in text:
        print("post-booking reschedule already applied")
        return

    old_function = '''async def _post_booking_support_answer(chat_id: str, phone: str, session: dict[str, Any], text: str) -> str:
    low = _low(text)
    session["is_booked_client"] = True
    session["post_booking_support_active"] = True
    session["first_touch_blocked_reason"] = session.get("first_touch_blocked_reason") or "active_appointment"
    if _has_any(low, ADDRESS_WORDS):
        return "Адрес: Кабанбай батыра 28, внутренний двор, подъезд 3.\\nЗаезд со стороны Кунаева, после ворот поверните направо 📍\\n\\n2ГИС: https://2gis.kz/astana/inside/9570784863354265/firm/70000001105992248?m=71.416112%2C51.134091%2F16"
    if _has_any(low, POST_BOOKING_RESCHEDULE_WORDS):
        appt = session.get("nearest_active_appointment") if isinstance(session.get("nearest_active_appointment"), dict) else None
        if not appt:
            appt = await _lookup_active_appointment(chat_id, phone, session)
        if appt:
            session["reschedule_mode"] = True
            session["original_appointment"] = appt
            return "Хорошо, перенесём 🌿 На какой день и время Вам было бы удобно?"
        session["manual_takeover"] = True
        return "Сейчас передам администратору, чтобы он помог перенести запись 🌿"
    if _is_cancel(text):
        ans = await _handle_cancel_appointment(chat_id, phone, session, text)
        return "Запись отменена 🌿 Хорошо, что предупредили. Запись отменили." if session.get("cancelled") else ans
    if _has_any(low, POST_BOOKING_TIME_WORDS) or _wants_existing_lookup(text):
        appt = session.get("nearest_active_appointment") if isinstance(session.get("nearest_active_appointment"), dict) else None
        if not appt:
            appt = await _lookup_active_appointment(chat_id, phone, session)
        if appt:
            date = session.get("appointment_date") or appt.get("date") or appt.get("appointmentDate") or ""
            time = session.get("appointment_time") or appt.get("timeStart") or appt.get("time") or ""
            doctor = session.get("appointment_doctor_name") or appt.get("doctorName") or "врачу"
            if date and time:
                return f"Вы уже записаны на {_format_booking_date_human(date, 'ru')} в {time} к врачу {doctor} 🌿"
        session["manual_takeover"] = True
        return "Сейчас передам администратору, чтобы проверили запись вручную 🌿"
    if low.strip() in POST_BOOKING_OK_WORDS or any(x == low.strip() for x in POST_BOOKING_OK_WORDS):
        return _post_booking_ok_answer(session)
    return _post_booking_ok_answer(session)
'''

    new_function = '''def _reschedule_original_appointment(session: dict[str, Any]) -> dict[str, Any] | None:
    original = session.get("original_appointment") if isinstance(session.get("original_appointment"), dict) else None
    if original:
        return original
    nearest = session.get("nearest_active_appointment") if isinstance(session.get("nearest_active_appointment"), dict) else None
    if nearest:
        return nearest
    if session.get("appointment_id") or (session.get("appointment_date") and session.get("appointment_time")):
        return {
            "id": session.get("appointment_id"),
            "date": session.get("appointment_date") or "",
            "timeStart": session.get("appointment_time") or "",
            "doctorLogin": session.get("appointment_doctor_login") or "",
            "doctorName": session.get("appointment_doctor_name") or "",
            "status": session.get("appointment_status") or "booked",
        }
    return None


def _reschedule_appointment_id(appt: dict[str, Any] | None) -> Any:
    appt = appt or {}
    return appt.get("id") or appt.get("appointmentId") or appt.get("record_id") or appt.get("recordId") or appt.get("bookingId")


def _clear_reschedule_state(session: dict[str, Any], *, keep_original: bool = False) -> None:
    session["reschedule_mode"] = False
    session["reschedule_step"] = ""
    session["reschedule_date"] = ""
    session["reschedule_slots"] = []
    if not keep_original:
        session.pop("original_appointment", None)


def _reschedule_slots_answer(session: dict[str, Any], slots: list[dict[str, Any]]) -> str:
    lang = str(session.get("language") or "ru")
    if not slots:
        return (
            "Бұл күнге бос уақыт табылмады 🌿 Басқа ыңғайлы күнді айтыңызшы."
            if lang == "kk"
            else "На этот день свободных окошек не нашла 🌿 Подскажите, пожалуйста, другой удобный день."
        )
    lines = _slots_text(slots, lang)
    if lang == "kk":
        return f"Ауыстыруға бос уақыттар:\\n{lines}\\n\\nҚай уақыт ыңғайлы?"
    return f"Для переноса есть такие свободные окошки:\\n{lines}\\n\\nКакое время Вам удобно?"


async def _continue_post_booking_reschedule(chat_id: str, phone: str, session: dict[str, Any], text: str) -> str:
    lang = str(session.get("language") or "ru")
    original = _reschedule_original_appointment(session)
    if not original:
        try:
            original = await _lookup_active_appointment(chat_id, phone, session)
        except Exception:
            original = None
    if not original:
        session["manual_takeover"] = True
        session["escalated"] = True
        _clear_reschedule_state(session, keep_original=True)
        return (
            "Жазбаны CRM-нен нақтылай алмадым. Әкімші тексеріп, ауыстыруға көмектеседі 🌿"
            if lang == "kk"
            else "Не смогла надёжно подтвердить текущую запись в CRM. Передам администратору, чтобы он помог перенести её 🌿"
        )

    session["original_appointment"] = dict(original)
    appointment_id = _reschedule_appointment_id(original) or session.get("appointment_id")
    doctor_login = str(original.get("doctorLogin") or original.get("doctor_login") or session.get("appointment_doctor_login") or "").strip()
    doctor_name = str(original.get("doctorName") or original.get("doctor_name") or session.get("appointment_doctor_name") or "").strip()
    if not appointment_id or not doctor_login:
        session["manual_takeover"] = True
        session["escalated"] = True
        _clear_reschedule_state(session, keep_original=True)
        return (
            "Жазбаның деректері толық емес. Қате ауыстырмау үшін әкімші тексеріп көмектеседі 🌿"
            if lang == "kk"
            else "В текущей записи не хватает данных для безопасного автоматического переноса. Передам администратору, чтобы он всё проверил 🌿"
        )

    step = str(session.get("reschedule_step") or "date")
    if step == "date":
        new_date = _parse_date(text)
        if not new_date:
            return "Қай күнге ауыстырған ыңғайлы? 🌿" if lang == "kk" else "На какой день Вам удобно перенести запись? 🌿"
        try:
            data = await crm.check_slots(new_date, doctor_login=doctor_login)
            if isinstance(data, dict):
                data.setdefault("date", new_date)
            slots = _format_slots(data if isinstance(data, dict) else {}, max_count=10)
            slots = [slot for slot in slots if _slot_doctor_login(slot).strip().lower() == doctor_login.lower()]
        except Exception as exc:
            session["manual_takeover"] = True
            session["escalated"] = True
            session["reschedule_error"] = str(exc)[:300]
            return (
                "Қазір бос уақытты CRM арқылы тексере алмадым. Бастапқы жазбаңыз сақталады, әкімші ауыстыруға көмектеседі 🌿"
                if lang == "kk"
                else "Сейчас не удалось проверить свободное время в CRM. Ваша текущая запись остаётся без изменений; передам администратору для переноса 🌿"
            )
        session["reschedule_date"] = new_date
        session["reschedule_slots"] = slots[:5]
        if not session["reschedule_slots"]:
            session["reschedule_step"] = "date"
            return _reschedule_slots_answer(session, [])
        session["reschedule_step"] = "time"
        return _reschedule_slots_answer(session, session["reschedule_slots"])

    slots = [slot for slot in (session.get("reschedule_slots") or []) if isinstance(slot, dict)]
    selected = _select_slot(text, slots)
    if not selected or not _slot_in_offered_set(selected, slots):
        return _reschedule_slots_answer(session, slots)

    new_date = _slot_date(selected) or str(session.get("reschedule_date") or "")
    new_time = _slot_time(selected)
    try:
        # Polyglot-style race protection: refresh the exact doctor/date before
        # changing an existing appointment. Do not trust a stale offered slot.
        if hasattr(crm, "clear_slots_cache"):
            crm.clear_slots_cache(new_date)
        fresh = await crm.check_slots(new_date, doctor_login=doctor_login)
        if isinstance(fresh, dict):
            fresh.setdefault("date", new_date)
        fresh_slots = _format_slots(fresh if isinstance(fresh, dict) else {}, max_count=1000)
        matched = next((slot for slot in fresh_slots if _same_slot_identity(slot, selected)), None)
        if matched is None:
            safe_fresh = [slot for slot in fresh_slots if _slot_doctor_login(slot).strip().lower() == doctor_login.lower()]
            session["reschedule_slots"] = safe_fresh[:5]
            session["reschedule_step"] = "time" if session["reschedule_slots"] else "date"
            return (
                _reschedule_slots_answer(session, session["reschedule_slots"])
                if session["reschedule_slots"]
                else ("Бұл уақыт енді бос емес 🌿 Басқа күнді таңдаңызшы." if lang == "kk" else "Это окошко уже занято 🌿 Подскажите другой удобный день — проверю актуальные варианты.")
            )
        selected = matched
        new_date = _slot_date(selected)
        new_time = _slot_time(selected)
        result = await crm.reschedule_appointment(
            phone=crm.normalize_phone(phone or session.get("phone") or ""),
            appointment_id=appointment_id,
            new_date=new_date,
            new_time_start=new_time,
            reason="перенос через бота",
        )
        if isinstance(result, dict) and (result.get("ok") is False or result.get("rescheduled") is False):
            raise crm.CRMError("CRM reschedule returned unsuccessful result")
    except Exception as exc:
        # Never cancel or create a second appointment as a fallback. The original
        # booking fields deliberately remain untouched on failure.
        session["manual_takeover"] = True
        session["escalated"] = True
        session["reschedule_error"] = str(exc)[:300]
        session["booking_confirmed"] = True
        return (
            "Ауыстыруды CRM-де растай алмадым. Бастапқы жазбаңыз сақталады, әкімші көмектеседі 🌿"
            if lang == "kk"
            else "Не удалось подтвердить перенос в CRM. Ваша текущая запись остаётся без изменений; передам администратору 🌿"
        )

    session["reschedule_original_appointment"] = dict(original)
    session["appointment_id"] = appointment_id
    session["appointment_date"] = new_date
    session["appointment_time"] = new_time
    session["appointment_doctor_login"] = _slot_doctor_login(selected) or doctor_login
    session["appointment_doctor_name"] = _slot_doctor_name(selected) or doctor_name
    session["nearest_active_appointment"] = {
        **dict(original),
        "id": appointment_id,
        "date": new_date,
        "timeStart": new_time,
        "doctorLogin": session["appointment_doctor_login"],
        "doctorName": session["appointment_doctor_name"],
        "status": "booked",
    }
    session["booking_confirmed"] = True
    session["booking_visible_in_crm"] = True
    session["post_booking_support_active"] = True
    session["rescheduled"] = True
    session["step"] = "booked"
    _clear_reschedule_state(session)
    if lang == "kk":
        return f"Жазбаңыз ауыстырылды 🌿 Жаңа уақыт: {new_date} {new_time}."
    return f"Запись перенесли 🌿 Новое время: {_format_booking_date_human(new_date, 'ru')} в {new_time}."


async def _post_booking_support_answer(chat_id: str, phone: str, session: dict[str, Any], text: str) -> str:
    low = _low(text)
    session["is_booked_client"] = True
    session["post_booking_support_active"] = True
    session["first_touch_blocked_reason"] = session.get("first_touch_blocked_reason") or "active_appointment"
    if session.get("reschedule_mode") and _is_cancel_direct_request(text):
        _clear_reschedule_state(session)
        ans = await _handle_cancel_appointment(chat_id, phone, session, text)
        return "Запись отменена 🌿 Хорошо, что предупредили. Запись отменили." if session.get("cancelled") else ans
    if session.get("reschedule_mode"):
        return await _continue_post_booking_reschedule(chat_id, phone, session, text)
    if _has_any(low, ADDRESS_WORDS):
        return "Адрес: Кабанбай батыра 28, внутренний двор, подъезд 3.\\nЗаезд со стороны Кунаева, после ворот поверните направо 📍\\n\\n2ГИС: https://2gis.kz/astana/inside/9570784863354265/firm/70000001105992248?m=71.416112%2C51.134091%2F16"
    if _is_reschedule_request(text):
        appt = session.get("nearest_active_appointment") if isinstance(session.get("nearest_active_appointment"), dict) else None
        if not appt:
            appt = await _lookup_active_appointment(chat_id, phone, session)
        if appt:
            session["reschedule_mode"] = True
            session["reschedule_step"] = "date"
            session["reschedule_date"] = ""
            session["reschedule_slots"] = []
            session["original_appointment"] = dict(appt)
            return "Қай күнге ауыстырған ыңғайлы? 🌿" if str(session.get("language") or "ru") == "kk" else "Хорошо, перенесём 🌿 На какой день Вам было бы удобно?"
        session["manual_takeover"] = True
        return "Әкімші жазбаны тексеріп, ауыстыруға көмектеседі 🌿" if str(session.get("language") or "ru") == "kk" else "Сейчас передам администратору, чтобы он помог перенести запись 🌿"
    if _is_cancel(text):
        ans = await _handle_cancel_appointment(chat_id, phone, session, text)
        return "Запись отменена 🌿 Хорошо, что предупредили. Запись отменили." if session.get("cancelled") else ans
    if _has_any(low, POST_BOOKING_TIME_WORDS) or _wants_existing_lookup(text):
        appt = session.get("nearest_active_appointment") if isinstance(session.get("nearest_active_appointment"), dict) else None
        if not appt:
            appt = await _lookup_active_appointment(chat_id, phone, session)
        if appt:
            date = session.get("appointment_date") or appt.get("date") or appt.get("appointmentDate") or ""
            time = session.get("appointment_time") or appt.get("timeStart") or appt.get("time") or ""
            doctor = session.get("appointment_doctor_name") or appt.get("doctorName") or "врачу"
            if date and time:
                return f"Вы уже записаны на {_format_booking_date_human(date, 'ru')} в {time} к врачу {doctor} 🌿"
        session["manual_takeover"] = True
        return "Сейчас передам администратору, чтобы проверили запись вручную 🌿"
    if low.strip() in POST_BOOKING_OK_WORDS or any(x == low.strip() for x in POST_BOOKING_OK_WORDS):
        return _post_booking_ok_answer(session)
    return _post_booking_ok_answer(session)
'''

    text = replace_once(text, old_function, new_function, "post-booking support function")

    old_route = '''        if _is_cancel(text) or _has_any(_low(text), POST_BOOKING_RESCHEDULE_WORDS) or _has_any(_low(text), POST_BOOKING_TIME_WORDS) or _wants_existing_lookup(text) or _has_any(_low(text), ADDRESS_WORDS) or _low(text).strip() in POST_BOOKING_OK_WORDS:
            booked_answer = await _post_booking_support_answer(chat_id, phone, session, text)
'''
    new_route = '''        if session.get("reschedule_mode") or _is_cancel(text) or _is_reschedule_request(text) or _has_any(_low(text), POST_BOOKING_TIME_WORDS) or _wants_existing_lookup(text) or _has_any(_low(text), ADDRESS_WORDS) or _low(text).strip() in POST_BOOKING_OK_WORDS:
            booked_answer = await _post_booking_support_answer(chat_id, phone, session, text)
'''
    text = replace_once(text, old_route, new_route, "active-booking reschedule continuation route")

    compile(text, str(PATH), "exec")
    PATH.write_text(text, encoding="utf-8")
    print("safe post-booking reschedule migration applied")


if __name__ == "__main__":
    main()
