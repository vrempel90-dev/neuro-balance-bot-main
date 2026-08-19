from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("SQLITE_PATH", tempfile.NamedTemporaryFile(delete=False, suffix=".sqlite3").name)
os.environ.setdefault("CRM_BOT_SECRET", "test")
os.environ.setdefault("OPENAI_API_KEY", "")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import ai  # noqa: E402


def test_canonical_prompt_has_precedence_over_dialog_protocol() -> None:
    prompt = ai._full_dialog_brain_system_prompt()
    protocol_pos = prompt.index("Ты — живой AI-администратор клиники Neuro Balance")
    precedence_pos = prompt.index("INSTRUCTION PRECEDENCE — Neuro Balance")
    canonical_pos = prompt.index("# Системный промпт бота Neuro Balance")
    python_override_pos = prompt.rindex("PROJECT OVERRIDES — Python enforces")

    assert protocol_pos < precedence_pos < canonical_pos < python_override_pos
    assert "SYSTEM_PROMPT_rendered.md is the canonical source" in prompt


def test_local_prompts_do_not_override_canonical_window_vocabulary() -> None:
    assert "Не используй слово «окошки»" not in ai.HUMAN_ACK_PROMPT
    assert "НЕ используй слово «окошки»" not in ai.HUMANIZE_REPLY_PROMPT
    assert "сохраняй именно его" in ai.HUMAN_ACK_PROMPT
    assert "сохрани именно это слово" in ai.HUMANIZE_REPLY_PROMPT


def test_fallback_humanizer_preserves_window_word() -> None:
    source = "Есть окошко на 15:00. Вам удобно?"
    result = ai._fallback_humanize(source)
    assert "окошко" in result.lower()
    assert "свободное время для записи" not in result.lower()


def test_strict_humanizer_guard_rejects_canonical_vocabulary_rewrite() -> None:
    base = "Есть окошко на 15:00. Вам удобно?"
    same = "Есть окошко на 15:00. Вам удобно?"
    rewritten = "Есть свободное время для записи на 15:00. Вам удобно?"

    assert ai._humanize_guard_ok(base, same) is True
    assert ai._humanize_guard_ok(base, rewritten) is False


def test_slot_reply_with_window_word_is_deterministic_for_legacy_humanizer() -> None:
    # Regression contract: slot facts and canonical vocabulary should never be
    # sent through a stylistic rewrite that can alter CRM facts or wording.
    source = Path(ai.__file__).read_text(encoding="utf-8")
    assert 'or "окошк" in lower' in source


def test_time_question_detector_understands_canonical_window_word() -> None:
    assert ai._infer_last_question_type("Есть окошко на 15:00, Вам удобно?") == "time"
