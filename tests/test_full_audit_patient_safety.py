"""Business regressions derived from the approved prompt, with no real providers."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import agent
import ai
import bot_tools
import crm
import services
import state
from config import get_settings


PROFILE_CASES = [
    ("У мамы грыжа позвоночника", "profile"),
    ("У папы артроз колена", "profile"),
    ("У ребёнка болит спина", "profile"),
    ("У ребенка болит спина", "profile"),
    ("Болит поясница", "profile"),
    ("После операции на позвоночнике нужна реабилитация", "profile"),
    ("У жены пяточная шпора", "non_profile"),
    ("Болит пятка при ходьбе", "non_profile"),
    ("Нужен стоматолог", "non_profile"),
    ("У мамы сильная боль", "unclear"),
    ("Хочу записать отца", "unclear"),
    ("Болит спина и пятка", "unclear"),
    ("У жены грыжа, она беременна", "profile"),
    ("Спина не болит, болит только пятка", "non_profile"),
    ("У меня не грыжа, просто болит пятка", "non_profile"),
    ("Болит не пятка, а поясница", "profile"),
    ("Нет боли в пятке, болит поясница", "profile"),
    ("Горло уже прошло, сейчас болит поясница", "profile"),
    ("У мамы болит поясница, противопоказаний у неё нет", "profile"),
    ("Хочу записать ребёнка", "unclear"),
    ("У меня был аппендицит, сейчас болит колено", "profile"),
    ("После операции на желудке нужна реабилитация", "non_profile"),
    ("После операции нужна реабилитация", "unclear"),
    ("Болит зуб и поясница", "unclear"),
    ("У мамы болит спена", "profile"),
    ("Анамның белі ауырады", "profile"),
    ("Әкемнің тізесі ауырады", "profile"),
    ("Баламның арқасы ауырады", "profile"),
    ("Жұбайымның өкшесі ауырады", "non_profile"),
    ("Менің белім ауырмайды, тек өкшем ауырады", "non_profile"),
    ("Өкшем ауырмайды, белім ауырады", "profile"),
    ("Анам қатты ауырады", "unclear"),
    ("Балама жазылғым келеді", "unclear"),
    ("Маған тіс дәрігері керек", "non_profile"),
    ("Арқам және өкшем ауырады", "unclear"),
]


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "patient.sqlite3"))
    get_settings.cache_clear()
    state.init_db()
    monkeypatch.setattr(agent, "_log", lambda *a, **kw: None)
    yield
    get_settings.cache_clear()


def run(coro):
    return asyncio.run(coro)


def ready_session(age=34):
    session = {"language": "ru", "age": age, "phone": "77000000001",
               "complaint": "Болит поясница", "profile_status": "profile",
               "complaint_gate": "COMPLAINT_OK", "contraindications_ok": True,
               "contraindications_verdict": "proceed", "contraindications_raw": "ничего из перечисленного нет"}
    agent._remember_offered_slots(session, [
        {"doctor_login": "qa_doc", "doctor_name": "Тестовый Врач", "date": "2031-03-07", "time_start": "14:00"},
        {"doctor_login": "qa_doc", "doctor_name": "Тестовый Врач", "date": "2031-03-07", "time_start": "15:00"},
    ])
    session["last_slots"] = list(session["crm_offered_slots"].values())
    return session


def booking_args(time="14:00", **extra):
    return {"patient_name": "Тест", "doctor_login": "qa_doc", "date": "2031-03-07", "time_start": time, **extra}


@pytest.mark.parametrize("text,status", PROFILE_CASES)
def test_active_patient_complaint_has_conservative_profile(text, status):
    verdict = services.classify_by_keywords(text)
    actual = {True: "profile", False: "non_profile", None: "unclear"}[verdict["can_help"]]
    assert actual == status


def test_unclear_complaint_keeps_supplied_facts_but_gate_stays_closed():
    session = {}
    result = agent._tool_record_patient_facts("qa", session, {
        "complaint": "У мамы сильная боль", "age": 62, "patient_relation": "mother",
        "contraindications_clear": True, "contraindications_note": "У мамы ничего из перечисленного нет",
        "preferred_date_text": "в пятницу", "time_preference": "после обеда",
    })
    assert result["profile_status"] == "unclear"
    assert session["age"] == 62
    assert session["contraindications_ok"] is True
    assert session["preferred_date_text"] == "в пятницу"
    assert bot_tools.booking_gate_status(session)[0] is False
    agent._tool_record_patient_facts("qa", session, {"complaint": "Болит поясница"})
    assert session["age"] == 62 and session["contraindications_ok"] is True


def test_patient_switch_clears_medical_facts_name_and_selected_slot():
    session = ready_session()
    session.update(patient_name="Отправитель", selected_time="14:00", selected_date="2031-03-07",
                   selected_doctor_login="qa_doc", known_user_facts={"age": 34, "patient_name": "Отправитель"})
    agent._tool_record_patient_facts("qa", session, {"patient_relation": "mother", "complaint": "У мамы артроз"})
    assert not session.get("age")
    assert not session.get("patient_name")
    assert session.get("contraindications_ok") is not True
    assert not session.get("selected_time") and not session.get("crm_offered_slots")
    assert session["phone"] == "77000000001"
    assert session["patient_context_version"] == 1
    assert "age" not in session["known_user_facts"]


def test_same_relation_alias_does_not_reset_mothers_age():
    session = {"patient_relation": "mother", "age": 62, "complaint": "Болит поясница", "profile_status": "profile"}
    agent._tool_record_patient_facts("qa", session, {"patient_relation": "мама"})
    assert session["age"] == 62
    assert session["patient_relation"] == "mother"


def test_explicit_self_clears_previous_relative_context():
    session = {"patient_relation": "mother", "age": 62, "patient_name": "Мать"}
    agent._tool_record_patient_facts("qa", session, {"patient_relation": "", "complaint": "Болит спина", "age": 34})
    assert session.get("patient_relation", "") == ""
    assert session["age"] == 34
    assert not session.get("patient_name")


@pytest.mark.parametrize("note", ["У меня кардиостимулятор", "У жены беременность", "У пациента эпилепсия", "У мамы тромбоз", "Жүктімін", "Кардиостимулятор бар"])
def test_positive_contraindication_overrides_model_clear_flag(note):
    session = ready_session()
    agent._tool_record_patient_facts("qa", session, {"contraindications_clear": True, "contraindications_note": note})
    assert session.get("contraindications_ok") is not True
    assert bot_tools.booking_gate_status(session)[0] is False


@pytest.mark.parametrize("note", ["Что такое кардиостимулятор?", "Кардиостимулятор деген не?", "Не знаю, есть ли тромбоз"])
def test_question_or_uncertainty_is_not_medical_clearance(note):
    session = ready_session()
    session.update(contraindications_ok=None, contraindications_verdict="")
    agent._tool_record_patient_facts("qa", session, {"contraindications_clear": True, "contraindications_note": note})
    assert session.get("contraindications_ok") is not True


@pytest.mark.parametrize("note", ["Беременности нет, противопоказаний нет", "Ничего из перечисленного нет", "Қарсы көрсетілімдер жоқ", "Кардиостимулятор жоқ, қарсы көрсетілімдер жоқ"])
def test_explicit_absence_is_preserved(note):
    session = ready_session()
    agent._tool_record_patient_facts("qa", session, {"contraindications_clear": True, "contraindications_note": note})
    assert session["contraindications_ok"] is True


@pytest.mark.parametrize("age", [16, 17, 18])
def test_adolescent_cannot_book_without_explicit_accompaniment(monkeypatch, age):
    posts = []
    async def book(**kw):
        posts.append(kw)
        return {"ok": True, "appointmentId": "qa"}
    monkeypatch.setattr(crm, "book_appointment", book)
    result = run(agent._tool_book_appointment("qa", ready_session(age), "77000000001", booking_args()))
    assert result["booking_success"] is False
    assert result["error"] == "guardian_accompaniment_required"
    assert posts == []


def test_unverified_relative_never_reuses_senders_identity(monkeypatch):
    posts = []
    async def book(**kw):
        posts.append(kw)
        return {"ok": True, "appointmentId": "qa"}
    monkeypatch.setattr(crm, "book_appointment", book)
    session = ready_session(62)
    session["patient_relation"] = "mother"
    result = run(agent._tool_book_appointment("qa", session, "77000000001", booking_args(patient_relation="mother")))
    assert result["booking_success"] is False
    assert result["error"] == "relative_identity_unverified"
    assert posts == []


@pytest.mark.parametrize("session", [{}, {"complaint": "Болит пятка", "age": 34, "contraindications_ok": True}, ready_session(12), ready_session(17), ready_session(79)])
def test_availability_is_not_shown_before_all_medical_gates(monkeypatch, session):
    calls = []
    async def slots(*a, **kw):
        calls.append((a, kw))
        return {"slots": [{"date": "2031-03-07", "timeStart": "14:00", "doctorLogin": "qa_doc", "doctorName": "Тест"}]}
    monkeypatch.setattr(crm, "check_slots", slots)
    result = run(agent._tool_get_available_slots("qa", session.copy(), {"date_from": "2031-03-07"}))
    assert result["ok"] is False
    assert result.get("slots", []) == []
    assert calls == []


@pytest.mark.parametrize("text", ["Не в 14:00", "14:00 не подходит", "В 14:00 не могу", "В 14:00?", "14:00 болмайды", "14:00-ге жазбаңыз"])
def test_rejected_or_questioned_time_is_not_selection(text):
    session = ready_session()
    assert agent._resolve_explicit_slot_choice(session, text) is None
    assert not session.get("selected_time")


def test_uncertain_post_blocks_a_different_slot_even_with_fresh_session(monkeypatch):
    posts = []
    async def book(**kw):
        posts.append(kw)
        raise TimeoutError("synthetic")
    monkeypatch.setattr(crm, "book_appointment", book)
    first = run(agent._tool_book_appointment("qa", ready_session(), "77000000001", booking_args()))
    second = run(agent._tool_book_appointment("qa-restart", ready_session(), "77000000001", booking_args("15:00")))
    assert first["booking_success"] is False
    assert second["booking_success"] is False
    assert len(posts) == 1


def test_term_explanation_survives_full_contraindications_enforcement():
    session = {"language": "ru", "age": 34, "profile_status": "profile"}
    reply = agent._enforce_approved_contraindications_reply(session, "Что такое кардиостимулятор?", "Кардиостимулятор — устройство, которое помогает поддерживать ритм сердца.")
    assert "устройство" in reply
    assert "дефибриллятор" in reply and "кохлеар" in reply


def test_kazakh_contraindications_cannot_be_abbreviated():
    reply = agent._enforce_approved_contraindications_reply({"language": "kk", "age": 34, "profile_status": "profile"}, "Қандай қарсы көрсетілімдер бар?", "Жүктілік жоқ па?")
    for term in ["кардиостимулятор", "дефибриллятор", "инсулин", "кохлеар", "тромб", "эпилеп", "тиреотокс"]:
        assert term in reply.lower()


def test_existing_ready_state_cannot_book_before_current_refusal_is_read(monkeypatch):
    posts = []
    async def book(**kw):
        posts.append(kw)
        return {"ok": True, "appointmentId": "qa"}
    async def verify(*a, **kw):
        return {"verified": True}
    async def create(**kw):
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="Хорошо, запись не создаю.", tool_calls=[]))])
    monkeypatch.setattr(crm, "book_appointment", book)
    monkeypatch.setattr(crm, "verify_saved_booking", verify)
    monkeypatch.setattr(agent, "agent_skip_reason", lambda *a: "")
    monkeypatch.setattr(ai, "_openai_client", lambda *a: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    monkeypatch.setattr(agent.ai_budget, "check_allowed", lambda *a: (True, ""))
    monkeypatch.setattr(agent.ai_budget, "record_usage", lambda *a, **kw: {})
    session = ready_session()
    session.update(patient_name="Тест", selected_time="14:00", selected_date="2031-03-07", selected_doctor_login="qa_doc")
    result = run(agent.run_agent_turn(chat_id="qa", phone="77000000001", session=session, user_text="Пока не записывайте"))
    assert posts == []
    assert not result.booked


@pytest.mark.parametrize("source", ["Я Тест", "Нет беременности", "У меня кардиостимулятор, можно записаться?", "Эпилепсия была раньше, сейчас приступов нет"])
def test_model_boolean_cannot_replace_explicit_full_clearance(source):
    session = ready_session()
    session.update(last_user_text=source, contraindications_ok=None, contraindications_verdict="")
    agent._tool_record_patient_facts("qa", session, {"contraindications_clear": True})
    assert session.get("contraindications_ok") is not True


def test_positive_condition_in_complaint_overrides_contradictory_clearance():
    session = ready_session()
    agent._tool_record_patient_facts("qa", session, {"complaint": "Болит поясница, беременна", "contraindications_clear": True, "contraindications_note": "нет"})
    assert session["profile_status"] == "profile"
    assert session.get("contraindications_ok") is not True


@pytest.mark.parametrize("age", ["not-age", -1, 0, 16.9])
def test_invalid_stored_age_cannot_reach_booking(monkeypatch, age):
    posts = []
    async def book(**kw):
        posts.append(kw)
        return {"ok": True, "appointmentId": "qa"}
    monkeypatch.setattr(crm, "book_appointment", book)
    result = run(agent._tool_book_appointment("qa", ready_session(age), "77000000001", booking_args()))
    assert result["booking_success"] is False
    assert posts == []


@pytest.mark.parametrize("source,want", [("Болит пятка", "non_profile"), ("Болит спина и пятка", "unclear"), ("Спина не болит, болит пятка", "non_profile")])
def test_raw_scope_cannot_be_removed_from_extracted_complaint(source, want):
    session = ready_session()
    session["last_user_text"] = source
    result = agent._tool_record_patient_facts("qa", session, {"complaint": "Болит спина"})
    assert result["profile_status"] == want
    assert bot_tools.booking_gate_status(session)[0] is False


def test_clear_affirmative_time_choice_remains_supported():
    session = ready_session()
    chosen = agent._resolve_explicit_slot_choice(session, "Давайте на 14:00")
    assert chosen is not None and chosen["time_start"] == "14:00"
