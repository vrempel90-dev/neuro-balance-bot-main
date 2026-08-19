from __future__ import annotations

from datetime import datetime, timedelta, timezone

import repair_service


def test_night_window_boundaries_asia_almaty() -> None:
    # Asia/Almaty is UTC+5: 15:00 UTC = 20:00 local, 03:00 UTC = 08:00 local.
    assert repair_service._night_window(datetime(2026, 8, 19, 15, 0, tzinfo=timezone.utc)) is True
    assert repair_service._night_window(datetime(2026, 8, 20, 2, 59, tzinfo=timezone.utc)) is True
    assert repair_service._night_window(datetime(2026, 8, 20, 3, 0, tzinfo=timezone.utc)) is False
    assert repair_service._night_window(datetime(2026, 8, 20, 14, 59, tzinfo=timezone.utc)) is False


def test_sanitize_events_keeps_only_real_wazzup_conversations_and_redacts_ids() -> None:
    now = datetime.now(timezone.utc)
    created = now.isoformat()
    events = [
        {
            "chat_id": "77001234567",
            "event_type": "incoming_message_received",
            "created_at": created,
            "payload": {
                "source": "wazzup",
                "kind": "text",
                "text_preview": "Позвоните мне +7 700 123 45 67, болит спина",
                "phone": "+77001234567",
            },
        },
        {
            "chat_id": "77001234567",
            "event_type": "dialog_result",
            "created_at": created,
            "payload": {
                "answer_preview": "Подскажите, сколько Вам лет?",
                "answer_empty": False,
                "step": "age",
                "phone_raw": "77001234567",
            },
        },
        {
            "chat_id": "debug-chat",
            "event_type": "incoming_message_received",
            "created_at": created,
            "payload": {"source": "debug", "text_preview": "synthetic"},
        },
    ]

    result = repair_service._sanitize_events(events, cutoff=now - timedelta(minutes=5))

    assert len(result) == 2
    assert all(item["conversation_id"] != "77001234567" for item in result)
    assert "77001234567" not in str(result)
    assert "+7 700 123 45 67" not in str(result)
    assert "[phone]" in str(result)
    assert "debug-chat" not in str(result)


def test_unexpected_empty_answer_is_a_signal_but_expected_silence_is_not() -> None:
    assert "unexpected_empty_answer" in repair_service._event_signal(
        "dialog_result",
        {"answer_empty": True, "no_reply_reason": ""},
    )
    assert repair_service._event_signal(
        "dialog_result",
        {"answer_empty": True, "no_reply_reason": "working_hours_ai_disabled"},
    ) == []


def test_known_brain_guard_and_language_mismatch_are_signals() -> None:
    signals = repair_service._event_signal(
        "dialog_result",
        {
            "answer_empty": False,
            "openai_brain_guard_reason": "asked_name_too_early",
            "language_guard_result": "mixed_language_response",
        },
    )
    assert "brain_guard:asked_name_too_early" in signals
    assert "language_guard:mixed_language_response" in signals


def test_booking_confirmation_must_be_visible_in_crm() -> None:
    signals = repair_service._event_signal(
        "dialog_result",
        {"booking_confirmed": True, "booking_visible_in_crm": False},
    )
    assert "booking_confirmation_not_visible_in_crm" in signals


def test_only_whitelisted_payload_fields_reach_claude() -> None:
    now = datetime.now(timezone.utc)
    created = now.isoformat()
    result = repair_service._sanitize_events(
        [
            {
                "chat_id": "77005550000",
                "event_type": "incoming_message_received",
                "created_at": created,
                "payload": {"source": "wazzup", "text_preview": "Хочу записаться"},
            },
            {
                "chat_id": "77005550000",
                "event_type": "dialog_result",
                "created_at": created,
                "payload": {
                    "answer_preview": "Что Вас беспокоит?",
                    "step": "complaint",
                    "crm_raw_response": {"patient": "secret"},
                    "authorization": "Bearer secret",
                    "patient_name": "Иван Иванов",
                },
            },
        ],
        cutoff=now - timedelta(minutes=5),
    )
    payload = result[-1]["payload"]
    assert "crm_raw_response" not in payload
    assert "authorization" not in payload
    assert "patient_name" not in payload
