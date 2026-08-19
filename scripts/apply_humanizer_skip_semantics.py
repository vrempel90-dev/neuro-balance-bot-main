from __future__ import annotations

from pathlib import Path

PATH = Path("ai.py")


def main() -> None:
    text = PATH.read_text(encoding="utf-8")
    old = '''    detail = _openai_config_missing_detail(settings, chat_id=str(session.get("chat_id") or session.get("phone") or ""), step=str(session.get("step") or session.get("current_step") or "start"), brain=False)
    if detail["missing_keys"] or detail["disabled_flags"]:
        debug["openai_skip_reason"] = "config_missing"
        debug["openai_config_missing_detail"] = detail
        debug["openai_missing_keys"] = detail["missing_keys"]
        debug["openai_disabled_flags"] = detail["disabled_flags"]
        _log_openai_config_missing_detail(detail)
        return base_answer, debug
'''
    new = '''    detail = _openai_config_missing_detail(settings, chat_id=str(session.get("chat_id") or session.get("phone") or ""), step=str(session.get("step") or session.get("current_step") or "start"), brain=False)
    if detail["missing_keys"] or detail["disabled_flags"]:
        # A deliberately disabled humanizer is not a missing OpenAI configuration.
        # Keep brain/runtime diagnostics truthful: missing credentials remain
        # config_missing; the independent style layer reports humanizer_disabled.
        debug["openai_skip_reason"] = "config_missing" if detail["missing_keys"] else "humanizer_disabled"
        debug["openai_config_missing_detail"] = detail
        debug["openai_missing_keys"] = detail["missing_keys"]
        debug["openai_disabled_flags"] = detail["disabled_flags"]
        _log_openai_config_missing_detail(detail)
        return base_answer, debug
'''
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"humanizer config block: expected one match, got {count}")
    text = text.replace(old, new, 1)
    compile(text, str(PATH), "exec")
    PATH.write_text(text, encoding="utf-8")
    print("humanizer skip semantics corrected")


if __name__ == "__main__":
    main()
