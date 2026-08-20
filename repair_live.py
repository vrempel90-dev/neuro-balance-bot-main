from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request

import repair_service as base


router = APIRouter()
_DB = Path(os.getenv("CLAUDE_LIVE_CASES_DB", "/tmp/claude_live_cases.sqlite3"))
_MODEL = os.getenv("CLAUDE_REPAIR_REVIEW_MODEL", "claude-sonnet-4-20250514")
_SHARED_SECRET = os.getenv("CLAUDE_REPAIR_SHARED_SECRET", "")


def _connect() -> sqlite3.Connection:
    _DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(_DB)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE IF NOT EXISTS live_cases (
        id TEXT PRIMARY KEY,
        conversation_id TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        reviewed_at TEXT NOT NULL DEFAULT '',
        reviewer_ok INTEGER NOT NULL DEFAULT 0,
        bug INTEGER NOT NULL DEFAULT 0,
        severity TEXT NOT NULL DEFAULT '',
        category TEXT NOT NULL DEFAULT '',
        reason TEXT NOT NULL DEFAULT '',
        expected TEXT NOT NULL DEFAULT '',
        delivered_at TEXT NOT NULL DEFAULT '',
        reviewer_error TEXT NOT NULL DEFAULT ''
        )"""
    )
    return conn


def _auth_shared(request: Request) -> None:
    supplied = str(request.headers.get("x-repair-secret") or "")
    if not _SHARED_SECRET or supplied != _SHARED_SECRET:
        raise HTTPException(status_code=401, detail="Invalid repair secret")


def _is_new(payload: dict[str, Any]) -> bool:
    state = str(payload.get("crm_patient_state") or "").strip().upper()
    if state in {"RETURNING_PATIENT_NO_ACTIVE_BOOKING", "ACTIVE_BOOKING", "RETURNING_PATIENT", "EXISTING_PATIENT"}:
        return False
    if payload.get("raw_crm_isNew") is False or payload.get("crm_patient_is_new") is False:
        return False
    return bool(
        payload.get("raw_crm_isNew") is True
        or payload.get("crm_patient_is_new") is True
        or state in {"NEW_LEAD", "NEW_PATIENT", "NEW_CLIENT", "NEW"}
    )


def _case_id(payload: dict[str, Any]) -> str:
    raw = "|".join([
        str(payload.get("conversation_id") or ""),
        str(payload.get("message_id") or ""),
        str(payload.get("user_text") or ""),
        str(payload.get("answer") or ""),
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _review_prompt(payload: dict[str, Any]) -> str:
    compact = {
        k: payload.get(k)
        for k in (
            "user_text", "answer", "step", "state_before_step", "crm_patient_state",
            "detected_intent", "openai_brain_used", "openai_brain_intent",
            "openai_brain_action", "openai_brain_guard_failed", "openai_brain_guard_reason",
            "fallback_reason", "no_reply_reason", "language", "language_guard_result",
            "last_slots_count", "selected_slot", "booking_ready", "booking_confirmed",
            "booking_visible_in_crm", "created_by_ai",
        )
    }
    return (
        "You are a production QA reviewer for a Kazakhstan clinic WhatsApp booking agent. "
        "This is a REAL NEW-LEAD turn. Detect a concrete software/dialogue contract bug, not style preference. "
        "A bug includes: empty/irrelevant answer, loop, wrong state transition, asked name too early, "
        "slot hallucination, stale/lost slot, false booking confirmation, ignored user FAQ, RU/KK mix, "
        "contraindication gate regression, or contradiction between user intent and state/action. "
        "Do NOT call something a bug merely because wording could be nicer. Return ONLY strict JSON: "
        '{"bug":true|false,"severity":"low|medium|high|critical","category":"short_code",'
        '"reason":"specific evidence","expected":"what should have happened"}. '
        "Turn data: " + json.dumps(compact, ensure_ascii=False, default=str)
    )


async def _review(case_id: str, payload: dict[str, Any]) -> None:
    key = str(os.getenv("ANTHROPIC_API_KEY") or "").strip()
    if not key:
        with _connect() as conn:
            conn.execute("UPDATE live_cases SET reviewer_error=? WHERE id=?", ("ANTHROPIC_API_KEY missing", case_id))
        return
    body = {
        "model": _MODEL,
        "max_tokens": 350,
        "temperature": 0,
        "messages": [{"role": "user", "content": _review_prompt(payload)}],
    }
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(45.0, connect=8.0)) as client:
            response = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                json=body,
            )
            response.raise_for_status()
        data = response.json()
        text = "".join(
            str(item.get("text") or "")
            for item in (data.get("content") or [])
            if isinstance(item, dict) and item.get("type") == "text"
        ).strip()
        if text.startswith("```"):
            text = text.strip("`").replace("json\n", "", 1).strip()
        verdict = json.loads(text)
        bug = bool(verdict.get("bug"))
        now = datetime.now(timezone.utc).isoformat()
        with _connect() as conn:
            conn.execute(
                "UPDATE live_cases SET reviewed_at=?, reviewer_ok=1, bug=?, severity=?, category=?, reason=?, expected=?, reviewer_error='' WHERE id=?",
                (now, 1 if bug else 0, str(verdict.get("severity") or "")[:20], str(verdict.get("category") or "")[:80], str(verdict.get("reason") or "")[:1000], str(verdict.get("expected") or "")[:1000], case_id),
            )
    except Exception as exc:
        with _connect() as conn:
            conn.execute("UPDATE live_cases SET reviewer_error=? WHERE id=?", (f"{type(exc).__name__}: {str(exc)[:250]}", case_id))


@router.post("/ingest/new-lead")
async def ingest_new_lead(request: Request) -> dict[str, Any]:
    _auth_shared(request)
    payload = await request.json()
    if not isinstance(payload, dict) or payload.get("source") != "real_wazzup_new_lead":
        raise HTTPException(status_code=400, detail="Only real Wazzup new-lead events are accepted")
    if not _is_new(payload):
        return {"ok": True, "accepted": False, "reason": "not_explicit_new_lead"}
    cid = _case_id(payload)
    created = datetime.now(timezone.utc).isoformat()
    inserted = False
    with _connect() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO live_cases(id, conversation_id, payload_json, created_at) VALUES(?,?,?,?)",
            (cid, str(payload.get("conversation_id") or ""), json.dumps(payload, ensure_ascii=False, default=str), created),
        )
        inserted = cur.rowcount > 0
    if inserted:
        asyncio.create_task(_review(cid, payload))
    return {"ok": True, "accepted": True, "case_id": cid, "review_started": inserted}


@router.get("/live-cases")
async def live_cases(request: Request) -> dict[str, Any]:
    base._verify_github_oidc(request)
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=8)).isoformat()
    redeliver_before = (datetime.now(timezone.utc) - timedelta(minutes=90)).isoformat()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM live_cases WHERE created_at>=? AND reviewer_ok=1 AND bug=1 AND (delivered_at='' OR delivered_at<?) ORDER BY created_at ASC LIMIT 3",
            (cutoff, redeliver_before),
        ).fetchall()
        ids = [row["id"] for row in rows]
        now = datetime.now(timezone.utc).isoformat()
        for cid in ids:
            conn.execute("UPDATE live_cases SET delivered_at=? WHERE id=?", (now, cid))
    cases = []
    for row in rows:
        payload = json.loads(row["payload_json"])
        cases.append({
            "case_id": row["id"],
            "conversation_id": row["conversation_id"],
            "created_at": row["created_at"],
            "severity": row["severity"],
            "category": row["category"],
            "reason": row["reason"],
            "expected": row["expected"],
            "turn": payload,
        })
    return {"ok": True, "source": "real_wazzup_new_leads_only", "cases": cases, "count": len(cases)}


@router.get("/live-stats")
async def live_stats() -> dict[str, Any]:
    with _connect() as conn:
        total = conn.execute("SELECT COUNT(*) FROM live_cases").fetchone()[0]
        reviewed = conn.execute("SELECT COUNT(*) FROM live_cases WHERE reviewer_ok=1").fetchone()[0]
        bugs = conn.execute("SELECT COUNT(*) FROM live_cases WHERE reviewer_ok=1 AND bug=1").fetchone()[0]
        errors = conn.execute("SELECT COUNT(*) FROM live_cases WHERE reviewer_error<>''").fetchone()[0]
    return {"ok": True, "new_lead_turns": total, "reviewed": reviewed, "bugs": bugs, "review_errors": errors, "model": _MODEL}
