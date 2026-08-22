from __future__ import annotations

import asyncio
import json
import os
import tempfile
from typing import Any

import pytest


os.environ.setdefault("SQLITE_PATH", tempfile.NamedTemporaryFile(delete=False, suffix=".sqlite3").name)
os.environ.setdefault("CRM_BOT_SECRET", "test")
os.environ.setdefault("OPENAI_API_KEY", "")


import ai
import crm
import dialog
import state


state.init_db()


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def seed(chat_id: str, extra: dict[str, Any]) -> None:
    state.reset_session(chat_id)
    session = state.get_session(chat_id)
    session.update(
        {
            "ai_lead_started": True,
            "first_touch_info_sent": True,
            "gate_reason": "active_ai_lead",
            **extra,
        }
    )
    state.save_session(chat_id, session)


def brain_decision(
    action: str,
    reply: str,
    *,
    next_step: str,
    extracted: dict[str, Any] | None = None,
    tool: str = "none",
) -> tuple[dict[str, Any], dict[str, Any]]:
    values = {
        "complaint": "",
        "age": None,
        "contraindications_clear": None,
        "contraindication_confirmed": False,
        "contraindication_term_asked": "",
        "contraindication_red_flags": [],
        "symptom_duration": "",
        "preferred_date_text": "",
        "time_preference": "",
        "slot_choice": None,
        "patient_name": "",
        "patient_relation": "",
        "doctor_preference": "",
        "wants_human": False,
        "faq_type": "",
        "language": "ru",
        "detected_language": "ru",
        "preferred_response_language": "ru",
        "language_confidence": 1.0,
    }
    values.update(extracted or {})
    decision = {
        "intent": "date_preference" if action == "show_slots" else "complaint",
        "action": action,
        "next_step": next_step,
        "patient_meaning": "",
        "reply": reply,
        "extracted": values,
        "needs_python_tool": tool,
        "safety": {
            "hard_stop": False,
            "reason": "",
            "unsafe_medical_claim": False,
            "tries_to_book_without_rules": False,
        },
        "safety_flags": {},
    }
    return decision, {
        "openai_brain_used": True,
        "openai_brain_action": action,
        "openai_brain_needs_python_tool": tool,
        "openai_brain_extracted": values,
    }


async def new_patient_lookup(_phone: str) -> dict[str, Any]:
    return {
        "ok": True,
        "found": False,
        "isNew": True,
        "patient": None,
        "lead": None,
        "lastAppointment": None,
        "hasActiveAppointment": False,
        "appointment": None,
        "appointments": [],
    }


async def returning_lead_lookup(_phone: str) -> dict[str, Any]:
    return {
        "ok": True,
        "found": True,
        "isNew": False,
        "patient": {"name": "Пациент"},
        "lead": {"id": "lead-1", "status": "CONTACTED"},
        "lastAppointment": None,
        "hasActiveAppointment": False,
        "appointment": None,
        "appointments": [],
    }


def test_returning_lead_without_active_ai_flow_stays_silent(monkeypatch: pytest.MonkeyPatch) -> None:
    chat_id = "audit_existing_returning_lead"
    state.reset_session(chat_id)
    monkeypatch.setattr(crm, "lookup_active_appointments_by_phone", returning_lead_lookup)

    answer = run(dialog.handle_message(chat_id, "77011234567", "Здравствуйте, хочу записаться"))
    session = state.get_session(chat_id)

    assert answer == ""
    assert session["crm_patient_state"] == "RETURNING_PATIENT_NO_ACTIVE_BOOKING"
    assert session["no_reply_reason"] == "old_lead_from_crm"
    assert session["ai_muted"] is True


def test_active_ai_booking_survives_crm_lead_reclassification(monkeypatch: pytest.MonkeyPatch) -> None:
    """CRM creating/updating a lead mid-dialog must not silence the active funnel."""
    chat_id = "audit_crm_reclassified_after_date"
    seed(
        chat_id,
        {
            "step": "date",
            "complaint": "болит поясница",
            "age": 38,
            "contraindications_ok": True,
            "contraindications_verdict": "proceed",
        },
    )
    calls: list[dict[str, Any]] = []

    async def fake_slots(date: str, doctor_login: str | None = None) -> dict[str, Any]:
        calls.append({"date": date, "doctor_login": doctor_login})
        return {
            "availability": [
                {
                    "doctorLogin": "kaisar_k",
                    "doctorName": "Куанышулы Кайсар Куанышулы",
                    "date": date,
                    "availableSlots": ["11:20"],
                }
            ]
        }

    async def fake_brain(**_kwargs: Any):
        return brain_decision(
            "show_slots",
            "Проверю свободное время на понедельник.",
            next_step="time",
            extracted={"preferred_date_text": "в понедельник"},
            tool="check_slots",
        )

    monkeypatch.setattr(crm, "lookup_active_appointments_by_phone", returning_lead_lookup)
    monkeypatch.setattr(crm, "check_slots", fake_slots)
    monkeypatch.setattr(dialog, "run_openai_dialog_brain", fake_brain)

    answer = run(dialog.handle_message(chat_id, "77011234567", "в понедельник"))
    session = state.get_session(chat_id)

    assert calls, "active booking flow was muted before CRM check-slots"
    assert "11:20" in answer
    assert session["step"] == "time"
    assert session["manual_takeover"] is False
    assert session["ai_muted"] is False
    assert session.get("active_ai_flow_preserved_from_crm_reclassification") is True


def test_first_new_patient_turn_is_led_by_gpt(monkeypatch: pytest.MonkeyPatch) -> None:
    chat_id = "audit_gpt_first_turn"
    state.reset_session(chat_id)
    called = {"brain": 0}

    async def fake_brain(**_kwargs: Any):
        called["brain"] += 1
        return brain_decision(
            "ask_age",
            "Понимаю, поясница беспокоит уже несколько дней. Сколько лет пациенту?",
            next_step="age",
            extracted={"complaint": "болит поясница"},
        )

    monkeypatch.setattr(crm, "lookup_active_appointments_by_phone", new_patient_lookup)
    monkeypatch.setattr(dialog, "run_openai_dialog_brain", fake_brain)

    answer = run(
        dialog.handle_message(
            chat_id,
            "77011234567",
            "Здравствуйте, хочу записаться, уже несколько дней болит поясница",
        )
    )
    session = state.get_session(chat_id)

    assert called["brain"] == 1
    assert answer.startswith("Понимаю")
    assert session["step"] == "age"
    assert session["openai_brain_used"] is True


def test_manual_takeover_is_not_cleared_by_new_patient_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    chat_id = "audit_manual_takeover_sticky"
    seed(
        chat_id,
        {
            "step": "escalated",
            "manual_takeover": True,
            "ai_muted": True,
            "escalated": True,
            "no_reply_reason": "manual_takeover",
        },
    )

    async def should_not_call_brain(**_kwargs: Any):
        raise AssertionError("GPT must remain silent after a real manual takeover")

    monkeypatch.setattr(crm, "lookup_active_appointments_by_phone", new_patient_lookup)
    monkeypatch.setattr(dialog, "run_openai_dialog_brain", should_not_call_brain)

    answer = run(dialog.handle_message(chat_id, "77011234567", "Вы тут?"))
    session = state.get_session(chat_id)

    assert answer == ""
    assert session["manual_takeover"] is True
    assert session["ai_muted"] is True
    assert session["no_reply_reason"] == "manual_takeover"


def test_gpt_doctor_preference_is_resolved_to_real_crm_login(monkeypatch: pytest.MonkeyPatch) -> None:
    chat_id = "audit_specific_doctor"
    seed(
        chat_id,
        {
            "step": "date",
            "complaint": "болит колено",
            "age": 42,
            "contraindications_ok": True,
            "contraindications_verdict": "proceed",
        },
    )
    calls: list[dict[str, Any]] = []

    async def fake_doctors() -> dict[str, Any]:
        return {
            "doctors": [
                {
                    "doctorLogin": "kaisar_k",
                    "doctorName": "Куанышулы Кайсар Куанышулы",
                    "specialization": "суставы, колено",
                },
                {
                    "doctorLogin": "zhuma_md",
                    "doctorName": "Жумабек Мади Мухтарович",
                    "specialization": "позвоночник",
                },
            ]
        }

    async def fake_slots(date: str, doctor_login: str | None = None) -> dict[str, Any]:
        calls.append({"date": date, "doctor_login": doctor_login})
        return {
            "availability": [
                {
                    "doctorLogin": doctor_login,
                    "doctorName": "Куанышулы Кайсар Куанышулы",
                    "date": date,
                    "availableSlots": ["14:00"],
                }
            ]
        }

    async def fake_brain(**_kwargs: Any):
        return brain_decision(
            "show_slots",
            "Хорошо, посмотрю время именно к Кайсару Куанышулы.",
            next_step="time",
            extracted={
                "preferred_date_text": "в понедельник",
                # GPT must preserve the patient's natural grammatical form.
                "doctor_preference": "Кайсару",
            },
            tool="check_slots",
        )

    monkeypatch.setattr(crm, "lookup_active_appointments_by_phone", new_patient_lookup)
    monkeypatch.setattr(crm, "get_doctors", fake_doctors)
    monkeypatch.setattr(crm, "check_slots", fake_slots)
    monkeypatch.setattr(dialog, "run_openai_dialog_brain", fake_brain)

    answer = run(dialog.handle_message(chat_id, "77011234567", "в понедельник к Кайсару"))
    session = state.get_session(chat_id)

    assert calls[-1]["doctor_login"] == "kaisar_k"
    assert session["selected_doctor_login"] == "kaisar_k"
    assert session["selected_doctor_name"] == "Куанышулы Кайсар Куанышулы"
    assert "14:00" in answer


def test_relative_patient_name_and_specific_doctor_reach_crm(monkeypatch: pytest.MonkeyPatch) -> None:
    chat_id = "audit_relative_books_patient"
    slot = {
        "doctorLogin": "kaisar_k",
        "doctorName": "Куанышулы Кайсар Куанышулы",
        "date": "2099-01-05",
        "timeStart": "14:00",
        "doctor_login": "kaisar_k",
        "doctor_name": "Куанышулы Кайсар Куанышулы",
        "time": "14:00",
    }
    seed(
        chat_id,
        {
            "step": "time",
            "patient_subject": "relative",
            "patient_relation": "мама",
            "complaint": "у мамы болит колено",
            "age": 64,
            "contraindications_ok": True,
            "contraindications_verdict": "proceed",
            "preferred_date": "2099-01-05",
            "last_slots": [slot],
        },
    )
    booked = {"done": False}
    booking_calls: list[dict[str, Any]] = []

    async def lookup(_phone: str) -> dict[str, Any]:
        if booked["done"]:
            return {
                "ok": True,
                "found": True,
                "isNew": False,
                "patient": {"name": "Елена"},
                "hasActiveAppointment": True,
                "appointment": {
                    "id": "booking-relative-1",
                    "date": "2099-01-05",
                    "timeStart": "14:00",
                    "doctorName": "Куанышулы Кайсар Куанышулы",
                    "doctorLogin": "kaisar_k",
                    "status": "booked",
                },
                "appointments": [],
            }
        return await new_patient_lookup(_phone)

    async def fake_book(**kwargs: Any) -> dict[str, Any]:
        booking_calls.append(kwargs)
        booked["done"] = True
        return {"ok": True, "id": "booking-relative-1", **kwargs}

    async def fallback_brain(**_kwargs: Any):
        return brain_decision("fallback_rule_based", "", next_step="keep_current")

    monkeypatch.setattr(crm, "lookup_active_appointments_by_phone", lookup)
    monkeypatch.setattr(crm, "book_appointment", fake_book)
    monkeypatch.setattr(dialog, "run_openai_dialog_brain", fallback_brain)

    ask_name = run(dialog.handle_message(chat_id, "77011234567", "1 вариант"))
    confirmation = run(dialog.handle_message(chat_id, "77011234567", "Елена"))
    session = state.get_session(chat_id)

    assert "имя мамы" in ask_name.lower() or "имя пациента" in ask_name.lower()
    assert booking_calls and booking_calls[0]["patient_name"] == "Елена"
    assert booking_calls[0]["doctor_login"] == "kaisar_k"
    assert session["appointment_doctor_login"] == "kaisar_k"
    assert session["booking_confirmed"] is True
    assert "запись подтверждена" in confirmation.lower()


def test_dialog_brain_uses_one_current_prompt_and_strict_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    prompt = ai._full_dialog_brain_system_prompt()
    assert "Запись на третье лицо" not in prompt
    assert "Шаг 1 — знакомство" not in prompt
    assert "жалоба → возраст → противопоказания → дата" in prompt
    assert "за родственника" in prompt

    captured: dict[str, Any] = {}
    payload = {
        "understood_context": {
            "patient_meaning": "болит спина",
            "is_answer_to_last_question": False,
            "is_new_question": False,
            "contains_multiple_entities": False,
            "should_answer_faq_first": False,
            "should_return_to_pending_step": False,
        },
        "intent": "complaint",
        "entities": {"complaint": "болит спина"},
        "next_required_step": "age",
        "needs_python_tool": "none",
        "reply": "Понимаю. Сколько лет пациенту?",
        "safety": {
            "hard_stop": False,
            "unsafe_medical_claim": False,
            "invented_fact_risk": False,
            "reason": "",
        },
    }

    class FakeCompletions:
        async def create(self, **kwargs: Any):
            captured.update(kwargs)
            choice = type(
                "Choice",
                (),
                {
                    "message": type("Message", (), {"content": json.dumps(payload, ensure_ascii=False)})(),
                    "finish_reason": "stop",
                },
            )()
            return type("Response", (), {"choices": [choice]})()

    class FakeClient:
        chat = type("Chat", (), {"completions": FakeCompletions()})()

    settings = ai.get_settings()
    monkeypatch.setattr(settings, "openai_api_key", "test-key")
    monkeypatch.setattr(ai, "AsyncOpenAI", object)
    monkeypatch.setattr(ai, "_openai_client", lambda _key: FakeClient())

    decision, debug = run(
        ai.run_openai_dialog_brain(
            user_text="болит спина",
            session={"chat_id": "audit_strict_schema", "step": "complaint"},
        )
    )

    assert decision["action"] == "ask_age"
    assert debug["openai_brain_used"] is True
    assert captured["response_format"]["type"] == "json_schema"
    assert captured["response_format"]["json_schema"]["strict"] is True


def test_availability_request_cannot_skip_required_gates(monkeypatch: pytest.MonkeyPatch) -> None:
    chat_id = "audit_no_slots_before_contraindications"
    seed(chat_id, {"step": "age", "complaint": "болит спина"})
    crm_calls = {"slots": 0, "nearest": 0}

    async def forbidden_slots(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        crm_calls["slots"] += 1
        raise AssertionError("CRM slots must not be requested before required gates")

    async def forbidden_nearest(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        crm_calls["nearest"] += 1
        raise AssertionError("nearest slots must not be requested before required gates")

    async def fake_brain(**_kwargs: Any):
        return brain_decision(
            "ask_age",
            "Сначала уточню возраст пациента. Сколько ему лет?",
            next_step="age",
        )

    monkeypatch.setattr(crm, "lookup_active_appointments_by_phone", new_patient_lookup)
    monkeypatch.setattr(crm, "check_slots", forbidden_slots)
    monkeypatch.setattr(crm, "find_nearest_available_slots", forbidden_nearest)
    monkeypatch.setattr(dialog, "run_openai_dialog_brain", fake_brain)

    answer = run(dialog.handle_message(chat_id, "77011234567", "Какие ближайшие даты есть?"))
    session = state.get_session(chat_id)

    assert crm_calls == {"slots": 0, "nearest": 0}
    assert session["step"] == "age"
    assert "сколько" in answer.lower()


def test_active_gpt_no_reply_is_repaired_to_visible_progress(monkeypatch: pytest.MonkeyPatch) -> None:
    chat_id = "audit_active_no_reply_repaired"
    seed(chat_id, {"step": "age", "complaint": "болит спина"})

    async def fake_brain(**_kwargs: Any):
        return brain_decision("no_reply", "", next_step="age")

    monkeypatch.setattr(crm, "lookup_active_appointments_by_phone", new_patient_lookup)
    monkeypatch.setattr(dialog, "run_openai_dialog_brain", fake_brain)

    answer = run(dialog.handle_message(chat_id, "77011234567", "Я не понял вопрос"))
    session = state.get_session(chat_id)

    assert answer
    assert session["step"] == "age"
    assert session["openai_brain_guard_reason"] == "active_lead_no_reply"


def test_duplicate_answer_loop_is_bounded() -> None:
    chat_id = "audit_duplicate_loop_guard"
    repeated = "Подскажите, пожалуйста, сколько Вам лет?"
    seed(
        chat_id,
        {
            "step": "age",
            "complaint": "болит спина",
            "last_assistant_answer": repeated,
        },
    )

    answers: list[str] = []
    for _ in range(3):
        session = state.get_session(chat_id)
        session["last_assistant_answer"] = repeated
        answers.append(dialog._finalize(chat_id, session, repeated))

    session = state.get_session(chat_id)
    assert all(answers)
    assert session["step"] == "escalated"
    assert session["handoff_reason"] == "duplicate_answer_loop_guard"
