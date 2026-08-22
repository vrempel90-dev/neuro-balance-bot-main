from __future__ import annotations

import asyncio
from typing import Any

import dialog
import weekend_legacy_block_bypass as bypass


WEEKEND_TEXT = "запишите меня в субботу 22 августа"
VALID_PHONE = "77011234567"


def _run(coro: Any) -> str:
    return asyncio.run(coro)


def _install_bypass(monkeypatch) -> None:
    current = dialog._mentions_weekend_day
    original = getattr(current, "_neuro_weekend_legacy_original", current)
    monkeypatch.setattr(dialog, "_mentions_weekend_day", original)
    bypass.install_weekend_legacy_block_bypass()


def _isolate_dialog(monkeypatch, session: dict[str, Any]) -> None:
    monkeypatch.setattr(dialog.state, "get_session", lambda chat_id: session)
    monkeypatch.setattr(dialog, "_safe_add_message", lambda *args, **kwargs: None)
    monkeypatch.setattr(dialog, "_safe_log", lambda *args, **kwargs: None)
    monkeypatch.setattr(dialog, "_safe_save", lambda *args, **kwargs: None)
    monkeypatch.setattr(dialog, "_is_new_leads_only_enabled", lambda: False)
    monkeypatch.setattr(
        dialog,
        "_finalize",
        lambda chat_id, current, answer: str(answer),
    )
    monkeypatch.setattr(
        dialog,
        "_no_reply",
        lambda chat_id, current, reason: f"NO_REPLY:{reason}",
    )


async def _unexpected_show_slots(*args: Any, **kwargs: Any) -> str:
    raise AssertionError("weekend request reached _show_slots after a safety gate")


async def _unexpected_slot_crm(*args: Any, **kwargs: Any) -> Any:
    raise AssertionError("weekend request reached CRM slot availability after a safety gate")


def _block_slot_path(monkeypatch) -> None:
    monkeypatch.setattr(dialog, "_show_slots", _unexpected_show_slots)
    monkeypatch.setattr(dialog.crm, "check_slots", _unexpected_slot_crm, raising=False)
    monkeypatch.setattr(dialog.crm, "check_slots_range", _unexpected_slot_crm, raising=False)


def test_install_does_not_wrap_handle_message(monkeypatch) -> None:
    original_handle = dialog.handle_message
    current = dialog._mentions_weekend_day
    original_mentions = getattr(current, "_neuro_weekend_legacy_original", current)
    monkeypatch.setattr(dialog, "_mentions_weekend_day", original_mentions)

    bypass.install_weekend_legacy_block_bypass()

    assert dialog.handle_message is original_handle
    assert dialog._mentions_weekend_day("в субботу") is False
    assert dialog._mentions_weekend_day("в воскресенье") is False
    assert dialog._mentions_weekend_day("сенбі") is False
    assert getattr(dialog._mentions_weekend_day, "_neuro_weekend_legacy_original") is original_mentions


def test_invalid_phone_stops_weekend_before_any_crm_or_slots(monkeypatch) -> None:
    session = {"step": "date", "language": "ru", "visit_type": "consultation"}
    _isolate_dialog(monkeypatch, session)
    _install_bypass(monkeypatch)
    _block_slot_path(monkeypatch)

    async def unexpected_active_lookup(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("invalid phone reached active-appointment CRM lookup")

    monkeypatch.setattr(dialog, "_lookup_active_appointment", unexpected_active_lookup)
    monkeypatch.setattr(dialog, "_valid_crm_phone", lambda phone: False)

    answer = _run(dialog.handle_message("chat-invalid", "not-a-phone", WEEKEND_TEXT))

    assert answer == "NO_REPLY:invalid_phone_for_crm_lookup"
    assert session["first_touch_blocked_reason"] == "invalid_phone_for_crm_lookup"
    assert session["crm_lookup_called"] is False


def test_active_appointment_stops_weekend_before_slot_availability(monkeypatch) -> None:
    session = {"step": "date", "language": "ru", "visit_type": "consultation"}
    _isolate_dialog(monkeypatch, session)
    _install_bypass(monkeypatch)
    _block_slot_path(monkeypatch)
    monkeypatch.setattr(dialog, "_valid_crm_phone", lambda phone: True)

    calls: list[str] = []

    async def active_lookup(chat_id: str, phone: str, current: dict[str, Any]) -> dict[str, str]:
        calls.append(phone)
        current["appointment_date"] = "2026-08-24"
        current["appointment_time"] = "10:00"
        current["appointment_doctor_name"] = "Врач Тест"
        return {
            "date": "2026-08-24",
            "timeStart": "10:00",
            "doctorName": "Врач Тест",
        }

    monkeypatch.setattr(dialog, "_lookup_active_appointment", active_lookup)

    answer = _run(dialog.handle_message("chat-booked", VALID_PHONE, WEEKEND_TEXT))

    assert calls == [VALID_PHONE]
    assert session["first_touch_blocked_reason"] == "active_appointment"
    assert session["gate_reason"] == "post_booking_support"
    assert "уже записаны" in answer.lower()


def test_manual_takeover_stops_weekend_before_slot_availability(monkeypatch) -> None:
    session = {
        "step": "date",
        "language": "ru",
        "visit_type": "consultation",
        "manual_takeover": True,
    }
    _isolate_dialog(monkeypatch, session)
    _install_bypass(monkeypatch)
    _block_slot_path(monkeypatch)
    monkeypatch.setattr(dialog, "_valid_crm_phone", lambda phone: True)

    async def no_active_appointment(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(dialog, "_lookup_active_appointment", no_active_appointment)

    answer = _run(dialog.handle_message("chat-manual", VALID_PHONE, WEEKEND_TEXT))

    assert answer == "NO_REPLY:manual_takeover"
    assert session["manual_takeover"] is True
    assert session["ai_muted"] is True
    assert session["openai_brain_skip_reason"] == "manual_takeover"


def test_first_touch_stops_weekend_before_slot_availability(monkeypatch) -> None:
    session = {"step": "start", "language": "ru", "visit_type": "consultation"}
    _isolate_dialog(monkeypatch, session)
    _install_bypass(monkeypatch)
    _block_slot_path(monkeypatch)
    monkeypatch.setattr(dialog, "_valid_crm_phone", lambda phone: True)

    async def no_active_appointment(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(dialog, "_lookup_active_appointment", no_active_appointment)
    monkeypatch.setattr(dialog, "_is_first_touch_due", lambda current: True)
    monkeypatch.setattr(dialog, "_first_touch_answer", lambda current, text: "FIRST_TOUCH_SAFE")

    answer = _run(dialog.handle_message("chat-first", VALID_PHONE, WEEKEND_TEXT))

    assert answer == "FIRST_TOUCH_SAFE"
    assert session["gate_reason"] == "new_lead"
    assert session["lead_source"] == "new_lead"
    assert session.get("weekend_procedure_day_requested") is None
