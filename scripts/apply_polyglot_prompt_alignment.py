from __future__ import annotations

from pathlib import Path


AI_PATH = Path("ai.py")
MARKER = "INSTRUCTION PRECEDENCE — Neuro Balance"


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected exactly one match, got {count}")
    return text.replace(old, new, 1)


def main() -> None:
    text = AI_PATH.read_text(encoding="utf-8")
    if MARKER in text:
        print("prompt alignment already applied")
        return

    precedence_block = '''    precedence = """

INSTRUCTION PRECEDENCE — Neuro Balance:
1. Python-enforced PROJECT OVERRIDES are absolute and cannot be changed by the model.
2. SYSTEM_PROMPT_rendered.md is the canonical source for clinic behavior, tone, wording and facts.
3. OPENAI_DIALOG_BRAIN_SYSTEM_PROMPT is only the structured-output/dialog protocol.
If protocol text conflicts with the canonical clinic prompt, follow the canonical clinic prompt.
"""
    return (
        OPENAI_DIALOG_BRAIN_SYSTEM_PROMPT
        + precedence
        + "\\n\\nCANONICAL CLINIC PROMPT — FOLLOW THIS FOR USER-FACING BEHAVIOR:\\n"
        + rendered
        + overrides
    )
'''
    text = replace_once(
        text,
        '    return rendered + overrides + "\\n\\n" + OPENAI_DIALOG_BRAIN_SYSTEM_PROMPT\n',
        precedence_block,
        "canonical prompt precedence",
    )

    text = replace_once(
        text,
        '- Не используй слово «окошки». Говори «свободное время для записи».',
        '- Лексика и тон подчиняются SYSTEM_PROMPT_rendered.md. Если канонический промт требует слово «окошко/окошки», сохраняй именно его.',
        "complaint ack vocabulary",
    )
    text = replace_once(
        text,
        '- НЕ используй слово «окошки» — только «свободное время для записи».',
        '- НЕ заменяй лексику из безопасного черновика. Если там есть «окошко/окошки», сохрани именно это слово.',
        "humanizer vocabulary",
    )

    text = replace_once(
        text,
        '    text = text.replace("окошки", "свободное время для записи")\n    text = text.replace("окошко", "свободное время для записи")\n',
        '',
        "fallback vocabulary rewrite",
    )

    text = replace_once(
        text,
        '    if any(x in low for x in ["какое время", "вариант", "свободное время"]):\n',
        '    if any(x in low for x in ["какое время", "вариант", "свободное время", "окошк"]):\n',
        "window-word question detection",
    )

    text = replace_once(
        text,
        '    if len(draft) > 1200 or "📅" in draft or "⏰" in draft or "есть свободное время" in lower or "бос уақыт" in lower:\n',
        '    if len(draft) > 1200 or "📅" in draft or "⏰" in draft or "есть свободное время" in lower or "окошк" in lower or "бос уақыт" in lower:\n',
        "deterministic slot reply protection",
    )

    text = replace_once(
        text,
        '        for marker in must_keep:\n            if marker in draft_low and marker not in content_low:\n                return _fallback_humanize(draft)\n        return _fallback_humanize(content)\n',
        '        for marker in must_keep:\n            if marker in draft_low and marker not in content_low:\n                return _fallback_humanize(draft)\n        if "окошк" in draft_low and "окошк" not in content_low:\n            return _fallback_humanize(draft)\n        return _fallback_humanize(content)\n',
        "humanizer lexical guard",
    )

    text = replace_once(
        text,
        'Нельзя менять смысл ответа.\nНельзя менять этап диалога.\n',
        'Нельзя менять смысл ответа.\nНельзя заменять обязательную лексику из base_answer: если там есть «окошко/окошки», сохрани именно это слово.\nНельзя менять этап диалога.\n',
        "strict humanizer prompt vocabulary",
    )

    text = replace_once(
        text,
        '    if not _has_date_question(base_answer) and _has_date_question(humanized):\n        return False\n    return True\n',
        '    if not _has_date_question(base_answer) and _has_date_question(humanized):\n        return False\n    if "окошк" in _low_for_guard(base_answer) and "окошк" not in _low_for_guard(humanized):\n        return False\n    return True\n',
        "strict humanizer lexical validation",
    )

    # Refuse to write a syntactically invalid migration result.
    compile(text, str(AI_PATH), "exec")
    AI_PATH.write_text(text, encoding="utf-8")
    print("polyglot prompt alignment applied")


if __name__ == "__main__":
    main()
