from __future__ import annotations

import asyncio
import json
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

import ai
import crm
import dialog
import main
import state
from dialog import handle_message

state.init_db()


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def reset(chat_id: str, preset: dict[str, Any] | None = None) -> None:
    state.reset_session(chat_id)
    session = state.get_session(chat_id)
    session.update({"ai_lead_started": True, "gate_reason": "active_ai_lead"})
    if preset:
        session.update(preset)
    state.save_session(chat_id, session)


def brain(action: str, reply: str = "ок", extracted: dict[str, Any] | None = None, tool: str = "none") -> tuple[dict[str, Any], dict[str, Any]]:
    decision = {
        "action": action,
        "reply": reply,
        "extracted": {
            "complaint": "", "age": None, "contraindications_clear": None,
            "contraindication_red_flags": [], "preferred_date_text": "", "time_preference": "", "slot_choice": None,
            "patient_name": "", "faq_type": "", "language": "ru",
            **(extracted or {}),
        },
        "needs_python_tool": tool,
        "safety_flags": {"promised_cure": False, "asked_name_too_early": False, "offered_date_before_contra": False, "medical_diagnosis": False},
    }
    debug = {"openai_brain_used": True, "openai_brain_action": action, "openai_brain_needs_python_tool": tool, "openai_brain_extracted": decision["extracted"]}
    return decision, debug


class FakeChoice:
    def __init__(self, content: str):
        self.message = type("Msg", (), {"content": content})()


class FakeCompletions:
    def __init__(self, content: str | Exception):
        self.content = content

    async def create(self, **kwargs: Any):
        if isinstance(self.content, Exception):
            raise self.content
        return type("Resp", (), {"choices": [FakeChoice(self.content)]})()


class FakeClient:
    def __init__(self, content: str | Exception):
        self.chat = type("Chat", (), {"completions": FakeCompletions(content)})()


def test_brain_valid_json_parses(monkeypatch: Any) -> None:
    payload = brain("ask_age", "МРТ заранее не обязательно. Сколько Вам лет?", {"complaint": "поясница", "faq_type": "mri"})[0]
    monkeypatch.setattr(ai.get_settings(), "openai_api_key", "test-key")
    monkeypatch.setattr(ai, "AsyncOpenAI", object)
    monkeypatch.setattr(ai, "_openai_client", lambda key: FakeClient(json.dumps(payload, ensure_ascii=False)))
    decision, debug = run(ai.run_openai_dialog_brain(user_text="мрт?", session={"step": "complaint"}))
    assert decision["action"] == "ask_age"
    assert debug["openai_brain_used"] is True


def test_brain_invalid_json_and_exception_fallback(monkeypatch: Any) -> None:
    monkeypatch.setattr(ai.get_settings(), "openai_api_key", "test-key")
    monkeypatch.setattr(ai, "AsyncOpenAI", object)
    monkeypatch.setattr(ai, "_openai_client", lambda key: FakeClient("not json"))
    decision, _ = run(ai.run_openai_dialog_brain(user_text="x", session={}))
    assert decision["action"] == "fallback_rule_based"
    monkeypatch.setattr(ai, "_openai_client", lambda key: FakeClient(RuntimeError("boom")))
    decision, _ = run(ai.run_openai_dialog_brain(user_text="x", session={}))
    assert decision["action"] == "fallback_rule_based"


def test_brain_missing_api_key_no_crash(monkeypatch: Any) -> None:
    monkeypatch.setattr(ai.get_settings(), "openai_api_key", "")
    decision, debug = run(ai.run_openai_dialog_brain(user_text="x", session={}))
    assert decision["action"] == "fallback_rule_based"
    assert debug["openai_brain_fallback_used"] is True


def test_complex_first_message_uses_brain(monkeypatch: Any) -> None:
    chat_id = "brain_complex_first"
    reset(chat_id, {"step": "complaint"})
    async def fake_brain(**kwargs: Any):
        return brain("ask_age", "МРТ заранее делать не обязательно. Подскажите, сколько Вам лет?", {"complaint": "поясница отдаёт в ногу", "faq_type": "mri"})
    monkeypatch.setattr(dialog, "run_openai_dialog_brain", fake_brain)
    answer = run(handle_message(chat_id, "77011234567", "Здравствуйте, поясница бкспокоит, в ногу отдаёт. МРТ старое есть?"))
    session = state.get_session(chat_id)
    assert session["step"] == "age"
    assert "МРТ" in answer and "лет" in answer


def test_age_faq_and_clean_contra_to_date(monkeypatch: Any) -> None:
    chat_id = "brain_age_contra"
    reset(chat_id, {"step": "age", "complaint": "спина"})
    async def age_brain(**kwargs: Any):
        return brain("ask_contraindications", "Безоперационные методы применяются. Есть противопоказания?", {"age": 34, "faq_type": "non_surgical"})
    monkeypatch.setattr(dialog, "run_openai_dialog_brain", age_brain)
    run(handle_message(chat_id, "77011234567", "34. А без операции можно?"))
    assert state.get_session(chat_id)["step"] == "contraindications"
    async def contra_brain(**kwargs: Any):
        return brain("ask_date", "Длительность врач скажет после осмотра. На какой день удобно?", {"contraindications_clear": True, "faq_type": "course_duration"})
    monkeypatch.setattr(dialog, "run_openai_dialog_brain", contra_brain)
    answer = run(handle_message(chat_id, "77011234567", "всё чисто, сколько дней курс?"))
    assert state.get_session(chat_id)["step"] == "date"
    assert "день" in answer.lower()


def test_date_address_checks_slots_and_video_selects_second(monkeypatch: Any) -> None:
    chat_id = "brain_slots_select"
    reset(chat_id, {"step": "date", "contraindications_ok": True, "complaint": "спина", "age": 34})
    calls: list[Any] = []
    async def fake_slots(date: str, doctor_login: str | None = None):
        calls.append(date)
        return {"availability": [{"doctorLogin": "d", "doctorName": "Врач", "availableSlots": ["10:00", "12:00"]}]}
    monkeypatch.setattr(crm, "check_slots", fake_slots)
    async def date_brain(**kwargs: Any):
        return brain("show_slots", "Адрес: Астана, Кабанбай батыра 28.", {"preferred_date_text": "в понедельник", "faq_type": "address"}, "check_slots")
    monkeypatch.setattr(dialog, "run_openai_dialog_brain", date_brain)
    answer = run(handle_message(chat_id, "77011234567", "в понеддельник и адрес"))
    assert calls and state.get_session(chat_id)["step"] == "time"
    assert "Кабанбай" in answer and "10:00" in answer
    async def select_brain(**kwargs: Any):
        return brain("select_slot", "Как на видео может быть, но план врач подбирает после осмотра.", {"slot_choice": 2, "faq_type": "video_procedure"})
    monkeypatch.setattr(dialog, "run_openai_dialog_brain", select_brain)
    run(handle_message(chat_id, "77011234567", "как на видео? давайте 2 вариант"))
    session = state.get_session(chat_id)
    assert session["selected_slot"] == session["last_slots"][1]
    assert session["step"] == "name"


def test_age_message_with_clear_contra_and_date_checks_slots(monkeypatch: Any) -> None:
    chat_id = "brain_real_test_1"
    reset(chat_id, {"step": "age", "complaint": "болит спина"})
    calls: list[str] = []

    async def fake_slots(date: str, doctor_login: str | None = None):
        calls.append(date)
        return {"availability": [{"doctorLogin": "d", "doctorName": "Врач", "availableSlots": ["11:00", "15:00"]}]}

    async def fake_brain(**kwargs: Any):
        return brain(
            "show_slots",
            "Спасибо, поняла 🌿 Проверю свободные окошки на понедельник, не самые ранние.",
            {
                "age": 34,
                "contraindications_clear": True,
                "preferred_date_text": "в понедельник",
                "time_preference": "не рано",
                "slot_choice": None,
                "patient_name": "",
                "faq_type": "",
                "language": "ru",
            },
            "check_slots",
        )

    async def should_not_humanize(**kwargs: Any):
        raise AssertionError("humanize called")

    monkeypatch.setattr(crm, "check_slots", fake_slots)
    monkeypatch.setattr(dialog, "run_openai_dialog_brain", fake_brain)
    monkeypatch.setattr(main, "humanize_reply_with_openai", should_not_humanize)

    raw = run(handle_message(chat_id, "77011234567", "34, все чисто, можно в понеддельник не рано?"))
    answer = run(main._maybe_humanize_answer(chat_id, "34, все чисто, можно в понеддельник не рано?", raw))
    session = state.get_session(chat_id)

    assert session["age"] == 34
    assert session["contraindications_ok"] is True
    assert session["contraindications_raw"] == "все чисто"
    assert "понедельник" in session["preferred_date_text"]
    assert session["time_preference"] == "не рано"
    assert calls
    assert session["step"] == "time"
    assert "противопоказ" not in answer.lower()
    assert "11:00" in answer and "15:00" in answer
    assert session["openai_brain_used"] is True
    assert session["openai_brain_action"] == "show_slots"
    assert session["openai_used"] is True
    assert session["openai_skip_reason"] == ""


def test_age_message_with_no_contra_phrase_date_and_time_keeps_brain_status(monkeypatch: Any) -> None:
    chat_id = "brain_multi_entity_no_contra_phrase"
    reset(chat_id, {"step": "age", "complaint": "поясница болит", "ai_lead_started": True})
    calls: list[str] = []

    async def fake_slots(date: str, doctor_login: str | None = None):
        calls.append(date)
        return {"availability": [{"doctorLogin": "d", "doctorName": "Врач", "availableSlots": ["11:00", "15:00"]}]}

    async def fake_brain(**kwargs: Any):
        return brain(
            "show_slots",
            "Спасибо, проверю свободное время на понедельник.",
            {
                "age": 34,
                "contraindications_clear": True,
                "preferred_date_text": "понедельник",
                "time_preference": "не рано",
            },
            "check_slots",
        )

    monkeypatch.setattr(crm, "check_slots", fake_slots)
    monkeypatch.setattr(dialog, "run_openai_dialog_brain", fake_brain)

    raw = run(handle_message(chat_id, "77011234567", "34, противопоказаний нет, можно в понедельник не рано?"))
    answer = run(main._maybe_humanize_answer(chat_id, "34, противопоказаний нет, можно в понедельник не рано?", raw))
    session = state.get_session(chat_id)

    assert session["age"] == 34
    assert session["contraindications_ok"] is True
    assert session["contraindications_raw"] == "противопоказаний нет"
    assert "понедельник" in session["preferred_date_text"]
    assert session["time_preference"] == "не рано"
    assert session["step"] != "contraindications"
    assert "противопоказ" not in answer.lower()
    assert calls
    assert session["openai_used"] is True
    assert session["openai_brain_used"] is True
    assert session["openai_skip_reason"] == ""


def test_age_message_with_date_without_clear_contra_asks_contra(monkeypatch: Any) -> None:
    chat_id = "brain_age_date_no_clear"
    reset(chat_id, {"step": "age", "complaint": "болит спина"})
    calls: list[str] = []

    async def fake_slots(date: str, doctor_login: str | None = None):
        calls.append(date)
        return {"availability": [{"doctorLogin": "d", "doctorName": "Врач", "availableSlots": ["11:00"]}]}

    async def fake_brain(**kwargs: Any):
        return brain(
            "show_slots",
            "Проверю понедельник.",
            {"age": 34, "contraindications_clear": None, "preferred_date_text": "в понедельник"},
            "check_slots",
        )

    monkeypatch.setattr(crm, "check_slots", fake_slots)
    monkeypatch.setattr(dialog, "run_openai_dialog_brain", fake_brain)

    answer = run(handle_message(chat_id, "77011234567", "34, можно в понедельник?"))
    session = state.get_session(chat_id)

    assert session["age"] == 34
    assert session["step"] == "contraindications"
    assert not calls
    assert "противопоказ" in answer.lower()


def test_guard_blocks_bad_ask_name(monkeypatch: Any) -> None:
    chat_id = "brain_guard_name"
    reset(chat_id, {"step": "age", "complaint": "спина"})
    async def fake_brain(**kwargs: Any):
        return brain("ask_name", "Как Вас зовут?")
    monkeypatch.setattr(dialog, "run_openai_dialog_brain", fake_brain)
    answer = run(handle_message(chat_id, "77011234567", "34"))
    session = state.get_session(chat_id)
    assert session["openai_brain_guard_failed"] is True
    assert session.get("selected_slot") is None
    assert "зовут" not in answer.lower()


def test_refund_booked_old_manual_never_calls_brain(monkeypatch: Any) -> None:
    async def should_not_call(**kwargs: Any):
        raise AssertionError("brain called")
    monkeypatch.setattr(dialog, "run_openai_dialog_brain", should_not_call)
    for chat_id, preset, text in [
        ("brain_refund", {}, "Мама лечилась в рассрочку, когда отменят рассрочку?"),
        ("brain_booked", {"step": "booked", "booked": True}, "спасибо"),
        ("brain_old", {"old_chat": True}, "спина болит"),
        ("brain_manual", {"manual_takeover": True}, "спина болит"),
    ]:
        reset(chat_id, preset)
        run(handle_message(chat_id, "77011234567", text))


def test_booking_python_owned_and_crm_error_escalates(monkeypatch: Any) -> None:
    chat_id = "brain_python_book"
    reset(chat_id, {"step": "name", "complaint": "спина", "age": 34, "contraindications_ok": True, "selected_slot": {"date": "2099-01-01", "time": "10:00", "doctorLogin": "d", "doctorName": "Врач"}})
    called = {"brain": 0, "book": 0}
    async def fake_brain(**kwargs: Any):
        called["brain"] += 1
        return brain("ask_name", "name")
    async def fake_book(**kwargs: Any):
        called["book"] += 1
        raise crm.CRMError("500")
    monkeypatch.setattr(dialog, "run_openai_dialog_brain", fake_brain)
    monkeypatch.setattr(crm, "book_appointment", fake_book)
    answer = run(handle_message(chat_id, "77011234567", "Алия"))
    session = state.get_session(chat_id)
    assert called["brain"] == 0 and called["book"] == 1
    assert session["step"] == "escalated"
    assert ("администратор" in answer.lower()) or ("әкімші" in answer.lower())
    later = run(handle_message(chat_id, "77011234567", "?"))
    assert later == ""


def test_humanize_not_called_when_brain_reply_used(monkeypatch: Any) -> None:
    chat_id = "brain_no_humanize"
    reset(chat_id, {"step": "complaint"})
    async def fake_brain(**kwargs: Any):
        return brain("ask_age", "Подскажите, сколько Вам лет?", {"complaint": "спина"})
    async def should_not_humanize(**kwargs: Any):
        raise AssertionError("humanize called")
    monkeypatch.setattr(dialog, "run_openai_dialog_brain", fake_brain)
    monkeypatch.setattr(main, "humanize_reply_with_openai", should_not_humanize)
    raw = run(handle_message(chat_id, "77011234567", "спина болит"))
    answer = run(main._maybe_humanize_answer(chat_id, "спина болит", raw))
    assert answer == raw
    session = state.get_session(chat_id)
    assert session["openai_used"] is True
    assert session["openai_skip_reason"] == ""


def test_production_age_clear_date_typo_flow_python_fallback(monkeypatch: Any) -> None:
    chat_id = "production_multi_entity_fallback"
    state.reset_session(chat_id)
    calls: list[str] = []

    async def fake_slots(date: str, doctor_login: str | None = None):
        calls.append(date)
        return {"availability": [{"doctorLogin": "d", "doctorName": "Врач", "date": date, "availableSlots": ["11:00", "15:00"]}]}

    async def fallback_brain(**kwargs: Any):
        return brain("fallback_rule_based", "", {}, "none")

    monkeypatch.setattr(crm, "check_slots", fake_slots)
    monkeypatch.setattr(dialog, "run_openai_dialog_brain", fallback_brain)

    first = run(handle_message(chat_id, "77011234567", "Здравствуйте, у меня поясница бкспокоит, иногда в ногу отдаёт. МРТ старое есть, надо новое делать?"))
    session = state.get_session(chat_id)
    assert session["step"] == "age"
    assert session["ai_lead_started"] is True
    assert "лет" in first.lower() or "жас" in first.lower()

    second = run(handle_message(chat_id, "77011234567", "34, все чисто, можно в понеддельник не рано?"))
    session = state.get_session(chat_id)
    assert session["age"] == 34
    assert session["contraindications_ok"] is True
    assert session["time_preference"] == "не рано"
    assert calls
    assert session["step"] == "time"
    assert session["last_slots"]
    assert session["openai_brain_fallback_used"] is True
    assert "противопоказ" not in second.lower()
    assert "11:00" in second and "15:00" in second


def test_production_age_date_without_clear_keeps_contra_gate(monkeypatch: Any) -> None:
    chat_id = "production_no_clear_contra_gate"
    reset(chat_id, {"step": "age", "complaint": "болит спина"})
    calls: list[str] = []

    async def fake_slots(date: str, doctor_login: str | None = None):
        calls.append(date)
        return {"availability": [{"doctorLogin": "d", "doctorName": "Врач", "date": date, "availableSlots": ["11:00"]}]}

    async def fallback_brain(**kwargs: Any):
        return brain("fallback_rule_based", "", {}, "none")

    monkeypatch.setattr(crm, "check_slots", fake_slots)
    monkeypatch.setattr(dialog, "run_openai_dialog_brain", fallback_brain)

    answer = run(handle_message(chat_id, "77011234567", "34, можно в понедельник?"))
    session = state.get_session(chat_id)
    assert session["age"] == 34
    assert session.get("contraindications_ok") is not True
    assert not calls
    assert session["step"] == "contraindications"
    assert "противопоказ" in answer.lower()


def test_contraindication_term_question_is_not_hard_stop(monkeypatch: Any) -> None:
    chat_id = "brain_term_question"
    reset(chat_id, {"step": "contraindications", "age": 34})

    async def fake_brain(**kwargs: Any):
        return brain(
            "answer_faq_and_continue",
            "Кохлеарный имплант — это электронное устройство для слуха. Подскажите, у Вас его нет?",
            {"contraindication_term_asked": "кохлеарный имплант"},
        )

    monkeypatch.setattr(dialog, "run_openai_dialog_brain", fake_brain)
    answer = run(handle_message(chat_id, "77011234567", "Что такое кохлеарный имплант?"))
    session = state.get_session(chat_id)
    assert session["step"] == "contraindications"
    assert session.get("manual_takeover") is not True
    assert session.get("hard_contraindication_stop") is not True
    assert "устройство" in answer.lower()
    assert "у вас" in answer.lower()


def test_real_contraindication_stops_booking_after_brain_fallback(monkeypatch: Any) -> None:
    chat_id = "brain_real_contra"
    reset(chat_id, {"step": "contraindications", "age": 34})

    async def fallback_brain(**kwargs: Any):
        return brain("fallback_rule_based", "", {}, "none")

    monkeypatch.setattr(dialog, "run_openai_dialog_brain", fallback_brain)
    answer = run(handle_message(chat_id, "77011234567", "У меня есть кохлеарный имплант"))
    session = state.get_session(chat_id)
    assert session["step"] == "stopped"
    assert session.get("contraindications_ok") is False
    assert "противопоказ" in answer.lower()


def test_faq_on_time_step_preserves_slots(monkeypatch: Any) -> None:
    chat_id = "brain_time_faq"
    slots = [
        {"date": "2099-01-01", "time": "10:00", "doctorLogin": "d", "doctorName": "Врач"},
        {"date": "2099-01-01", "time": "12:00", "doctorLogin": "d", "doctorName": "Врач"},
    ]
    reset(chat_id, {"step": "time", "last_slots": slots, "contraindications_ok": True})

    async def fake_brain(**kwargs: Any):
        return brain(
            "answer_faq_and_continue",
            "Длительность зависит от врача и процедуры, обычно точнее скажут после осмотра.",
            {"faq_type": "duration"},
        )

    monkeypatch.setattr(dialog, "run_openai_dialog_brain", fake_brain)
    answer = run(handle_message(chat_id, "77011234567", "А сколько длится процедура?"))
    session = state.get_session(chat_id)
    assert session["step"] == "time"
    assert session["last_slots"] == slots
    assert "длительность" in answer.lower()
    assert "какое время" in answer.lower()


def test_ask_human_sets_manual_takeover_and_next_message_silent(monkeypatch: Any) -> None:
    chat_id = "brain_ask_human"
    reset(chat_id, {"step": "age", "complaint": "спина"})

    async def fake_brain(**kwargs: Any):
        return brain("handoff_admin", "Хорошо, передаю администратору. Он подключится и ответит Вам 🌿", {"wants_human": True}, "handoff_admin")

    monkeypatch.setattr(dialog, "run_openai_dialog_brain", fake_brain)
    answer = run(handle_message(chat_id, "77011234567", "Позови человека"))
    session = state.get_session(chat_id)
    assert session["manual_takeover"] is True
    assert session["step"] == "escalated"
    assert "администратор" in answer.lower()
    assert run(handle_message(chat_id, "77011234567", "вы тут?")) == ""


def test_slot_slang_second_variant_selects_second_slot(monkeypatch: Any) -> None:
    chat_id = "brain_slot_slang"
    slots = [
        {"date": "2099-01-01", "time": "10:00", "doctorLogin": "d", "doctorName": "Врач"},
        {"date": "2099-01-01", "time": "12:00", "doctorLogin": "d", "doctorName": "Врач"},
    ]
    reset(chat_id, {"step": "time", "last_slots": slots, "contraindications_ok": True})

    async def fallback_brain(**kwargs: Any):
        return brain("fallback_rule_based", "", {}, "none")

    monkeypatch.setattr(dialog, "run_openai_dialog_brain", fallback_brain)
    run(handle_message(chat_id, "77011234567", "2 варик"))
    session = state.get_session(chat_id)
    assert session["selected_slot"] == slots[1]
    assert session["step"] == "name"


def test_crm_empty_availability_keeps_date_and_clears_slots(monkeypatch: Any) -> None:
    chat_id = "reg_empty_availability"
    reset(chat_id, {"step": "date", "contraindications_ok": True, "last_slots": [{"time": "10:00"}]})

    async def fake_slots(date: str, doctor_login: str | None = None) -> dict[str, Any]:
        return {"availability": []}

    monkeypatch.setattr(crm, "check_slots", fake_slots)
    session = state.get_session(chat_id)
    answer = run(dialog._show_slots(chat_id, session, "2026-06-28"))
    assert "10:00" not in answer and "12:00" not in answer and "14:00" not in answer
    assert "есть свободные слоты" not in answer.lower()
    assert "свободных окошек не нашла" in answer or "другой" in answer.lower()
    assert session["step"] == "date"
    assert session.get("last_slots") == []


def test_llm_hallucinated_slots_blocked_when_crm_empty(monkeypatch: Any) -> None:
    chat_id = "reg_llm_hallucinated_slots"
    reset(chat_id, {"step": "date", "contraindications_ok": True})
    events: list[str] = []
    monkeypatch.setattr(state, "log_event", lambda chat_id, event, payload: events.append(event))

    async def fake_slots(date: str, doctor_login: str | None = None) -> dict[str, Any]:
        return {"availability": []}

    async def fake_brain(**kwargs: Any):
        return brain(
            "show_slots",
            "Показываю свободные слоты: 10:00, 12:00, 14:00",
            {"preferred_date_text": "в понедельник"},
            "check_slots",
        )

    monkeypatch.setattr(crm, "check_slots", fake_slots)
    monkeypatch.setattr(dialog, "run_openai_dialog_brain", fake_brain)
    answer = run(handle_message(chat_id, "77011234567", "в понедельник"))
    assert "10:00" not in answer and "12:00" not in answer and "14:00" not in answer
    assert "свободных окошек не нашла" in answer
    assert "llm_slot_hallucination_blocked" in events or "crm_slots_empty" in events
    assert state.get_session(chat_id)["step"] == "date"


def test_active_llm_asked_name_too_early_is_repaired(monkeypatch: Any) -> None:
    chat_id = "repair_name_too_early"
    reset(chat_id, {"step": "age", "complaint": "болит спина"})

    async def fake_brain(**kwargs: Any):
        return brain("ask_name", "Как Вас зовут?", {})

    monkeypatch.setattr(dialog, "run_openai_dialog_brain", fake_brain)
    answer = run(handle_message(chat_id, "77011234567", "можно записаться?"))
    session = state.get_session(chat_id)
    assert "Сколько Вам лет" in answer
    assert "имя" not in answer.lower()
    assert session["step"] == "age"
    assert session["llm_blocked"] is True
    assert session["llm_repaired"] is True
    assert session["repair_reason"] == "asked_name_too_early"


def test_active_llm_date_before_contra_is_repaired_without_crm(monkeypatch: Any) -> None:
    chat_id = "repair_date_before_contra"
    reset(chat_id, {"step": "age", "complaint": "болит спина"})
    calls: list[str] = []

    async def fake_slots(date: str, doctor_login: str | None = None) -> dict[str, Any]:
        calls.append(date)
        return {"availability": [{"doctorLogin": "d", "doctorName": "Врач", "availableSlots": ["11:00"]}]}

    async def fake_brain(**kwargs: Any):
        return brain("show_slots", "Покажу слоты на понедельник", {"age": 34, "preferred_date_text": "понедельник"}, "check_slots")

    monkeypatch.setattr(crm, "check_slots", fake_slots)
    monkeypatch.setattr(dialog, "run_openai_dialog_brain", fake_brain)
    answer = run(handle_message(chat_id, "77011234567", "34, в понедельник"))
    session = state.get_session(chat_id)
    assert "Противопоказаний из списка нет" in answer
    assert session["step"] == "contraindications"
    assert session["age"] == 34
    assert calls == []
    assert session["repair_reason"] == "date_before_contraindications"


def test_active_llm_false_hard_stop_is_repaired(monkeypatch: Any) -> None:
    chat_id = "repair_false_hard_stop"
    reset(chat_id, {"step": "contraindications", "complaint": "спина", "age": 34})

    async def fake_brain(**kwargs: Any):
        return brain("stop_contraindication", "К сожалению, записать не можем", {})

    monkeypatch.setattr(dialog, "run_openai_dialog_brain", fake_brain)
    answer = run(handle_message(chat_id, "77011234567", "нет"))
    session = state.get_session(chat_id)
    assert "для безопасности" in answer
    assert session["step"] == "contraindications"
    assert session.get("manual_takeover") is not True
    assert session.get("hard_contraindication_stop") is not True
    assert session["repair_reason"] == "contraindication_false_hard_stop"


def test_active_llm_invalid_json_is_repaired(monkeypatch: Any) -> None:
    chat_id = "repair_invalid_json"
    reset(chat_id, {"step": "date", "complaint": "спина", "age": 34, "contraindications_ok": True})

    async def fake_brain(**kwargs: Any):
        return (
            {"action": "fallback_rule_based", "reply": "", "extracted": {}, "needs_python_tool": "none"},
            {"openai_brain_used": False, "openai_brain_skip_reason": "invalid_json", "openai_brain_fallback_used": True},
        )

    monkeypatch.setattr(dialog, "run_openai_dialog_brain", fake_brain)
    answer = run(handle_message(chat_id, "77011234567", "хочу записаться"))
    session = state.get_session(chat_id)
    assert "На какой день Вам удобно прийти" in answer
    assert session["step"] == "date"
    assert session["repair_reason"] == "unknown_invalid_llm"


def test_real_crm_slots_only_are_saved(monkeypatch: Any) -> None:
    chat_id = "reg_real_slots_only"
    reset(chat_id, {"step": "date", "contraindications_ok": True})

    async def fake_slots(date: str, doctor_login: str | None = None) -> dict[str, Any]:
        return {"availability": [{"doctorLogin": "zhuma_md", "doctorName": "Жумабеков М.", "date": "2026-06-22", "availableSlots": ["09:20", "10:40"]}]}

    monkeypatch.setattr(crm, "check_slots", fake_slots)
    session = state.get_session(chat_id)
    answer = run(dialog._show_slots(chat_id, session, "2026-06-22"))
    assert "09:20" in answer and "10:40" in answer
    for fake_time in ("10:00", "12:00", "14:00"):
        assert fake_time not in answer
    assert session["step"] == "time"
    assert len(session["last_slots"]) == 2
    assert all(slot["doctorLogin"] == "zhuma_md" and slot["date"] == "2026-06-22" and slot["timeStart"] in {"09:20", "10:40"} for slot in session["last_slots"])


def test_doctor_names_without_slots_are_safe() -> None:
    chat_id = "reg_doctor_names_without_slots"
    reset(chat_id, {"step": "date", "last_slots": []})
    answer = run(handle_message(chat_id, "77011234567", "а имена их можно?"))
    assert "Врач зависит от выбранного дня" in answer
    assert "какой день" in answer
    assert "Жумабеков" not in answer and "Иван" not in answer


def test_doctor_names_from_last_slots_only() -> None:
    chat_id = "reg_doctor_names_with_slots"
    reset(chat_id, {"step": "time", "last_slots": [
        {"doctorName": "Жумабеков М.", "doctorLogin": "zhuma_md", "date": "2026-06-22", "timeStart": "09:20", "time": "09:20"},
        {"doctorName": "Садыкова А.", "doctorLogin": "sad", "date": "2026-06-22", "timeStart": "10:40", "time": "10:40"},
    ]})
    answer = run(handle_message(chat_id, "77011234567", "а врачи кто?"))
    assert "Жумабеков М." in answer and "Садыкова А." in answer
    assert "Иван" not in answer


def test_new_lead_first_message_start_allows_brain(monkeypatch: Any) -> None:
    chat_id = "new_lead_first_start_brain"
    state.reset_session(chat_id)

    async def fake_brain(**kwargs: Any):
        assert kwargs["session"].get("gate_reason") == "new_lead"
        assert kwargs["session"].get("step") in (None, "start")
        return brain("ask_age", "Поняла, поясница болит. Подскажите, пожалуйста, сколько Вам лет?", {"complaint": "поясница болит"})

    monkeypatch.setattr(dialog, "run_openai_dialog_brain", fake_brain)
    answer = run(handle_message(chat_id, "77011234567", "Здравствуйте, хочу записаться. Поясница болит"))
    session = state.get_session(chat_id)
    assert session["openai_brain_used"] is True
    assert session.get("openai_brain_skip_reason") in ("", None)
    assert session["step"] == "age"
    assert "лет" in answer.lower()


def test_start_and_name_steps_are_allowed_for_active_ai_lead() -> None:
    assert dialog._openai_brain_skip_reason({"step": "start", "gate_reason": "new_lead"}, "хочу записаться") == ""
    assert dialog._openai_brain_skip_reason({"step": "name", "ai_lead_started": True}, "Алия") == ""


def test_protected_and_working_hours_flows_still_skip_brain() -> None:
    assert dialog._openai_brain_skip_reason({"step": "date", "ai_lead_started": True, "booked": True}, "завтра") == "booked_or_handoff"
    assert dialog._openai_brain_skip_reason({"step": "date", "ai_lead_started": True, "manual_takeover": True}, "завтра") == "manual_or_muted"
    assert dialog._openai_brain_skip_reason({"step": "date", "ai_lead_started": True, "escalated": True}, "завтра") == "manual_or_muted"


def test_openai_error_debug_contains_type_and_message(monkeypatch: Any) -> None:
    monkeypatch.setattr(ai.get_settings(), "openai_api_key", "test-key")
    monkeypatch.setattr(ai, "AsyncOpenAI", object)
    err = RuntimeError("boom from openai")
    monkeypatch.setattr(ai, "_openai_client", lambda key: FakeClient(err))
    decision, debug = run(ai.run_openai_dialog_brain(user_text="x", session={"chat_id": "err_debug"}))
    assert decision["action"] == "fallback_rule_based"
    assert debug["openai_error_type"] == "RuntimeError"
    assert "boom from openai" in debug["openai_error_message_preview"]
    assert debug["openai_error_detail"]["model"]


def test_debug_chat_force_new_lead_empty_brain_reply_is_repaired(monkeypatch: Any) -> None:
    from fastapi.testclient import TestClient

    chat_id = "debug_brain_live_1"
    state.reset_session(chat_id)

    async def empty_brain(**kwargs: Any):
        return (
            {"action": "fallback_rule_based", "reply": "", "extracted": {}, "needs_python_tool": "none"},
            {"openai_brain_used": False, "openai_brain_skip_reason": "empty_reply", "openai_brain_fallback_used": True},
        )

    monkeypatch.setattr(dialog, "run_openai_dialog_brain", empty_brain)
    monkeypatch.setattr(main, "is_bot_work_time", lambda: False)

    response = TestClient(main.app).post(
        "/debug/chat",
        json={
            "chat_id": chat_id,
            "phone": "77000008881",
            "text": "Здравствуйте, хочу записаться. Поясница болит",
            "force": True,
        },
    )
    data = response.json()
    assert response.status_code == 200
    assert data["answer"] != ""
    assert data["session"]["step"] == "age"
    assert data["debug"]["no_reply_reason"] == ""
    assert data["debug"]["llm_repaired"] is True
    assert data["debug"]["repair_reason"] == "empty_active_reply"
    assert data["debug"]["repaired_step"] == "age"


def test_active_new_lead_openai_error_gets_safe_fallback(monkeypatch: Any) -> None:
    chat_id = "active_openai_error_repair"
    reset(chat_id, {"step": "start", "gate_reason": "new_lead"})

    async def error_brain(**kwargs: Any):
        return (
            {"action": "fallback_rule_based", "reply": "", "extracted": {}, "needs_python_tool": "none"},
            {"openai_brain_used": False, "openai_brain_skip_reason": "openai_error", "openai_brain_fallback_used": True},
        )

    monkeypatch.setattr(dialog, "run_openai_dialog_brain", error_brain)
    answer = run(handle_message(chat_id, "77000008881", "Здравствуйте, хочу записаться. Поясница болит"))
    session = state.get_session(chat_id)
    assert answer != ""
    assert "сколько Вам лет" in answer
    assert session["step"] == "age"
    assert session["llm_repaired"] is True


def test_active_new_lead_brain_skipped_not_allowed_step_no_visible_silence(monkeypatch: Any) -> None:
    chat_id = "active_not_allowed_repair"
    reset(chat_id, {"step": "weird_internal_step", "gate_reason": "active_conversation_reply"})

    async def should_not_call_brain(**kwargs: Any):
        raise AssertionError("Brain must be skipped before OpenAI call")

    monkeypatch.setattr(dialog, "run_openai_dialog_brain", should_not_call_brain)
    answer = run(handle_message(chat_id, "77000008881", "Здравствуйте, хочу записаться. Поясница болит"))
    session = state.get_session(chat_id)
    assert answer != ""
    assert session["step"] == "age"
    assert session["repair_reason"] == "empty_active_reply"


def test_debug_chat_working_hours_force_false_still_silent(monkeypatch: Any) -> None:
    from fastapi.testclient import TestClient

    chat_id = "debug_working_hours_silent"
    state.reset_session(chat_id)
    monkeypatch.setattr(main, "is_bot_work_time", lambda: False)

    response = TestClient(main.app).post(
        "/debug/chat",
        json={"chat_id": chat_id, "phone": "77000008881", "text": "Здравствуйте, хочу записаться. Поясница болит", "force": False},
    )
    data = response.json()
    assert response.status_code == 200
    assert data["answer"] == ""
    assert data["debug"]["no_reply_reason"] == "working_hours_ai_disabled"


def test_brain_receives_full_dialog_context(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}
    payload = {
        "understood_context": {"patient_meaning": "ответил возрастом", "is_answer_to_last_question": True},
        "intent": "age_answer",
        "entities": {"age": 34, "language": "ru"},
        "next_required_step": "contraindications",
        "needs_python_tool": "none",
        "reply": "Спасибо. Есть противопоказания?",
        "safety": {"hard_stop": False, "unsafe_medical_claim": False, "invented_fact_risk": False, "reason": ""},
    }

    class CapturingCompletions(FakeCompletions):
        async def create(self, **kwargs: Any):
            captured.update(kwargs)
            return await super().create(**kwargs)

    class CapturingClient:
        def __init__(self):
            self.chat = type("Chat", (), {"completions": CapturingCompletions(json.dumps(payload, ensure_ascii=False))})()

    monkeypatch.setattr(ai.get_settings(), "openai_api_key", "test-key")
    monkeypatch.setattr(ai, "AsyncOpenAI", object)
    monkeypatch.setattr(ai, "_openai_client", lambda key: CapturingClient())
    session = {"step": "age", "complaint": "спина", "last_required_step": "age", "last_required_question": "Сколько Вам лет?"}
    history = [{"role": "assistant", "content": "Сколько Вам лет?"}]
    decision, _ = run(ai.run_openai_dialog_brain(user_text="34", session=session, recent_history=history))
    body = json.loads(captured["messages"][1]["content"])
    ctx = body["dialog_context"]
    assert ctx["session_state"]["step"] == "age"
    assert ctx["session_state"]["complaint"] == "спина"
    assert ctx["recent_history"][0]["text"] == "Сколько Вам лет?"
    assert ctx["last_bot_question"]["type"] == "age"
    assert ctx["current_user_message"] == "34"
    assert decision["action"] == "ask_contraindications"
    assert decision["extracted"]["age"] == 34


def test_new_brain_schema_preserves_multiple_entities_and_symptom_duration() -> None:
    raw = {
        "understood_context": {"patient_meaning": "34 года, противопоказаний нет, хочет понедельник не рано", "is_answer_to_last_question": True, "contains_multiple_entities": True},
        "intent": "date_preference",
        "entities": {
            "age": 34,
            "symptom_duration": "15 лет",
            "contraindications_clear": True,
            "date_preference": "в понедельник",
            "time_preference": "не рано",
            "language": "ru",
        },
        "next_required_step": "time",
        "needs_python_tool": "check_slots",
        "reply": "Поняла, посмотрю свободное время.",
        "safety": {"hard_stop": False, "unsafe_medical_claim": False, "invented_fact_risk": False, "reason": ""},
    }
    decision, reason = ai._normalize_dialog_brain_decision(raw)
    assert reason == ""
    assert decision["action"] == "show_slots"
    assert decision["extracted"]["age"] == 34
    assert decision["extracted"]["contraindications_clear"] is True
    assert decision["extracted"]["preferred_date_text"] == "в понедельник"
    assert decision["extracted"]["time_preference"] == "не рано"
    assert decision["extracted"]["symptom_duration"] == "15 лет"


def test_long_history_context_summary_preserves_key_facts() -> None:
    history = []
    for i in range(30):
        history.append({"role": "user", "content": f"мусор {i}"})
    history.extend([
        {"role": "user", "content": "болит спина"},
        {"role": "assistant", "content": "Сколько Вам лет?"},
        {"role": "user", "content": "34"},
        {"role": "assistant", "content": "Противопоказаний нет?"},
        {"role": "user", "content": "нет, можно в понедельник"},
    ])
    session = {"step": "time", "complaint": "болит спина", "age": 34, "contraindications_ok": True, "preferred_date": "понедельник", "selected_slot": {"time": "10:00"}}
    ctx = ai.build_dialog_context(user_text="а врач кто?", session=session, recent_history=history)
    assert len(ctx["recent_history"]) <= 20
    assert ctx["session_state"]["complaint"] == "болит спина"
    assert ctx["session_state"]["age"] == 34
    assert ctx["session_state"]["contraindications_ok"] is True
    assert ctx["session_state"]["preferred_date"] == "понедельник"
    assert ctx["session_state"]["selected_slot"]["time"] == "10:00"


def test_humanize_disabled_does_not_disable_dialog_brain(monkeypatch: Any) -> None:
    payload = {
        "understood_context": {"patient_meaning": "age", "is_answer_to_last_question": True},
        "intent": "age_answer",
        "entities": {"age": 34},
        "next_required_step": "contraindications",
        "action": "ask_contraindications",
        "needs_python_tool": "none",
        "reply": "Спасибо. Есть ли противопоказания?",
        "safety": {"hard_stop": False, "unsafe_medical_claim": False, "invented_fact_risk": False, "reason": ""},
    }

    class Client:
        def __init__(self):
            self.chat = type("Chat", (), {"completions": FakeCompletions(json.dumps(payload, ensure_ascii=False))})()

    settings = ai.get_settings()
    monkeypatch.setattr(settings, "openai_api_key", "test-key")
    monkeypatch.setattr(settings, "openai_model", "gpt-4o-mini")
    monkeypatch.setattr(settings, "ai_brain_model", "gpt-4o-mini")
    monkeypatch.setattr(settings, "openai_humanize_replies", False)
    monkeypatch.setattr(settings, "openai_brain_enabled", True, raising=False)
    monkeypatch.setattr(ai, "AsyncOpenAI", object)
    monkeypatch.setattr(ai, "_openai_client", lambda key: Client())

    decision, debug = run(ai.run_openai_dialog_brain(user_text="34", session={"chat_id": "brain_humanize_off", "step": "age"}))

    assert decision["action"] == "ask_contraindications"
    assert debug["openai_brain_used"] is True
    assert debug["openai_brain_skip_reason"] == ""



def test_humanize_disabled_brain_success_keeps_openai_debug_clean(monkeypatch: Any) -> None:
    chat_id = "brain_success_humanize_disabled"
    reset(chat_id, {"step": "age"})
    payload = {
        "understood_context": {"patient_meaning": "age", "is_answer_to_last_question": True},
        "intent": "age_answer",
        "entities": {"age": 34},
        "next_required_step": "contraindications",
        "action": "ask_contraindications",
        "needs_python_tool": "none",
        "reply": "Спасибо. Есть ли противопоказания?",
        "safety": {"hard_stop": False, "unsafe_medical_claim": False, "invented_fact_risk": False, "reason": ""},
    }
    events: list[tuple[str, dict[str, Any]]] = []
    settings = ai.get_settings()
    monkeypatch.setattr(settings, "openai_api_key", "test-key")
    monkeypatch.setattr(settings, "openai_model", "gpt-4o-mini")
    monkeypatch.setattr(settings, "ai_brain_model", "")
    monkeypatch.setattr(settings, "openai_humanize_replies", False)
    monkeypatch.setattr(settings, "openai_brain_enabled", True, raising=False)
    monkeypatch.setattr(ai, "AsyncOpenAI", object)
    monkeypatch.setattr(ai, "_openai_client", lambda key: FakeClient(json.dumps(payload, ensure_ascii=False)))
    monkeypatch.setattr(state, "log_event", lambda chat_id, event, payload: events.append((event, payload)))

    raw = run(dialog._try_openai_dialog_brain(chat_id, "+77000000000", state.get_session(chat_id), "34"))
    answer = run(main._maybe_humanize_answer(chat_id, "34", raw or ""))
    session = state.get_session(chat_id)

    assert answer == "Спасибо. Есть ли противопоказания?"
    assert session["openai_used"] is True
    assert session["openai_brain_used"] is True
    assert session["openai_skip_reason"] == ""
    assert session["openai_disabled_flags"] == []
    assert any(event == "humanize_skipped_because_brain_valid" for event, _ in events)
    assert any(event == "humanize_skipped" and payload.get("reason") == "disabled" and payload.get("disabled_flags") == ["OPENAI_HUMANIZE_REPLIES=false"] for event, payload in events)
    assert not any(event == "openai_skipped" and payload.get("disabled_flags") == ["OPENAI_HUMANIZE_REPLIES=false"] for event, payload in events)

def test_api_key_and_openai_model_allow_brain(monkeypatch: Any) -> None:
    settings = ai.get_settings()
    monkeypatch.setattr(settings, "openai_api_key", "test-key")
    monkeypatch.setattr(settings, "openai_model", "gpt-4o-mini")
    monkeypatch.setattr(settings, "ai_brain_model", "")
    monkeypatch.setattr(settings, "openai_brain_enabled", True, raising=False)
    monkeypatch.setattr(ai, "AsyncOpenAI", object)

    detail = ai._openai_config_missing_detail(settings, chat_id="allowed", step="start", brain=True)

    assert detail["missing_keys"] == []
    assert detail["disabled_flags"] == []
    assert detail["brain_model"] == "gpt-4o-mini"


def test_brain_config_missing_debug_lists_specific_causes(monkeypatch: Any) -> None:
    settings = ai.get_settings()
    monkeypatch.setattr(settings, "openai_api_key", "")
    monkeypatch.setattr(settings, "openai_model", "")
    monkeypatch.setattr(settings, "ai_brain_model", "")
    monkeypatch.setattr(settings, "openai_brain_enabled", False, raising=False)
    monkeypatch.setattr(ai, "AsyncOpenAI", object)

    decision, debug = run(ai.run_openai_dialog_brain(user_text="x", session={"chat_id": "missing_detail", "step": "start"}))

    assert decision["action"] == "fallback_rule_based"
    assert debug["openai_brain_skip_reason"] == "config_missing"
    assert "OPENAI_API_KEY" in debug["openai_missing_keys"]
    assert "OPENAI_MODEL" in debug["openai_missing_keys"]
    assert "OPENAI_BRAIN_ENABLED=false" in debug["openai_disabled_flags"]
    assert debug["openai_config_missing_detail"]["chat_id"] == "missing_detail"
