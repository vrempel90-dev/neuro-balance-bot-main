from __future__ import annotations

import ast
import asyncio
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

os.environ["SQLITE_PATH"] = tempfile.NamedTemporaryFile(delete=False, suffix=".sqlite3").name
os.environ.setdefault("CRM_BOT_SECRET", "test")
os.environ.setdefault("OPENAI_API_KEY", "")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import crm
import main
import state
import dialog
import bot_tools
from dialog import handle_message


state.init_db()


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def setup_crm(monkeypatch: Any, *, lookup_error: bool = False, cancel_error: bool = False, slots_error: bool = False, book_error: bool = False) -> dict[str, list[Any]]:
    calls: dict[str, list[Any]] = {
        "lookup": [],
        "cancel": [],
        "slots": [],
        "book": [],
    }

    async def fake_patient_lookup(phone: str) -> dict[str, Any]:
        calls["lookup"].append(phone)
        if lookup_error:
            raise crm.CRMError("CRM unavailable")
        return {
            "hasActiveAppointment": True,
            "lastAppointment": {
                "appointmentId": 777,
                "date": "2099-01-01",
                "timeStart": "18:00",
                "doctorName": "Тестовый врач",
            },
        }

    async def fake_cancel_appointment(**kwargs: Any) -> dict[str, Any]:
        calls["cancel"].append(kwargs)
        if cancel_error:
            raise crm.CRMError("CRM unavailable")
        return {"ok": True, "cancelled": True, "appointmentId": kwargs.get("appointment_id")}

    async def fake_check_slots(date: str, doctor_login: str | None = None) -> dict[str, Any]:
        calls["slots"].append({"date": date, "doctor_login": doctor_login})
        if slots_error:
            raise crm.CRMError("CRM unavailable")
        return {
            "availability": [
                {
                    "doctorLogin": "doctor1",
                    "doctorName": "Тестовый врач",
                    "availableSlots": ["18:00"],
                }
            ]
        }

    async def fake_book_appointment(**kwargs: Any) -> dict[str, Any]:
        calls["book"].append(kwargs)
        if book_error:
            raise crm.CRMError("CRM unavailable")
        return {
            "ok": True,
            "appointmentId": 999,
            "date": kwargs.get("date"),
            "timeStart": kwargs.get("time_start"),
            "doctorName": kwargs.get("doctor_name") or "Тестовый врач",
        }

    monkeypatch.setattr(crm, "patient_lookup", fake_patient_lookup)
    monkeypatch.setattr(crm, "cancel_appointment", fake_cancel_appointment)
    monkeypatch.setattr(crm, "check_slots", fake_check_slots)
    monkeypatch.setattr(crm, "book_appointment", fake_book_appointment)
    return calls


def reset(chat_id: str, preset: dict[str, Any] | None = None) -> None:
    state.reset_session(chat_id)
    if preset:
        session = state.get_session(chat_id)
        session.update(preset)
        state.save_session(chat_id, session)


def answer(chat_id: str, text: str) -> str:
    return run(handle_message(chat_id, "77011234567", text))


def test_thanks_ok_guard_does_not_match_substrings() -> None:
    assert dialog._is_thanks_or_ok("ок") is True
    assert dialog._is_thanks_or_ok("спасибо") is True
    assert dialog._is_thanks_or_ok("Поясничная область начала беспокоить") is False
    assert dialog._is_thanks_or_ok("спина беспокоит") is False


def test_profile_age_answer_is_human_and_contextual() -> None:
    cases = [
        ("profile_human_back", "Спина болит", ["Поняла", "спина", "сколько Вам лет"], ["Здравствуйте", "С такими жалобами"]),
        ("profile_human_low_back", "Поясничная область начала беспокоить", ["Поняла", "Поясничная", "сколько Вам лет"], []),
        ("profile_human_protrusion", "У меня протрузия", ["Протруз", "сколько Вам лет"], []),
        ("profile_human_neck_numb", "Шея болит и рука немеет", ["сколько Вам лет"], []),
        ("profile_human_knee", "Колено болит", ["сколько Вам лет"], []),
    ]

    for chat_id, text, expected_parts, forbidden_parts in cases:
        reset(chat_id)
        result = answer(chat_id, text)

        for part in expected_parts:
            assert part in result
        for part in forbidden_parts:
            assert part not in result

        if text == "Шея болит и рука немеет":
            assert "шея" in result.lower() or "онемение" in result.lower()
        if text == "Колено болит":
            assert "сустав" in result.lower() or "суставам" in result.lower()


def test_contraindications_clear_no_phrases_go_to_date(monkeypatch: Any) -> None:
    setup_crm(monkeypatch)

    cases = [
        ("contra_clear_all_clean", "Все чисто"),
        ("contra_clear_none_of_this", "ничего из этого нет"),
        ("contra_clear_all_no", "по всем нет"),
        ("contra_clear_all_normal", "все нормально"),
    ]

    for chat_id, text in cases:
        reset(chat_id, {"step": "contraindications", "age": 34, "language": "ru", "language_locked": True})
        result = answer(chat_id, text)
        session = state.get_session(chat_id)

        assert "На какой день" in result
        assert session["contraindications_ok"] is True
        assert session["contraindications_raw"] == text
        assert session["contraindications_verdict"] == "proceed"
        assert session["step"] == "date"
        assert session["questionnaire_step"] == "date"


def test_contraindications_real_contraindication_still_stops_booking(monkeypatch: Any) -> None:
    setup_crm(monkeypatch)

    reset("contra_real_stop", {"step": "contraindications", "age": 34, "language": "ru", "language_locked": True})
    result = answer("contra_real_stop", "у меня кардиостимулятор")
    session = state.get_session("contra_real_stop")

    assert session["step"] == "stopped"
    assert session["contraindications_ok"] is False
    assert session["contraindications_verdict"] in {"stop", "refuse"}
    assert "останавливаю" in result


def test_standalone_thanks_ok_do_not_start_questionnaire(monkeypatch: Any) -> None:
    calls = setup_crm(monkeypatch)

    for text in ["спасибо", "рахмет", "ок", "спосибо", "ракмет"]:
        chat_id = f"thanks_{text}"
        reset(chat_id)
        result = answer(chat_id, text)
        session = state.get_session(chat_id)
        assert result == ""
        assert main._guard_answer(chat_id, result) == ""
        assert session["step"] == "start"

    assert calls["lookup"] == []
    assert calls["slots"] == []
    assert calls["book"] == []


def test_visit_confirmation_is_not_new_booking(monkeypatch: Any) -> None:
    calls = setup_crm(monkeypatch)

    for text in ["Буду", "приду", "буду в 18.00"]:
        chat_id = f"confirm_{text}"
        reset(chat_id)
        result = answer(chat_id, text)
        session = state.get_session(chat_id)
        assert "будем ждать" in result
        assert session["status"] == "visit_confirmed"
        assert session["step"] == "done"

    assert calls["lookup"] == []
    assert calls["slots"] == []
    assert calls["book"] == []


def test_existing_appointment_lookup_wins_over_booking_flow(monkeypatch: Any) -> None:
    calls = setup_crm(monkeypatch)

    for text in ["Я уже записан", "напомните время", "напомните время, хочу записаться"]:
        chat_id = f"lookup_{text}"
        reset(chat_id)
        result = answer(chat_id, text)
        assert "Вы уже записаны" in result
        assert "18:00" in result

    assert len(calls["lookup"]) == 3
    assert calls["slots"] == []
    assert calls["book"] == []


def test_cancel_and_negative_visit_use_crm_not_confirmation(monkeypatch: Any) -> None:
    calls = setup_crm(monkeypatch)

    for text in ["Отмените", "не приду", "ни приду", "атмените запись"]:
        chat_id = f"cancel_{text}"
        reset(chat_id)
        result = answer(chat_id, text)
        session = state.get_session(chat_id)
        assert "отменили" in result
        assert session["status"] == "cancelled"
        assert "будем ждать" not in result

    assert len(calls["lookup"]) == 4
    assert len(calls["cancel"]) == 4
    assert calls["slots"] == []
    assert calls["book"] == []


def test_mri_ct_images_do_not_start_questionnaire_and_viktor_is_not_ct(monkeypatch: Any) -> None:
    calls = setup_crm(monkeypatch)

    for text in ["Нужно ли МРТ?", "КТ надо?", "У меня есть снимки"]:
        chat_id = f"mri_{text}"
        reset(chat_id)
        result = answer(chat_id, text)
        session = state.get_session(chat_id)
        assert "заранее делать не обязательно" in result
        assert session["step"] == "start"

    reset("viktor_start")
    result = answer("viktor_start", "Виктор")
    assert "МРТ" not in result
    assert "КТ" not in result
    assert "чем можем помочь" in result

    assert calls["slots"] == []
    assert calls["book"] == []


def test_when_waiting_for_name_accepts_name_and_books(monkeypatch: Any) -> None:
    calls = setup_crm(monkeypatch)
    reset(
        "name_viktor",
        {
            "step": "name",
            "language": "ru",
            "language_locked": True,
            "complaint": "болит спина",
            "age": 35,
            "contraindications_ok": True,
            "selected_slot": {
                "doctor_login": "doctor1",
                "doctor_name": "Тестовый врач",
                "date": "2099-01-01",
                "time": "18:00",
            },
        },
    )

    result = answer("name_viktor", "Виктор")
    session = state.get_session("name_viktor")
    assert result
    assert session["patient_name"] == "Виктор"
    assert session["step"] == "done"
    assert session["status"] == "booked"
    assert len(calls["book"]) == 1


def test_uncertain_message_clarifies_instead_of_booking(monkeypatch: Any) -> None:
    calls = setup_crm(monkeypatch)
    reset("uncertain")

    result = answer("uncertain", "непонятно")
    session = state.get_session("uncertain")
    assert "чем можем помочь" in result
    assert session["step"] == "start"
    assert calls["slots"] == []
    assert calls["book"] == []


def test_crm_lookup_and_cancel_fallbacks_do_not_go_silent(monkeypatch: Any) -> None:
    setup_crm(monkeypatch, lookup_error=True)
    reset("lookup_fallback")
    result = answer("lookup_fallback", "напомните время")
    session = state.get_session("lookup_fallback")
    assert result
    assert "администратор" in result
    assert session["step"] == "escalated"

    setup_crm(monkeypatch, lookup_error=True, cancel_error=True)
    reset("cancel_fallback")
    result = answer("cancel_fallback", "не приду")
    session = state.get_session("cancel_fallback")
    assert result
    assert "администратор" in result
    assert session["step"] == "escalated"


def test_language_lock_keeps_ru_and_kk(monkeypatch: Any) -> None:
    setup_crm(monkeypatch)

    reset("lang_ru", {"language": "ru", "language_locked": True, "step": "date"})
    assert answer("lang_ru", "рахмет") == ""
    assert state.get_session("lang_ru")["language"] == "ru"

    reset("lang_kk", {"language": "kk", "language_locked": True, "step": "date"})
    assert answer("lang_kk", "ок") == ""
    assert state.get_session("lang_kk")["language"] == "kk"


def test_release_candidate_state_machine_and_faq_regressions(monkeypatch: Any) -> None:
    calls = setup_crm(monkeypatch)

    reset("rc_age", {"step": "age", "complaint": "болит спина", "language": "ru", "language_locked": True})
    result = answer("rc_age", "36 лет")
    session = state.get_session("rc_age")
    assert session["age"] == 36
    assert session["step"] == "contraindications"
    assert "противопоказ" in result.lower()

    reset("rc_age_price", {"step": "age", "complaint": "болит спина", "language": "ru", "language_locked": True})
    result = answer("rc_age_price", "Сколько стоит?")
    session = state.get_session("rc_age_price")
    assert "5 000" in result
    assert "сколько Вам лет" in result
    assert session["step"] == "age"

    reset("rc_contra_price", {"step": "contraindications", "complaint": "болит спина", "age": 36, "language": "ru", "language_locked": True})
    result = answer("rc_contra_price", "Сколько стоит курс лечения")
    session = state.get_session("rc_contra_price")
    assert "5 000" in result
    assert "Стоимость курса" in result
    assert "противопоказ" in result.lower()
    assert "сколько Вам лет" not in result
    assert session["step"] == "contraindications"

    reset("rc_date_price", {"step": "date", "complaint": "болит спина", "age": 36, "contraindications_ok": True, "language": "ru", "language_locked": True})
    result = answer("rc_date_price", "Сколько стоит приём?")
    session = state.get_session("rc_date_price")
    assert "5 000" in result
    assert "На какой день" in result
    assert session["step"] == "date"

    reset("rc_contra_no", {"step": "contraindications", "complaint": "болит спина", "age": 36, "language": "ru", "language_locked": True})
    result = answer("rc_contra_no", "Противопаказаний нет")
    session = state.get_session("rc_contra_no")
    assert session["contraindications_ok"] is True
    assert session["step"] == "date"
    assert "На какой день" in result

    assert calls["book"] == []


def test_release_candidate_done_mode_and_language_regressions(monkeypatch: Any) -> None:
    setup_crm(monkeypatch)

    reset("rc_done_lookup", {"step": "done", "booked": True, "language": "ru", "language_locked": True})
    result = answer("rc_done_lookup", "На какое число записали")
    assert "2099-01-01" in result and "18:00" in result

    reset("rc_done_address", {"step": "done", "booked": True, "language": "ru", "language_locked": True})
    result = answer("rc_done_address", "Куда обращаться?")
    assert "Кабанбай батыра 28" in result
    assert "Ваша запись уже оформлена" not in result

    reset("rc_done_advice", {"step": "done", "booked": True, "language": "ru", "language_locked": True})
    result = answer("rc_done_advice", "Посоветуйте")
    assert "Ваша запись уже оформлена" not in result
    assert "передам" in result.lower() or "уточ" in result.lower()

    reset("rc_kk_switch")
    result = answer("rc_kk_switch", "Қазақша жоқпа")
    assert "қазақша" in result.lower()
    result = answer("rc_kk_switch", "Ооо")
    assert state.get_session("rc_kk_switch")["language"] == "kk"
    assert "қазақша" not in result.lower()
    result = answer("rc_kk_switch", "Бел жағын қарайсыздарма")
    session = state.get_session("rc_kk_switch")
    assert session["step"] == "age"
    assert "Жасыңыз нешеде" in result
    result = answer("rc_kk_switch", "36")
    session = state.get_session("rc_kk_switch")
    assert session["age"] == 36
    assert session["step"] == "contraindications"
    assert "Қарсы көрсетілім" in result



def test_booking_request_without_complaint_asks_for_complaint(monkeypatch: Any) -> None:
    setup_crm(monkeypatch)

    reset("appt_diagnostics")
    result = answer("appt_diagnostics", "Здравствуйте хочу записаться на диагностику")
    session = state.get_session("appt_diagnostics")
    assert "можно записаться на диагностику" in result
    assert "что Вас беспокоит" in result
    assert session["step"] == "complaint"
    assert "этим направлением" not in result
    assert not session.get("escalated")

    reset("appt_consultation")
    result = answer("appt_consultation", "Здравствуйте хочу записаться на консультацию")
    session = state.get_session("appt_consultation")
    assert "можно записаться на консультацию" in result
    assert "что Вас беспокоит" in result
    assert session["step"] == "complaint"

    reset("appt_reception")
    result = answer("appt_reception", "Хочу записаться на приём")
    session = state.get_session("appt_reception")
    assert ("можно записаться на приём" in result) or ("можно записаться на прием" in result)
    assert "что Вас беспокоит" in result
    assert session["step"] == "complaint"

    reset("appt_diagnostics_with_profile")
    result = answer("appt_diagnostics_with_profile", "Хочу записаться на диагностику, спина болит")
    session = state.get_session("appt_diagnostics_with_profile")
    assert session["step"] == "age"
    assert "спина" in result.lower()
    assert "сколько Вам лет" in result

    reset("appt_diagnostics_nonprofile")
    result = answer("appt_diagnostics_nonprofile", "Хочу диагностику сердца")
    session = state.get_session("appt_diagnostics_nonprofile")
    assert session["step"] == "escalated"
    assert session.get("escalated") is True
    assert "этим направлением" in result

def test_release_candidate_profile_nonprofile_and_safety_regressions(monkeypatch: Any) -> None:
    setup_crm(monkeypatch)

    reset("rc_mri")
    result = answer("rc_mri", "МРТ заранее нужно делать?")
    assert "заранее делать не обязательно" in result
    assert state.get_session("rc_mri")["step"] == "start"

    reset("rc_typo_complaint")
    result = answer("rc_typo_complaint", "Здравствуйте у меня грижа и балит поесница")
    session = state.get_session("rc_typo_complaint")
    assert session["step"] == "age"
    assert "сколько Вам лет" in result
    assert "имя" not in result.lower()

    reset("rc_back_area_bothers")
    result = answer("rc_back_area_bothers", "Поясничная область начала беспокоить")
    session = state.get_session("rc_back_area_bothers")
    assert result != ""
    assert session["profile_status"] == "profile"
    assert session["step"] == "age"
    assert "сколько Вам лет" in result

    reset("rc_hard_contra")
    result = answer("rc_hard_contra", "кардиостемулятор")
    session = state.get_session("rc_hard_contra")
    assert session["step"] == "stopped"
    assert "останавливаю" in result

    reset("rc_nonprofile")
    result = answer("rc_nonprofile", "зуб болит")
    session = state.get_session("rc_nonprofile")
    assert session["step"] == "escalated"
    assert "этим направлением" in result
    assert "сколько Вам лет" not in result

    reset("rc_viktor")
    result = answer("rc_viktor", "Виктор")
    assert "МРТ" not in result and "КТ" not in result
    assert "чем можем помочь" in result

    from strict_prompt_guard import enforce_prompt_only
    assert enforce_prompt_only("") == ""
    unsafe = "Мы гарантируем результат и обязательно вылечим."
    guarded = enforce_prompt_only(unsafe)
    assert "гарантируем результат" not in guarded.lower()
    assert "обязательно вылечим" not in guarded.lower()


def test_release_candidate_crm_slots_and_book_fallbacks(monkeypatch: Any) -> None:
    setup_crm(monkeypatch, slots_error=True)
    reset("rc_slots_error", {"step": "date", "complaint": "болит спина", "age": 36, "contraindications_ok": True, "language": "ru", "language_locked": True})
    result = answer("rc_slots_error", "завтра")
    session = state.get_session("rc_slots_error")
    assert result
    assert "администратор" in result
    assert "CRM" not in result
    assert session["step"] == "escalated"

    setup_crm(monkeypatch, book_error=True)
    reset(
        "rc_book_error",
        {
            "step": "name",
            "language": "ru",
            "language_locked": True,
            "complaint": "болит спина",
            "age": 36,
            "contraindications_ok": True,
            "selected_slot": {"doctor_login": "doctor1", "doctor_name": "Тестовый врач", "date": "2099-01-01", "time": "18:00"},
        },
    )
    result = answer("rc_book_error", "Виктор")
    session = state.get_session("rc_book_error")
    assert result
    assert "администратор" in result
    assert "CRM" not in result
    assert session["step"] == "escalated"


def test_production_fix_live_admin_regressions(monkeypatch: Any) -> None:
    calls = setup_crm(monkeypatch)

    reset("pf_method")
    result = answer("pf_method", "Что за методика")
    session = state.get_session("pf_method")
    assert "безоперационные методы лечения" in result
    assert "магнитотерапия" in result
    assert "что Вас беспокоит" in result
    assert session["step"] == "complaint"

    reset("pf_doctors_age", {"step": "age", "complaint": "болит спина", "language": "ru", "language_locked": True})
    result = answer("pf_doctors_age", "У вас врачи или как")
    session = state.get_session("pf_doctors_age")
    assert "консультацию проводит врач" in result
    assert "сколько Вам лет" in result
    assert session["step"] == "age"

    reset("pf_doctors_age_inline", {"step": "age", "complaint": "болит спина", "language": "ru", "language_locked": True})
    result = answer("pf_doctors_age_inline", "У вас врачи или как 46")
    session = state.get_session("pf_doctors_age_inline")
    assert "консультацию проводит врач" in result
    assert session["age"] == 46
    assert session["step"] == "contraindications"
    assert "противопоказ" in result.lower()

    reset("pf_leg_radiation")
    result = answer("pf_leg_radiation", "У меня боль в пояснице и отдаёт на ногу")
    session = state.get_session("pf_leg_radiation")
    assert "поняла" in result.lower()
    old_phrase = "С такими жалобами к нам " + "обращаются"
    assert old_phrase not in result
    assert old_phrase.lower() not in result
    assert "сколько Вам лет" in result
    assert "противопоказание для записи" not in result
    assert session["step"] == "age"

    reset("pf_leg_pull")
    result = answer("pf_leg_pull", "тянет ногу и болит поясница")
    session = state.get_session("pf_leg_pull")
    assert "поняла" in result.lower()
    old_phrase = "С такими жалобами к нам " + "обращаются"
    assert old_phrase not in result
    assert old_phrase.lower() not in result
    assert "противопоказание для записи" not in result
    assert session["step"] == "age"

    reset("pf_duplicate", {"step": "date", "complaint": "болит спина", "age": 36, "contraindications_ok": True, "language": "ru", "language_locked": True})
    first = answer("pf_duplicate", "в субботу")
    second = answer("pf_duplicate", "в субботу")
    assert "процедурные дни" in first
    assert second == ""

    reset("pf_course_days", {"step": "date", "complaint": "болит спина", "age": 36, "contraindications_ok": True, "language": "ru", "language_locked": True})
    result = answer("pf_course_days", "Сколько дней будет всего")
    session = state.get_session("pf_course_days")
    assert "Количество дней и процедур врач сможет определить" in result
    assert "На какой день Вам удобно прийти" in result
    assert session["step"] == "date"

    reset("pf_no_contra", {"step": "contraindications", "complaint": "болит спина", "age": 36, "language": "ru", "language_locked": True})
    result = answer("pf_no_contra", "Противопоказаний нет!")
    session = state.get_session("pf_no_contra")
    assert session["contraindications_ok"] is True
    assert session["step"] == "date"
    assert "На какой день Вам удобно прийти" in result

    reset("pf_no_contra_with_course_days", {"step": "contraindications", "complaint": "болит спина", "age": 36, "language": "ru", "language_locked": True})
    result = answer("pf_no_contra_with_course_days", "Противопоказаний нет, но сколько дней лечение обычно длится?")
    session = state.get_session("pf_no_contra_with_course_days")
    assert "Количество дней и процедур врач сможет определить" in result
    assert "На какой день Вам удобно прийти" in result
    assert "Противопоказаний нет?" not in result
    assert session["contraindications_ok"] is True
    assert session["contraindications_raw"] == "Противопоказаний нет, но сколько дней лечение обычно длится?"
    assert session["contraindications_verdict"] == "proceed"
    assert session["step"] == "date"

    reset("pf_tomorrow_after_no_contra", {"step": "date", "complaint": "болит спина", "age": 36, "contraindications_ok": True, "language": "ru", "language_locked": True})
    result = answer("pf_tomorrow_after_no_contra", "Завтра")
    assert "противопоказ" not in result.lower()
    assert calls["slots"]

    reset("pf_saturday", {"step": "date", "complaint": "болит спина", "age": 36, "contraindications_ok": True, "language": "ru", "language_locked": True})
    result = answer("pf_saturday", "в субботу")
    session = state.get_session("pf_saturday")
    assert "процедурные дни" in result
    assert session["step"] == "date"

    reset("pf_sunday", {"step": "date", "complaint": "болит спина", "age": 36, "contraindications_ok": True, "language": "ru", "language_locked": True})
    result = answer("pf_sunday", "в воскресенье")
    session = state.get_session("pf_sunday")
    assert "процедурные дни" in result
    assert session["step"] == "date"

    for chat_id, preset, text in [
        ("pf_manual_like", {"manual_admin_intervention": True}, "👍"),
        ("pf_manual_good", {"manual_admin_intervention": True, "step": "date"}, "хорошо"),
        ("pf_manual_no_contra", {"manual_admin_intervention": True, "step": "contraindications"}, "ничего нет"),
    ]:
        reset(chat_id, {**preset, "language": "ru", "language_locked": True})
        assert answer(chat_id, text) == ""


def test_non_surgical_question_without_documents_asks_complaint() -> None:
    reset("non_surgical_plain")
    result = answer("non_surgical_plain", "Можно лечить без операции?")
    session = state.get_session("non_surgical_plain")

    assert "безоперационные методы" in result
    assert "что Вас беспокоит" in result
    assert session["step"] == "complaint"
    assert "по фото/документу" not in result.lower()
    assert not session.get("escalated")


def test_non_surgical_question_with_profile_complaint_asks_age() -> None:
    reset("non_surgical_profile")
    result = answer("non_surgical_profile", "Спина болит, можно без операции?")
    session = state.get_session("non_surgical_profile")

    assert "безоперационные методы" in result
    assert "спин" in result.lower()
    assert "сколько Вам лет" in result
    assert session["step"] == "age"


def test_non_surgical_question_by_mri_handoffs_to_doctor() -> None:
    reset("non_surgical_mri")
    result = answer("non_surgical_mri", "По МРТ можно понять, можно без операции?")
    session = state.get_session("non_surgical_mri")

    assert "по фото/снимку или документам" in result.lower() or "по снимку" in result.lower()
    assert "врач" in result.lower()
    assert session.get("escalated") is True or session.get("handoff_to_doctor") is True
    assert "сколько Вам лет" not in result


def test_non_surgical_kazakh_question_asks_complaint_in_kazakh() -> None:
    reset("non_surgical_kz")
    result = answer("non_surgical_kz", "Операциясыз емдейсіздер ме?")
    session = state.get_session("non_surgical_kz")

    assert "Иә" in result
    assert "операциясыз" in result
    assert session["step"] == "complaint"


def test_old_bot_tool_gates_and_operator_templates(monkeypatch: Any) -> None:
    calls = setup_crm(monkeypatch)

    reset("oldtpl_address")
    result = answer("oldtpl_address", "Как доехать, адрес и 2GIS?")
    session = state.get_session("oldtpl_address")
    assert "Кабанбай батыра 28" in result
    assert "2gis.kz" in result
    assert session["last_clinic_info_topic"] == "address"
    assert any(item.get("name") == "get_clinic_info" and item.get("topic") == "address" for item in session["tool_history"])

    reset("oldtpl_returning")
    result = answer("oldtpl_returning", "Я уже была у вас раньше")
    session = state.get_session("oldtpl_returning")
    assert "когда Вы у нас были" in result
    assert session["step"] == "escalated"
    assert any(item.get("name") == "escalate_to_human" for item in session["tool_history"])

    reset(
        "gate_no_complaint",
        {
            "step": "name",
            "language": "ru",
            "language_locked": True,
            "age": 36,
            "contraindications_ok": True,
            "contraindications_verdict": "proceed",
            "selected_slot": {"doctor_login": "doctor1", "doctor_name": "Тестовый врач", "date": "2099-01-01", "time": "18:00"},
        },
    )
    result = answer("gate_no_complaint", "Виктор")
    session = state.get_session("gate_no_complaint")
    assert "что именно Вас беспокоит" in result
    assert session["step"] == "complaint"
    assert calls["book"] == []

    reset(
        "gate_passed",
        {
            "step": "name",
            "language": "ru",
            "language_locked": True,
            "complaint": "болит спина",
            "complaint_gate": "COMPLAINT_OK",
            "age": 36,
            "contraindications_ok": True,
            "contraindications_verdict": "proceed",
            "selected_slot": {"doctor_login": "doctor1", "doctor_name": "Тестовый врач", "date": "2099-01-01", "time": "18:00"},
        },
    )
    result = answer("gate_passed", "Виктор")
    session = state.get_session("gate_passed")
    assert result
    assert session["status"] == "booked"
    assert len(calls["book"]) == 1


def test_combined_faq_and_slot_selection_asks_name() -> None:
    slots = [
        {"doctor_login": "doctor1", "doctor_name": "Тестовый врач", "date": "2099-01-01", "time": "09:20"},
        {"doctor_login": "doctor1", "doctor_name": "Тестовый врач", "date": "2099-01-01", "time": "10:00"},
    ]

    reset(
        "combined_address_slot",
        {
            "step": "time",
            "language": "ru",
            "language_locked": True,
            "complaint": "болит спина",
            "age": 36,
            "contraindications_ok": True,
            "last_slots": slots,
        },
    )
    result = answer("combined_address_slot", "А адрес какой? И давайте 2 вариант")
    session = state.get_session("combined_address_slot")
    assert "Кабанбай" in result or "адрес" in result.lower()
    assert "имя" in result.lower()
    assert session["step"] == "name"
    assert session["selected_slot"] == slots[1]

    reset(
        "combined_price_slot",
        {
            "step": "time",
            "language": "ru",
            "language_locked": True,
            "complaint": "болит спина",
            "age": 36,
            "contraindications_ok": True,
            "last_slots": slots,
        },
    )
    result = answer("combined_price_slot", "А сколько стоит? Давайте первый")
    session = state.get_session("combined_price_slot")
    assert "5 000" in result or "стоим" in result.lower()
    assert "имя" in result.lower()
    assert session["step"] == "name"
    assert session["selected_slot"] == slots[0]


def test_combined_faq_slot_selection_then_name_books_crm(monkeypatch: Any) -> None:
    calls = setup_crm(monkeypatch)
    slots = [
        {
            "doctor_login": "doctor1",
            "doctorName": "Первый врач",
            "date": "2099-01-01",
            "time": "09:20",
            "timeStart": "09:20",
        },
        {
            "doctor_login": "doctor2",
            "doctorName": "Второй врач",
            "date": "2099-01-01",
            "time": "10:00",
            "timeStart": "10:00",
        },
    ]

    reset(
        "combined_address_slot_then_book",
        {
            "step": "time",
            "language": "ru",
            "language_locked": True,
            "complaint": "болит спина",
            "age": 36,
            "contraindications_ok": True,
            "contraindications_verdict": "proceed",
            "last_slots": slots,
        },
    )

    first_result = answer("combined_address_slot_then_book", "А адрес какой? И давайте 2 вариант")
    session = state.get_session("combined_address_slot_then_book")
    assert "Кабанбай" in first_result or "адрес" in first_result.lower()
    assert session["step"] == "name"
    assert session["selected_slot"] == slots[1]
    assert session["selected_doctor_login"] == "doctor2"
    assert session["selected_date"] == "2099-01-01"
    assert session["selected_time"] == "10:00"
    assert bot_tools.booking_gate_status(session) == (True, "ok")

    second_result = answer("combined_address_slot_then_book", "Алия")
    session = state.get_session("combined_address_slot_then_book")

    assert len(calls["book"]) == 1
    assert calls["book"][0]["doctor_login"] == "doctor2"
    assert calls["book"][0]["doctor_name"] == "Второй врач"
    assert calls["book"][0]["date"] == "2099-01-01"
    assert calls["book"][0]["time_start"] == "10:00"
    assert "запись подтверждена" in second_result.lower()
    assert "передам администратору" not in second_result.lower()
    assert session["status"] == "booked"


def test_video_question_with_date_answers_then_shows_slots(monkeypatch: Any) -> None:
    setup_crm(monkeypatch)
    reset(
        "video_question_date",
        {
            "step": "date",
            "language": "ru",
            "language_locked": True,
            "complaint": "болит спина",
            "age": 36,
            "contraindications_ok": True,
            "contraindications_verdict": "proceed",
        },
    )

    result = answer("video_question_date", "Завтра. Так же будет как на видео?")
    session = state.get_session("video_question_date")

    assert "как показано в видео" in result
    assert "свободные окошки" in result
    assert session["step"] == "time"


def test_video_question_after_slots_returns_to_time_choice() -> None:
    slots = [
        {"doctor_login": "doctor1", "doctor_name": "Тестовый врач", "date": "2099-01-01", "time": "09:20"},
        {"doctor_login": "doctor1", "doctor_name": "Тестовый врач", "date": "2099-01-01", "time": "10:00"},
    ]
    selected_slot = {"doctor_login": "doctor0", "doctor_name": "Другой врач", "date": "2099-01-02", "time": "11:00"}
    reset(
        "video_question_time",
        {
            "step": "time",
            "language": "ru",
            "language_locked": True,
            "complaint": "болит спина",
            "age": 36,
            "contraindications_ok": True,
            "contraindications_verdict": "proceed",
            "last_slots": slots,
            "selected_slot": selected_slot.copy(),
        },
    )

    result = answer("video_question_time", "Так же будет да?")
    session = state.get_session("video_question_time")

    assert "точный план врач подбирает после осмотра" in result
    assert "Какое время" in result or "вариант" in result
    assert session["step"] == "time"
    assert session["selected_slot"] == selected_slot


def test_video_question_does_not_guarantee_exact_match() -> None:
    reset(
        "video_question_no_guarantee",
        {
            "step": "time",
            "language": "ru",
            "language_locked": True,
            "complaint": "болит спина",
            "age": 36,
            "contraindications_ok": True,
            "contraindications_verdict": "proceed",
            "last_slots": [
                {"doctor_login": "doctor1", "doctor_name": "Тестовый врач", "date": "2099-01-01", "time": "09:20"},
            ],
        },
    )

    result = answer("video_question_no_guarantee", "это точно как на видео будет?")

    assert "гарантируем" not in result.lower()
    assert "примерно" in result or "точный план" in result


def test_static_dialog_template_wiring_and_tr_arity() -> None:
    source = (PROJECT_ROOT / "dialog.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    tr_calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_tr"]
    assert tr_calls
    assert all(len(node.args) == 3 for node in tr_calls)
    assert "return _tr(\n    return _clinic_info_template" not in source
    assert "return _tr(lang" not in source
    assert (
        'if step in ("start", "", None):\n        if step in ("start", "", None) and not session.get("escalated")'
        not in source
    )

    session: dict[str, Any] = {"language": "ru"}
    address = dialog._address_answer(session)
    schedule = dialog._schedule_answer(session)
    mri = dialog._mri_answer_in_flow(session)

    assert "2gis.kz" in address
    assert "График приёма" in schedule
    assert "Снимок заранее делать не обязательно" in mri

    escalated_session: dict[str, Any] = {"language": "ru"}
    returning = dialog._clinic_answer("Я уже была у вас раньше", escalated_session)
    assert returning and "когда Вы у нас были" in returning
    assert escalated_session["step"] == "escalated"
