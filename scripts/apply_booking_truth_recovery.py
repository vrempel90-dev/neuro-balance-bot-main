from pathlib import Path

p = Path('dialog.py')
s = p.read_text(encoding='utf-8')

old = '''def _booking_failed_answer(session: dict[str, Any]) -> str:\n    return _final_confirmation_text(session)\n'''
new = '''def _booking_failed_answer(session: dict[str, Any]) -> str:\n    return _tr(\n        session,\n        "Не получилось подтвердить запись в CRM. Ваша запись пока не подтверждена — передам диалог администратору, чтобы он помог завершить запись 🌿",\n        "CRM жүйесінде жазбаны растау мүмкін болмады. Жазбаңыз әзірге расталған жоқ — аяқтауға көмектесу үшін диалогты әкімшіге беремін 🌿",\n    )\n'''
if old not in s:
    raise SystemExit('booking_failed_answer anchor not found')
s = s.replace(old, new, 1)

anchor = '''    except crm.CRMResponseError as exc:\n        log_payload = {\n'''
if anchor not in s:
    raise SystemExit('CRMResponseError anchor not found')
insert = '''    except crm.CRMResponseError as exc:\n        # Polyglot-style race recovery: a slot can become stale between selection and final booking.\n        # Treat HTTP 409 as a recoverable scheduling race, never as a confirmed booking.\n        if exc.status_code == 409:\n            session["booking_confirmed"] = False\n            session["crm_result"] = "slot_conflict"\n            session["booking_ready"] = False\n            selected_date = _slot_date(slot) or str(session.get("preferred_date") or "")\n            selected_login = _slot_doctor_login(slot)\n            try:\n                crm.clear_slots_cache(selected_date or None)\n                refreshed_data = await crm.check_slots(selected_date, doctor_login=selected_login or None)\n                refreshed_slots = _format_slots(refreshed_data, max_count=5)\n                refreshed_slots = [\n                    candidate for candidate in refreshed_slots\n                    if not selected_login or _slot_doctor_login(candidate) == selected_login\n                ]\n                session["last_slots"] = refreshed_slots[:5]\n                session.pop("selected_slot", None)\n                session["selected_time"] = ""\n                session["selected_date"] = ""\n                session["step"] = "time" if session["last_slots"] else "date"\n                session["handoff_reason"] = ""\n                session["manual_takeover"] = False\n                session["escalated"] = False\n                session["ai_muted"] = False\n                _safe_log(chat_id, "crm_booking_slot_conflict_recovered", {\n                    "date": selected_date,\n                    "doctor_login": selected_login,\n                    "old_time": _slot_time(slot),\n                    "new_slots_count": len(session["last_slots"]),\n                })\n                if session["last_slots"]:\n                    return _mandatory_step_prompt(session, "time")\n                return _mandatory_step_prompt(session, "date")\n            except Exception as refresh_exc:\n                _safe_log(chat_id, "crm_booking_slot_conflict_refresh_failed", {\n                    "date": selected_date,\n                    "doctor_login": selected_login,\n                    "error": str(refresh_exc)[:300],\n                })\n\n        log_payload = {\n'''
s = s.replace(anchor, insert, 1)

compile(s, 'dialog.py', 'exec')
p.write_text(s, encoding='utf-8')

# Update the old regression that incorrectly expected a success phrase on CRM 500.
t = Path('tests/test_production_booking_flow.py')
ts = t.read_text(encoding='utf-8')
old_assert = '    assert r == "Дана, запись подтверждена 🌿 С Вами свяжется специалист."\n'
new_assert = '    assert "запись подтверждена" not in r.lower()\n    assert "не подтверждена" in r.lower() or "не получилось" in r.lower()\n'
if old_assert not in ts:
    raise SystemExit('legacy false-confirmation assertion not found')
ts = ts.replace(old_assert, new_assert, 1)
compile(ts, 'tests/test_production_booking_flow.py', 'exec')
t.write_text(ts, encoding='utf-8')
print('booking truth/race recovery migration applied')
