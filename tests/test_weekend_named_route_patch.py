from __future__ import annotations

import asyncio

import dialog
import state
import weekend_named_route_patch as patch


def test_named_weekend_date_delegates_to_slot_policy(monkeypatch) -> None:
    session = {"step": "date", "language": "ru", "visit_type": "consultation"}
    monkeypatch.setattr(state, "get_session", lambda chat_id: session)
    monkeypatch.setattr(dialog, "_parse_date", lambda text: "2026-08-22")
    monkeypatch.setattr(dialog, "_is_new_patient_consultation", lambda current: True)
    monkeypatch.setattr(dialog, "_mentions_weekend_day", lambda text: True)
    monkeypatch.setattr(dialog, "_is_weekend_date", lambda date_iso: True)
    monkeypatch.setattr(dialog, "_has_video_procedure_question", lambda text: False)
    monkeypatch.setattr(dialog, "_finalize", lambda chat_id, current, answer: answer)

    calls = []
    async def fake_show_slots(chat_id, current, date_iso):
        calls.append(date_iso)
        return "weekend-slots"
    async def legacy(*, chat_id, phone, user_text):
        return "legacy-blocker"
    monkeypatch.setattr(dialog, "_show_slots", fake_show_slots)
    monkeypatch.setattr(dialog, "handle_message", legacy)

    patch.install_named_weekend_route()
    answer = asyncio.run(dialog.handle_message("chat", "+77000000000", "в субботу 22 августа"))
    assert answer == "weekend-slots"
    assert calls == ["2026-08-22"]


def test_non_weekend_turn_stays_on_original_handler(monkeypatch) -> None:
    session = {"step": "date", "language": "ru"}
    monkeypatch.setattr(state, "get_session", lambda chat_id: session)
    monkeypatch.setattr(dialog, "_parse_date", lambda text: "2026-08-24")
    monkeypatch.setattr(dialog, "_is_new_patient_consultation", lambda current: True)
    monkeypatch.setattr(dialog, "_mentions_weekend_day", lambda text: False)
    monkeypatch.setattr(dialog, "_is_weekend_date", lambda date_iso: False)
    async def legacy(*, chat_id, phone, user_text): return "weekday-legacy"
    monkeypatch.setattr(dialog, "handle_message", legacy)
    patch.install_named_weekend_route()
    assert asyncio.run(dialog.handle_message("chat", "+77000000000", "в понедельник")) == "weekday-legacy"
