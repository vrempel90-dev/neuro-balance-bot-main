from __future__ import annotations

import re
from pathlib import Path


PATH = Path("ai.py")
MARKER = "Клинические факты, профиль, цены, адрес, методы и стиль"


def sub_once(text: str, pattern: str, replacement: str, label: str) -> str:
    updated, count = re.subn(pattern, replacement, text, count=1, flags=re.DOTALL)
    if count != 1:
        raise RuntimeError(f"{label}: expected exactly one match, got {count}")
    return updated


def main() -> None:
    text = PATH.read_text(encoding="utf-8")
    if MARKER in text:
        print("canonical fact de-dup already applied")
        return

    text = sub_once(
        text,
        r"\nКлиника Neuro Balance помогает с:\n.*?\nКлиника НЕ занимается:\n.*?\n\nСтрогий сценарий:",
        """
Клинические факты, профиль, цены, адрес, методы и стиль бери ТОЛЬКО из блока
CANONICAL CLINIC PROMPT, который добавляется к этой инструкции ниже.
Не копируй и не переопределяй здесь факты клиники. Если данных нет в canonical
prompt или в результате разрешённого Python tool — не выдумывай их.

Строгий сценарий:""",
        "profile/facts duplicate block",
    )

    text = sub_once(
        text,
        r"Возрастные правила клиники \(до 16 и более 75 лет\) считаются по возрасту\nпациента, а не по возрасту того, кто написал\.\n",
        "Возрастные и иные клинические ограничения определяются canonical prompt и Python-guard'ами; здесь их не дублируй.\n",
        "age policy duplicate",
    )

    text = sub_once(
        text,
        r"\nЦена:\n.*?\nНе используй фразу [“\"]Вижу Ваш запрос[”\"] без необходимости\.\n",
        """
Все ответы по цене, МРТ/снимкам, методам, адресу, графику, врачам, акциям,
рассрочке, возвратам, профилю клиники, тону и лексике формируй только по
CANONICAL CLINIC PROMPT. Этот protocol prompt не является источником таких фактов.
""",
        "mutable clinic facts/style block",
    )

    # Keep the protocol useful if the canonical file is unexpectedly unavailable:
    # it may classify/structure, but it must never invent clinic facts.
    old_fallback = """    if not rendered:
        return OPENAI_DIALOG_BRAIN_SYSTEM_PROMPT
"""
    new_fallback = """    if not rendered:
        return OPENAI_DIALOG_BRAIN_SYSTEM_PROMPT + "\\n\\nCANONICAL CLINIC PROMPT is unavailable. Do not invent any clinic facts; use safe structured fallback/handoff for factual clinic questions."
"""
    if text.count(old_fallback) != 1:
        raise RuntimeError(f"canonical missing fallback: expected one match, got {text.count(old_fallback)}")
    text = text.replace(old_fallback, new_fallback, 1)

    compile(text, str(PATH), "exec")
    PATH.write_text(text, encoding="utf-8")
    print("canonical fact de-dup applied")


if __name__ == "__main__":
    main()
