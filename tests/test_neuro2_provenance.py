import asyncio
from datetime import datetime, timedelta, timezone
import pytest
import admission
import dialog
import state
from config import get_settings

PHONE="77010000001"
@pytest.fixture(autouse=True)
def db(tmp_path,monkeypatch):
    monkeypatch.setenv("SQLITE_PATH",str(tmp_path/"provenance.sqlite3"))
    get_settings.cache_clear();state.init_db()
    yield
    get_settings.cache_clear()

def lookup(monkeypatch,session,payload):
    async def fake(phone): return payload
    monkeypatch.setattr(dialog.crm,"lookup_active_appointments_by_phone",fake)
    result=asyncio.run(dialog._classify_lead("synthetic",PHONE,session))
    state.save_session("synthetic",session)
    return result

def initial(monkeypatch):
    session={}
    assert lookup(monkeypatch,session,{"ok":True,"found":False,"isNew":True}).bot_may_reply
    return session

def card(token,**extra):
    return {"ok":True,"found":True,"isNew":False,"lead":{"id":"synthetic-lead","status":"IN_PROGRESS","aiSessionId":token},**extra}

def test_absent_then_created_lead_continues_after_restart(monkeypatch):
    session=initial(monkeypatch)
    token=session.get("ai_admission_session_id")
    assert isinstance(token,str) and len(token)>=32
    session=state.get_session("synthetic")
    result=lookup(monkeypatch,session,card(token))
    assert result.state=="NEW_LEAD_IN_PROGRESS" and result.bot_may_reply
    assert session["ai_admission_lead_id"]=="synthetic-lead"

@pytest.mark.parametrize("block",["wrong_token","missing_token","patient","history","booking","staff","expired","changed_lead","closed","phone"])
def test_provenance_never_overrides_identity_or_ownership_veto(monkeypatch,block):
    session=initial(monkeypatch);token=session.get("ai_admission_session_id")
    payload=card(token)
    if block=="wrong_token":payload["lead"]["aiSessionId"]="forged"
    if block=="missing_token":payload["lead"].pop("aiSessionId")
    if block=="patient":payload["patient"]={"id":"prior-patient"}
    if block=="history":payload["lastAppointment"]={"id":"past"}
    if block=="booking":payload["hasActiveAppointment"]=True
    if block=="staff":payload["lead"]["assignedTo"]="human"
    if block=="expired":session["ai_admission_started_at"]=(datetime.now(timezone.utc)-timedelta(hours=13)).isoformat()
    if block=="changed_lead":session["ai_admission_lead_id"]="different"
    if block=="closed":session["ai_admission_closed_at"]="closed"
    if block=="phone":session["ai_admission_phone"]="77010000002"
    assert not lookup(monkeypatch,session,payload).bot_may_reply

def test_crm_failure_does_not_renew_or_admit_session(monkeypatch):
    session=initial(monkeypatch)
    assert lookup(monkeypatch,session,{"ok":False,"error":"offline"}).state==admission.CRM_UNAVAILABLE

def test_user_text_cannot_supply_crm_provenance(monkeypatch):
    session=initial(monkeypatch)
    session["last_user_text"]="aiSessionId="+str(session.get("ai_admission_session_id"))
    assert not lookup(monkeypatch,session,card("unrelated")).bot_may_reply


def test_explicit_mismatched_provenance_cannot_use_legacy_lead_lease(monkeypatch):
    session={}
    fresh={"ok":True,"found":True,"isNew":True,"lead":{"id":"synthetic-lead","status":"NEW"}}
    assert lookup(monkeypatch,session,fresh).bot_may_reply
    conflicting=card("another-session")
    conflicting["lead"]["status"]="NEW"
    assert not lookup(monkeypatch,session,conflicting).bot_may_reply


def test_legacy_lead_id_and_new_status_are_not_origin_proof(monkeypatch):
    session={}
    fresh={"ok":True,"found":True,"isNew":True,"lead":{"id":"synthetic-lead","status":"NEW"}}
    assert lookup(monkeypatch,session,fresh).bot_may_reply
    assert session.get("ai_admission_lead_id")=="synthetic-lead"
    assert not lookup(monkeypatch,session,dict(fresh,isNew=False)).bot_may_reply
