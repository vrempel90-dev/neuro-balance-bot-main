"""Один ход диалога: агент говорит, Python исполняет и, если надо, блокирует.

Здесь закреплён контракт, который заменил вторую детерминированную воронку:

* текст пациенту пишет агент, Python его не переписывает и не сокращает;
* технически недоступный агент — это «подключаю администратора», а не вторая
  воронка, и не молчание;
* ``_finalize`` умеет ровно три вещи: пропустить ответ, отдать шаблон «103»
  или передать диалог администратору. Ни одна из них не меняет формулировки.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

os.environ["SQLITE_PATH"] = tempfile.NamedTemporaryFile(delete=False, suffix=".sqlite3").name
os.environ.setdefault("CRM_BOT_SECRET", "test")
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest

import agent
import ai
import crm
import dialog
import main
import state
from config import get_settings
from fake_openai import FakeOpenAIClient, assistant_text, assistant_tool_call

state.init_db()

PHONE = "77011234567"
DOCTOR_LOGIN = "zhuma_md"
DOCTOR_NAME = "Жумабек Мади Мухтарович"
DATE = "2026-09-01"


@pytest.fixture(autouse=True)
def _openai_configured(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("AI_ENABLED", "true")
    monkeypatch.setenv("OPENAI_BRAIN_ENABLED", "true")
    get_settings.cache_clear()
    monkeypatch.setattr(ai, "AsyncOpenAI", object, raising=False)
    monkeypatch.setattr(agent.ai_budget, "check_allowed", lambda purpose: (True, ""))
    monkeypatch.setattr(agent.ai_budget, "record_usage", lambda *a, **k: {})
    yield
    get_settings.cache_clear()


class CRMStub:
    def __init__(self, *, lookup: dict[str, Any] | None = None, lookup_error: Exception | None = None):
        self.lookup = lookup if lookup is not None else {
            "ok": True, "found": False, "isNew": True, "patient": None, "lead": None,
            "lastAppointment": None, "hasActiveAppointment": False,
            "appointment": None, "appointments": [],
        }
        self.lookup_error = lookup_error
        self.lookup_calls: list[str] = []
        self.escalate_calls: list[dict[str, Any]] = []

    async def lookup_active_appointments_by_phone(self, phone: str) -> dict[str, Any]:
        self.lookup_calls.append(phone)
        if self.lookup_error is not None:
            raise self.lookup_error
        return dict(self.lookup)

    async def check_slots(self, date: str, doctor_login: str | None = None) -> dict[str, Any]:
        return {
            "ok": True,
            "date": date,
            "availability": [
                {"doctorLogin": DOCTOR_LOGIN, "doctorName": DOCTOR_NAME, "date": date, "availableSlots": ["09:20", "14:00"]}
            ],
        }

    async def escalate_to_operator(self, **kwargs: Any) -> dict[str, Any]:
        self.escalate_calls.append(kwargs)
        return {"ok": True}


def install_crm(monkeypatch: pytest.MonkeyPatch, stub: CRMStub) -> CRMStub:
    monkeypatch.setattr(crm, "lookup_active_appointments_by_phone", stub.lookup_active_appointments_by_phone)
    monkeypatch.setattr(crm, "check_slots", stub.check_slots)
    monkeypatch.setattr(crm, "escalate_to_operator", stub.escalate_to_operator)
    return stub


def install_openai(monkeypatch: pytest.MonkeyPatch, script: list[Any]) -> FakeOpenAIClient:
    client = FakeOpenAIClient(script)
    monkeypatch.setattr(ai, "_openai_client", lambda api_key: client)
    return client


def fresh_chat(chat_id: str, **extra: Any) -> dict[str, Any]:
    state.reset_session(chat_id)
    session = state.get_session(chat_id)
    session.update({"phone": PHONE})
    session.update(extra)
    state.save_session(chat_id, session)
    return session


def turn(chat_id: str, text: str, phone: str = PHONE) -> str:
    return asyncio.run(dialog.handle_message(chat_id, phone, text))


# ---------------------------------------------------------------------------
# Ответ пациенту — это текст агента
# ---------------------------------------------------------------------------


def test_new_lead_gets_the_agents_own_text(monkeypatch: pytest.MonkeyPatch) -> None:
    install_crm(monkeypatch, CRMStub())
    reply = "Здравствуйте 🌿 Расскажите, что именно беспокоит и как давно?"
    install_openai(monkeypatch, [assistant_text(reply)])
    chat_id = "dialog_agent_text"
    fresh_chat(chat_id)

    answer = turn(chat_id, "Здравствуйте")

    assert answer == reply, "Python не пишет и не правит текст пациенту"
    session = state.get_session(chat_id)
    assert session["answer_source"] == "gpt_agent"
    assert session["crm_patient_state"] == "NEW_PATIENT"


def test_long_agent_answer_reaches_the_patient_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """Финальный guard в main.py больше не подрезает ответ агента.

    Именно подрезка съедала третье предложение — то, в котором бот говорил,
    что именно он сейчас сделал.
    """
    install_crm(monkeypatch, CRMStub())
    reply = (
        "Свободно 09:20 и 14:00. Врач — Жумабек Мади Мухтарович. "
        "Записываю Вас на 14:00, подскажите, пожалуйста, имя пациента?"
    )
    install_openai(
        monkeypatch,
        [assistant_tool_call("get_available_slots", {"date_from": DATE}), assistant_text(reply)],
    )
    chat_id = "dialog_agent_long"
    fresh_chat(chat_id)

    answer = asyncio.run(main._maybe_humanize_answer(chat_id, "хочу записаться", turn(chat_id, "хочу записаться")))
    answer = main._guard_answer(chat_id, answer)

    assert answer == reply


# ---------------------------------------------------------------------------
# Агент недоступен технически — администратор, а не вторая воронка
# ---------------------------------------------------------------------------


def test_missing_openai_key_hands_off_to_the_operator(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = install_crm(monkeypatch, CRMStub())
    monkeypatch.setenv("OPENAI_API_KEY", "")
    get_settings.cache_clear()
    chat_id = "dialog_no_key"
    fresh_chat(chat_id)

    answer = turn(chat_id, "хочу записаться")

    assert answer == dialog.OPERATOR_HANDOFF_RU
    assert stub.escalate_calls, "администратор обязан узнать о ходе, который бот не ведёт"
    session = state.get_session(chat_id)
    assert session["openai_skip_reason"] == "openai_key_missing"


def test_exhausted_budget_hands_off(monkeypatch: pytest.MonkeyPatch) -> None:
    install_crm(monkeypatch, CRMStub())
    monkeypatch.setattr(agent.ai_budget, "check_allowed", lambda purpose: (False, "monthly_budget_exceeded"))
    chat_id = "dialog_budget"
    fresh_chat(chat_id)

    answer = turn(chat_id, "хочу записаться завтра")

    assert answer == dialog.OPERATOR_HANDOFF_RU


def test_openai_outage_hands_off_once_and_then_stays_quiet(monkeypatch: pytest.MonkeyPatch) -> None:
    """Одно «подключаю администратора», а не на каждое сообщение подряд."""
    stub = install_crm(monkeypatch, CRMStub())
    install_openai(monkeypatch, [RuntimeError("openai is down"), RuntimeError("openai is down")])
    chat_id = "dialog_outage"
    fresh_chat(chat_id)

    first = turn(chat_id, "хочу записаться")
    second = turn(chat_id, "ало?")

    assert first == dialog.OPERATOR_HANDOFF_RU
    assert second == ""
    assert len(stub.escalate_calls) == 1


def test_bot_resumes_after_a_transient_outage(monkeypatch: pytest.MonkeyPatch) -> None:
    """Один таймаут OpenAI не должен выключать бота для пациента навсегда."""
    install_crm(monkeypatch, CRMStub())
    install_openai(monkeypatch, [RuntimeError("openai is down"), assistant_text("Извините за паузу 🌿 Что именно беспокоит?")])
    chat_id = "dialog_transient"
    fresh_chat(chat_id)

    first = turn(chat_id, "хочу записаться")
    second = turn(chat_id, "болит спина")

    assert first == dialog.OPERATOR_HANDOFF_RU
    assert second == "Извините за паузу 🌿 Что именно беспокоит?"


def test_operator_takeover_keeps_the_bot_silent(monkeypatch: pytest.MonkeyPatch) -> None:
    install_crm(monkeypatch, CRMStub())
    install_openai(monkeypatch, [assistant_text("Не должно отправиться")])
    chat_id = "dialog_operator"
    fresh_chat(chat_id, manual_admin_intervention=True)

    assert turn(chat_id, "а когда приём?") == ""


# ---------------------------------------------------------------------------
# _finalize: пропустить, отдать «103» или передать администратору
# ---------------------------------------------------------------------------


def test_time_that_no_tool_returned_is_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    install_crm(monkeypatch, CRMStub())
    install_openai(monkeypatch, [assistant_text("Есть окошко завтра в 18:45, записываю?")])
    chat_id = "dialog_invented_time"
    fresh_chat(chat_id)

    answer = turn(chat_id, "когда можно прийти?")

    assert answer == dialog.OPERATOR_HANDOFF_RU
    assert "18:45" not in answer
    assert state.get_session(chat_id)["handoff_reason"] == "unverified_fact"


def test_time_from_a_tool_result_reaches_the_patient(monkeypatch: pytest.MonkeyPatch) -> None:
    install_crm(monkeypatch, CRMStub())
    install_openai(
        monkeypatch,
        [
            assistant_tool_call("get_available_slots", {"date_from": DATE}),
            assistant_text("Свободно 09:20 и 14:00 🌿 Какое время удобнее?"),
        ],
    )
    chat_id = "dialog_real_time"
    fresh_chat(chat_id)

    answer = turn(chat_id, "когда можно прийти?")

    assert "09:20" in answer and "14:00" in answer


def test_date_named_by_the_patient_is_not_a_hallucination(monkeypatch: pytest.MonkeyPatch) -> None:
    """Повтор даты, которую назвал сам пациент, — не выдумка бота."""
    install_crm(monkeypatch, CRMStub())
    install_openai(monkeypatch, [assistant_text("На 3 сентября посмотрю окошки и напишу 🌿")])
    chat_id = "dialog_patient_date"
    fresh_chat(chat_id)

    answer = turn(chat_id, "можно на 3 сентября?")

    assert answer == "На 3 сентября посмотрю окошки и напишу 🌿"


def test_doctor_that_no_tool_returned_is_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    install_crm(monkeypatch, CRMStub())
    install_openai(monkeypatch, [assistant_text("Вас примет врач Иванов Иван Иванович 🌿")])
    chat_id = "dialog_invented_doctor"
    fresh_chat(chat_id)

    answer = turn(chat_id, "кто принимает?")

    assert answer == dialog.OPERATOR_HANDOFF_RU
    assert "Иванов" not in answer


def test_empty_answer_is_handed_off_never_sent_as_silence() -> None:
    chat_id = "dialog_empty_answer"
    fresh_chat(chat_id, language="ru", last_user_text="ну")

    session = state.get_session(chat_id)
    answer = asyncio.run(dialog._finalize(chat_id, session, "   "))

    assert answer == dialog.OPERATOR_HANDOFF_RU
    assert session["handoff_reason"] == "empty_answer"


# ---------------------------------------------------------------------------
# Красные флаги
# ---------------------------------------------------------------------------


def test_red_flag_gets_the_103_template_and_escalation(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = install_crm(monkeypatch, CRMStub())
    install_openai(monkeypatch, [assistant_text("Записать Вас на консультацию?")])
    chat_id = "dialog_red_flag"
    fresh_chat(chat_id)

    answer = turn(chat_id, "сильная боль в груди, не могу дышать")

    assert answer == dialog.EMERGENCY_103_RU
    assert "103" in answer
    session = state.get_session(chat_id)
    assert session["handoff_reason"] == "red_flag"
    assert session["escalated"] is True
    assert stub.escalate_calls


def test_stroke_rehabilitation_is_not_an_emergency(monkeypatch: pytest.MonkeyPatch) -> None:
    """Реабилитация после инсульта — профиль клиники, а не вызов скорой."""
    install_crm(monkeypatch, CRMStub())
    reply = "Понимаю Вас 🌿 Расскажите, как давно это произошло и что сейчас беспокоит больше всего?"
    install_openai(monkeypatch, [assistant_text(reply)])
    chat_id = "dialog_rehab"
    fresh_chat(chat_id)

    answer = turn(chat_id, "после инсульта отнялась рука, хотим восстановление")

    assert answer == reply


def test_red_flag_detector_matches_the_kazakh_wording() -> None:
    assert dialog._has_red_flag("жүрегім ауырады, тыныс ала алмаймын") is True
    assert dialog._has_red_flag("тізе ауырады, жазылғым келеді") is False


# ---------------------------------------------------------------------------
# Допуск лида
# ---------------------------------------------------------------------------


def test_returning_patient_is_silent_and_the_admin_is_told_once(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = install_crm(
        monkeypatch,
        CRMStub(lookup={
            "ok": True, "found": True, "isNew": False, "patient": {"name": "Алия"},
            "lead": {"id": "l1"}, "lastAppointment": {"id": "a1", "status": "Завершён"},
            "hasActiveAppointment": False, "appointment": None, "appointments": [],
        }),
    )
    install_openai(monkeypatch, [assistant_text("Не должно отправиться")])
    chat_id = "dialog_returning"
    fresh_chat(chat_id)

    assert turn(chat_id, "хочу записаться") == ""
    assert turn(chat_id, "алло") == ""
    assert len(stub.escalate_calls) == 1, "администратора зовут один раз на диалог, а не на каждое сообщение"
    session = state.get_session(chat_id)
    assert session["silent_old_lead"] is True
    assert session["no_reply_reason"] == "old_lead_from_crm"


def test_crm_outage_is_silent_but_the_next_turn_is_reclassified(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail-closed на время сбоя — но не пожизненная блокировка нового лида."""
    stub = install_crm(monkeypatch, CRMStub(lookup_error=crm.CRMError("crm down")))
    install_openai(monkeypatch, [assistant_text("Здравствуйте 🌿 Что беспокоит?")])
    chat_id = "dialog_crm_outage"
    fresh_chat(chat_id)

    assert turn(chat_id, "Здравствуйте") == ""
    assert state.get_session(chat_id)["no_reply_reason"] == "crm_lookup_failed"

    stub.lookup_error = None
    assert turn(chat_id, "Здравствуйте") == "Здравствуйте 🌿 Что беспокоит?"
    session = state.get_session(chat_id)
    assert session["crm_patient_state"] == "NEW_PATIENT"
    assert session["ai_muted"] is False


def test_phone_crm_cannot_use_is_never_answered(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = install_crm(monkeypatch, CRMStub())
    install_openai(monkeypatch, [assistant_text("Не должно отправиться")])
    chat_id = "dialog_bad_phone"
    fresh_chat(chat_id)

    assert turn(chat_id, "Здравствуйте", phone="12345") == ""
    assert not stub.lookup_calls
    assert state.get_session(chat_id)["no_reply_reason"] == "invalid_phone_for_crm_lookup"


def test_identical_answer_is_sent_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """Два одновременных сообщения дают один и тот же ответ — уходит один.

    Пациент может дослать второе сообщение, пока бот думает над первым: оба
    хода обрабатываются параллельно и оба возвращают одинаковое подтверждение.
    Дубль режется на отправке, а не подменой текста в диалоге.
    """
    sent: list[str] = []

    async def fake_send_text(**kwargs: Any) -> dict[str, Any]:
        sent.append(str(kwargs.get("text") or ""))
        return {"ok": True, "status_code": 200}

    monkeypatch.setattr(main, "send_text", fake_send_text)
    chat_id = "dialog_duplicate_send"
    fresh_chat(chat_id)

    answer = "Записала 🌿 8 сентября в 09:20."
    for _ in range(2):
        asyncio.run(main._send_answer_parts(chat_id=chat_id, answer=answer, chat_type="whatsapp", channel_id=None, phone=PHONE))

    assert sent == [answer]
    assert state.get_session(chat_id)["outgoing_duplicate_guard_blocked"] is True


# ---------------------------------------------------------------------------
# Совместимость вызова с моделью
# ---------------------------------------------------------------------------


def test_tool_calls_disable_reasoning_where_the_api_demands_it() -> None:
    """gpt-5.5+ отвергает function tools, пока рассуждение не выключено.

    Прод 26.08.2026: AI_BRAIN_MODEL=gpt-5.6-terra, и КАЖДЫЙ ход агента падал с
    400 «Function tools with reasoning_effort are not supported ... set
    reasoning_effort to none» — пациент вместо диалога получал передачу
    администратору. Параметр не передавался вовсе: у этих моделей рассуждение
    включено по умолчанию.
    """
    for model in ("gpt-5.6-terra", "gpt-5.6-sol", "gpt-5.6-luna", "gpt-5.6", "gpt-5.5"):
        assert ai.brain_tool_call_kwargs(model) == {"reasoning_effort": "none"}, model
    # Модели без этого конфликта параметр не принимают — передавать нельзя.
    for model in ("gpt-5.4-mini", "gpt-4o-mini", ""):
        assert ai.brain_tool_call_kwargs(model) == {}, model


def test_agent_sends_the_model_compatibility_kwargs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Проверка на реальном вызове, а не только на хелпере."""
    monkeypatch.setenv("AI_BRAIN_MODEL", "gpt-5.6-terra")
    get_settings.cache_clear()
    install_crm(monkeypatch, CRMStub())
    client = install_openai(monkeypatch, [assistant_text("Здравствуйте 🌿")])
    chat_id = "dialog_model_kwargs"
    fresh_chat(chat_id)

    turn(chat_id, "здравствуйте")

    assert client.calls, "модель должна быть вызвана"
    call = client.calls[0]
    assert call["model"] == "gpt-5.6-terra"
    assert call["reasoning_effort"] == "none"
    assert call["tools"], "инструменты передаются в том же вызове"


def test_crm_lookup_debug_survives_for_incident_analysis(monkeypatch: pytest.MonkeyPatch) -> None:
    """В отладке видно не только вывод admission, но и что ответила CRM."""
    install_crm(
        monkeypatch,
        CRMStub(lookup={
            "ok": True, "found": True, "isNew": True, "patient": None,
            "lead": {"id": 44934, "status": "НОВАЯ"}, "lastAppointment": None,
            "hasActiveAppointment": False, "appointment": None, "appointments": [],
        }),
    )
    install_openai(monkeypatch, [assistant_text("Здравствуйте 🌿 Что беспокоит?")])
    chat_id = "dialog_crm_debug"
    fresh_chat(chat_id)

    turn(chat_id, "хочу записаться")

    session = state.get_session(chat_id)
    assert session["crm_patient_state"] == "NEW_PATIENT"
    assert session["raw_crm_found"] is True
    assert session["raw_crm_isNew"] is True
    assert session["raw_crm_has_lead"] is True
    assert session["raw_crm_lead_status"] == "НОВАЯ"
    assert session["raw_crm_has_patient"] is False
    assert session["raw_crm_hasActiveAppointment"] is False


def test_debug_says_gpt_ran_when_the_agent_wrote_the_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ответ агента не переписывается — но в диагностике GPT обязан числиться вызванным.

    Прод 26.08.2026: живой ход агента писался как openai_used=false,
    skip_reason=locked_template, потому что «не переписывать текст» и «модель не
    вызывалась» попали в одну ветку. По такому логу решают, работает GPT или нет.
    """
    install_crm(monkeypatch, CRMStub())
    install_openai(monkeypatch, [assistant_text("Здравствуйте 🌿 Что беспокоит?")])
    chat_id = "dialog_debug_openai_used"
    fresh_chat(chat_id)

    answer = turn(chat_id, "здравствуйте")
    asyncio.run(main._maybe_humanize_answer(chat_id, "здравствуйте", answer))

    session = state.get_session(chat_id)
    assert session["openai_used"] is True
    assert session["openai_skip_reason"] == ""
    assert session["humanize_skipped_because_brain_valid"] is True


def test_debug_marks_python_templates_as_not_gpt(monkeypatch: pytest.MonkeyPatch) -> None:
    """А вот «подключаю администратора» пишет Python — и это должно быть видно."""
    install_crm(monkeypatch, CRMStub())
    monkeypatch.setenv("OPENAI_API_KEY", "")
    get_settings.cache_clear()
    chat_id = "dialog_debug_python_template"
    fresh_chat(chat_id)

    answer = turn(chat_id, "хочу записаться")
    asyncio.run(main._maybe_humanize_answer(chat_id, "хочу записаться", answer))

    assert answer == dialog.OPERATOR_HANDOFF_RU
    session = state.get_session(chat_id)
    assert session["openai_used"] is False
    assert session["openai_skip_reason"] == "python_template"
