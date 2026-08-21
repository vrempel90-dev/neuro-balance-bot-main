from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault("SQLITE_PATH", tempfile.NamedTemporaryFile(delete=False, suffix=".sqlite3").name)
os.environ.setdefault("CRM_BOT_SECRET", "test")
os.environ.setdefault("OPENAI_API_KEY", "")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import dialog


def silence_side_effects(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dialog, "_safe_save", lambda *args, **kwargs: None)
    monkeypatch.setattr(dialog, "_safe_add_message", lambda *args, **kwargs: None)
    monkeypatch.setattr(dialog, "_safe_log", lambda *args, **kwargs: None)


def active_session(step: str, last_answer: str) -> dict:
    session = {
        "language": "ru",
        "step": step,
        "ai_lead_started": True,
        "last_user_text": "новое сообщение пациента",
        "last_assistant_answer": last_answer,
        "last_bot_answer": last_answer,
        "complaint": "болит поясница",
        "age": 54,
        "contraindications_ok": True,
        "contraindications_verdict": "proceed",
    }
    if step in {"time", "select_slot", "name"}:
        session["preferred_date"] = "2099-08-22"
    if step in {"time", "select_slot"}:
        session["last_slots"] = [{
            "doctorLogin": "zhuma_md",
            "doctorName": "Жумабек Мади Мухтарович",
            "date": "2099-08-22",
            "timeStart": "14:00",
            "doctor_login": "zhuma_md",
            "doctor_name": "Жумабек Мади Мухтарович",
            "time": "14:00",
        }]
    if step == "name":
        session["selected_slot"] = {
            "doctorLogin": "zhuma_md",
            "doctorName": "Жумабек Мади Мухтарович",
            "date": "2099-08-22",
            "timeStart": "14:00",
            "doctor_login": "zhuma_md",
            "doctor_name": "Жумабек Мади Мухтарович",
            "time": "14:00",
        }
        session["selected_time"] = "14:00"
    return session


@pytest.mark.parametrize("step,answer", [
    ("date", "Отлично 🌿 На какой день Вам удобно прийти?"),
    ("time", "Есть такие свободные окошки: 14:00 🌿 Какое время Вам удобно?"),
    ("name", "Подскажите, пожалуйста, Ваше имя для записи."),
])
def test_new_patient_turn_never_becomes_empty_only_because_answer_repeats(
    monkeypatch: pytest.MonkeyPatch,
    step: str,
    answer: str,
) -> None:
    silence_side_effects(monkeypatch)
    session = active_session(step, answer)

    result = dialog._finalize("active-repeat", session, answer)

    assert result
    assert session.get("outbound_duplicate_guard_blocked") is not True
    assert session.get("fallback_reason") == "active_booking_repeat_answer_allowed"


def test_weekend_business_rule_can_repeat_instead_of_silence(monkeypatch: pytest.MonkeyPatch) -> None:
    silence_side_effects(monkeypatch)
    answer = dialog._weekend_primary_block_answer({"language": "ru"})
    session = active_session("date", answer)
    session["last_user_text"] = "в субботу"

    result = dialog._finalize("weekend-repeat", session, answer)

    assert "процедурные дни" in result
    assert result == answer
    assert session.get("outbound_duplicate_guard_blocked") is not True


def test_intentional_no_reply_still_remains_available(monkeypatch: pytest.MonkeyPatch) -> None:
    silence_side_effects(monkeypatch)
    session = {"language": "ru", "step": "booked", "booking_confirmed": True}

    assert dialog._no_reply("booked-thanks", session, "post_booking_thanks") == ""
    assert session["should_send_wazzup"] is False
    assert session["no_reply_reason"] == "post_booking_thanks"
