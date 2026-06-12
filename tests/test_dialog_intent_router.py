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
    assert "с такими жалобами к нам обращаются" in result.lower()
    assert "сколько Вам лет" in result
    assert "противопоказание для записи" not in result
    assert session["step"] == "age"

    reset("pf_leg_pull")
    result = answer("pf_leg_pull", "тянет ногу и болит поясница")
    session = state.get_session("pf_leg_pull")
    assert "с такими жалобами к нам обращаются" in result.lower()
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


def test_static_dialog_template_wiring_and_tr_arity() -> None:
    source = (PROJECT_ROOT / "dialog.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    tr_calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_tr"]
    assert tr_calls
    assert all(len(node.args) == 3 for node in tr_calls)

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
