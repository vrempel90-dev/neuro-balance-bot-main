from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from config import get_settings


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
    "last_slots": [],
    "escalated": False,
    "active_window_id": "",
    "questionnaire_step": "start",
    "minor_parent_required": False,
    "minor_parent_notice_given": False,
    "mobility_check_pending": False,
    "mobility_ok": None,
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
            CREATE TABLE IF NOT EXISTS admin_style_examples (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS bot_action_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT NOT NULL,
                action_type TEXT NOT NULL,
                action_detail TEXT NOT NULL,
                tool_name TEXT NOT NULL DEFAULT '',
                tool_args_json TEXT NOT NULL DEFAULT '{}',
                tool_result TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            """
        )


def add_message(chat_id: str, role: str, content: str) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO messages(chat_id, role, content, created_at) VALUES (?, ?, ?, ?)",
            (chat_id, role, content, now_iso()),
        )


def log_event(chat_id: str, event_type: str, payload: dict[str, Any]) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO events(chat_id, event_type, payload_json, created_at) VALUES (?, ?, ?, ?)",
            (chat_id, event_type, json.dumps(payload, ensure_ascii=False), now_iso()),
        )


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
        conn.execute(
            """
            INSERT INTO sessions(chat_id, data_json, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET data_json=excluded.data_json, updated_at=excluded.updated_at
            """,
            (chat_id, json.dumps(cleaned, ensure_ascii=False), now_iso()),
        )


def reset_session(chat_id: str) -> None:
    save_session(chat_id, dict(DEFAULT_SESSION))


def reset_chat_for_active_window(chat_id: str, window_id: str) -> None:
    """Начинает новую ночную сессию для чата.

    Это специально не даёт GPT продолжать дневную переписку админов.
    Внутри одного ночного окна история сохраняется, чтобы бот помнил свои ночные шаги.
    """
    session = get_session(chat_id)
    if session.get("active_window_id") == window_id:
        return

    with _connect() as conn:
        conn.execute("DELETE FROM messages WHERE chat_id=?", (chat_id,))

    fresh = dict(DEFAULT_SESSION)
    fresh["active_window_id"] = window_id
    save_session(chat_id, fresh)


def add_admin_style_example(chat_id: str, content: str) -> None:
    """Сохраняет только стиль ответа администратора, не как историю конкретного чата."""
    content = (content or "").strip()
    if not content:
        return
    init_db()
    with _connect() as conn:
        conn.execute(
            "INSERT INTO admin_style_examples(chat_id, content, created_at) VALUES (?, ?, ?)",
            (chat_id, content, now_iso()),
        )


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


def get_history(chat_id: str, limit: int = 24) -> list[dict[str, str]]:
    """История для GPT-agent: последние сообщения пользователя/ассистента."""
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT role, content FROM messages WHERE chat_id=? ORDER BY id DESC LIMIT ?",
            (chat_id, limit),
        ).fetchall()
    return [{"role": str(r["role"]), "content": str(r["content"])} for r in reversed(rows)]



def get_recent_admin_style_examples(limit: int = 18) -> str:
    """Возвращает последние живые ответы администраторов как примеры стиля.

    Важно: это НЕ история конкретного пациента и НЕ контекст для продолжения дневного диалога.
    """
    init_db()
    limit = max(1, min(int(limit or 18), 40))
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT chat_id, content
            FROM admin_style_examples
            WHERE length(trim(content)) > 0
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    items: list[str] = []
    for row in reversed(rows):
        content = str(row["content"]).strip()
        if not content:
            continue
        if len(content) > 600:
            content = content[:600].rstrip() + "…"
        items.append(f"Администратор: {content}")
    return "\n\n".join(items)


def log_bot_action(
    chat_id: str,
    action_type: str,
    action_detail: str = "",
    *,
    tool_name: str = "",
    tool_args: dict[str, Any] | None = None,
    tool_result: str = "",
) -> None:
    """Аналог bot_action_logs из старого TS-бота.

    Нужен не для красоты, а для программных гейтов:
    - была ли зафиксирована жалоба;
    - прошли ли противопоказания;
    - не было ли отказа;
    - что именно сделал бот перед записью.
    """
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO bot_action_logs(
                chat_id, action_type, action_detail, tool_name, tool_args_json, tool_result, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                chat_id,
                action_type,
                action_detail,
                tool_name,
                json.dumps(tool_args or {}, ensure_ascii=False),
                tool_result or "",
                now_iso(),
            ),
        )


def has_action(chat_id: str, *, tool_name: str = "", contains: str = "") -> bool:
    query = "SELECT 1 FROM bot_action_logs WHERE chat_id=?"
    params: list[Any] = [chat_id]
    if tool_name:
        query += " AND tool_name=?"
        params.append(tool_name)
    if contains:
        query += " AND tool_result LIKE ?"
        params.append("%" + contains + "%")
    query += " LIMIT 1"
    with _connect() as conn:
        row = conn.execute(query, tuple(params)).fetchone()
    return bool(row)


def get_recent_actions(chat_id: str, limit: int = 20) -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT action_type, action_detail, tool_name, tool_args_json, tool_result, created_at
            FROM bot_action_logs
            WHERE chat_id=?
            ORDER BY id DESC
            LIMIT ?
            """,
            (chat_id, limit),
        ).fetchall()
    return [dict(row) for row in rows]
