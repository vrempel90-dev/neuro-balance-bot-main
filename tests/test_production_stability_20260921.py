"""Regression coverage for the 2026-09-21 production stability pass.

These tests pin the hard invariants that previously depended on the model:
fresh availability before booking, persisted slot choice, deterministic
weekend policy, terminal recovery, patient-identity isolation, and replay of a
confirmed outbound answer after a Wazzup send failure.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

import agent
import dialog
import main
import crm
import state


DOCTOR_LOGIN = "zhuma_md"
DOCTOR_NAME = "Жумабек Мади Мухтарович"
WEEKDAY = "2026-09-22"
SATURDAY = "2026-09-26"
SUNDAY = "2026-09-27"


def offered_slot(date: str = WEEKDAY, time_start: str = "10:00") -> dict[str, str]:
    return {
        "doctor_login": DOCTOR_LOGIN,
        "doctor_name": DOCTOR_NAME,
        "date": date,
        "time_start": time_start,
    }


def ready_session(*, name: str = "Азамат", date: str = WEEKDAY, time_start: str = "10:00") -> dict[str, Any]:
    session: dict[str, Any] = {
        "complaint": "боль в спине",
        "age": 35,
        "contraindications_ok": True,
        "contraindications_verdict": "proceed",
        "contraindications_raw": "нет",
        "patient_name": name,
    }
    slot = offered_slot(date, time_start)
    agent._remember_offered_slots(session, [slot])
    selected = agent._tool_select_offered_slot(
        "test-select",
        session,
        {"doctor_login": DOCTOR_LOGIN, "date": date, "time_start": time_start},
    )
    assert selected["ok"] is True
    return session


class BookingCRM:
    def __init__(self, *, available: bool = True, fail_check: bool = False):
        self.available = available
        self.fail_check = fail_check
        self.check_calls: list[tuple[str, str | None]] = []
        self.book_calls: list[dict[str, Any]] = []

    async def check_slots(self, date: str, doctor_login: str | None = None) -> dict[str, Any]:
        self.check_calls.append((date, doctor_login))
        if self.fail_check:
            raise crm.CRMError("availability unavailable")
        times = ["10:00"] if self.available else []
        return {
            "ok": True,
            "date": date,
            "availability": [
                {
                    "doctorLogin": DOCTOR_LOGIN,
                    "doctorName": DOCTOR_NAME,
                    "date": date,
                    "availableSlots": times,
                }
            ],
        }

    async def book_appointment(self, **kwargs: Any) -> dict[str, Any]:
        self.book_calls.append(dict(kwargs))
        return {
            "appointmentId": 91001,
            "doctorName": DOCTOR_NAME,
            "date": kwargs["date"],
            "timeStart": kwargs["time_start"],
        }


def install_booking_crm(monkeypatch: pytest.MonkeyPatch, stub: BookingCRM) -> BookingCRM:
    monkeypatch.setattr(crm, "check_slots", stub.check_slots)
    monkeypatch.setattr(crm, "book_appointment", stub.book_appointment)
    return stub


def test_selected_slot_is_persisted_and_unoffered_slot_is_rejected() -> None:
    session: dict[str, Any] = {}
    agent._remember_offered_slots(session, [offered_slot()])

    selected = agent._tool_select_offered_slot(
        "slot-state",
        session,
        {"doctor_login": DOCTOR_LOGIN, "date": WEEKDAY, "time_start": "10:00"},
    )
    assert selected["selected"] is True
    assert session["selected_slot"]["time_start"] == "10:00"
    assert session["selected_date"] == WEEKDAY
    assert session["step"] == "name"

    rejected = agent._tool_select_offered_slot(
        "slot-state",
        session,
        {"doctor_login": DOCTOR_LOGIN, "date": WEEKDAY, "time_start": "19:30"},
    )
    assert rejected["ok"] is False
    assert rejected["error"] == "slot_not_offered_by_crm"


def test_booking_revalidates_slot_immediately_before_exactly_one_post(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = install_booking_crm(monkeypatch, BookingCRM())
    session = ready_session()
    session["crm_lead_id"] = 321
    session["crm_conversation_id"] = 654

    result = asyncio.run(
        agent._tool_book_appointment(
            "fresh-booking",
            session,
            "77010000001",
            {
                "patient_name": "Азамат",
                "doctor_login": DOCTOR_LOGIN,
                "date": WEEKDAY,
                "time_start": "10:00",
            },
        )
    )

    assert result["booking_success"] is True
    assert stub.check_calls == [(WEEKDAY, DOCTOR_LOGIN)]
    assert len(stub.book_calls) == 1
    assert stub.book_calls[0]["lead_id"] == 321
    assert stub.book_calls[0]["conversation_id"] == 654
    assert session["booking_confirmed"] is True


def test_stale_slot_is_rejected_before_booking_post(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = install_booking_crm(monkeypatch, BookingCRM(available=False))
    session = ready_session()

    result = asyncio.run(
        agent._tool_book_appointment(
            "stale-booking",
            session,
            "77010000002",
            {
                "patient_name": "Азамат",
                "doctor_login": DOCTOR_LOGIN,
                "date": WEEKDAY,
                "time_start": "10:00",
            },
        )
    )

    assert result["booking_success"] is False
    assert result["error"] == "slot_conflict"
    assert stub.check_calls == [(WEEKDAY, DOCTOR_LOGIN)]
    assert stub.book_calls == []
    assert "selected_slot" not in session


def test_revalidation_failure_is_fail_closed_and_never_posts(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = install_booking_crm(monkeypatch, BookingCRM(fail_check=True))
    session = ready_session()

    result = asyncio.run(
        agent._tool_book_appointment(
            "check-failed",
            session,
            "77010000003",
            {
                "patient_name": "Азамат",
                "doctor_login": DOCTOR_LOGIN,
                "date": WEEKDAY,
                "time_start": "10:00",
            },
        )
    )

    assert result["error"] == "slot_revalidation_failed"
    assert stub.book_calls == []
    assert session.get("booking_confirmed") is not True


@pytest.mark.parametrize(
    ("date", "expected_error"),
    [(SATURDAY, "saturday_procedure_day"), (SUNDAY, "sunday_closed")],
)
def test_non_consultation_days_are_blocked_even_if_a_stale_offer_exists(
    monkeypatch: pytest.MonkeyPatch, date: str, expected_error: str
) -> None:
    stub = install_booking_crm(monkeypatch, BookingCRM())
    session = {
        "complaint": "боль",
        "age": 35,
        "contraindications_ok": True,
        "contraindications_verdict": "proceed",
        "patient_name": "Азамат",
    }
    agent._remember_offered_slots(session, [offered_slot(date)])

    result = asyncio.run(
        agent._tool_book_appointment(
            f"blocked-{date}",
            session,
            "77010000004",
            {
                "patient_name": "Азамат",
                "doctor_login": DOCTOR_LOGIN,
                "date": date,
                "time_start": "10:00",
            },
        )
    )

    assert result["error"] == expected_error
    assert stub.check_calls == []
    assert stub.book_calls == []


def test_openai_interruption_after_name_finishes_booking_in_same_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = install_booking_crm(monkeypatch, BookingCRM())
    session = ready_session(name="Азамат")

    result = asyncio.run(
        agent._recover_after_agent_interruption(
            "recover-after-name",
            "77010000005",
            session,
            agent.AgentResult(used=True),
            reason="openai_error_after_tools",
        )
    )

    assert result.booked is True
    assert len(stub.book_calls) == 1
    assert "10:00" in result.reply
    assert "вернусь" not in result.reply.lower()
    assert result.escalate is False


def test_interruption_without_safe_booking_state_escalates_instead_of_promising_future_work() -> None:
    session: dict[str, Any] = {"language": "ru"}

    result = asyncio.run(
        agent._recover_after_agent_interruption(
            "recover-terminal",
            "77010000006",
            session,
            agent.AgentResult(used=True),
            reason="tool_iteration_limit",
        )
    )

    assert result.escalate is True
    assert session["manual_takeover"] is True
    assert result.reply.strip()
    assert "вернусь" not in result.reply.lower()
    assert "секунду" not in result.reply.lower()


def test_switching_from_self_to_relative_clears_patient_scoped_facts() -> None:
    session: dict[str, Any] = {
        "complaint": "моя спина",
        "complaint_gate": True,
        "age": 40,
        "contraindications_ok": True,
        "contraindications_verdict": "proceed",
        "contraindications_raw": "нет",
        "patient_name": "Иван",
        "known_user_facts": {"complaint": "моя спина", "age": 40, "patient_name": "Иван"},
    }

    first = agent._tool_record_patient_facts("relative-reset", session, {"patient_relation": "мама"})

    assert first["ok"] is True
    assert session["patient_relation"] == "мама"
    assert "complaint" not in session
    assert "age" not in session
    assert "contraindications_ok" not in session
    assert "patient_name" not in session

    second = agent._tool_record_patient_facts(
        "relative-reset",
        session,
        {
            "patient_relation": "мама",
            "complaint": "болит шея",
            "age": 63,
            "contraindications_clear": True,
            "contraindications_note": "нет",
            "patient_name": "Гульнара",
        },
    )
    assert second["ok"] is True
    assert session["complaint"] == "болит шея"
    assert session["age"] == 63
    assert session["patient_name"] == "Гульнара"


def test_crm_lookup_ids_are_preserved_for_single_booking_write() -> None:
    session: dict[str, Any] = {}
    dialog._store_crm_lookup_debug(
        session,
        {
            "raw": {
                "found": False,
                "isNew": True,
                "lead": {"id": 77, "status": "new"},
                "conversation": {"id": 88},
                "hasActiveAppointment": False,
            }
        },
    )
    assert session["crm_lead_id"] == 77
    assert session["crm_conversation_id"] == 88


@pytest.mark.parametrize(
    ("session", "question", "field"),
    [
        ({"age": 35}, "Сколько Вам лет?", "age"),
        ({"complaint": "боль"}, "Что Вас беспокоит?", "complaint"),
        ({"contraindications_ok": True}, "Есть ли противопоказания?", "contraindications"),
        ({"preferred_date": WEEKDAY}, "На какой день Вам удобно?", "date"),
        ({"selected_time": "10:00"}, "Какое время выбираете?", "time"),
        ({"patient_name": "Азамат"}, "Ваше имя?", "name"),
    ],
)
def test_known_mandatory_fact_cannot_be_reasked(
    session: dict[str, Any], question: str, field: str
) -> None:
    assert dialog._reasked_known_fact(session, question) == field


def test_exact_same_question_is_blocked_even_when_field_is_not_classified() -> None:
    session = {"last_assistant_answer": "Подскажите, пожалуйста, это удобно?"}
    assert dialog._reasked_known_fact(session, "Подскажите, пожалуйста, это удобно?") == "exact_repeat"


def test_failed_wazzup_send_keeps_pending_answer_and_retry_does_not_rebuild(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chat_id = "outbox-replay-20260921"
    state.reset_session(chat_id)
    message = {
        "chat_id": chat_id,
        "phone": "77010000007",
        "kind": "text",
        "text": "Азамат",
        "message_key": "outbox-msg-1",
        "message_id": "outbox-msg-1",
        "chat_type": "whatsapp",
    }
    monkeypatch.setattr(main, "get_settings", lambda: SimpleNamespace(message_debounce_seconds=0))

    built: list[str] = []
    sent: list[str] = []

    async def build(_message: dict[str, Any]) -> str:
        built.append("built")
        return "Запись подтверждена 22 сентября в 10:00."

    async def send_parts(**kwargs: Any) -> None:
        sent.append(str(kwargs["answer"]))
        if len(sent) == 1:
            raise RuntimeError("wazzup timeout")

    monkeypatch.setattr(main, "_build_answer_for_message", build)
    monkeypatch.setattr(main, "_send_answer_parts", send_parts)

    asyncio.run(main._debounced_process_and_send(dict(message)))
    assert built == ["built"]
    assert main._pending_outbound(chat_id, message)

    asyncio.run(main._debounced_process_and_send(dict(message)))
    assert built == ["built"], "retry must replay, not re-run dialog/CRM"
    assert sent == [
        "Запись подтверждена 22 сентября в 10:00.",
        "Запись подтверждена 22 сентября в 10:00.",
    ]
    assert main._pending_outbound(chat_id, message) == ""


def test_last_sent_answer_is_written_only_after_wazzup_success(monkeypatch: pytest.MonkeyPatch) -> None:
    chat_id = "last-sent-after-success"
    state.reset_session(chat_id)

    async def fail_send(**kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("network down")

    monkeypatch.setattr(main, "send_text", fail_send)
    with pytest.raises(RuntimeError):
        asyncio.run(
            main._send_answer_parts(
                chat_id=chat_id,
                answer="Подтверждение записи",
                chat_type="whatsapp",
                channel_id=None,
                phone="77010000008",
            )
        )
    assert not state.get_session(chat_id).get("last_sent_answer")

    async def ok_send(**kwargs: Any) -> dict[str, Any]:
        return {"status_code": 200}

    monkeypatch.setattr(main, "send_text", ok_send)
    asyncio.run(
        main._send_answer_parts(
            chat_id=chat_id,
            answer="Подтверждение записи",
            chat_type="whatsapp",
            channel_id=None,
            phone="77010000008",
        )
    )
    assert state.get_session(chat_id).get("last_sent_answer") == "Подтверждение записи"
