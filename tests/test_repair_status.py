from __future__ import annotations

import repair_status


def _event(created_at: str, conversation_id: str, event: str, signals=None):
    return {
        "created_at": created_at,
        "conversation_id": conversation_id,
        "event": event,
        "payload": {},
        "signals": list(signals or []),
    }


def test_scan_counters_deduplicate_overlapping_windows(tmp_path, monkeypatch):
    monkeypatch.setattr(repair_status, "_STATUS_PATH", tmp_path / "status.json")
    result = {
        "events": [
            _event("2026-08-20T00:00:00+00:00", "conv-a", "dialog_result", ["unexpected_empty_answer"]),
            _event("2026-08-20T00:00:02+00:00", "conv-a", "bot_processing_finish"),
            _event("2026-08-20T00:01:00+00:00", "conv-b", "dialog_result"),
        ]
    }

    repair_status.record_scan(result)
    repair_status.record_scan(result)

    data = repair_status.snapshot(
        production_reachable=True,
        prs={"available": True, "confirmed_bugs": 0, "fixes_prepared": 0, "merged": 0, "last_pr": None},
    )
    assert data["scans"] == 2
    assert data["checked_dialogues"] == 2
    assert data["checked_events"] == 3
    assert data["signals"] == 1
    assert data["last_scan_dialogues"] == 2


def test_claude_calls_and_pr_metrics_are_separate(tmp_path, monkeypatch):
    monkeypatch.setattr(repair_status, "_STATUS_PATH", tmp_path / "status.json")
    repair_status.record_claude_call(200)
    repair_status.record_claude_call(200)

    data = repair_status.snapshot(
        production_reachable=True,
        prs={
            "available": True,
            "confirmed_bugs": 1,
            "fixes_prepared": 1,
            "merged": 0,
            "last_pr": {"number": 92, "url": "https://example.invalid/92", "state": "open"},
        },
    )
    assert data["claude_api_calls"] == 2
    assert data["confirmed_bugs"] == 1
    assert data["fixes_prepared"] == 1
    assert data["merged_fixes"] == 0
    assert data["last_pr"]["number"] == 92


def test_dashboard_contains_only_aggregate_labels():
    page = repair_status.dashboard_document()
    assert "Проверено диалогов за ночь" in page
    assert "Подтверждено багов" in page
    assert "Переписки, телефоны, имена и ключи" in page
    assert "text_preview" not in page
    assert "patient_name" not in page
