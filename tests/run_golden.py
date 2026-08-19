"""Раннер золотого набора диалогов.

Гоняет сценарии из golden_cases.json через handle_message и печатает
снапшот «пациент → бот» вместе с шагом воронки и языком.

    python tests/run_golden.py                  # весь набор
    python tests/run_golden.py 13 33            # только эти сценарии
    python tests/run_golden.py --json out.json  # снапшот в файл

CRM здесь замокан: подключаться к боевому CRM нельзя. OpenAI не
используется (ключ пустой), поэтому снапшот фиксирует детерминированный
Python-путь и работу слоёв валидации — именно то, что мы меняем.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

os.environ.setdefault("SQLITE_PATH", tempfile.NamedTemporaryFile(delete=False, suffix=".sqlite3").name)
os.environ.setdefault("CRM_BOT_SECRET", "test")
os.environ.setdefault("OPENAI_API_KEY", "")
os.environ.setdefault("NEW_LEADS_ONLY", "false")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import crm  # noqa: E402
import state  # noqa: E402
from dialog import handle_message  # noqa: E402

CASES_PATH = Path(__file__).parent / "golden_cases.json"
PHONE = "77011234567"


async def _fake_lookup(phone: str) -> dict[str, Any]:
    return {
        "ok": True, "status_code": 200, "phone_raw": phone,
        "phone_normalized": phone, "phone_variants": [phone],
        "endpoint_url": "test", "query_params": {},
        "appointments_raw_count": 0, "appointments": [], "appointment": None,
        "appointments_cache": [], "active_count": 0, "raw": {"found": False},
    }


async def _fake_patient_lookup(phone: str) -> dict[str, Any]:
    return {"found": False}


async def _fake_check_slots(date: str, doctor_login: str | None = None) -> dict[str, Any]:
    """Два стабильных слота — чтобы набор доходил до выбора времени.

    Слоты приходят ТОЛЬКО отсюда: любое время в ответе бота, которого нет в
    этом списке, означает выдуманный слот.
    """
    return {"availability": [{
        "doctorLogin": "test_doctor",
        "doctorName": "Тестовый Врач",
        "availableSlots": ["10:00", "12:00"],
    }]}


FAKE_SLOT_TIMES = ("10:00", "12:00")


def install_offline_crm() -> None:
    crm.lookup_active_appointments_by_phone = _fake_lookup  # type: ignore[assignment]
    crm.patient_lookup = _fake_patient_lookup  # type: ignore[assignment]
    crm.check_slots = _fake_check_slots  # type: ignore[assignment]


def load_cases() -> list[dict[str, Any]]:
    return json.loads(CASES_PATH.read_text(encoding="utf-8"))


def _answer_via_main(chat_id: str, message: str) -> str:
    """Полный production-путь, включая входные guard'ы main.py.

    Нужен сценариям, где проверяется именно блокировка ответа (ночное окно,
    вмешательство администратора, дубликат вебхука): эти guard'ы живут в
    main, а не в handle_message.
    """
    import main

    payload = {
        "chat_id": chat_id, "phone": PHONE, "text": message,
        "kind": "text", "source": "wazzup", "direction": "incoming",
        "message_key": f"{chat_id}:{abs(hash(message))}",
    }
    return asyncio.run(main._build_answer_for_message(payload)) or ""


def run_case(case: dict[str, Any]) -> dict[str, Any]:
    """Прогоняет один сценарий и возвращает структурированный результат."""
    chat_id = f"golden::{case['name']}"
    state.reset_session(chat_id)
    session = state.get_session(chat_id)
    session.update({"ai_lead_started": True, "gate_reason": "active_ai_lead"})
    session.update(case.get("preset") or {})
    state.save_session(chat_id, session)

    via_main = bool(case.get("via_main"))
    turns: list[dict[str, Any]] = []
    for message in case["messages"]:
        if via_main:
            answer = _answer_via_main(chat_id, message)
        else:
            answer = asyncio.run(handle_message(chat_id, PHONE, message)) or ""
        after = state.get_session(chat_id)
        turns.append({
            "user": message,
            "bot": answer,
            "step": str(after.get("step") or ""),
            "language": str(after.get("language") or ""),
            "answer_source": str(after.get("answer_source") or ""),
            "slots_count": len(after.get("last_slots") or []),
        })

    final = state.get_session(chat_id)
    return {
        "name": case["name"],
        "why": case.get("why", ""),
        "turns": turns,
        "final_step": str(final.get("step") or ""),
        "final_language": str(final.get("language") or ""),
    }


def run_all(only: list[str] | None = None) -> list[dict[str, Any]]:
    install_offline_crm()
    state.init_db()
    logging.disable(logging.CRITICAL)
    results = []
    for case in load_cases():
        if only and not any(token in case["name"] for token in only):
            continue
        results.append(run_case(case))
    return results


def print_results(results: list[dict[str, Any]]) -> None:
    for result in results:
        print("=" * 74)
        print(f"{result['name']}  [шаг={result['final_step']} язык={result['final_language']}]")
        print(f"  {result['why']}")
        print("=" * 74)
        for turn in result["turns"]:
            print(f"П> {turn['user']}")
            bot = turn["bot"] or "<нет ответа>"
            print(f"Б> {bot}")
            tail = f"   [шаг={turn['step']} язык={turn['language']}"
            if turn["answer_source"]:
                tail += f" источник={turn['answer_source']}"
            print(tail + "]")
        print()


def main() -> None:
    args = [a for a in sys.argv[1:]]
    out_path = ""
    if "--json" in args:
        idx = args.index("--json")
        out_path = args[idx + 1]
        args = args[:idx] + args[idx + 2:]

    results = run_all(args or None)
    print_results(results)
    if out_path:
        Path(out_path).write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"снапшот сохранён: {out_path}")


if __name__ == "__main__":
    main()
