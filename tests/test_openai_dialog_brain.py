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
    assert session["time_preference"] == "не рано"
    assert calls
    assert session["step"] == "time"
    assert "противопоказ" not in answer.lower()
    assert "11:00" in answer and "15:00" in answer
    assert session["openai_brain_used"] is True
    assert session["openai_brain_action"] == "show_slots"
    assert session["openai_used"] is True
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
