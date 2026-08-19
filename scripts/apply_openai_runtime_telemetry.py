from __future__ import annotations

from pathlib import Path


def replace_once(path: Path, old: str, new: str, label: str) -> None:
    text = path.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected one match, got {count}")
    text = text.replace(old, new, 1)
    compile(text, str(path), "exec")
    path.write_text(text, encoding="utf-8")


def main() -> None:
    state_path = Path("state.py")
    ai_path = Path("ai.py")
    dialog_path = Path("dialog.py")
    main_path = Path("main.py")
    budget_path = Path("ai_budget.py")

    # Railway stdout must expose AI evidence, while the existing sanitizer keeps secrets redacted.
    replace_once(
        state_path,
        '    "openai_called",\n    "openai_skipped",\n    "humanize_skipped",\n',
        '    "openai_called",\n    "openai_brain_called",\n    "openai_brain_decision",\n    "openai_config_missing_detail",\n    "openai_error_detail",\n    "ai_usage_recorded",\n    "openai_skipped",\n    "humanize_skipped",\n',
        "stdout AI diagnostic events",
    )

    # Every successful brain call returns concrete usage telemetry in debug.
    replace_once(
        ai_path,
        '    detail = _openai_config_missing_detail(settings, chat_id=str(session.get("chat_id") or ""), step=str(session.get("step") or session.get("current_step") or "start"), brain=True)\n',
        '    debug.update({"openai_prompt_tokens": 0, "openai_completion_tokens": 0, "openai_cached_tokens": 0, "openai_cost_usd": 0.0})\n    detail = _openai_config_missing_detail(settings, chat_id=str(session.get("chat_id") or ""), step=str(session.get("step") or session.get("current_step") or "start"), brain=True)\n',
        "brain usage defaults",
    )
    replace_once(
        ai_path,
        '        ai_budget.record_usage(response, model=model, purpose=ai_budget.PURPOSE_BRAIN)\n        content = response.choices[0].message.content or ""\n',
        '        usage = ai_budget.record_usage(response, model=model, purpose=ai_budget.PURPOSE_BRAIN)\n        debug.update({\n            "openai_prompt_tokens": int(usage.get("prompt_tokens") or 0),\n            "openai_completion_tokens": int(usage.get("completion_tokens") or 0),\n            "openai_cached_tokens": int(usage.get("cached_tokens") or 0),\n            "openai_cost_usd": float(usage.get("cost_usd") or 0.0),\n        })\n        content = response.choices[0].message.content or ""\n',
        "brain usage capture",
    )

    # Persist telemetry in the session so webhook/result logs can surface it.
    replace_once(
        dialog_path,
        '    session["openai_disabled_flags"] = []\n    session["humanize_skipped_because_brain_valid"] = False\n',
        '    session["openai_disabled_flags"] = []\n    session["openai_prompt_tokens"] = 0\n    session["openai_completion_tokens"] = 0\n    session["openai_cached_tokens"] = 0\n    session["openai_cost_usd"] = 0.0\n    session["humanize_skipped_because_brain_valid"] = False\n',
        "session AI telemetry reset",
    )
    replace_once(
        dialog_path,
        '        "openai_config_missing_detail", "openai_missing_keys", "openai_disabled_flags",\n    ):\n',
        '        "openai_config_missing_detail", "openai_missing_keys", "openai_disabled_flags",\n        "openai_prompt_tokens", "openai_completion_tokens", "openai_cached_tokens", "openai_cost_usd",\n    ):\n',
        "session AI telemetry apply",
    )

    # Expose per-turn AI proof in dialog debug and the compact production dialog_result event.
    replace_once(
        main_path,
        '        "openai_disabled_flags": session.get("openai_disabled_flags") or [],\n        "humanize_skipped_because_brain_valid": bool(session.get("humanize_skipped_because_brain_valid")),\n',
        '        "openai_disabled_flags": session.get("openai_disabled_flags") or [],\n        "openai_prompt_tokens": int(session.get("openai_prompt_tokens") or 0),\n        "openai_completion_tokens": int(session.get("openai_completion_tokens") or 0),\n        "openai_cached_tokens": int(session.get("openai_cached_tokens") or 0),\n        "openai_cost_usd": float(session.get("openai_cost_usd") or 0.0),\n        "humanize_skipped_because_brain_valid": bool(session.get("humanize_skipped_because_brain_valid")),\n',
        "dialog debug AI telemetry",
    )
    replace_once(
        main_path,
        '        **{k: debug[k] for k in ("source", "local_time", "bot_work_time_now", "working_hours_allowed", "step", "gate_reason", "no_reply_reason", "ai_muted", "manual_takeover", "ai_lead_started", "openai_used", "openai_skip_reason")},\n',
        '        **{k: debug[k] for k in ("source", "local_time", "bot_work_time_now", "working_hours_allowed", "step", "gate_reason", "no_reply_reason", "ai_muted", "manual_takeover", "ai_lead_started", "openai_used", "openai_skip_reason")},\n        "openai_brain_used": debug["openai_brain_used"],\n        "openai_brain_model": debug["openai_brain_model"],\n        "openai_brain_skip_reason": debug["openai_brain_skip_reason"],\n        "openai_prompt_tokens": debug["openai_prompt_tokens"],\n        "openai_completion_tokens": debug["openai_completion_tokens"],\n        "openai_cached_tokens": debug["openai_cached_tokens"],\n        "openai_cost_usd": debug["openai_cost_usd"],\n        "openai_missing_keys": debug["openai_missing_keys"],\n        "openai_disabled_flags": debug["openai_disabled_flags"],\n        "openai_config_missing_detail": debug["openai_config_missing_detail"],\n',
        "dialog_result AI telemetry",
    )

    # Startup must confirm configuration presence without exposing secrets.
    replace_once(
        main_path,
        '        "new_leads_only": bool(getattr(settings, "new_leads_only", True)),\n        "wazzup_allowed_channel_ids_count": len(allowed_channel_ids),\n',
        '        "new_leads_only": bool(getattr(settings, "new_leads_only", True)),\n        "openai_api_key_present": bool(str(getattr(settings, "openai_api_key", "") or "").strip()),\n        "ai_enabled": bool(getattr(settings, "ai_enabled", True)),\n        "openai_brain_enabled": bool(getattr(settings, "openai_brain_enabled", True)),\n        "openai_brain_model": str(getattr(settings, "ai_brain_model", "") or getattr(settings, "openai_model", "") or ""),\n        "openai_humanize_replies": bool(getattr(settings, "openai_humanize_replies", True)),\n        "wazzup_allowed_channel_ids_count": len(allowed_channel_ids),\n',
        "startup AI configuration",
    )

    # Budget accounting failures remain non-fatal but are no longer invisible.
    replace_once(
        budget_path,
        '    except Exception:\n        # Учёт расхода никогда не должен ронять ответ пациенту.\n        return {"cost_usd": 0.0}\n',
        '    except Exception as exc:\n        # Учёт расхода никогда не должен ронять ответ пациенту, но failure должен быть видим в Railway.\n        if state is not None:\n            try:\n                state.log_event("system", "ai_usage_record_failed", {\n                    "level": "warning",\n                    "purpose": purpose,\n                    "model": model,\n                    "error_type": type(exc).__name__,\n                    "error_preview": str(exc).replace("\\n", " ")[:300],\n                })\n            except Exception:\n                pass\n        return {"cost_usd": 0.0}\n',
        "AI usage failure observability",
    )

    print("OpenAI runtime telemetry migration applied")


if __name__ == "__main__":
    main()
