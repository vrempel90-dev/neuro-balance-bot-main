from __future__ import annotations

from pathlib import Path

DIALOG = Path("dialog.py")
TEST = Path("tests/test_production_booking_flow.py")
MARKER = "async def _recover_booking_slot_race("


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected exactly one match, got {count}")
    return text.replace(old, new, 1)


def main() -> None:
    text = DIALOG.read_text(encoding="utf-8")
    if MARKER not in text:
        old_failure = '''def _booking_failed_answer(session: dict[str, Any]) -> str:
    return _final_confirmation_text(session)
'''
        new_failure = '''def _booking_failed_answer(session: dict[str, Any]) -> str:
    return _tr(
        session,
        "Не удалось подтвердить запись в CRM. Я передала информацию администратору — он проверит запись и свяжется с Вами 🌿",
        "Жазбаны CRM-де растай алмадым. Ақпаратты әкімшіге бердім — ол жазбаны тексеріп, Сізбен байланысады 🌿",
    )


def _booking_race_answer(session: dict[str, Any], slots: list[dict[str, Any]]) -> str:
    if slots:
        times = _join_times([_slot_time(slot) for slot in slots])
        return _tr(
            session,
            f"Пока оформляла запись, это окошко уже заняли 🌿 Сейчас свободны: {times}. Какое время Вам удобно?",
            f"Жазбаны рәсімдеп жатқанда бұл уақыт бос болмай қалды 🌿 Қазір бос уақыттар: {times}. Қай уақыт ыңғайлы?",
        )
    return _tr(
        session,
        "Пока оформляла запись, это окошко уже заняли 🌿 На этот день свободных вариантов больше не вижу. Подскажите другой удобный день — проверю актуальные окошки.",
        "Жазбаны рәсімдеп жатқанда бұл уақыт бос болмай қалды 🌿 Бұл күнге басқа бос уақыт көріп тұрған жоқпын. Басқа ыңғайлы күнді айтыңызшы — өзекті уақыттарды тексеремін.",
    )


async def _recover_booking_slot_race(
    chat_id: str,
    session: dict[str, Any],
    selected_slot: dict[str, Any],
    exc: crm.CRMResponseError,
) -> str | None:
    """Recover when a CRM booking fails because the offered slot disappeared.

    The CRM route may surface uniqueness/slot conflicts as a generic 500, so the
    only trustworthy signal is current CRM availability after clearing cache.
    Authentication/permission failures are never treated as slot races.
    """
    if exc.status_code in {401, 403}:
        return None

    date = _slot_date(selected_slot).strip()
    doctor_login = _slot_doctor_login(selected_slot).strip()
    if not date or not doctor_login:
        return None

    try:
        if hasattr(crm, "clear_slots_cache"):
            crm.clear_slots_cache(date)
        data = await crm.check_slots(date, doctor_login=doctor_login)
        if isinstance(data, dict):
            data.setdefault("date", date)
        fresh_slots = _format_slots(data if isinstance(data, dict) else {}, max_count=1000)
    except Exception as refresh_exc:
        _safe_log(chat_id, "crm_booking_race_recheck_failed", {
            "status_code": exc.status_code,
            "date": date,
            "doctor_login": doctor_login,
            "error": str(refresh_exc)[:300],
        })
        return None

    if any(_same_slot_identity(candidate, selected_slot) for candidate in fresh_slots):
        return None

    same_doctor = [
        candidate
        for candidate in fresh_slots
        if _slot_doctor_login(candidate).strip().lower() == doctor_login.lower()
    ]
    session["booking_confirmed"] = False
    session["booking_ready"] = False
    session["manual_takeover"] = False
    session["escalated"] = False
    session["ai_muted"] = False
    session["handoff_reason"] = ""
    session["crm_result"] = "slot_race_recovered"
    session["slot_race_recovered"] = True
    session.pop("selected_slot", None)
    session["selected_time"] = ""
    session["last_slots"] = same_doctor[:5]
    session["step"] = "time" if session["last_slots"] else "date"
    _safe_log(chat_id, "crm_booking_slot_race_recovered", {
        "status_code": exc.status_code,
        "date": date,
        "doctor_login": doctor_login,
        "requested_time": _slot_time(selected_slot),
        "replacement_slots_count": len(session["last_slots"]),
    })
    return _booking_race_answer(session, session["last_slots"])
'''
        text = replace_once(text, old_failure, new_failure, "honest booking failure + race helper")

        old_except = '''    except crm.CRMResponseError as exc:
        log_payload = {
'''
        new_except = '''    except crm.CRMResponseError as exc:
        race_answer = await _recover_booking_slot_race(chat_id, session, slot, exc)
        if race_answer is not None:
            return race_answer
        log_payload = {
'''
        text = replace_once(text, old_except, new_except, "CRM response race recovery hook")

        compile(text, str(DIALOG), "exec")
        DIALOG.write_text(text, encoding="utf-8")

    test = TEST.read_text(encoding="utf-8")
    old_failure_assert = '''def test_booking_500_soft_client_text_honest_state(monkeypatch: Any) -> None:
'''
    start = test.find(old_failure_assert)
    if start < 0:
        raise RuntimeError("CRM 500 regression test not found")
    next_test = test.find("\ndef test_", start + len(old_failure_assert))
    end = len(test) if next_test < 0 else next_test
    block = test[start:end]
    old_line = '    assert r == "Дана, запись подтверждена 🌿 С Вами свяжется специалист."\n'
    new_lines = '    assert "подтверждена" not in r.lower()\n    assert "не удалось" in r.lower() or "администратор" in r.lower()\n'
    if old_line in block:
        block = block.replace(old_line, new_lines, 1)
        test = test[:start] + block + test[end:]
        compile(test, str(TEST), "exec")
        TEST.write_text(test, encoding="utf-8")
    elif 'assert "подтверждена" not in r.lower()' not in block:
        raise RuntimeError("CRM 500 failure assertion not found in target test")

    print("honest booking failure and race recovery migration applied")


if __name__ == "__main__":
    main()
