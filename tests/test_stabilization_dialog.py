"""Offline regression checks for verified handoff, response safety and languages.

All external transports are mocked/refused by conftest. These are deterministic
runtime tests, not evidence of live CRM/LLM or clinician language review.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import agent
import ai
import dialog
import state
from config import get_settings


def run(coro):
    return asyncio.run(coro)


@pytest.mark.parametrize("response", [{"ok": True}, {"success": True}, {"created": True}, {"status": "Записан"}, {}])
def test_bare_success_cannot_confirm_booking(response):
    assert agent._crm_booking_succeeded(response) is False


def test_post_with_real_identifier_still_needs_readback():
    assert agent._crm_booking_succeeded({"ok": True, "appointmentId": 123}) is True
    assert agent._crm_booking_succeeded({"ok": False, "appointmentId": 123}) is False


@pytest.mark.parametrize("answer", ["Вы записаны.", "Запись подтверждена.", "Жазылуыңыз расталды."])
def test_model_cannot_confirm_booking_without_successful_tool(answer):
    session = {"last_user_text": "подтверди мою запись", "language": "ru"}
    assert dialog._unverified_fact("stabilization-false-booking", session, answer, agent.AgentResult(used=True)) == "booking_confirmation"


@pytest.mark.parametrize("answer", ["Я подключу администратора.", "Передам Ваш вопрос администратору.", "Әкімшіні қосамын."])
def test_model_cannot_promise_unconfirmed_handoff(answer):
    assert dialog._unverified_fact("stabilization-false-handoff", {}, answer, agent.AgentResult(used=True)) == "handoff_unconfirmed"


def test_escalation_failure_is_truthful_and_retryable(monkeypatch):
    attempts = []
    async def escalate(**kwargs):
        attempts.append(kwargs)
        if len(attempts) == 1:
            raise TimeoutError("offline transport")
        return {"ok": True}
    monkeypatch.setattr(dialog.crm, "escalate_to_operator", escalate)
    session = {"language": "ru", "phone": "77010000000"}
    first = run(dialog._handoff("stabilization-handoff", session, dialog.OPERATOR_HANDOFF_RU, "test"))
    assert first == dialog.HANDOFF_UNAVAILABLE_RU
    assert session["operator_handoff_confirmed"] is False
    assert not session.get("admin_notified_reason")
    assert session["manual_takeover"] is False
    second = run(dialog._handoff("stabilization-handoff", session, dialog.OPERATOR_HANDOFF_RU, "test"))
    assert second == dialog.OPERATOR_HANDOFF_RU
    assert session["operator_handoff_confirmed"] is True
    assert session["manual_takeover"] is True
    assert len(attempts) == 2
    assert run(dialog._notify_admin_once("stabilization-handoff", session, session["phone"], "test")) is True
    assert len(attempts) == 2


@pytest.mark.parametrize("response", [{"ok": False}, {"error": "unavailable"}, None])
def test_http_success_without_escalation_ack_is_not_handoff(monkeypatch, response):
    async def escalate(**kwargs):
        return response
    monkeypatch.setattr(dialog.crm, "escalate_to_operator", escalate)
    session = {}
    assert run(dialog._notify_admin_once("stabilization-no-ack", session, "77010000000", "test")) is False
    assert session["operator_handoff_confirmed"] is False
    assert "admin_notified_reason" not in session


@pytest.mark.parametrize("current,text,expected", [
    ("ru", "Сәлеметсіз бе, белім ауырады, дәрігерге жазылғым келеді", "kk"),
    ("kk", "Здравствуйте, у меня болит спина, хочу записаться", "ru"),
    ("ru", "Salem belim auyrady dariger kerek", "kk"),
])
def test_stale_language_lock_cannot_block_patient_language_switch(current, text, expected):
    assert dialog._detect_lang(text, {"language": current, "language_locked": True}) == expected


def test_invalid_whatsapp_sender_does_not_reuse_stored_other_phone():
    session = {"phone": "77010000000", "transport": "whatsapp"}
    dialog._start_turn("stabilization-phone", session, "12345", "Здравствуйте")
    assert session["phone"] == ""


def test_wrong_language_without_safe_repair_is_blocked():
    session = {"language": "kk", "last_user_text": "Қай уақытта?"}
    answer = run(dialog._finalize("stabilization-language", session, "Свободного времени на эту дату нет.", agent.AgentResult(used=True)))
    assert answer != "Свободного времени на эту дату нет."
    assert session["language_guard_result"] == "unresolved_language_blocked"


def test_malformed_model_empty_choices_is_safe_skip(monkeypatch):
    async def create(**kwargs):
        return SimpleNamespace(choices=[], usage=None)
    fake = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setenv("OPENAI_API_KEY", "test-only-not-real")
    monkeypatch.setenv("AI_ENABLED", "true")
    monkeypatch.setenv("OPENAI_BRAIN_ENABLED", "true")
    get_settings.cache_clear()
    monkeypatch.setattr(ai, "AsyncOpenAI", object)
    monkeypatch.setattr(ai, "_openai_client", lambda key: fake)
    monkeypatch.setattr(agent.ai_budget, "check_allowed", lambda purpose: (True, ""))
    monkeypatch.setattr(agent.ai_budget, "record_usage", lambda *args, **kwargs: {})
    session = {"language": "ru"}
    result = run(agent.run_agent_turn(chat_id="stabilization-empty-choices", phone="77010000000", session=session, user_text="Здравствуйте"))
    assert result.used is False
    assert result.skip_reason == "invalid_model_response"


def _ready_booking_session():
    session = {
        "complaint": "спина", "complaint_gate": "COMPLAINT_OK", "age": 40,
        "contraindications_ok": True, "contraindications_verdict": "proceed",
        "crm_known_doctors": {"doctor_test": "Тестовый Врач"},
    }
    agent._remember_offered_slots(session, [{
        "doctor_login": "doctor_test", "doctor_name": "Тестовый Врач",
        "date": "2026-11-02", "time_start": "14:00",
    }])
    return session


@pytest.mark.parametrize("verified", [False, True])
def test_booking_confirmation_requires_separate_persisted_readback(monkeypatch, verified):
    calls = []
    async def book(**kwargs):
        calls.append("post")
        return {"ok": True, "id": "test-appointment"}
    async def verify(response, **kwargs):
        calls.append("readback")
        assert response["id"] == "test-appointment"
        return {"verified": verified, "reason": "test_readback", "appointment": {"id": "test-appointment"}}
    monkeypatch.setattr(agent.crm, "book_appointment", book)
    monkeypatch.setattr(agent.crm, "verify_saved_booking", verify)
    session = _ready_booking_session()
    args = {"patient_name": "Тестовый Пациент", "doctor_login": "doctor_test", "date": "2026-11-02", "time_start": "14:00"}
    result = run(agent._tool_book_appointment("stabilization-booking-readback", session, "77010000000", args))
    assert calls == ["post", "readback"]
    assert result["booking_success"] is verified
    assert session.get("booking_confirmed") is verified
    if not verified:
        again = run(agent._tool_book_appointment("stabilization-booking-readback", _ready_booking_session(), "77010000000", args))
        assert again["booking_success"] is False
        assert again["error"] == "booking_already_in_progress"
        assert calls == ["post", "readback"]
        assert session["booking_uncertain"] is True


@pytest.mark.parametrize("answer", ["Записала Вас, ждём!", "Готово, ждём Вас!", "Сізді жазып қойдым."])
def test_uncertain_booking_blocks_all_model_success_wording(answer):
    assert dialog._unverified_fact("stabilization-uncertain", {"booking_uncertain": True, "booking_confirmed": False}, answer, agent.AgentResult(used=True)) == "booking_uncertain"


def test_uncertain_booking_handoff_discloses_unconfirmed_status(monkeypatch):
    async def escalate(**kwargs):
        return {"ok": True}
    monkeypatch.setattr(dialog.crm, "escalate_to_operator", escalate)
    session = {"language": "ru", "booking_uncertain": True, "phone": "77010000000"}
    answer = run(dialog._finalize("stabilization-uncertain-final", session, "Готово, ждём Вас!", agent.AgentResult(used=True)))
    assert "Статус Вашей записи пока не подтверждён" in answer
    assert "Готово" not in answer
    assert session["operator_handoff_confirmed"] is True


@pytest.mark.parametrize("answer", [
    "У Вас остеохондроз, принимайте ибупрофен 200 мг.",
    "Ваш диагноз — артрит.",
    "Принимайте по 1 таблетке обезболивающего утром.",
    "Начните принимать препарат 10 мг ежедневно.",
    "Сізде остеохондроз. Ибупрофен 200 мг қабылдаңыз.",
    "Сізде грыжа болуы мүмкін.",
    "Дәріні күніне екі рет ішіңіз.",
])
def test_direct_model_diagnosis_or_prescription_is_blocked(answer):
    assert dialog._medical_advice_violation(answer) is True
    assert dialog._unverified_fact("stabilization-medical", {}, answer, agent.AgentResult(used=True)) == "medical_diagnosis_or_prescription"


@pytest.mark.parametrize("answer", [
    "Врач поставит диагноз и при необходимости назначит лечение на приёме.",
    "Чтобы обсудить диагноз и лечение, нужна консультация врача.",
    "Не принимайте препарат без назначения врача.",
    "Диагнозды дәрігер қояды, емді тексеруден кейін тағайындайды.",
    "Дәрігер қабылдауда ем жоспарын түсіндіреді.",
])
def test_general_registrar_medical_information_is_not_blocked(answer):
    assert dialog._medical_advice_violation(answer) is False


@pytest.mark.parametrize("language,answer", [
    ("ru", "У Вас остеохондроз, принимайте препарат 200 мг."),
    ("kk", "Сізде остеохондроз. Дәріні күніне екі рет қабылдаңыз."),
])
def test_medical_model_violation_gets_bilingual_registrar_fallback_without_false_handoff(monkeypatch, language, answer):
    async def should_not_escalate(**kwargs):
        raise AssertionError("a routine medical question does not imply a real human handoff")
    monkeypatch.setattr(dialog.crm, "escalate_to_operator", should_not_escalate)
    session = {"language": language, "last_user_text": "медицинский вопрос"}
    actual = run(dialog._finalize("stabilization-medical-final", session, answer, agent.AgentResult(used=True)))
    assert actual != answer
    assert "200" not in actual and "остеохондроз" not in actual
    assert session["answer_source"] == "medical_safety_template"
    assert session["language_guard_result"] == "ok"


@pytest.mark.parametrize("lookup", [
    {"ok": True, "found": True, "isNew": False, "lead": {"id": "test-lead", "status": "НОВАЯ"}},
    {"ok": True, "found": True, "isNew": True, "lead": {"id": "test-lead", "status": "В РАБОТЕ"}},
    {"ok": True, "found": True, "isNew": False},
])
def test_active_admission_lease_cannot_override_existing_or_staff_owned_crm_contact(monkeypatch, lookup):
    from datetime import datetime, timezone
    async def crm_lookup(phone):
        return lookup
    async def escalate(**kwargs):
        return {"ok": True}
    async def forbidden_model(**kwargs):
        raise AssertionError("an existing client must be excluded before any model call")
    monkeypatch.setattr(dialog.crm, "lookup_active_appointments_by_phone", crm_lookup)
    monkeypatch.setattr(dialog.crm, "escalate_to_operator", escalate)
    monkeypatch.setattr(dialog.agent, "run_agent_turn", forbidden_model)
    chat_id = "stabilization-crm-authoritative"
    state.reset_session(chat_id)
    session = state.get_session(chat_id)
    session.update({"phone": "77010000000", "ai_lead_started": True,
        "ai_admission_started_at": datetime.now(timezone.utc).isoformat(),
        "language": "ru"})
    state.save_session(chat_id, session)
    answer = run(dialog.handle_message(chat_id, "77010000000", "Здравствуйте, хочу записаться"))
    assert answer == ""
    actual = state.get_session(chat_id)
    assert actual["crm_patient_is_new"] is False
    assert actual["crm_patient_state"] == "RETURNING_PATIENT_NO_ACTIVE_BOOKING"
    assert actual["should_send_wazzup"] is False
    assert actual["ai_muted"] is True


def test_still_new_crm_lead_can_continue_without_any_local_override(monkeypatch):
    from datetime import datetime, timezone
    calls = []
    async def crm_lookup(phone):
        return {"ok": True, "found": True, "isNew": True,
                "lead": {"id": "test-lead", "status": "НОВАЯ"}}
    async def model(**kwargs):
        calls.append(kwargs)
        return agent.AgentResult(used=True, reply="Здравствуйте! Чем могу помочь?")
    monkeypatch.setattr(dialog.crm, "lookup_active_appointments_by_phone", crm_lookup)
    monkeypatch.setattr(dialog.agent, "run_agent_turn", model)
    chat_id = "stabilization-crm-new-continuation"
    state.reset_session(chat_id)
    session = state.get_session(chat_id)
    session.update({"phone": "77010000000", "ai_lead_started": True,
        "ai_admission_started_at": datetime.now(timezone.utc).isoformat(),
        "language": "ru"})
    state.save_session(chat_id, session)
    answer = run(dialog.handle_message(chat_id, "77010000000", "Здравствуйте"))
    assert answer == "Здравствуйте! Чем могу помочь?"
    assert len(calls) == 1
    assert state.get_session(chat_id)["crm_patient_is_new"] is True
    assert state.get_session(chat_id)["crm_state_reason"] != "active_ai_conversation"


def test_two_distinct_patients_cannot_post_one_doctor_slot_concurrently(monkeypatch):
    posts = []
    async def book(**kwargs):
        posts.append(kwargs)
        await asyncio.sleep(0.02)
        return {"ok": True, "id": "test-global-appointment"}
    async def verify(response, **kwargs):
        return {"verified": True, "reason": "test_persisted_readback"}
    monkeypatch.setattr(agent.crm, "book_appointment", book)
    monkeypatch.setattr(agent.crm, "verify_saved_booking", verify)
    args = {"patient_name": "Тестовый Пациент", "doctor_login": "doctor_test", "date": "2026-11-02", "time_start": "14:00"}
    async def concurrent():
        return await asyncio.gather(
            agent._tool_book_appointment("global-first", _ready_booking_session(), "77010000001", args),
            agent._tool_book_appointment("global-second", _ready_booking_session(), "77010000002", args),
        )
    results = run(concurrent())
    assert len(posts) == 1
    assert sum(r["booking_success"] for r in results) == 1
    assert [r["error"] for r in results if not r["booking_success"]] == ["slot_busy"]
    assert state.booking_claim_status("slot:2026-11-02|14:00|doctor_test") == "confirmed"


@pytest.mark.parametrize("reject", [False, True])
def test_global_slot_claim_stays_uncertain_unless_crm_explicitly_rejects(monkeypatch, reject):
    posts = []
    async def book(**kwargs):
        posts.append(kwargs)
        return {"ok": False, "error": "validation_rejected"} if reject else {"ok": True, "id": "test-uncertain-global"}
    async def verify(response, **kwargs):
        return {"verified": False, "reason": "readback_unavailable"}
    monkeypatch.setattr(agent.crm, "book_appointment", book)
    monkeypatch.setattr(agent.crm, "verify_saved_booking", verify)
    args = {"patient_name": "Тестовый Пациент", "doctor_login": "doctor_test", "date": "2026-11-02", "time_start": "14:00"}
    result = run(agent._tool_book_appointment("global-rejection", _ready_booking_session(), "77010000001", args))
    assert result["booking_success"] is False
    key = "slot:2026-11-02|14:00|doctor_test"
    assert state.booking_claim_status(key) == ("" if reject else "uncertain")
    second = run(agent._tool_book_appointment("global-other-patient", _ready_booking_session(), "77010000002", args))
    assert len(posts) == (2 if reject else 1)
    if not reject:
        assert second["error"] == "slot_busy"


def test_legacy_patient_claim_closes_new_global_claim_until_reconciliation(monkeypatch):
    async def forbidden_post(**kwargs):
        raise AssertionError("the old uncertain patient operation must not be repeated")
    monkeypatch.setattr(agent.crm, "book_appointment", forbidden_post)
    patient_key = "2026-11-02|14:00|doctor_test|77010000001"
    assert state.claim_booking(patient_key, "legacy-chat")
    state.mark_booking_claim(patient_key, "uncertain")
    args = {"patient_name": "Тестовый Пациент", "doctor_login": "doctor_test", "date": "2026-11-02", "time_start": "14:00"}
    result = run(agent._tool_book_appointment("legacy-chat", _ready_booking_session(), "77010000001", args))
    assert result["error"] == "booking_already_in_progress"
    assert state.booking_claim_status("slot:2026-11-02|14:00|doctor_test") == "uncertain"
    assert state.booking_claim_status(patient_key) == "uncertain"
