from __future__ import annotations

import dialog


def install_weekend_legacy_block_bypass() -> None:
    """Let safe in-flow weekend branches continue to canonical ``_show_slots``.

    The legacy weekend blocker is evaluated inside ``dialog.handle_message`` only
    after its normal admission/CRM/operator protections. Replacing only the
    weekend-mention predicate prevents the three legacy early returns without
    wrapping or bypassing ``handle_message`` itself. The canonical ``_show_slots``
    wrapper then performs the actual weekend-to-weekday CRM lookup.
    """
    current = dialog._mentions_weekend_day
    if getattr(current, "_neuro_weekend_legacy_bypass", False):
        return

    def allow_canonical_slot_path(text: str) -> bool:
        # Preserve the original parser for diagnostics, but never activate the
        # legacy early-return blocker. Non-weekend text was already False.
        current(text)
        return False

    allow_canonical_slot_path._neuro_weekend_legacy_bypass = True  # type: ignore[attr-defined]
    allow_canonical_slot_path._neuro_weekend_legacy_original = current  # type: ignore[attr-defined]
    dialog._mentions_weekend_day = allow_canonical_slot_path
