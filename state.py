from __future__ import annotations

import json
import logging
import sqlite3
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from config import get_settings
from schedule import astana_now, time_gate_status

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("production_events")
_LAST_EVENTS_MEMORY: deque[dict[str, Any]] = deque(maxlen=500)

_SECRET_KEY_PARTS = ("secret", "api_key", "apikey", "token", "authorization", "password")
_STARTUP_HEALTH_EVENTS = {"bot_runtime_config", "health_check", "startup", "app_startup", "config_summary"}
_WARNING_ERROR_WORDS = ("error", "warning", "critical", "exception", "failed", "invalid_json")

_STDOUT_EVENTS = {
    "wazzup_received",
    "dialog_start",
    "dialog_result",
    "bot_no_reply",
    "wazzup_send_attempt",
    "wazzup_send_result",
    "wazzup_webhook_received",
    "wazzup_webhook_invalid_json",
    "wazzup_send_success",
    "wazzup_send_failed",
    "wazzup_send_skipped",
    "wazzup_message_normalized",
    "bot_processing_start",
    "time_gate_result",
    "crm_lookup_start",
    "crm_lookup_result",
    "bot_decision",
    "ai_or_template_response_ready",
    "wazzup_send_start",
    "bot_processing_finish",
    "openai_called",
    "openai_skipped",
    "humanize_skipped",
}


def _safe_log_value(key: str, value: Any) -> Any:
    key_low = key.lower()
    if any(part in key_low for part in _SECRET_KEY_PARTS):
        return "***"
    if isinstance(value, dict):
        return {str(k): _safe_log_value(str(k), v) for k, v in value.items()}
    if isinstance(value, list):
        return [_safe_log_value(key, item) for item in value[:20]]
    if isinstance(value, str):
        limit = 120 if key_low in {"text", "content", "transcript", "answer"} else 500
        return value.replace("\n", " ").strip()[:limit]
    return value


def log_window_status(now: datetime | None = None) -> dict[str, Any]:
    local = (now or astana_now())
    gate = time_gate_status(local)
    allowed = bool(gate["bot_work_time_now"] or gate["test_window_now"])
    return {
        "local_time": local.isoformat(),
        "timezone": getattr(get_settings(), "bot_timezone", "Asia/Almaty"),
        "bot_work_time_now": bool(gate["bot_work_time_now"]),
        "normal_work_time_now": bool(gate["normal_work_time_now"]),
        "test_window_now": bool(gate["test_window_now"]),
        "log_window_allowed": allowed,
    }


def _is_error_warning_event(event_type: str, payload: dict[str, Any]) -> bool:
    event_low = str(event_type or "").lower()
    if any(word in event_low for word in _WARNING_ERROR_WORDS):
        return True
    level = str((payload or {}).get("level") or (payload or {}).get("severity") or "").lower()
    if level in {"error", "warning", "critical", "exception"}:
        return True
    if (payload or {}).get("skipped") or (payload or {}).get("dry_run"):
        return False
    silent_reason = str((payload or {}).get("silent_reason") or "").lower()
    if silent_reason in {"send_failed", "crm_lookup_failed"}:
        return True
    if (payload or {}).get("success") is False and any(word in event_low for word in ("send", "crm", "parse")):
        return True
    if (payload or {}).get("ok") is False and any(word in event_low for word in ("send", "crm", "parse")):
        return True
    return False


def _is_startup_health_event(event_type: str) -> bool:
    event_low = str(event_type or "").lower()
    return event_low in _STARTUP_HEALTH_EVENTS or "startup" in event_low or "health" in event_low or "runtime_config" in event_low


def should_emit_production_event(event_type: str, payload: dict[str, Any] | None = None) -> bool:
    settings = get_settings()
    if not bool(getattr(settings, "production_log_active_window_only", True)):
        return True
    payload = payload or {}
    if _is_error_warning_event(event_type, payload) or _is_startup_health_event(event_type):
        return True
    if str(payload.get("source") or "").lower() == "debug" and not (bool(payload.get("force")) or bool(payload.get("debug"))):
        return False
    return bool(log_window_status()["log_window_allowed"])


def _safe_log_payload(chat_id: str, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    enriched = {**log_window_status(), **(payload or {})}
    safe = {"event": event_type, "chat_id": chat_id}
    for key, value in enriched.items():
        out_key = "text_preview" if str(key).lower() in {"text", "content", "transcript"} else str(key)
        safe[out_key] = _safe_log_value(str(key), value)
    return safe


DEFAULT_SESSION = {
    "step": "start",
    "patient_name": "",
    "phone": "",
    "complaint": "",
    "service": "",
    "can_help": None,
    "age": 0,
    "contraindications_ok": None,
    "contraindications_raw": "",
    "preferred_date": "",
    "selected_doctor_login": "",
    "selected_doctor_name": "",
    "selected_date": "",
    "selected_time": "",
    "last_required_step": "",
    "last_required_question": "",
    "last_required_question_count": 0,
    "last_slots": [],
    "first_touch_info_sent": False,
    "conversation_turns_count": 0,
    "booking_confirmed": False,
    "manual_takeover": False,
    "ai_muted": False,
    "escalated": False,
}


def _connect() -> sqlite3.Connection:
    settings = get_settings()
    if "/" in settings.sqlite_path:
        Path(settings.sqlite_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(settings.sqlite_path)
    conn.row_factory = sqlite3.Row
    return conn


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_db() -> None:
    with _connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sessions (
                chat_id TEXT PRIMARY KEY,
                data_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS processed_messages (
                message_key TEXT PRIMARY KEY,
                chat_id TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS ai_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                day TEXT NOT NULL,
                month TEXT NOT NULL,
                model TEXT NOT NULL,
                purpose TEXT NOT NULL,
                prompt_tokens INTEGER NOT NULL DEFAULT 0,
                completion_tokens INTEGER NOT NULL DEFAULT 0,
                cached_tokens INTEGER NOT NULL DEFAULT 0,
                cost_usd REAL NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_ai_usage_month ON ai_usage(month);
            CREATE INDEX IF NOT EXISTS idx_ai_usage_day ON ai_usage(day);
            """
        )


_AI_USAGE_DDL = """
CREATE TABLE IF NOT EXISTS ai_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    day TEXT NOT NULL,
    month TEXT NOT NULL,
    model TEXT NOT NULL,
    purpose TEXT NOT NULL,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    cached_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
)
"""


def record_ai_usage(
    *,
    day: str,
    month: str,
    model: str,
    purpose: str,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    cached_tokens: int = 0,
    cost_usd: float = 0.0,
) -> None:
    """Сохранить расход токенов одного вызова OpenAI с разбивкой по дню/месяцу/модели."""
    with _connect() as conn:
        conn.execute(_AI_USAGE_DDL)
        conn.execute(
            "INSERT INTO ai_usage(day, month, model, purpose, prompt_tokens, completion_tokens,"
            " cached_tokens, cost_usd, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(day),
                str(month),
                str(model),
                str(purpose),
                int(prompt_tokens or 0),
                int(completion_tokens or 0),
                int(cached_tokens or 0),
                float(cost_usd or 0.0),
                now_iso(),
            ),
        )


def ai_usage_month_cost_usd(month: str) -> float:
    """Суммарный расход в USD за расчётный месяц (YYYY-MM)."""
    with _connect() as conn:
        conn.execute(_AI_USAGE_DDL)
        row = conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) AS total FROM ai_usage WHERE month=?",
            (str(month),),
        ).fetchone()
    return float(row["total"] or 0.0) if row else 0.0


def ai_usage_day_calls(day: str) -> int:
    """Количество вызовов OpenAI за сутки (YYYY-MM-DD)."""
    with _connect() as conn:
        conn.execute(_AI_USAGE_DDL)
        row = conn.execute(
            "SELECT COUNT(*) AS calls FROM ai_usage WHERE day=?",
            (str(day),),
        ).fetchone()
    return int(row["calls"] or 0) if row else 0


def ai_usage_breakdown(*, month: str = "", day: str = "") -> list[dict[str, Any]]:
    """Разбивка расхода по моделям — для /debug и отчётов."""
    where: list[str] = []
    params: list[Any] = []
    if month:
        where.append("month=?")
        params.append(str(month))
    if day:
        where.append("day=?")
        params.append(str(day))
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    with _connect() as conn:
        conn.execute(_AI_USAGE_DDL)
        rows = conn.execute(
            "SELECT model, purpose, COUNT(*) AS calls,"
            " COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,"
            " COALESCE(SUM(completion_tokens), 0) AS completion_tokens,"
            " COALESCE(SUM(cached_tokens), 0) AS cached_tokens,"
            " COALESCE(SUM(cost_usd), 0) AS cost_usd"
            f" FROM ai_usage{clause} GROUP BY model, purpose ORDER BY cost_usd DESC",
            tuple(params),
        ).fetchall()
    return [dict(row) for row in rows]


def add_message(chat_id: str, role: str, content: str) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO messages(chat_id, role, content, created_at) VALUES (?, ?, ?, ?)",
            (chat_id, role, content, now_iso()),
        )


def log_event(chat_id: str, event_type: str, payload: dict[str, Any]) -> None:
    payload = dict(payload or {})
    enriched_payload = {**log_window_status(), **payload}
    event_record = {"chat_id": chat_id, "event_type": event_type, "payload": enriched_payload, "created_at": now_iso()}
    _LAST_EVENTS_MEMORY.append(event_record)
    if event_type in _STDOUT_EVENTS or _is_startup_health_event(event_type) or _is_error_warning_event(event_type, payload):
        if should_emit_production_event(event_type, payload):
            logger.info(json.dumps(_safe_log_payload(chat_id, event_type, payload), ensure_ascii=False, default=str))
    with _connect() as conn:
        conn.execute(
            "INSERT INTO events(chat_id, event_type, payload_json, created_at) VALUES (?, ?, ?, ?)",
            (chat_id, event_type, json.dumps(enriched_payload, ensure_ascii=False), now_iso()),
        )


def recent_memory_events(limit: int = 100) -> list[dict[str, Any]]:
    return list(_LAST_EVENTS_MEMORY)[-max(1, min(int(limit or 100), 500)):]


def log_bot_action(
    chat_id: str,
    action: str,
    note: str = "",
    payload: dict[str, Any] | None = None,
    **extra: Any,
) -> None:
    data = dict(payload or {})
    data.update(extra)
    if note:
        data["note"] = note
    log_event(chat_id, f"bot_action:{action}", data)


def get_session(chat_id: str) -> dict[str, Any]:
    with _connect() as conn:
        row = conn.execute("SELECT data_json FROM sessions WHERE chat_id=?", (chat_id,)).fetchone()
    if not row:
        return dict(DEFAULT_SESSION)
    data = dict(DEFAULT_SESSION)
    try:
        data.update(json.loads(row["data_json"]))
    except Exception:
        pass
    return data


def save_session(chat_id: str, data: dict[str, Any]) -> None:
    cleaned = dict(DEFAULT_SESSION)
    cleaned.update(data)
    with _connect() as conn:
        row = conn.execute("SELECT data_json FROM sessions WHERE chat_id=?", (chat_id,)).fetchone()
        if row:
            try:
                previous = json.loads(row["data_json"])
            except Exception:
                previous = {}
            if previous.get("contraindications_ok") is True and cleaned.get("contraindications_ok") is not True:
                verdict = str(cleaned.get("contraindications_verdict") or "")
                # contraindications_ok=True is sticky unless the patient explicitly reports a real contraindication.
                if verdict not in {"stop", "refuse", "admin_contact", "need_details"}:
                    cleaned["contraindications_ok"] = True
                    cleaned["contraindications_verdict"] = previous.get("contraindications_verdict") or "proceed"
                    cleaned["contraindications_raw"] = cleaned.get("contraindications_raw") or previous.get("contraindications_raw") or ""
            previous_slots = previous.get("last_slots") if isinstance(previous.get("last_slots"), list) else []
            current_slots = cleaned.get("last_slots") if isinstance(cleaned.get("last_slots"), list) else []
            same_preferred_date = (previous.get("preferred_date") or "") == (cleaned.get("preferred_date") or "")
            booking_not_selected = not (cleaned.get("selected_time") or cleaned.get("selected_slot") or cleaned.get("booked"))
            still_choosing_time = str(cleaned.get("step") or previous.get("step") or "") in {"time", "select_slot"}
            if previous_slots and not current_slots and same_preferred_date and booking_not_selected and still_choosing_time and not cleaned.get("manual_takeover") and not cleaned.get("escalated"):
                cleaned["last_slots"] = previous_slots
                cleaned["last_slots_preserved_reason"] = "prevent_lost_slots_between_messages"
        conn.execute(
            """
            INSERT INTO sessions(chat_id, data_json, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET data_json=excluded.data_json, updated_at=excluded.updated_at
            """,
            (chat_id, json.dumps(cleaned, ensure_ascii=False), now_iso()),
        )


def reset_session(chat_id: str) -> None:
    save_session(chat_id, dict(DEFAULT_SESSION))


def count_events_since(event_type: str, since_iso: str) -> int:
    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM events WHERE event_type=? AND created_at>=?",
            (event_type, since_iso),
        ).fetchone()
    return int(row["c"] if row else 0)



def is_processed_message(message_key: str) -> bool:
    if not message_key:
        return False
    with _connect() as conn:
        row = conn.execute("SELECT 1 FROM processed_messages WHERE message_key=?", (message_key,)).fetchone()
    return bool(row)


def mark_processed_message(message_key: str, chat_id: str) -> None:
    if not message_key:
        return
    with _connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO processed_messages(message_key, chat_id, created_at) VALUES (?, ?, ?)",
            (message_key, chat_id, now_iso()),
        )


def append_pending_message(chat_id: str, batch_id: str, content: str) -> None:
    init_db()
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pending_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT NOT NULL,
                batch_id TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "INSERT INTO pending_messages(chat_id, batch_id, content, created_at) VALUES (?, ?, ?, ?)",
            (chat_id, batch_id, content, now_iso()),
        )


def latest_pending_batch_id(chat_id: str) -> str:
    init_db()
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pending_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT NOT NULL,
                batch_id TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        row = conn.execute(
            "SELECT batch_id FROM pending_messages WHERE chat_id=? ORDER BY id DESC LIMIT 1",
            (chat_id,),
        ).fetchone()
    return str(row["batch_id"]) if row else ""


def pop_pending_messages(chat_id: str) -> str:
    init_db()
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pending_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT NOT NULL,
                batch_id TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        rows = conn.execute(
            "SELECT id, content FROM pending_messages WHERE chat_id=? ORDER BY id ASC",
            (chat_id,),
        ).fetchall()
        ids = [int(r["id"]) for r in rows]
        if ids:
            conn.executemany("DELETE FROM pending_messages WHERE id=?", [(i,) for i in ids])
    return "\n".join(str(r["content"]).strip() for r in rows if str(r["content"]).strip())


def recent_events_for_phone(phone: str, limit: int = 50) -> list[dict[str, Any]]:
    phone = str(phone or "").strip()
    if not phone:
        return []
    like = f'%"phone": "{phone}"%'
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT chat_id, event_type, payload_json, created_at
            FROM events
            WHERE chat_id=? OR payload_json LIKE ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (phone, like, int(limit or 50)),
        ).fetchall()
    out: list[dict[str, Any]] = []
    for row in reversed(rows):
        try:
            payload = json.loads(row["payload_json"])
        except Exception:
            payload = {}
        out.append({
            "created_at": row["created_at"],
            "chat_id": row["chat_id"],
            "event": row["event_type"],
            "payload": payload,
        })
    return out


def get_history(chat_id: str, limit: int = 24) -> list[dict[str, str]]:
    """История для GPT-agent: последние сообщения пользователя/ассистента."""
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT role, content FROM messages WHERE chat_id=? ORDER BY id DESC LIMIT ?",
            (chat_id, limit),
        ).fetchall()
    return [{"role": str(r["role"]), "content": str(r["content"])} for r in reversed(rows)]


def determine_next_step(session: dict[str, Any]) -> str:
    """Production booking state-machine: Python is the source of truth."""
    if session.get("booking_confirmed") is True or session.get("booked") is True:
        return "booked"
    if session.get("manual_takeover") is True or session.get("escalated") is True:
        return "escalated"
    if session.get("first_touch_info_sent") is not True and int(session.get("conversation_turns_count") or 0) == 0:
        return "first_touch"
    try:
        age_ok = int(session.get("age") or 0) > 0
    except (TypeError, ValueError):
        age_ok = False
    if not session.get("complaint"):
        return "complaint"
    if not age_ok:
        return "age"
    if session.get("contraindications_ok") is not True:
        return "contraindications"
    if not (session.get("selected_date") or session.get("preferred_date")):
        return "date"
    if not session.get("selected_time"):
        return "time"
    if not session.get("patient_name"):
        return "name"
    return "booking"
