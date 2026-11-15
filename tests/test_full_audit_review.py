"""Adversarial regressions from the independent whole-branch review."""
import asyncio

import pytest

import agent
import bot_tools
import crm
import services
import state
from config import get_settings
from test_full_audit_patient_safety import ready_session, booking_args


@pytest.fixture(autouse=True)
def isolated_review_storage(tmp_path, monkeypatch):
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "review.sqlite3"))
    get_settings.cache_clear()
    state.init_db()
    monkeypatch.setattr(agent, "_log", lambda *a, **kw: None)
    yield
    get_settings.cache_clear()


@pytest.mark.parametrize("text", [
    "Кардиостимулятора нет и эпилепсия есть, противопоказаний нет",
    "У меня нет кардиостимулятора и есть эпилепсия",
    "У меня эпилепсия, что такое кардиостимулятор?",
    "Қан ұюының елеулі бұзылыстары бар",
    "Жүрекке байланысты ауыр мәселелер бар",
    "Тромбофлебит жоқ және эпилепсия бар",
])
@pytest.mark.parametrize("fields", [{"patient_name": "Тест"}, {"contraindications_clear": True, "contraindications_note": "противопоказаний нет"}])
def test_each_present_condition_overrides_absence_or_term_question(text, fields):
    session = ready_session()
    session["last_user_text"] = text
    agent._tool_record_patient_facts("review", session, fields)
    assert session["contraindications_ok"] is False
    assert bot_tools.booking_gate_status(session)[0] is False


@pytest.mark.parametrize("text", [
    "Кардиостимулятора нет и эпилепсии нет, противопоказаний нет",
    "Нет кардиостимулятора и эпилепсии, противопоказаний нет",
    "Кардиостимулятор жоқ және эпилепсия жоқ, қарсы көрсетілімдер жоқ",
])
def test_negative_enumeration_does_not_invent_a_contraindication(text):
    session = ready_session()
    session["last_user_text"] = text
    agent._tool_record_patient_facts("review", session, {"contraindications_clear": True, "contraindications_note": text})
    assert session["contraindications_ok"] is True


@pytest.mark.parametrize("supplied", ["self", "", "я", "өзім"])
def test_booking_args_cannot_weaken_stored_relative_identity(monkeypatch, supplied):
    posts = []
    async def book(**kwargs):
        posts.append(kwargs)
        return {"ok": True, "appointmentId": "synthetic"}
    async def verify(*a, **kw):
        return {"verified": True}
    monkeypatch.setattr(crm, "book_appointment", book)
    monkeypatch.setattr(crm, "verify_saved_booking", verify)
    session = ready_session(62)
    session["patient_relation"] = "mother"
    result = asyncio.run(agent._tool_book_appointment("review", session, "77000000001", booking_args(patient_relation=supplied)))
    assert result["booking_success"] is False
    assert posts == []


@pytest.mark.parametrize("status", ["pending", "uncertain", "confirmed"])
def test_upgrade_detects_legacy_phone_slot_claim_before_another_post(monkeypatch, status):
    legacy_key = "2031-03-07|14:00|qa_doc|77000000001"
    assert state.claim_booking(legacy_key, "pre-upgrade")
    state.mark_booking_claim(legacy_key, status)
    posts = []
    async def book(**kw):
        posts.append(kw)
        return {"ok": True, "appointmentId": "synthetic"}
    async def verify(*a, **kw):
        return {"verified": True}
    monkeypatch.setattr(crm, "book_appointment", book)
    monkeypatch.setattr(crm, "verify_saved_booking", verify)
    state.init_db()  # startup of the new code must retain the old evidence
    result = asyncio.run(agent._tool_book_appointment("post-upgrade", ready_session(), "77000000001", booking_args("15:00")))
    assert result["booking_success"] is False
    assert posts == []


@pytest.mark.parametrize("text", [
    "У меня была операция на позвоночнике, нужна реабилитация",
    "Была операция на коленном суставе, нужна реабилитация",
    "Омыртқаға операция жасалды, оңалту қажет",
])
def test_historical_msk_surgery_with_current_rehab_remains_profile(text):
    assert services.classify_by_keywords(text)["can_help"] is True


@pytest.mark.parametrize("text", ["Приду один", "Без родителей", "Жалғыз келемін"])
def test_adolescent_model_flag_cannot_override_explicit_no_guardian(text):
    session = ready_session(17)
    session.update(last_user_text=text, guardian_accompaniment_confirmed=True)
    agent._tool_record_patient_facts("review", session, {"guardian_accompaniment_confirmed": True})
    assert session["guardian_accompaniment_confirmed"] is False
    assert bot_tools.booking_gate_status(session)[0] is False


def test_upgrade_protects_legacy_doctor_slot_for_a_different_patient(monkeypatch):
    legacy_key = "2031-03-07|14:00|qa_doc|77000000001"
    state.claim_booking(legacy_key, "pre-upgrade")
    state.mark_booking_claim(legacy_key, "uncertain")
    posts = []
    async def book(**kw):
        posts.append(kw)
        return {"ok": True, "appointmentId": "synthetic"}
    async def verify(*a, **kw):
        return {"verified": True}
    monkeypatch.setattr(crm, "book_appointment", book)
    monkeypatch.setattr(crm, "verify_saved_booking", verify)
    result = asyncio.run(agent._tool_book_appointment("other-patient", ready_session(), "77000000002", booking_args()))
    assert result["booking_success"] is False
    assert posts == []


@pytest.mark.parametrize("text", ["Стоп", "Пока", "Позже", "Тоқта"])
def test_stop_or_farewell_is_not_deterministically_captured_as_name(text):
    session = ready_session()
    session.update(selected_time="14:00", last_assistant_answer="Для оформления записи подскажите имя пациента?")
    assert agent._simple_patient_name_candidate(session, text) == ""
