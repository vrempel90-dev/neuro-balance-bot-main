"""Production regression: an accepted active turn must never end silently.

Reproduces the reported production bug:

    клиент хочет записаться
    → получает вариант
    → выбирает запись/дату/время
    → handler заканчивается
    → outbound отсутствует

Root cause chain proven by these tests:

1. Keyword branches in ``dialog.handle_message`` (``_select_slot``,
   ``_parse_date``, ``_explicit_slot_selection_text``) do not understand
   conversational phrasing ("мне удобно в обед", "можно в начале недели",
   "да"), so Python answers with a verbatim repeat of the step prompt.
2. ``_finalize``'s ``duplicate_answer_guard`` sees that repeat, returns ``""``
   and does so *before* ``final_no_empty_guard`` — so no recovery runs and the
   patient gets nothing.

The invariant asserted here is the one from the task: every accepted active
inbound user turn must end with an outbound answer (or an explicit escalation
that still answers the patient). Silence is never a terminal state.
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
os.environ.setdefault("OPENAI_API_KEY", "")
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest

import crm
import dialog
import state

state.init_db()

PHONE = "77011234567"
SLOT_TIMES = ["09:20", "14:00", "15:40"]
DOCTOR_LOGIN = "zhuma_md"
DOCTOR_NAME = "Жумабек Мади Мухтарович"


def _patch_crm(monkeypatch: pytest.MonkeyPatch, slot_times: list[str] | None = None) -> dict[str, list[Any]]:
    calls: dict[str, list[Any]] = {"slots": [], "book": []}

    async def fake_check_slots(date: str, doctor_login: str | None = None) -> dict[str, Any]:
        calls["slots"].append({"date": date, "doctor_login": doctor_login})
        return {
            "availability": [
                {
                    "doctorLogin": doctor_login or DOCTOR_LOGIN,
                    "doctorName": DOCTOR_NAME,
                    "date": date,
                    "availableSlots": list(slot_times or SLOT_TIMES),
                }
            ]
        }

    async def fake_book(**kwargs: Any) -> dict[str, Any]:
        calls["book"].append(kwargs)
        return {"ok": True, "id": 4242, "status": "Записан", **kwargs}

    async def fake_lookup(phone: str) -> dict[str, Any]:
        return {"ok": True, "found": False, "isNew": True, "appointments": [], "appointment": None}

    monkeypatch.setattr(crm, "check_slots", fake_check_slots)
    monkeypatch.setattr(crm, "book_appointment", fake_book)
    monkeypatch.setattr(crm, "lookup_active_appointments_by_phone", fake_lookup)
    return calls


def _reset(chat_id: str, preset: dict[str, Any] | None = None) -> None:
    state.reset_session(chat_id)
    session = state.get_session(chat_id)
    session.update({"ai_lead_started": True, "phone": PHONE})
    if preset:
        session.update(preset)
    state.save_session(chat_id, session)


async def _say(chat_id: str, text: str) -> str:
    return await dialog.handle_message(chat_id, PHONE, text)


async def _run_turns(chat_id: str, messages: list[str]) -> list[str]:
    answers: list[str] = []
    for message in messages:
        answers.append(await _say(chat_id, message))
    return answers


# --------------------------------------------------------------------------
# The exact production bug: patient picks a time/date conversationally twice
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("case", "messages"),
    [
        (
            "conversational_time_choice",
            ["Болит поясница", "34", "нет", "давайте завтра", "мне удобно в обед", "а можно в обед?"],
        ),
        (
            "short_yes_after_slots",
            ["Болит поясница", "34", "нет", "давайте завтра", "да", "да"],
        ),
        (
            "conversational_date_choice",
            ["Болит поясница", "34", "нет", "на следующей неделе", "можно в начале недели"],
        ),
    ],
)
def test_active_booking_turn_never_ends_silently(
    monkeypatch: pytest.MonkeyPatch, case: str, messages: list[str]
) -> None:
    """CRITICAL SILENT TURN BUG regression.

    Before the fix each of these conversations ended with ``answer == ""`` and
    an empty ``no_reply_reason`` — the handler returned, no outbound was sent
    and the patient was abandoned mid-booking.
    """
    _patch_crm(monkeypatch)
    chat_id = f"silent_turn_{case}"
    _reset(chat_id)

    answers = asyncio.run(_run_turns(chat_id, messages))

    for index, (message, answer) in enumerate(zip(messages, answers)):
        session = state.get_session(chat_id)
        assert str(answer or "").strip(), (
            f"[{case}] turn #{index} for {message!r} produced no outbound; "
            f"step={session.get('step')!r} no_reply_reason={session.get('no_reply_reason')!r} "
            f"duplicate_guard={session.get('outbound_duplicate_guard_blocked')!r}"
        )


def test_repeated_conversational_answer_does_not_repeat_same_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Anti-loop: the bot must not answer two identical replies in a row.

    The duplicate guard used to be the *only* protection here and it enforced
    the rule by going silent. Silence is not an allowed way to avoid a repeat —
    the turn has to move forward instead.
    """
    _patch_crm(monkeypatch)
    chat_id = "silent_turn_no_repeat"
    _reset(chat_id)

    answers = asyncio.run(
        _run_turns(chat_id, ["Болит поясница", "34", "нет", "давайте завтра", "да", "да"])
    )

    assert all(str(a or "").strip() for a in answers)
    assert answers[-1].strip() != answers[-2].strip(), (
        "bot repeated the identical slot prompt twice in a row instead of advancing the turn"
    )


def test_silent_turn_regression_marks_reason_when_intentionally_quiet() -> None:
    """Deliberate silence must always carry an explicit reason.

    ``_no_reply`` is a legitimate terminal state (e.g. patient said "спасибо"
    after booking), but it must never be reached with an empty reason: that is
    exactly how the production bug hid itself in telemetry.
    """
    chat_id = "silent_turn_reason"
    _reset(chat_id, {"step": "done", "booked": True})
    session = state.get_session(chat_id)

    answer = dialog._no_reply(chat_id, session, "thanks/done")

    assert answer == ""
    assert state.get_session(chat_id)["no_reply_reason"] == "thanks/done"
