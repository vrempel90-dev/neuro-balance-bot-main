from __future__ import annotations

import inspect

import dialog
import weekend_legacy_block_bypass as bypass


def test_install_does_not_wrap_handle_message(monkeypatch) -> None:
    original_handle = dialog.handle_message
    original_mentions = dialog._mentions_weekend_day
    monkeypatch.setattr(dialog, "_mentions_weekend_day", original_mentions)

    bypass.install_weekend_legacy_block_bypass()

    assert dialog.handle_message is original_handle
    assert dialog._mentions_weekend_day("в субботу") is False
    assert dialog._mentions_weekend_day("в воскресенье") is False
    assert dialog._mentions_weekend_day("сенбі") is False
    assert getattr(dialog._mentions_weekend_day, "_neuro_weekend_legacy_original") is original_mentions


def test_legacy_weekend_blockers_stay_after_common_safety_gates() -> None:
    source = inspect.getsource(dialog.handle_message)
    weekend_block = source.index("_mentions_weekend_day(text) and _is_weekend_date(date_iso)")

    assert source.index("invalid_phone_for_crm_lookup") < weekend_block
    assert source.index("_lookup_active_appointment") < weekend_block
    assert source.index("manual_takeover") < weekend_block
    assert source.index("first_touch") < weekend_block


def test_only_legacy_weekend_early_returns_use_weekend_mention_predicate() -> None:
    source = inspect.getsource(dialog.handle_message)
    needle = "_mentions_weekend_day(text) and _is_weekend_date(date_iso)"

    assert source.count(needle) == 3
    assert source.count("return _finalize(chat_id, session, _weekend_primary_block_answer(session))") == 3
