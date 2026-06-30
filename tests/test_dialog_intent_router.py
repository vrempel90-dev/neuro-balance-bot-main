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
    session = state.get_session(chat_id)
    # This module exercises the legacy router after a lead has already entered
    # the AI funnel. New/old-lead admission is covered separately by
    # test_new_lead_safety_gate.py.
    session["ai_lead_started"] = True
    if preset:
        session.update(preset)
    state.save_session(chat_id, session)


def answer(chat_id: str, text: str) -> str:
    return run(handle_message(chat_id, "77011234567", text))


def add_history(chat_id: str, messages: list[tuple[str, str]]) -> None:
    for role, content in messages:
        state.add_message(chat_id, role, content)


def test_thanks_ok_guard_does_not_match_substrings() -> None:
    assert dialog._is_thanks_or_ok("ок") is True
    assert dialog._is_thanks_or_ok("спасибо") is True
    assert dialog._is_thanks_or_ok("Поясничная область начала беспокоить") is False
    assert dialog._is_thanks_or_ok("спина беспокоит") is False


def test_history_mri_yes_continues_booking_without_restart(monkeypatch: Any) -> None:
    setup_crm(monkeypatch)
    chat_id = "ctx_mri_yes"
    reset(chat_id, {"language": "kk", "language_locked": True})
    add_history(
        chat_id,
        [
            ("user", "ОСМС-пен емделуге болама?"),
            ("admin", "Сәлеметсіз бе! Біз ОСМС жүйесі бойынша жұмыс істемейміз. Сізді қандай мәселе мазалайды?"),
            ("user", "Белім мазалайды"),
            ("admin", "Бұрын МРТ түсіріліміңіз өткен бе еді?"),
        ],
    )

    result = answer(chat_id, "ия")
    session = state.get_session(chat_id)

    assert "қалай көмектесе" not in result.lower()
    assert "чем можем помочь" not in result.lower()
    assert session["step"] == "age"
    assert session["complaint"] == "Белім мазалайды"
    assert session["used_history_context"] is True
    assert session["last_bot_question_type"] == "mri"


def test_history_age_answer_moves_to_contraindications(monkeypatch: Any) -> None:
    setup_crm(monkeypatch)
    chat_id = "ctx_age_34"
    reset(chat_id, {"language": "ru", "language_locked": True, "complaint": "Спина болит"})
    add_history(chat_id, [("user", "Спина болит"), ("assistant", "Подскажите, сколько Вам лет?")])

    result = answer(chat_id, "34")
    session = state.get_session(chat_id)

    assert session["step"] == "contraindications"
    assert session["questionnaire_step"] == "contra"
    assert "Есть ли у Вас какие-нибудь противопоказания?" in result


def test_history_contra_clear_answer_moves_to_date(monkeypatch: Any) -> None:
    setup_crm(monkeypatch)
    chat_id = "ctx_contra_clear"
    reset(chat_id, {"language": "ru", "language_locked": True, "complaint": "болит спина", "age": 34})
    add_history(chat_id, [("assistant", dialog._ask_contra({"language": "ru"}))])

    result = answer(chat_id, "Все чисто")
    session = state.get_session(chat_id)

    assert session["contraindications_ok"] is True
    assert session["step"] == "date"
    assert "На какой день" in result


def test_history_slot_choice_selects_second_slot(monkeypatch: Any) -> None:
    setup_crm(monkeypatch)
    chat_id = "ctx_slot_second"
    slots = [
        {"doctor_login": "doctor1", "doctorName": "Первый врач", "date": "2099-01-01", "time": "09:20", "timeStart": "09:20"},
        {"doctor_login": "doctor2", "doctorName": "Второй врач", "date": "2099-01-01", "time": "10:40", "timeStart": "10:40"},
    ]
    reset(
        chat_id,
        {
            "language": "ru",
            "language_locked": True,
            "complaint": "болит спина",
            "age": 34,
            "contraindications_ok": True,
            "contraindications_verdict": "proceed",
            "last_slots": slots,
        },
    )
    add_history(chat_id, [("assistant", "Есть варианты:\n1) 09:20\n2) 10:40\nКакое время из вариантов выше Вам удобно?")])

    result = answer(chat_id, "2 вариант")
    session = state.get_session(chat_id)

    assert session["step"] == "name"
    assert session["selected_slot"] == slots[1]
    assert "имя" in result.lower()


def test_manual_admin_thanks_stays_silent_with_reason(monkeypatch: Any) -> None:
    setup_crm(monkeypatch)
    chat_id = "ctx_manual_thanks"
    reset(chat_id, {"manual_admin_intervention": True, "manual_takeover": True, "ai_muted": True})
    add_history(chat_id, [("admin", "Хорошо, ожидаем Вас завтра.")])

    result = answer(chat_id, "рахмет")
    session = state.get_session(chat_id)

    assert result == ""
    assert session["no_reply_reason"] == "manual_takeover"



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
    assert session["step"] == "booked"
    assert session["appointment_status"] == "booked"
    assert session["ai_muted"] is True
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
    assert result == "Перед записью уточню для безопасности 🌿 Есть ли у Вас какие-нибудь противопоказания?"
    assert "кардиостим" not in result.lower()
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
    assert result == ""
    assert state.get_session("rc_done_lookup")["no_reply_reason"] == "booked_session_ai_disabled"

    reset("rc_done_address", {"step": "done", "booked": True, "language": "ru", "language_locked": True})
    result = answer("rc_done_address", "Куда обращаться?")
    assert result == ""

    reset("rc_done_advice", {"step": "done", "booked": True, "language": "ru", "language_locked": True})
    result = answer("rc_done_advice", "Посоветуйте")
    assert result == ""

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
    assert result == "Перед записью уточню для безопасности 🌿 Есть ли у Вас какие-нибудь противопоказания?"
    assert session["age"] == 46
    assert session["step"] == "contraindications"

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
    assert "записала" in second_result.lower()
    assert "передам администратору" not in second_result.lower()
    assert session["status"] == "booked"



def test_combined_video_faq_and_second_slot_selection_asks_name() -> None:
    slots = [
        {"doctor_login": "doctor1", "doctor_name": "Тестовый врач", "date": "2099-01-01", "time": "09:20"},
        {"doctor_login": "doctor1", "doctor_name": "Тестовый врач", "date": "2099-01-01", "time": "10:00"},
    ]
    reset(
        "combined_video_second_slot",
        {
            "step": "time",
            "language": "ru",
            "language_locked": True,
            "last_slots": slots,
        },
    )

    result = answer("combined_video_second_slot", "А это будет как на видео в инстаграме? Если да, давайте 2 вариант")
    session = state.get_session("combined_video_second_slot")

    assert "видео" in result.lower()
    assert "имя" in result.lower()
    assert "Когда вам будет удобно прийти" not in result
    assert "На какой день Вам удобно прийти" not in result
    assert session["step"] == "name"
    assert session["questionnaire_step"] == "name"
    assert session["selected_slot"] == slots[1]


def test_combined_video_faq_and_first_slot_selection_asks_name() -> None:
    slots = [
        {"doctor_login": "doctor1", "doctor_name": "Тестовый врач", "date": "2099-01-01", "time": "09:20"},
        {"doctor_login": "doctor1", "doctor_name": "Тестовый врач", "date": "2099-01-01", "time": "10:00"},
    ]
    reset(
        "combined_video_first_slot",
        {
            "step": "time",
            "language": "ru",
            "language_locked": True,
            "last_slots": slots,
        },
    )

    result = answer("combined_video_first_slot", "Так же будет да? Давайте первый")
    session = state.get_session("combined_video_first_slot")

    assert "видео" in result.lower() or "процедуры" in result.lower()
    assert "имя" in result.lower()
    assert session["selected_slot"] == slots[0]
    assert session["step"] == "name"


def test_video_faq_without_slot_keeps_time_step_and_asks_slot() -> None:
    slots = [
        {"doctor_login": "doctor1", "doctor_name": "Тестовый врач", "date": "2099-01-01", "time": "09:20"},
        {"doctor_login": "doctor1", "doctor_name": "Тестовый врач", "date": "2099-01-01", "time": "10:00"},
    ]
    reset(
        "video_without_slot_keeps_time",
        {
            "step": "time",
            "language": "ru",
            "language_locked": True,
            "last_slots": slots,
        },
    )

    result = answer("video_without_slot_keeps_time", "как на видео?")
    session = state.get_session("video_without_slot_keeps_time")

    assert "видео" in result.lower()
    assert session["step"] == "time"
    assert "Какое время" in result or "вариант" in result
    assert "selected_slot" not in session

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


def _crm_response_error(status: int, data: dict[str, Any]) -> crm.CRMResponseError:
    import httpx

    response = httpx.Response(status, json=data, request=httpx.Request("POST", "https://crm.test/api/bot/book"))
    return crm.CRMResponseError("book", response, data)


def test_crm_book_new_contract_success_and_payload(monkeypatch: Any) -> None:
    calls = setup_crm(monkeypatch)
    chat_id = "crm_new_contract_success"
    reset(chat_id, {
        "step": "name",
        "language": "ru",
        "language_locked": True,
        "complaint": "спина",
        "age": 31,
        "contraindications_ok": True,
        "selected_slot": {"doctorLogin": "zhuma_md", "doctorName": "Жумабеков М.", "date": "2026-06-22", "timeStart": "10:40"},
    })

    result = answer(chat_id, "Алия")
    session = state.get_session(chat_id)

    assert "записала" in result.lower()
    assert session["step"] == "booked"
    assert session["ai_muted"] is True
    assert session["appointment"]["appointmentId"] == 999
    assert calls["book"] == [{
        "patient_name": "Алия",
        "phone": "77011234567",
        "doctor_login": "zhuma_md",
        "doctor_name": "Жумабеков М.",
        "date": "2026-06-22",
        "time_start": "10:40",
        "notes": calls["book"][0]["notes"],
    }]
    assert "doctor_id" not in calls["book"][0]
    assert "service_id" not in calls["book"][0]


def test_format_slots_maps_availability_to_booking_contract_object() -> None:
    slots = dialog._format_slots({"availability": [{"doctorLogin": "zhuma_md", "doctorName": "Жумабеков М.", "date": "2026-06-22", "availableSlots": ["09:00"]}]})
    assert slots[0]["doctorLogin"] == "zhuma_md"
    assert slots[0]["doctorName"] == "Жумабеков М."
    assert slots[0]["date"] == "2026-06-22"
    assert slots[0]["timeStart"] == "09:00"


def test_crm_book_409_slot_conflict_refreshes_slots(monkeypatch: Any) -> None:
    calls = {"book": [], "slots": []}

    async def fake_book_appointment(**kwargs: Any) -> dict[str, Any]:
        calls["book"].append(kwargs)
        raise _crm_response_error(409, {"error": "Этот слот уже занят", "code": "slot_conflict", "conflicts": []})

    async def fake_check_slots(date: str, doctor_login: str | None = None) -> dict[str, Any]:
        calls["slots"].append({"date": date, "doctor_login": doctor_login})
        return {"availability": [{"doctorLogin": "zhuma_md", "doctorName": "Жумабеков М.", "date": date, "availableSlots": ["11:20", "12:00"]}]}

    monkeypatch.setattr(crm, "book_appointment", fake_book_appointment)
    monkeypatch.setattr(crm, "check_slots", fake_check_slots)
    chat_id = "crm_conflict_refresh"
    reset(chat_id, {"step": "name", "language": "ru", "language_locked": True, "complaint": "спина", "age": 31, "contraindications_ok": True, "selected_slot": {"doctorLogin": "zhuma_md", "doctorName": "Жумабеков М.", "date": "2026-06-22", "timeStart": "10:40"}})

    result = answer(chat_id, "Алия")
    session = state.get_session(chat_id)

    assert "администратору" in result.lower()
    assert calls["slots"] == []
    assert session["step"] == "escalated"
    assert session["selected_slot"]["timeStart"] == "10:40"


def test_crm_book_409_doctor_not_scheduled_refreshes_slots(monkeypatch: Any) -> None:
    async def fake_book_appointment(**kwargs: Any) -> dict[str, Any]:
        raise _crm_response_error(409, {"error": "Врач вне расписания", "code": "doctor_not_scheduled"})

    async def fake_check_slots(date: str, doctor_login: str | None = None) -> dict[str, Any]:
        return {"availability": [{"doctorLogin": "other", "doctorName": "Другой врач", "date": date, "availableSlots": ["14:00"]}]}

    monkeypatch.setattr(crm, "book_appointment", fake_book_appointment)
    monkeypatch.setattr(crm, "check_slots", fake_check_slots)
    chat_id = "crm_doctor_not_scheduled"
    reset(chat_id, {"step": "name", "language": "ru", "language_locked": True, "complaint": "спина", "age": 31, "contraindications_ok": True, "selected_slot": {"doctorLogin": "zhuma_md", "doctorName": "Жумабеков М.", "date": "2026-06-22", "timeStart": "10:40"}})

    result = answer(chat_id, "Алия")
    session = state.get_session(chat_id)

    assert "администратору" in result.lower()
    assert session["step"] == "escalated"
    assert session["selected_slot"]["timeStart"] == "10:40"


def test_crm_book_500_logs_body_and_escalates(monkeypatch: Any) -> None:
    events: list[tuple[str, str, dict[str, Any]]] = []

    async def fake_book_appointment(**kwargs: Any) -> dict[str, Any]:
        raise _crm_response_error(500, {"error": "boom"})

    monkeypatch.setattr(crm, "book_appointment", fake_book_appointment)
    monkeypatch.setattr(state, "log_event", lambda chat_id, event, payload: events.append((chat_id, event, payload)))
    chat_id = "crm_500_escalates"
    reset(chat_id, {"step": "name", "language": "ru", "language_locked": True, "complaint": "спина", "age": 31, "contraindications_ok": True, "selected_slot": {"doctorLogin": "zhuma_md", "doctorName": "Жумабеков М.", "date": "2026-06-22", "timeStart": "10:40"}})

    result = answer(chat_id, "Алия")
    session = state.get_session(chat_id)

    assert "администратору" in result.lower()
    assert session["step"] == "escalated"
    assert session.get("escalated") is True
    assert any(item.get("name") == "escalate_to_human" for item in session.get("tool_history", []))
    assert events[-1][1] == "crm_booking_failed"
    assert events[-1][2]["status_code"] == 500
    assert "boom" in events[-1][2]["response_text"]


def test_crm_book_http_payload_uses_new_contract(monkeypatch: Any) -> None:
    import httpx

    captured: dict[str, Any] = {}

    class FakeClient:
        async def post(self, url: str, **kwargs: Any) -> httpx.Response:
            captured["url"] = url
            captured.update(kwargs)
            return httpx.Response(201, json={"appointmentId": 789, "doctorName": "Жумабеков М.", "date": "2026-06-22", "timeStart": "10:40", "timeEnd": "11:20"}, request=httpx.Request("POST", url))

    monkeypatch.setattr(crm, "_client", lambda: FakeClient())

    booked = run(crm.book_appointment(
        patient_name="Айгерим",
        phone="+77021234567",
        doctor_login="zhuma_md",
        doctor_name="Жумабеков М.",
        date="2026-06-22",
        time_start="10:40",
        notes="комментарий",
    ))

    assert booked["appointmentId"] == 789
    assert captured["url"].endswith("/api/bot/book")
    assert captured["json"] == {
        "patientName": "Айгерим",
        "phone": "77021234567",
        "doctorLogin": "zhuma_md",
        "date": "2026-06-22",
        "timeStart": "10:40",
        "doctorName": "Жумабеков М.",
        "notes": "комментарий",
    }
    assert "doctorId" not in captured["json"]
    assert "serviceId" not in captured["json"]


def test_contraindications_hemorrhoids_unknown_not_hard_stop(monkeypatch: Any) -> None:
    calls = setup_crm(monkeypatch)
    events: list[str] = []
    monkeypatch.setattr(state, "log_event", lambda chat_id, event, payload: events.append(event))
    chat_id = "contra_hemorrhoids_unknown"
    reset(chat_id, {"step": "contraindications", "language": "ru", "language_locked": True})

    result = answer(chat_id, "Геморрой")
    session = state.get_session(chat_id)
    low = result.lower()

    assert "геморрой является противопоказанием" not in low
    assert "процесс записи останавливаю" not in low
    assert result == "Перед записью уточню для безопасности 🌿 Есть ли у Вас какие-нибудь противопоказания?"
    assert session.get("step") == "contraindications"
    assert session.get("contraindications_ok") is False
    assert session.get("contraindications_verdict") == "admin_contact"
    assert calls["book"] == []
    assert "llm_unknown_contraindication_blocked" in events


def test_contraindications_pressure_unknown_not_hard_stop(monkeypatch: Any) -> None:
    calls = setup_crm(monkeypatch)
    chat_id = "contra_pressure_unknown"
    reset(chat_id, {"step": "contraindications", "language": "ru", "language_locked": True})

    result = answer(chat_id, "давление")
    session = state.get_session(chat_id)

    assert "является противопоказанием" not in result.lower()
    assert session.get("step") == "contraindications"
    assert session.get("contraindications_ok") is False
    assert calls["book"] == []


def test_contraindications_gastritis_unknown_not_hard_stop(monkeypatch: Any) -> None:
    calls = setup_crm(monkeypatch)
    chat_id = "contra_gastritis_unknown"
    reset(chat_id, {"step": "contraindications", "language": "ru", "language_locked": True})

    result = answer(chat_id, "гастрит")
    session = state.get_session(chat_id)

    assert "является противопоказанием" not in result.lower()
    assert session.get("step") == "contraindications"
    assert session.get("contraindications_ok") is False
    assert calls["book"] == []


def test_contraindications_cochlear_implant_hard_stop_allowed(monkeypatch: Any) -> None:
    setup_crm(monkeypatch)
    chat_id = "contra_cochlear_implant"
    reset(chat_id, {"step": "contraindications", "language": "ru", "language_locked": True})

    result = answer(chat_id, "у меня есть кохлеарный имплант")
    session = state.get_session(chat_id)

    assert session.get("step") == "stopped"
    assert session.get("contraindications_verdict") in {"refuse", "stop"}
    assert "процесс записи останавливаю" in result.lower()


def test_contraindications_thrombosis_hard_stop_allowed(monkeypatch: Any) -> None:
    setup_crm(monkeypatch)
    chat_id = "contra_thrombosis"
    reset(chat_id, {"step": "contraindications", "language": "ru", "language_locked": True})

    result = answer(chat_id, "у меня тромбоз")
    session = state.get_session(chat_id)

    assert session.get("step") == "stopped"
    assert session.get("contraindications_verdict") in {"refuse", "stop"}
    assert "процесс записи останавливаю" in result.lower()


def test_contraindications_term_question_not_hard_stop(monkeypatch: Any) -> None:
    calls = setup_crm(monkeypatch)
    chat_id = "contra_term_question_cochlear"
    reset(chat_id, {"step": "contraindications", "language": "ru", "language_locked": True})

    result = answer(chat_id, "что такое кохлеарный имплант?")
    session = state.get_session(chat_id)

    assert result == "Перед записью уточню для безопасности 🌿 Есть ли у Вас какие-нибудь противопоказания?"
    assert session.get("step") == "contraindications"
    assert session.get("contraindications_verdict") != "stop"
    assert calls["book"] == []


def test_openai_decision_validator_blocks_invented_contraindication() -> None:
    decision = {
        "action": "stop_contraindication",
        "next_step": "escalated",
        "reply": "К сожалению, геморрой является противопоказанием. Процесс записи останавливаю.",
        "extracted": {"contraindication_confirmed": True, "contraindication_red_flags": []},
        "safety": {"hard_stop": True, "unsafe_medical_claim": False, "tries_to_book_without_rules": False},
        "needs_python_tool": "handoff_admin",
    }

    ok, reason = dialog.validate_openai_dialog_decision(
        decision,
        {"step": "contraindications", "ai_lead_started": True},
        "Геморрой",
    )

    assert ok is False
    assert reason == "llm_unknown_contraindication_blocked"



def test_short_contraindications_question_after_age() -> None:
    chat_id = "short_contra_after_age"
    reset(chat_id, {"step": "age", "complaint": "болит спина", "language": "ru", "language_locked": True})

    result = answer(chat_id, "36 лет")
    session = state.get_session(chat_id)

    assert "Есть ли у Вас какие-нибудь противопоказания?" in result
    assert "кардиостимулятора/дефибриллятора" not in result
    assert "инсулиновой помпы" not in result
    assert session["step"] == "contraindications"


def test_no_contraindications_answer_advances_to_date() -> None:
    chat_id = "short_contra_no_advances"
    reset(chat_id, {"step": "contraindications", "complaint": "болит спина", "age": 36, "language": "ru", "language_locked": True})

    result = answer(chat_id, "нет")
    session = state.get_session(chat_id)

    assert session["contraindications_ok"] is True
    assert session["step"] == "date"
    assert result == "Отлично 🌿 На какой день Вам удобно прийти?"

def test_hotfix_contraindications_clear_phrase_advances() -> None:
    chat_id = "hotfix_contra_clear_phrase"
    reset(chat_id, {"step": "contraindications", "complaint": "болит спина", "age": 36, "language": "ru", "language_locked": True})

    result = answer(chat_id, "То что перечислено, этого нет")
    session = state.get_session(chat_id)

    assert session["contraindications_ok"] is True
    assert session["contraindications_raw"] == "То что перечислено, этого нет"
    assert session["step"] != "contraindications"
    assert "Противопоказаний нет?" not in result


def test_hotfix_repeated_contraindications_checklist_is_short() -> None:
    chat_id = "hotfix_contra_repeated"
    reset(chat_id, {
        "step": "contraindications",
        "complaint": "болит спина",
        "age": 36,
        "language": "ru",
        "language_locked": True,
        "contraindications_checklist_sent_count": 1,
    })

    result = answer(chat_id, "где написано о противопоказаниях?")

    assert result == "Перед записью уточню для безопасности 🌿 Есть ли у Вас какие-нибудь противопоказания?"
    assert "кардиостимулятора/дефибриллятора" not in result
    assert "инсулиновой помпы" not in result


def test_hotfix_user_irritated_mutes_followups() -> None:
    chat_id = "hotfix_irritated"
    reset(chat_id, {"step": "date", "complaint": "болит спина", "age": 36, "contraindications_ok": True, "language": "ru", "language_locked": True})

    first = answer(chat_id, "Я уже от вас ничего не хочу")
    session = state.get_session(chat_id)
    second = answer(chat_id, "вы тут?")

    assert "больше не буду беспокоить" in first
    assert session["ai_muted"] is True
    assert session["manual_takeover"] is True
    assert session["escalated"] is True
    assert second == ""
    assert state.get_session(chat_id)["no_reply_reason"] == "manual_takeover"


def test_hotfix_instagram_detail_request_does_not_start_booking() -> None:
    chat_id = "hotfix_instagram_detail"
    reset(chat_id)

    result = answer(chat_id, "Привет! Можно узнать об этом подробнее? https://instagram.com/p/test")
    session = state.get_session(chat_id)

    assert "что именно заинтересовало" in result
    assert "боль, процедура или запись" in result
    assert session["step"] == "complaint"
    assert "сколько Вам лет" not in result


def test_final_name_with_selected_slot_books_crm_without_admin_fallback(monkeypatch: Any) -> None:
    calls = setup_crm(monkeypatch)
    chat_id = "final_name_booking_ready"
    reset(chat_id, {
        "step": "name",
        "questionnaire_step": "name",
        "complaint": "поясница болит",
        "complaint_gate": bot_tools.COMPLAINT_OK,
        "age": 34,
        "contraindications_ok": True,
        "contraindications_verdict": bot_tools.CONTRA_PROCEED,
        "selected_date": "2026-07-06",
        "selected_time": "14:00",
        "selected_doctor_login": "zhuma_md",
        "selected_doctor_name": "Жумабек Мади Мухтарович",
        "phone": "77000008881",
    })

    result = answer(chat_id, "Виктор")
    session = state.get_session(chat_id)

    assert session["patient_name"] == "Виктор"
    assert len(calls["book"]) == 1
    assert calls["book"][0]["patient_name"] == "Виктор"
    assert calls["book"][0]["date"] == "2026-07-06"
    assert calls["book"][0]["time_start"] == "14:00"
    assert calls["book"][0]["doctor_login"] == "zhuma_md"
    assert session["step"] == "booked"
    assert session["ai_muted"] is True
    assert session["booking_confirmed"] is True
    assert session.get("escalated") is not True
    assert "Виктор" in result
    assert "записала" in result
    assert "6 июля" in result
    assert "14:00" in result
    assert "Жумабек Мади Мухтарович" in result
    assert "Кабанбай батыра 28" in result
    low = result.lower()
    assert "передам администратору" not in low
    assert "уточню" not in low
    assert "назовите имя" not in low


def test_regression_contra_clear_long_phrase_never_reasks(monkeypatch: Any) -> None:
    setup_crm(monkeypatch)
    reset("reg_contra_phrase", {"step": "contraindications", "age": 34, "language": "ru", "language_locked": True})

    result = answer("reg_contra_phrase", "Из того что вы перечислили, ничего такого нет")
    session = state.get_session("reg_contra_phrase")

    assert session["contraindications_ok"] is True
    assert session["step"] != "contraindications"
    assert "противопоказ" not in result.lower()


def test_regression_contra_ok_price_faq_resumes_date(monkeypatch: Any) -> None:
    setup_crm(monkeypatch)
    reset("reg_contra_ok_price", {
        "step": "date",
        "complaint": "болит спина",
        "age": 36,
        "contraindications_ok": True,
        "contraindications_verdict": "proceed",
        "last_required_step": "contraindications",
        "pending_step_after_faq": "contraindications",
        "language": "ru",
        "language_locked": True,
    })

    result = answer("reg_contra_ok_price", "А сколько стоит?")
    session = state.get_session("reg_contra_ok_price")

    assert "5 000" in result or "5000" in result
    assert "противопоказ" not in result.lower()
    assert session["step"] == "date"
    assert session.get("last_required_step") != "contraindications"
    assert session.get("pending_step_after_faq") != "contraindications"


def test_regression_contra_ok_blocks_brain_next_contra(monkeypatch: Any) -> None:
    setup_crm(monkeypatch)

    async def fake_brain(**kwargs: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        return {
            "intent": "faq",
            "reply": "Подскажите, противопоказаний нет?",
            "action": "ask_contraindications",
            "next_step": "contraindications",
            "extracted": {},
            "needs_python_tool": "none",
        }, {"openai_brain_used": True}

    monkeypatch.setattr(dialog, "run_openai_dialog_brain", fake_brain)
    reset("reg_brain_contra", {
        "step": "date",
        "complaint": "спина",
        "age": 34,
        "contraindications_ok": True,
        "contraindications_verdict": "proceed",
        "language": "ru",
        "language_locked": True,
    })

    result = answer("reg_brain_contra", "хочу записаться")
    session = state.get_session("reg_brain_contra")

    assert session["repair_reason"] == "contraindications_already_ok"
    assert session["step"] == "date"
    assert "На какой день" in result
    assert "противопоказ" not in result.lower()


def test_regression_contra_ok_checklist_count_no_long_repeat(monkeypatch: Any) -> None:
    setup_crm(monkeypatch)
    reset("reg_count", {
        "step": "date",
        "complaint": "спина",
        "age": 34,
        "contraindications_ok": True,
        "contraindications_verdict": "proceed",
        "contraindications_checklist_sent_count": 1,
        "language": "ru",
        "language_locked": True,
    })

    result = answer("reg_count", "повторите список противопоказаний")
    session = state.get_session("reg_count")

    assert session["contraindications_ok"] is True
    assert "кардиостимулятор" not in result.lower()
    assert "противопоказ" not in result.lower()


def test_regression_contra_ok_sticky_after_faq_and_next_message(monkeypatch: Any) -> None:
    setup_crm(monkeypatch)
    reset("reg_sticky", {
        "step": "date",
        "complaint": "спина",
        "age": 34,
        "contraindications_ok": True,
        "contraindications_verdict": "proceed",
        "language": "ru",
        "language_locked": True,
    })

    first = answer("reg_sticky", "Где вы находитесь?")
    session = state.get_session("reg_sticky")
    assert session["contraindications_ok"] is True
    assert "противопоказ" not in first.lower()

    second = answer("reg_sticky", "спасибо")
    session = state.get_session("reg_sticky")
    assert session["contraindications_ok"] is True
    assert session["contraindications_ok"] is not None


def test_required_repeat_guard_no_contra_answer_moves_to_date(monkeypatch: Any) -> None:
    setup_crm(monkeypatch)
    reset("req_no_contra", {"step": "contraindications", "complaint": "спина", "age": 35, "language": "ru", "language_locked": True})

    result = answer("req_no_contra", "Нет")
    session = state.get_session("req_no_contra")

    assert session["contraindications_ok"] is True
    assert session["step"] == "date"
    assert "На какой день" in result


def test_required_repeat_guard_age_already_set_not_asked_again(monkeypatch: Any) -> None:
    setup_crm(monkeypatch)
    reset("req_age_set", {"step": "age", "complaint": "спина", "age": 35, "language": "ru", "language_locked": True})

    result = answer("req_age_set", "Хочу записаться")
    session = state.get_session("req_age_set")

    assert session["step"] == "contraindications"
    assert "сколько Вам лет" not in result
    assert "противопоказ" in result.lower()


def test_required_repeat_guard_selected_time_asks_name_not_time(monkeypatch: Any) -> None:
    setup_crm(monkeypatch)
    reset("req_time_set", {
        "step": "time",
        "complaint": "спина",
        "age": 35,
        "contraindications_ok": True,
        "preferred_date": "2026-07-06",
        "selected_date": "2026-07-06",
        "selected_time": "14:00",
        "selected_doctor_login": "zhuma_md",
        "language": "ru",
        "language_locked": True,
    })

    result = answer("req_time_set", "да")
    session = state.get_session("req_time_set")

    assert session["step"] == "name"
    assert "имя" in result.lower()
    assert "какое время" not in result.lower()


def test_required_repeat_guard_patient_name_existing_books_not_reask(monkeypatch: Any) -> None:
    calls = setup_crm(monkeypatch)
    reset("req_name_set", {
        "step": "name",
        "complaint": "спина",
        "complaint_gate": bot_tools.COMPLAINT_OK,
        "age": 35,
        "contraindications_ok": True,
        "contraindications_verdict": bot_tools.CONTRA_PROCEED,
        "selected_date": "2026-07-06",
        "selected_time": "14:00",
        "selected_doctor_login": "zhuma_md",
        "selected_doctor_name": "Жумабек Мади Мухтарович",
        "patient_name": "Алия",
        "phone": "77011234567",
        "language": "ru",
        "language_locked": True,
    })

    result = answer("req_name_set", "да")
    session = state.get_session("req_name_set")

    assert len(calls["book"]) == 1
    assert calls["book"][0]["patient_name"] == "Алия"
    assert session["step"] == "booked"
    assert "имя" not in result.lower()


def test_time_empty_answer_final_repair_shows_slots() -> None:
    chat_id = "hotfix_time_empty_answer"
    slots = [{"time": t} for t in ["11:20", "12:00", "12:40", "13:20", "14:00"]]
    reset(chat_id, {"step": "time", "last_slots": slots, "selected_time": "", "patient_name": "Спасибо"})
    session = state.get_session(chat_id)

    result = dialog._finalize(chat_id, session, "")
    saved = state.get_session(chat_id)

    assert result
    assert "11:20" in result
    assert "Какое время Вам удобно?" in result
    assert saved["patient_name"] == ""


def test_time_lost_slots_rechecks_crm_and_selects_normalized_time(monkeypatch: Any) -> None:
    chat_id = "hotfix_time_lost_slots_select"
    calls = setup_crm(monkeypatch)

    async def fake_check_slots(date: str, doctor_login: str | None = None) -> dict[str, Any]:
        calls["slots"].append({"date": date, "doctor_login": doctor_login})
        return {
            "availability": [
                {
                    "doctorLogin": "zhuma_md",
                    "doctorName": "Жумабек Мади Мухтарович",
                    "date": date,
                    "availableSlots": ["11:20", "12:00", "12:40", "13:20", "14:00"],
                }
            ]
        }

    monkeypatch.setattr(crm, "check_slots", fake_check_slots)
    reset(chat_id, {"step": "time", "preferred_date": "2026-07-02", "last_slots": [], "selected_time": ""})

    result = answer(chat_id, "14/00")
    session = state.get_session(chat_id)

    assert calls["slots"] == [{"date": "2026-07-02", "doctor_login": None}]
    assert calls["book"] == []
    assert session["selected_time"] == "14:00"
    assert session["selected_date"] == "2026-07-02"
    assert session["selected_doctor_login"] == "zhuma_md"
    assert session["step"] == "name"
    assert result
    assert "имя" in result.lower()


def test_time_lost_slots_rechecks_crm_and_shows_slots_without_time(monkeypatch: Any) -> None:
    chat_id = "hotfix_time_lost_slots_thanks"
    calls = setup_crm(monkeypatch)

    async def fake_check_slots(date: str, doctor_login: str | None = None) -> dict[str, Any]:
        calls["slots"].append({"date": date, "doctor_login": doctor_login})
        return {
            "availability": [
                {
                    "doctorLogin": "zhuma_md",
                    "doctorName": "Жумабек Мади Мухтарович",
                    "date": date,
                    "availableSlots": ["11:20", "12:00", "12:40", "13:20", "14:00"],
                }
            ]
        }

    monkeypatch.setattr(crm, "check_slots", fake_check_slots)
    reset(chat_id, {"step": "time", "preferred_date": "2026-07-02", "last_slots": [], "selected_time": "", "patient_name": ""})

    result = answer(chat_id, "спасибо")
    session = state.get_session(chat_id)

    assert calls["slots"] == [{"date": "2026-07-02", "doctor_login": None}]
    assert "11:20" in result
    assert "14:00" in result
    assert session["step"] == "time"
    assert session["patient_name"] == ""


def test_last_slots_preserved_between_messages_until_selection_or_date_change() -> None:
    chat_id = "hotfix_preserve_last_slots"
    slots = [{"date": "2026-07-02", "time": "14:00"}]
    reset(chat_id, {"step": "time", "preferred_date": "2026-07-02", "last_slots": slots, "selected_time": ""})

    session = state.get_session(chat_id)
    session["last_slots"] = []
    state.save_session(chat_id, session)
    assert state.get_session(chat_id)["last_slots"] == slots

    session = state.get_session(chat_id)
    session["preferred_date"] = "2026-07-03"
    session["last_slots"] = []
    state.save_session(chat_id, session)
    assert state.get_session(chat_id)["last_slots"] == []


def test_time_thanks_does_not_save_patient_name_and_repeats_slots() -> None:
    chat_id = "hotfix_time_thanks"
    slots = [{"time": t} for t in ["11:20", "12:00", "12:40", "13:20", "14:00"]]
    reset(chat_id, {"step": "time", "last_slots": slots, "selected_time": "", "patient_name": "Спасибо"})

    result = answer(chat_id, "спасибо")
    session = state.get_session(chat_id)

    assert session["patient_name"] == ""
    assert session["step"] == "time"
    assert "11:20" in result
    assert "Какое время Вам удобно?" in result


def test_name_thanks_does_not_save_patient_name_and_asks_name_again() -> None:
    chat_id = "hotfix_name_thanks"
    reset(chat_id, {"step": "name", "selected_slot": {"time": "11:20"}, "selected_time": "11:20", "complaint": "спина", "age": 35, "contraindications_ok": True})

    result = answer(chat_id, "спасибо")
    session = state.get_session(chat_id)

    assert session["patient_name"] == ""
    assert "Подскажите, пожалуйста, Ваше имя для записи" in result


def test_kazakh_age_25te_moves_to_contraindications() -> None:
    chat_id = "hotfix_kz_age_25te"
    reset(chat_id, {"step": "age", "complaint": "белім ауырады", "language": "kk", "language_locked": True})
    result = answer(chat_id, "25те")
    session = state.get_session(chat_id)
    assert session["age"] == 25
    assert session["step"] == "contraindications"
    assert "Қарсы көрсетілімдеріңіз бар ма?" in result
    assert result.strip()


def test_kazakh_age_25_zhasta_and_zhasym_25_parse() -> None:
    for chat_id, text in [("hotfix_kz_age_zhasta", "25 жаста"), ("hotfix_kz_age_zhasym", "жасым 25")]:
        reset(chat_id, {"step": "age", "complaint": "белім ауырады", "language": "kk", "language_locked": True})
        answer(chat_id, text)
        session = state.get_session(chat_id)
        assert session["age"] == 25
        assert session["step"] == "contraindications"


def test_kazakh_complaint_answer_is_human_not_dry_age_only() -> None:
    chat_id = "hotfix_kz_complaint_bel_san"
    reset(chat_id, {"step": "start", "language": "kk", "language_locked": True})
    result = answer(chat_id, "Мені мазалайтыны белім ауырады, сосын саным ауырады")
    assert "беліңіз" in result
    assert "санға" in result
    assert "жасыңыз нешеде" in result.lower()
    assert result.strip() != "Жасыңыз нешеде?"


def test_kazakh_no_contra_moves_to_date() -> None:
    chat_id = "hotfix_kz_no_contra"
    reset(chat_id, {"step": "contraindications", "complaint": "белім ауырады", "age": 25, "language": "kk", "language_locked": True})
    result = answer(chat_id, "жоқ")
    session = state.get_session(chat_id)
    assert session["contraindications_ok"] is True
    assert session["step"] == "date"
    assert "Қай күнге" in result


def test_active_age_empty_answer_repair_is_non_empty() -> None:
    chat_id = "hotfix_active_age_empty_repair"
    reset(chat_id, {"step": "age", "complaint": "белім ауырады", "language": "kk", "language_locked": True, "gate_reason": "active_conversation_reply"})
    session = state.get_session(chat_id)
    session["last_user_text"] = "түсінікті"
    repaired = dialog.repair_empty_active_reply(session, session["last_user_text"])
    assert repaired.answer.strip()
    assert repaired.answer == "Жасыңызды жаза аласыз ба?"


def test_address_faq_during_contraindications_keeps_step_and_required_question() -> None:
    chat_id = "hotfix_address_faq_contra"
    reset(chat_id, {
        "step": "contraindications",
        "complaint": "Беспокоит колено, кажется артроз...",
        "age": 48,
        "contraindications_ok": None,
        "language": "ru",
        "language_locked": True,
    })

    result = answer(chat_id, "А где вы находитесь? В каком городе?")
    session = state.get_session(chat_id)

    assert result.strip()
    assert "Кабанбай батыра 28" in result
    assert "2ГИС" in result
    assert "Есть ли у Вас какие-нибудь противопоказания?" in result
    assert session["step"] == "contraindications"
    assert session.get("manual_takeover") is not True
    assert session.get("escalated") is not True


def test_faq_types_at_every_active_step_never_return_empty() -> None:
    faq_cases = {
        "price": "Сколько стоит первичный прием?",
        "address": "А где вы находитесь? В каком городе? 2гис",
        "medical": "Это опасно? Можно лечить?",
        "mri": "Нужно ли МРТ или снимок?",
    }
    active_steps = {
        "complaint": {"step": "complaint", "language": "ru", "language_locked": True},
        "age": {"step": "age", "complaint": "болит спина", "language": "ru", "language_locked": True},
        "contraindications": {"step": "contraindications", "complaint": "болит спина", "age": 48, "contraindications_ok": None, "language": "ru", "language_locked": True},
        "date": {"step": "date", "complaint": "болит спина", "age": 48, "contraindications_ok": True, "language": "ru", "language_locked": True},
        "time": {"step": "time", "complaint": "болит спина", "age": 48, "contraindications_ok": True, "preferred_date": "2026-07-02", "last_slots": [{"time": "11:20"}, {"time": "12:00"}], "language": "ru", "language_locked": True},
        "name": {"step": "name", "complaint": "болит спина", "age": 48, "contraindications_ok": True, "selected_slot": {"time": "11:20"}, "selected_time": "11:20", "language": "ru", "language_locked": True},
    }

    for step, preset in active_steps.items():
        for faq_type, text in faq_cases.items():
            chat_id = f"hotfix_faq_nonempty_{step}_{faq_type}"
            reset(chat_id, dict(preset))
            result = answer(chat_id, text)
            assert result.strip(), f"{faq_type} FAQ returned empty answer at step={step}"


def test_production_slash_time_selects_real_slot_without_booking(monkeypatch: Any) -> None:
    chat_id = "prod_slash_1120"
    calls = setup_crm(monkeypatch)
    slots = [{"doctorLogin":"kaisar_k","doctorName":"Куанышулы Кайсар Куанышулы","date":"2026-07-03","timeStart":"11:20","doctor_login":"kaisar_k","doctor_name":"Куанышулы Кайсар Куанышулы","time":"11:20"}]
    reset(chat_id, {"step":"time", "last_slots": slots, "selected_time":"", "patient_name":"", "complaint":"спина", "age": 35, "contraindications_ok": True})

    result = answer(chat_id, "11/20")
    session = state.get_session(chat_id)

    assert result
    assert session["selected_time"] == "11:20"
    assert session["selected_date"] == "2026-07-03"
    assert session["selected_doctor_login"] == "kaisar_k"
    assert session["selected_doctor_name"] == "Куанышулы Кайсар Куанышулы"
    assert session["step"] == "name"
    assert "Подскажите, пожалуйста, Ваше имя" in result
    assert calls["book"] == []
    assert dialog._booking_ready(session, "77011234567") is False


def test_production_slash_time_0920_normalized(monkeypatch: Any) -> None:
    chat_id = "prod_slash_0920"
    setup_crm(monkeypatch)
    slots = [{"doctorLogin":"kaisar_k","doctorName":"Куанышулы Кайсар Куанышулы","date":"2026-07-03","timeStart":"09:20","doctor_login":"kaisar_k","doctor_name":"Куанышулы Кайсар Куанышулы","time":"09:20"}]
    reset(chat_id, {"step":"time", "last_slots": slots, "selected_time":"", "patient_name":""})

    answer(chat_id, "9/20")
    session = state.get_session(chat_id)

    assert session["selected_time"] == "09:20"
    assert session["step"] == "name"


def test_production_reserve_slots_filtered_when_showing(monkeypatch: Any) -> None:
    chat_id = "prod_reserve_filtered"
    calls = setup_crm(monkeypatch)

    async def fake_check_slots(date: str, doctor_login: str | None = None) -> dict[str, Any]:
        calls["slots"].append({"date": date, "doctor_login": doctor_login})
        return {"availability": [
            {"doctorLogin":"reserve", "doctorName":"Резерв", "date": date, "availableSlots":["10:00"]},
            {"doctorLogin":"kaisar_k", "doctorName":"Куанышулы Кайсар Куанышулы", "date": date, "availableSlots":["11:20"]},
        ]}

    monkeypatch.setattr(crm, "check_slots", fake_check_slots)
    reset(chat_id, {"step":"date", "complaint":"спина", "age": 35, "contraindications_ok": True})
    session = state.get_session(chat_id)

    result = run(dialog._show_slots(chat_id, session, "2026-07-03"))
    state.save_session(chat_id, session)
    session = state.get_session(chat_id)

    assert len(session["last_slots"]) == 1
    assert session["last_slots"][0]["doctorLogin"] == "kaisar_k"
    assert "Резерв" not in result
    assert "10:00" not in result
    assert "11:20" in result


def test_production_only_reserve_slots_escalates_after_range_empty(monkeypatch: Any) -> None:
    chat_id = "prod_only_reserve"
    calls = setup_crm(monkeypatch)

    async def fake_check_slots(date: str, doctor_login: str | None = None) -> dict[str, Any]:
        calls["slots"].append({"date": date, "doctor_login": doctor_login})
        return {"availability": [{"doctorLogin":"reserve", "doctorName":"Резерв", "date": date, "availableSlots":["10:00"]}]}

    async def fake_nearest(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"slots": [{"doctorLogin":"reserve", "doctorName":"Резерв", "date":"2026-07-04", "timeStart":"10:00"}]}

    monkeypatch.setattr(crm, "check_slots", fake_check_slots)
    monkeypatch.setattr(crm, "find_nearest_available_slots", fake_nearest)
    reset(chat_id, {"step":"date", "complaint":"спина", "age": 35, "contraindications_ok": True})
    session = state.get_session(chat_id)

    result = run(dialog._show_slots(chat_id, session, "2026-07-03"))
    state.save_session(chat_id, session)
    session = state.get_session(chat_id)

    assert session.get("last_slots") == []
    assert session.get("manual_takeover") is True
    assert session.get("escalated") is True
    assert calls["book"] == []
    assert "администратор" in result.lower() or "администратора" in result.lower()


def test_production_booking_ready_blocks_reserve_and_empty_login(monkeypatch: Any) -> None:
    setup_crm(monkeypatch)
    base = {"patient_name":"Алия", "phone":"77011234567", "complaint":"спина", "age":35, "contraindications_ok": True, "selected_date":"2026-07-03", "selected_time":"11:20", "selected_doctor_name":"Резерв", "selected_slot":{"doctorLogin":"reserve", "doctorName":"Резерв", "date":"2026-07-03", "timeStart":"11:20"}}
    assert dialog._booking_ready({**base, "selected_doctor_login":"reserve"}, "77011234567") is False
    assert dialog._booking_ready({**base, "selected_doctor_login":"", "selected_doctor_name":"Куанышулы Кайсар Куанышулы", "selected_slot":{"doctorLogin":"", "doctorName":"Куанышулы Кайсар Куанышулы", "date":"2026-07-03", "timeStart":"11:20"}}, "77011234567") is False


def test_production_llm_book_before_name_repaired_to_ask_name(monkeypatch: Any) -> None:
    chat_id = "prod_llm_book_before_name"
    calls = setup_crm(monkeypatch)

    async def fake_brain(**kwargs: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        return ({"action":"ask_name", "next_step":"booked", "reply":"", "extracted":{}, "needs_python_tool":"book_appointment"}, {"openai_brain_used": True})

    monkeypatch.setattr(dialog, "run_openai_dialog_brain", fake_brain)
    slots = [{"doctorLogin":"kaisar_k","doctorName":"Куанышулы Кайсар Куанышулы","date":"2026-07-03","timeStart":"11:20","doctor_login":"kaisar_k","doctor_name":"Куанышулы Кайсар Куанышулы","time":"11:20"}]
    reset(chat_id, {"step":"time", "last_slots": slots, "selected_time":"", "patient_name":"", "complaint":"спина", "age": 35, "contraindications_ok": True})

    result = answer(chat_id, "11/20")
    session = state.get_session(chat_id)

    assert session.get("llm_blocked") is True
    assert result
    assert session["step"] == "name"
    assert session["selected_time"] == "11:20"
    assert calls["book"] == []
