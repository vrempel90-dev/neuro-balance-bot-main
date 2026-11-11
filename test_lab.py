"""Isolated NeuroBalance Test Lab.

Uses the production dialog and GPT agent unchanged, with the REAL OpenAI
client and a deterministic in-process CRM adapter. It never loads main.app,
accepts Wazzup webhooks, or sends a request to the production CRM.
Run ONLY as a dedicated Railway service with NEURO_TEST_LAB=true.
"""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from difflib import SequenceMatcher
import hmac
import json
import os
import re
import secrets
import sqlite3
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

# Fail closed BEFORE importing application modules.
if os.environ.get("NEURO_TEST_LAB", "").lower() != "true":
    raise RuntimeError("Test Lab must run only with NEURO_TEST_LAB=true")
if not os.environ.get("TEST_LAB_PASSWORD", "").strip():
    raise RuntimeError("TEST_LAB_PASSWORD is required")
os.environ["CRM_BASE_URL"] = "http://127.0.0.1:1/disabled"
os.environ["CRM_BOT_SECRET"] = ""
os.environ["EXTERNAL_BOOKING_API_SECRET"] = ""
os.environ["WAZZUP_API_KEY"] = ""
os.environ["WAZZUP_CHANNEL_ID"] = ""
os.environ["SQLITE_PATH"] = os.environ.get("SQLITE_PATH") or "/tmp/neuro_test_lab.sqlite3"

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, Field

import crm
import dialog
import state

TEST_CRM_DOCTORS = (
    {"doctorLogin": "test_asel", "doctorName": "Оспанова Асель Кайроллаевна"},
    {"doctorLogin": "test_bekbolat", "doctorName": "Бекболат Шуринбайулы"},
    {"doctorLogin": "test_kaisар", "doctorName": "Куанышулы Кайсар"},
)
# The third login intentionally uses ASCII, as required by the agent's CRM validator.
TEST_CRM_DOCTORS = (
    TEST_CRM_DOCTORS[0], TEST_CRM_DOCTORS[1],
    {"doctorLogin": "test_kaisar", "doctorName": "Куанышулы Кайсар"},
)

SCENARIOS: list[dict[str, Any]] = [
    {
        "id": "doctor_indifference",
        "name": "Не имеет значения, какой врач",
        "lead_state": "new",
        "messages": [
            "Здравствуйте, хочу записаться на завтра после обеда. Болит поясница, мне 35 лет, противопоказаний нет.",
            "Мне без разницы, к какому врачу. Подберите свободное время, пожалуйста.",
        ],
    },
    {
        "id": "address_and_complaint",
        "name": "Адрес клиники и жалоба",
        "lead_state": "new",
        "messages": [
            "Здравствуйте! Можете скинуть адрес клиники?",
            "Меня беспокоит грыжа. Можно записаться?",
        ],
    },
    {
        "id": "relative",
        "name": "Запись за маму",
        "lead_state": "new",
        "messages": [
            "Добрый день. Хочу записать маму: у неё болит поясница, маме 58 лет.",
            "У мамы нет противопоказаний. Когда можно прийти?",
        ],
    },
    {
        "id": "kazakh",
        "name": "Диалог на казахском",
        "lead_state": "new",
        "messages": [
            "Сәлеметсіз бе! Белім ауырады. Дәрігерге жазылғым келеді.",
            "Мен 35 жастамын. Қай күні келуге болады?",
        ],
    },
    {
        "id": "existing_patient",
        "name": "Повторный пациент — молчание",
        "lead_state": "returning",
        "messages": ["Здравствуйте, хочу записаться ещё раз."],
    },
]

@dataclass
class Fixture:
    sid: str
    lead_state: str
    bookings: list[dict[str, Any]]

active_fixture: ContextVar[Fixture | None] = ContextVar("lab_fixture", default=None)
fixtures: dict[str, Fixture] = {}
locks: dict[str, asyncio.Lock] = {}
suite_tasks: dict[str, asyncio.Task[Any]] = {}

def current_fixture() -> Fixture:
    value = active_fixture.get()
    if value is None:
        raise RuntimeError("CRM fixture used outside authorized Test Lab turn")
    return value

def forbid_external_crm() -> Any:
    raise RuntimeError("Network access to CRM is disabled in Test Lab")

def appointment_date() -> str:
    day = date.today() + timedelta(days=2)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day.isoformat()

async def fake_lookup(phone: str) -> dict[str, Any]:
    fixture = current_fixture()
    if fixture.lead_state == "crm_down":
        raise crm.CRMError("Simulated CRM outage")
    if fixture.lead_state == "returning":
        raw = {"ok": True, "found": True, "isNew": False, "patient": {"name": "Тестовый пациент"}, "hasActiveAppointment": False}
    elif fixture.lead_state == "active":
        raw = {"ok": True, "found": True, "isNew": False, "patient": {"name": "Тестовый пациент"}, "hasActiveAppointment": True}
    else:
        raw = {"ok": True, "found": False, "isNew": True, "hasActiveAppointment": False}
    return {"ok": True, "raw": raw, "appointments": [], "appointment": None, "active_count": int(fixture.lead_state == "active")}

async def fake_doctors(force: bool = False) -> dict[str, Any]:
    current_fixture()
    return {"ok": True, "doctors": list(TEST_CRM_DOCTORS)}

async def fake_services(force: bool = False) -> dict[str, Any]:
    current_fixture()
    return {"ok": True, "treatable": [], "notTreatable": [], "doctorCount": len(TEST_CRM_DOCTORS)}

async def fake_slots(date: str, doctor_login: str | None = None) -> dict[str, Any]:
    current_fixture()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        raise crm.CRMError("Invalid test date")
    if date < date.today().isoformat() or date > (date.today() + timedelta(days=35)).isoformat():
        return {"ok": True, "date": date, "availability": [], "slots": []}
    if datetime.strptime(date, "%Y-%m-%d").weekday() >= 5:
        return {"ok": True, "date": date, "availability": [], "slots": []}
    offerings = (("test_asel", ("14:00", "14:40")), ("test_bekbolat", ("16:00", "16:40")), ("test_kaisar", ("09:20", "11:00")))
    availability, flat = [], []
    for login, times in offerings:
        if doctor_login and doctor_login != login:
            continue
        name = next((d["doctorName"] for d in TEST_CRM_DOCTORS if d["doctorLogin"] == login), login)
        availability.append({"doctorLogin": login, "doctorName": name, "date": date, "availableSlots": list(times)})
        for when in times:
            flat.append({"doctorLogin": login, "doctorName": name, "doctor_login": login, "doctor_name": name, "date": date, "timeStart": when, "time_start": when})
    return {"ok": True, "date": date, "availability": availability, "slots": flat}

async def fake_book(**kwargs: Any) -> dict[str, Any]:
    fixture = current_fixture()
    new_booking = {"id": "LAB-" + uuid4().hex[:10], "ok": True, "status": "Записан",
                   "date": kwargs["date"], "timeStart": kwargs["time_start"],
                   "doctorLogin": kwargs["doctor_login"], "doctorName": kwargs.get("doctor_name") or "",
                   "patientName": kwargs["patient_name"]}
    if any(b["date"] == new_booking["date"] and b["timeStart"] == new_booking["timeStart"] and b["doctorLogin"] == new_booking["doctorLogin"] for b in fixture.bookings):
        return {"ok": False, "error": "slot_conflict", "status": "rejected"}
    fixture.bookings.append(new_booking)
    return new_booking

async def fake_verify(response: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    fixture = current_fixture()
    return {"verified": any(b["id"] == response.get("id") for b in fixture.bookings)}

async def fake_escalate(**kwargs: Any) -> dict[str, Any]:
    current_fixture()
    return {"ok": True, "escalated": True, "test_only": True}

async def fake_cancel(**kwargs: Any) -> dict[str, Any]:
    current_fixture()
    return {"ok": False, "error": "test_existing_record_unavailable"}

async def fake_reschedule(**kwargs: Any) -> dict[str, Any]:
    current_fixture()
    return {"ok": False, "error": "test_existing_record_unavailable"}

def install_isolated_crm() -> None:
    # Monkey-patches live only in this dedicated process, never production.
    crm._client = forbid_external_crm
    crm._url = forbid_external_crm
    crm.lookup_active_appointments_by_phone = fake_lookup
    crm.get_doctors = fake_doctors
    crm.get_services = fake_services
    crm.check_slots = fake_slots
    crm.book_appointment = fake_book
    crm.verify_saved_booking = fake_verify
    crm.escalate_to_operator = fake_escalate
    crm.cancel_appointment = fake_cancel
    crm.reschedule_appointment = fake_reschedule
    crm.log_outcome = fake_escalate
    crm.clear_slots_cache = lambda date=None: None

install_isolated_crm()
state.init_db()
DB_PATH = os.environ["SQLITE_PATH"]

def db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, timeout=15)
    con.row_factory = sqlite3.Row
    return con

with db() as con:
    con.executescript("""
    CREATE TABLE IF NOT EXISTS lab_sessions (
        sid TEXT PRIMARY KEY, name TEXT NOT NULL, lead_state TEXT NOT NULL,
        phone TEXT NOT NULL, created_at TEXT NOT NULL, turns_json TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS lab_suites (
        run_id TEXT PRIMARY KEY, status TEXT NOT NULL, created_at TEXT NOT NULL,
        results_json TEXT NOT NULL);
    UPDATE lab_suites SET status='interrupted' WHERE status='running';
    """)

app = FastAPI(title="NeuroBalance Real Dialog Test Lab", docs_url=None, redoc_url=None, openapi_url=None)
basic = HTTPBasic(auto_error=False)

def authenticate(credentials: HTTPBasicCredentials | None = Depends(basic)) -> bool:
    expected = os.environ["TEST_LAB_PASSWORD"]
    if credentials is None or not (
        hmac.compare_digest(credentials.username.encode(), b"admin") and
        hmac.compare_digest(credentials.password.encode(), expected.encode())
    ):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required",
                            headers={"WWW-Authenticate": 'Basic realm="NeuroBalance Test Lab"'})
    return True

@app.middleware("http")
async def security_headers(request: Request, call_next):
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        origin = request.headers.get("origin")
        if origin and urlparse(origin).netloc != request.headers.get("host", ""):
            return JSONResponse({"detail": "Cross-origin request forbidden"}, status_code=403)
    response = await call_next(request)
    response.headers.update({
        "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer",
        "Content-Security-Policy": "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; object-src 'none'; frame-ancestors 'none'",
    })
    return response

@app.get("/health")
def health():
    return {"status": "ok", "mode": "isolated-real-gpt", "production_crm_connected": False}

def load_session(sid: str) -> dict[str, Any]:
    with db() as con:
        row = con.execute("SELECT * FROM lab_sessions WHERE sid=?", (sid,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Test session not found")
    obj = dict(row)
    obj["turns"] = json.loads(obj.pop("turns_json"))
    return obj

def persist_turns(sid: str, turns: list[dict[str, Any]]) -> None:
    with db() as con:
        con.execute("UPDATE lab_sessions SET turns_json=? WHERE sid=?", (json.dumps(turns, ensure_ascii=False), sid))

def open_session(name: str, lead_state: str) -> dict[str, Any]:
    if lead_state not in {"new", "returning", "active", "crm_down"}:
        raise ValueError("Invalid simulated lead state")
    sid = uuid4().hex
    phone = "77" + str(int(sid[:12], 16) % 10**9).zfill(9)
    with db() as con:
        con.execute("INSERT INTO lab_sessions VALUES (?,?,?,?,?,?)",
                    (sid, name[:80], lead_state, phone, datetime.now(timezone.utc).isoformat(), "[]"))
    fixtures[sid] = Fixture(sid, lead_state, [])
    return {"sid": sid, "name": name, "lead_state": lead_state, "turns": []}

def judgement(obj: dict[str, Any]) -> list[dict[str, str]]:
    turns = obj["turns"]
    issues: list[dict[str, str]] = []
    for i, turn in enumerate(turns):
        answer = turn["answer"]
        user = turn["user"].lower()
        if obj["lead_state"] in {"returning", "active", "crm_down"}:
            if answer:
                issues.append({"type": "critical", "rule": "admission", "message": "AI responded to a non-new or unverified patient."})
            continue
        if not answer and not turn.get("booking_confirmed") and not turn.get("manual_takeover"):
            issues.append({"type": "critical", "rule": "silent", "message": "The new lead received no answer."})
        if turn.get("handoff_reason") in {"unverified_fact", "agent_exception", "empty_answer"}:
            issues.append({"type": "warning", "rule": "handoff", "message": "Agent was blocked: " + turn["handoff_reason"]})
        if "без разниц" in user or "любой врач" in user:
            if re.search(r"(?:к кому|кого предпоч|выбер(?:ите|и).*врач|какому врач)", answer.lower()):
                issues.append({"type": "warning", "rule": "doctor_indifference", "message": "After the client accepted any doctor, AI asked to choose again."})
        if i > 0:
            prev = turns[i-1]["answer"]
            norm = lambda s: re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", s.lower())).strip()
            if answer and prev and ("?" in answer) and ("?" in prev):
                ratio = SequenceMatcher(None, norm(answer), norm(prev)).ratio()
                if ratio >= 0.83:
                    issues.append({"type": "warning", "rule": "repeated_question", "message": "Similar question repeated in consecutive answers (" + str(round(ratio, 2)) + ")."})
    return issues

async def process_turn(sid: str, user_text: str) -> dict[str, Any]:
    obj = load_session(sid)
    fixture = fixtures.setdefault(sid, Fixture(sid, obj["lead_state"], []))
    async with locks.setdefault(sid, asyncio.Lock()):
        obj = load_session(sid)
        if len(obj["turns"]) >= 40:
            raise HTTPException(status_code=409, detail="Start a fresh session after 40 turns")
        cv = active_fixture.set(fixture)
        start = asyncio.get_running_loop().time()
        try:
            answer = await asyncio.wait_for(
                dialog.handle_message("lab_" + sid, obj["phone"], user_text), timeout=110
            )
            session = state.get_session("lab_" + sid)
            entry = {
                "user": user_text, "answer": answer,
                "duration_ms": round((asyncio.get_running_loop().time() - start) * 1000),
                "openai_used": bool(session.get("openai_used")),
                "model": str(session.get("openai_brain_model") or os.getenv("AI_BRAIN_MODEL") or ""),
                "tool_calls": session.get("agent_tool_calls") or [],
                "crm_patient_state": session.get("crm_patient_state") or "",
                "booking_confirmed": bool(session.get("booking_confirmed")),
                "manual_takeover": bool(session.get("manual_takeover")),
                "handoff_reason": str(session.get("handoff_reason") or ""),
                "no_reply_reason": str(session.get("no_reply_reason") or ""),
                "openai_skip_reason": str(session.get("openai_skip_reason") or ""),
                "fixture_bookings": len(fixture.bookings),
            }
        finally:
            active_fixture.reset(cv)
        obj["turns"].append(entry)
        persist_turns(sid, obj["turns"])
        return {"sid": sid, "turn": entry, "issues": judgement(obj), "turns": obj["turns"]}

class CreateRequest(BaseModel):
    name: str = Field(default="Ручная проверка", max_length=80)
    lead_state: str = Field(default="new", pattern=r"^(new|returning|active|crm_down)$")

class MessageRequest(BaseModel):
    text: str = Field(min_length=1, max_length=2000)

@app.get("/api/scenarios", dependencies=[Depends(authenticate)])
def scenarios():
    return {"scenarios": [{"id": s["id"], "name": s["name"], "messages": s["messages"], "lead_state": s["lead_state"]} for s in SCENARIOS]}

@app.post("/api/sessions", dependencies=[Depends(authenticate)])
def create_session(data: CreateRequest):
    return open_session(data.name, data.lead_state)

@app.get("/api/sessions/{sid}", dependencies=[Depends(authenticate)])
def get_session(sid: str):
    obj = load_session(sid)
    return {"session": {k: v for k, v in obj.items() if k != "phone"}, "issues": judgement(obj)}

@app.post("/api/sessions/{sid}/messages", dependencies=[Depends(authenticate)])
async def send_message(sid: str, data: MessageRequest):
    return await process_turn(sid, data.text)

async def execute_suite(run_id: str) -> None:
    results = []
    try:
        for spec in SCENARIOS:
            session = open_session(spec["name"], spec["lead_state"])
            sid = session["sid"]
            for message in spec["messages"]:
                await process_turn(sid, message)
            saved = load_session(sid)
            results.append({"id": spec["id"], "name": spec["name"], "sid": sid,
                            "issues": judgement(saved), "turns": saved["turns"]})
            with db() as con:
                con.execute("UPDATE lab_suites SET results_json=? WHERE run_id=?", (json.dumps(results, ensure_ascii=False), run_id))
        final_status = "finished"
    except Exception as exc:
        final_status = "failed"
        results.append({"error_type": type(exc).__name__, "error": str(exc)[:160]})
    with db() as con:
        con.execute("UPDATE lab_suites SET status=?,results_json=? WHERE run_id=?",
                    (final_status, json.dumps(results, ensure_ascii=False), run_id))
    suite_tasks.pop(run_id, None)

@app.post("/api/suites", dependencies=[Depends(authenticate)])
async def start_suite():
    if any(not task.done() for task in suite_tasks.values()):
        raise HTTPException(status_code=409, detail="Another suite is running")
    run_id = uuid4().hex
    with db() as con:
        con.execute("INSERT INTO lab_suites VALUES (?,?,?,?)",
                    (run_id, "running", datetime.now(timezone.utc).isoformat(), "[]"))
    suite_tasks[run_id] = asyncio.create_task(execute_suite(run_id))
    return {"run_id": run_id, "status": "running", "scenario_count": len(SCENARIOS)}

@app.get("/api/suites/{run_id}", dependencies=[Depends(authenticate)])
def get_suite(run_id: str):
    with db() as con:
        row = con.execute("SELECT * FROM lab_suites WHERE run_id=?", (run_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Report not found")
    return {"run_id": run_id, "status": row["status"], "results": json.loads(row["results_json"])}

HTML = """<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>NeuroBalance — Real Dialog Test Lab</title>
<style>
:root{color-scheme:dark;font:15px system-ui,Arial;background:#0d1117;color:#e6edf3}
body{max-width:1100px;margin:28px auto;padding:0 16px}
header{display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:12px}
h1{font-size:25px;margin:0}h2{font-size:18px}
small,.muted{color:#9ca8b5}.tag{font-size:12px;background:#392d12;color:#f6ce79;border-radius:8px;padding:7px}
.layout{display:grid;grid-template-columns:1.5fr 1fr;gap:16px;margin-top:20px}
.panel{background:#161b22;border:1px solid #30363d;padding:18px;border-radius:12px}
.chat{height:430px;overflow-y:auto;display:flex;flex-direction:column;gap:10px;padding:12px;background:#0d1117;border:1px solid #30363d;border-radius:10px}
.bubble{white-space:pre-wrap;max-width:86%;padding:10px 13px;border-radius:12px}
.user{align-self:flex-end;background:#164c35}.bot{align-self:flex-start;background:#263340}
.meta{font-size:11px;color:#9ca8b5}
.controls{display:flex;gap:7px;margin-top:12px}
button,select,input{border:1px solid #405062;border-radius:8px;color:#e6edf3;background:#212a35;padding:11px;font:inherit}
button{cursor:pointer;background:#23875d;border:none}button:disabled{opacity:.5}
input{width:100%;min-width:0}select{flex:1}
.log{margin-top:12px;white-space:pre-wrap;max-height:260px;overflow:auto;font:12px ui-monospace,monospace;color:#9ed8ba}
.warn{color:#f4be65}.error{color:#f78282}
.result{margin:10px 0;padding:10px;background:#0d1117;border-radius:8px;white-space:pre-wrap;font-size:13px}
@media(max-width:760px){.layout{grid-template-columns:1fr}.chat{height:350px}}
</style></head><body>
<header><div><h1>NeuroBalance — Real Dialog Test Lab</h1><small>Настоящий GPT + production-код dialog.py / agent.py</small></div><span class="tag">ISOLATED CRM · NO WAZZUP · NOT PRODUCTION</span></header>
<div class="layout"><section class="panel"><h2>Ручной диалог</h2>
<div class="controls"><select id="lead"><option value="new">Новый клиент</option><option value="returning">Повторный клиент</option><option value="active">Уже записан</option><option value="crm_down">CRM недоступна</option></select><button onclick="fresh()">Новый диалог</button></div>
<p class="muted" id="sessionInfo">Создайте диалог для проверки.</p>
<div class="chat" id="chat"></div>
<div class="controls"><input id="msg" maxlength="2000" placeholder="Напишите как клиент..." onkeydown="if(event.key==='Enter')send()"><button id="send" onclick="send()">Отправить</button></div>
<p id="outcome" class="muted">Записи создаются только в тестовой CRM.</p>
<div class="log" id="trace"></div>
</section><section class="panel"><h2>Автоматическая проверка</h2><p class="muted">5 сценариев запускают настоящую модель и оригинальный Python-агент. Ответы не подготовлены заранее; тестовая только CRM.</p>
<button id="suiteButton" onclick="suite()">Запустить 5 сценариев</button>
<p id="suiteInfo" class="muted">Проверяем повтор вопросов, выбор врача, адрес, запись родственника, казахский язык и запрет ответов повторным клиентам.</p>
<div id="results"></div>
</section></div>
<script>
let sid=null,busy=false;
async function api(path,method='GET',body){
  const r=await fetch(path,{method,headers:body?{'Content-Type':'application/json'}:{},body:body?JSON.stringify(body):undefined});
  const data=await r.json();
  if(!r.ok)throw Error(data.detail||'HTTP '+r.status);return data;
}
function el(tag,klass,text){const node=document.createElement(tag);node.className=klass||'';node.textContent=text||'';return node}
function add(role,text,meta){
 const area=document.getElementById('chat'),wrap=el('div','bubble '+role,text);
 if(meta)wrap.appendChild(el('div','meta',meta));area.appendChild(wrap);area.scrollTop=area.scrollHeight;
}
async function fresh(){
 if(busy)return;try{const d=await api('/api/sessions','POST',{name:'Ручная проверка',lead_state:document.getElementById('lead').value});
 sid=d.sid;document.getElementById('chat').textContent='';document.getElementById('trace').textContent='';
 document.getElementById('sessionInfo').textContent='Диалог '+sid.slice(0,8)+' · '+d.lead_state;
 document.getElementById('outcome').textContent='Диалог готов. Введите первое сообщение.'}
 catch(e){alert(e.message)}
}
async function send(){
 if(busy)return;if(!sid){await fresh();if(!sid)return}
 const input=document.getElementById('msg');const text=input.value.trim();if(!text)return;input.value='';busy=true;
 document.getElementById('send').disabled=true;add('user',text);
 try{const d=await api('/api/sessions/'+sid+'/messages','POST',{text});
 const t=d.turn;add('bot',t.answer||'∅ Нет ответа',t.duration_ms+' мс · OpenAI: '+(t.openai_used?'Да':'Нет')+' · '+(t.model||'model n/a'));
 document.getElementById('outcome').textContent='CRM: '+t.crm_patient_state+' | Тестовых записей: '+t.fixture_bookings+' | Проблем: '+d.issues.length;
 document.getElementById('trace').textContent=JSON.stringify({tool_calls:t.tool_calls,no_reply_reason:t.no_reply_reason,handoff_reason:t.handoff_reason,openai_skip_reason:t.openai_skip_reason,issues:d.issues},null,2)}
 catch(e){add('bot','Ошибка запроса: '+e.message)}
 finally{busy=false;document.getElementById('send').disabled=false}
}
async function suite(){
 const btn=document.getElementById('suiteButton');btn.disabled=true;
 const info=document.getElementById('suiteInfo');const root=document.getElementById('results');root.textContent='';
 try{const created=await api('/api/suites','POST');info.textContent='Запущены реальные вызовы OpenAI. Выполняются сценарии...';
 const poll=async()=>{const d=await api('/api/suites/'+created.run_id);root.textContent='';
 for(const v of d.results){const b=el('div','result');
 b.appendChild(el('strong','',v.name||v.error||'Error'));
 if(v.turns){b.appendChild(el('div','muted',v.turns.length+' ходов · вызовов OpenAI: '+v.turns.filter(x=>x.openai_used).length+' · проблем: '+v.issues.length));
 for(const issue of v.issues)b.appendChild(el('div',issue.type==='critical'?'error':'warn',issue.rule+': '+issue.message));
 for(const t of v.turns)b.appendChild(el('div','', 'Клиент: '+t.user+'\\nAI: '+(t.answer||'∅')+'\\n'))}
 root.appendChild(b)}
 info.textContent='Статус: '+d.status+' · '+d.results.length+' / 5';
 if(d.status==='running')setTimeout(poll,3000);else btn.disabled=false;
 };await poll();}
 catch(e){info.textContent='Ошибка: '+e.message;btn.disabled=false}
}
fresh();
</script></body></html>"""

@app.get("/", response_class=HTMLResponse, dependencies=[Depends(authenticate)])
def index():
    return HTMLResponse(HTML)
