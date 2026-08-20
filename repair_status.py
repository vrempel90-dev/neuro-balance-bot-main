from __future__ import annotations

import hashlib
import json
import os
import threading
from datetime import date, datetime, timedelta, timezone
from html import escape
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx

_ALMATY = ZoneInfo("Asia/Almaty")
_STATUS_PATH = Path(os.getenv("CLAUDE_REPAIR_STATUS_PATH", "/tmp/claude_repair_status.json"))
_REPOSITORY = os.getenv("CLAUDE_REPAIR_GITHUB_REPOSITORY", "vrempel90-dev/neuro-balance-bot-main")
_LOCK = threading.Lock()


def _night_key(now: datetime | None = None) -> str:
    local = (now or datetime.now(timezone.utc)).astimezone(_ALMATY)
    d: date = local.date()
    if local.hour < 8:
        d = d - timedelta(days=1)
    return d.isoformat()


def _default_status(now: datetime | None = None) -> dict[str, Any]:
    return {
        "night_key": _night_key(now),
        "scans": 0,
        "conversation_ids": [],
        "event_fingerprints": [],
        "signal_fingerprints": [],
        "last_scan_at": "",
        "last_scan_ok": None,
        "last_scan_events": 0,
        "last_scan_dialogues": 0,
        "last_scan_signals": 0,
        "last_error": "",
        "claude_api_calls": 0,
        "last_claude_at": "",
        "last_claude_status": 0,
    }


def _load(now: datetime | None = None) -> dict[str, Any]:
    try:
        data = json.loads(_STATUS_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("invalid status")
    except Exception:
        data = _default_status(now)
    current_key = _night_key(now)
    if str(data.get("night_key") or "") != current_key:
        data = _default_status(now)
    return data


def _save(data: dict[str, Any]) -> None:
    _STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _STATUS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    tmp.replace(_STATUS_PATH)


def _fingerprint(event: dict[str, Any]) -> str:
    raw = "|".join(
        [
            str(event.get("created_at") or ""),
            str(event.get("conversation_id") or ""),
            str(event.get("event") or ""),
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def record_scan(result: dict[str, Any] | None = None, *, error: str = "") -> None:
    now = datetime.now(timezone.utc)
    with _LOCK:
        data = _load(now)
        data["scans"] = int(data.get("scans") or 0) + 1
        data["last_scan_at"] = now.isoformat()
        if error:
            data["last_scan_ok"] = False
            data["last_scan_events"] = 0
            data["last_scan_dialogues"] = 0
            data["last_scan_signals"] = 0
            data["last_error"] = str(error)[:300]
            _save(data)
            return

        events = (result or {}).get("events") if isinstance(result, dict) else []
        events = events if isinstance(events, list) else []
        conversations = {
            str(item.get("conversation_id") or "")
            for item in events
            if isinstance(item, dict) and str(item.get("conversation_id") or "")
        }
        existing_conversations = set(str(v) for v in (data.get("conversation_ids") or []))
        existing_conversations.update(conversations)
        data["conversation_ids"] = sorted(existing_conversations)[-1000:]

        event_fingerprints = set(str(v) for v in (data.get("event_fingerprints") or []))
        signal_fingerprints = set(str(v) for v in (data.get("signal_fingerprints") or []))
        signal_count = 0
        for item in events:
            if not isinstance(item, dict):
                continue
            fp = _fingerprint(item)
            event_fingerprints.add(fp)
            for signal in item.get("signals") or []:
                signal_fp = hashlib.sha256(f"{fp}|{signal}".encode("utf-8")).hexdigest()[:20]
                signal_fingerprints.add(signal_fp)
                signal_count += 1

        data["event_fingerprints"] = sorted(event_fingerprints)[-4000:]
        data["signal_fingerprints"] = sorted(signal_fingerprints)[-2000:]
        data["last_scan_ok"] = True
        data["last_scan_events"] = len(events)
        data["last_scan_dialogues"] = len(conversations)
        data["last_scan_signals"] = signal_count
        data["last_error"] = ""
        _save(data)


def record_claude_call(status_code: int) -> None:
    now = datetime.now(timezone.utc)
    with _LOCK:
        data = _load(now)
        data["claude_api_calls"] = int(data.get("claude_api_calls") or 0) + 1
        data["last_claude_at"] = now.isoformat()
        data["last_claude_status"] = int(status_code or 0)
        _save(data)


def _minutes_since(value: str, now: datetime) -> float | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return max(0.0, (now - parsed.astimezone(timezone.utc)).total_seconds() / 60.0)
    except Exception:
        return None


async def github_repair_summary() -> dict[str, Any]:
    url = f"https://api.github.com/repos/{_REPOSITORY}/pulls"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(8.0, connect=4.0)) as client:
            response = await client.get(
                url,
                params={"state": "all", "per_page": 50, "sort": "created", "direction": "desc"},
                headers={"Accept": "application/vnd.github+json"},
            )
            response.raise_for_status()
        pulls = response.json()
    except Exception:
        return {"available": False, "confirmed_bugs": 0, "fixes_prepared": 0, "merged": 0, "last_pr": None}

    repairs: list[dict[str, Any]] = []
    for pr in pulls if isinstance(pulls, list) else []:
        head = pr.get("head") if isinstance(pr, dict) else None
        ref = str((head or {}).get("ref") or "") if isinstance(head, dict) else ""
        if ref.startswith("claude/real-wazzup-"):
            repairs.append(pr)

    last_pr = None
    if repairs:
        newest = repairs[0]
        last_pr = {
            "number": int(newest.get("number") or 0),
            "url": str(newest.get("html_url") or ""),
            "state": "merged" if newest.get("merged_at") else str(newest.get("state") or "open"),
        }
    return {
        "available": True,
        "confirmed_bugs": len(repairs),
        "fixes_prepared": len(repairs),
        "merged": sum(1 for pr in repairs if pr.get("merged_at")),
        "last_pr": last_pr,
    }


def snapshot(*, production_reachable: bool | None, prs: dict[str, Any]) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    local = now.astimezone(_ALMATY)
    with _LOCK:
        data = _load(now)

    scan_age = _minutes_since(str(data.get("last_scan_at") or ""), now)
    claude_age = _minutes_since(str(data.get("last_claude_at") or ""), now)
    in_window = local.hour >= 20 or local.hour < 8

    if not in_window:
        state = "off"
        state_label = "Ночной режим выключен"
    elif data.get("last_scan_ok") is False and scan_age is not None and scan_age <= 45:
        state = "error"
        state_label = "Ошибка наблюдателя"
    elif scan_age is not None and scan_age <= 40:
        state = "active"
        state_label = "Claude наблюдает"
    else:
        state = "waiting"
        state_label = "Ждём очередную проверку"

    return {
        "state": state,
        "state_label": state_label,
        "night_window": in_window,
        "local_time": local.isoformat(timespec="seconds"),
        "window": "20:00–08:00 Asia/Almaty",
        "production_reachable": production_reachable,
        "scans": int(data.get("scans") or 0),
        "checked_dialogues": len(data.get("conversation_ids") or []),
        "checked_events": len(data.get("event_fingerprints") or []),
        "signals": len(data.get("signal_fingerprints") or []),
        "last_scan_at": str(data.get("last_scan_at") or ""),
        "last_scan_minutes_ago": round(scan_age, 1) if scan_age is not None else None,
        "last_scan_dialogues": int(data.get("last_scan_dialogues") or 0),
        "last_scan_events": int(data.get("last_scan_events") or 0),
        "last_scan_signals": int(data.get("last_scan_signals") or 0),
        "claude_api_calls": int(data.get("claude_api_calls") or 0),
        "last_claude_at": str(data.get("last_claude_at") or ""),
        "last_claude_minutes_ago": round(claude_age, 1) if claude_age is not None else None,
        "last_claude_status": int(data.get("last_claude_status") or 0),
        "confirmed_bugs": int(prs.get("confirmed_bugs") or 0),
        "fixes_prepared": int(prs.get("fixes_prepared") or 0),
        "merged_fixes": int(prs.get("merged") or 0),
        "last_pr": prs.get("last_pr"),
        "github_pr_status_available": bool(prs.get("available")),
        "last_error": str(data.get("last_error") or ""),
        "privacy": "Переписки, имена, телефоны и API-ключи на dashboard не выводятся.",
    }


def dashboard_document() -> str:
    return """<!doctype html>
<html lang=\"ru\">
<head>
<meta charset=\"utf-8\">
<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">
<meta http-equiv=\"refresh\" content=\"300\">
<title>Neuro × Claude — ночной ремонт</title>
<style>
:root{font-family:Inter,system-ui,-apple-system,BlinkMacSystemFont,\"Segoe UI\",sans-serif;color:#eef2ff;background:#0b1020}
body{margin:0;padding:24px;min-height:100vh;background:radial-gradient(circle at top,#17203d 0,#0b1020 48%)}
.wrap{max-width:920px;margin:0 auto}.top{display:flex;gap:16px;align-items:center;justify-content:space-between;flex-wrap:wrap;margin-bottom:22px}
h1{font-size:25px;margin:0}.sub{opacity:.68;font-size:14px;margin-top:6px}.badge{padding:10px 14px;border-radius:999px;background:#1c2748;font-weight:700}.active{background:#123d2d;color:#94f7c5}.waiting{background:#403715;color:#ffe08a}.error{background:#4b1f29;color:#ffb4c2}.off{background:#293044;color:#cbd5e1}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px}.card{background:rgba(26,35,65,.83);border:1px solid rgba(255,255,255,.08);border-radius:18px;padding:18px;box-shadow:0 12px 35px rgba(0,0,0,.18)}
.num{font-size:31px;font-weight:800;margin-top:8px}.label{font-size:13px;opacity:.7}.wide{grid-column:1/-1}.row{display:flex;gap:12px;justify-content:space-between;align-items:flex-start;flex-wrap:wrap}.muted{opacity:.65;font-size:13px}.ok{color:#94f7c5}.bad{color:#ffb4c2}a{color:#a8c7ff}.err{white-space:pre-wrap;margin-top:8px;color:#ffb4c2}.footer{margin-top:16px;font-size:12px;opacity:.55}
</style>
</head>
<body><div class=\"wrap\">
<div class=\"top\"><div><h1>Neuro × Claude</h1><div class=\"sub\">Наблюдение за реальными Wazzup-диалогами · 20:00–08:00 Asia/Almaty</div></div><div id=\"state\" class=\"badge waiting\">Загрузка…</div></div>
<div class=\"grid\">
<div class=\"card\"><div class=\"label\">Проверено диалогов за ночь</div><div id=\"dialogues\" class=\"num\">—</div></div>
<div class=\"card\"><div class=\"label\">Сигналов возможного бага</div><div id=\"signals\" class=\"num\">—</div></div>
<div class=\"card\"><div class=\"label\">Вызовов Claude API</div><div id=\"claude\" class=\"num\">—</div></div>
<div class=\"card\"><div class=\"label\">Подтверждено багов</div><div id=\"bugs\" class=\"num\">—</div></div>
<div class=\"card\"><div class=\"label\">Подготовлено фиксов</div><div id=\"fixes\" class=\"num\">—</div></div>
<div class=\"card\"><div class=\"label\">Уже смержено</div><div id=\"merged\" class=\"num\">—</div></div>
<div class=\"card wide\"><div class=\"row\"><div><div class=\"label\">Последняя проверка</div><div id=\"lastScan\">—</div><div id=\"lastBatch\" class=\"muted\"></div></div><div><div class=\"label\">Связь с production</div><div id=\"production\">—</div></div><div><div class=\"label\">Последний repair PR</div><div id=\"lastPr\">Пока нет</div></div></div><div id=\"error\" class=\"err\"></div></div>
</div><div class=\"footer\">Страница обновляется автоматически каждые 20 секунд. Переписки, телефоны, имена и ключи здесь не отображаются.</div>
</div>
<script>
function ago(v){if(v===null||v===undefined)return 'ещё не было';if(v<1)return 'меньше минуты назад';return Math.round(v)+' мин назад'}
async function refresh(){try{const r=await fetch('/status.json',{cache:'no-store'});const d=await r.json();
const s=document.getElementById('state');s.textContent=(d.state==='active'?'🟢 ':d.state==='error'?'🔴 ':d.state==='off'?'⚫ ':'🟡 ')+d.state_label;s.className='badge '+d.state;
dialogues.textContent=d.checked_dialogues;signals.textContent=d.signals;claude.textContent=d.claude_api_calls;bugs.textContent=d.confirmed_bugs;fixes.textContent=d.fixes_prepared;merged.textContent=d.merged_fixes;
lastScan.textContent=ago(d.last_scan_minutes_ago);lastBatch.textContent='Последний проход: '+d.last_scan_dialogues+' диалогов · '+d.last_scan_events+' событий · '+d.last_scan_signals+' сигналов';
production.innerHTML=d.production_reachable===true?'<span class=\"ok\">● доступен</span>':d.production_reachable===false?'<span class=\"bad\">● недоступен</span>':'не проверено';
if(d.last_pr&&d.last_pr.number){lastPr.innerHTML='<a href=\"'+d.last_pr.url+'\" target=\"_blank\">#'+d.last_pr.number+'</a> · '+d.last_pr.state}else{lastPr.textContent='Пока нет'}
error.textContent=d.last_error?('Последняя ошибка: '+d.last_error):'';
}catch(e){const s=document.getElementById('state');s.textContent='🔴 Dashboard недоступен';s.className='badge error'}}
refresh();setInterval(refresh,20000);
</script></body></html>"""
