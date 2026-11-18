import agent
import pytest

def route(status='PROFILE',domain='боль в спине',evidence='Тянет ниже ремня после долгого сидения'):
    return {'status':status,'domain':domain,'evidence':evidence,'confidence':0.95}

def test_semantic_paraphrase_is_routed_without_keyword_rewrite():
    text='Тянет ниже ремня после долгого сидения'
    s={'last_user_text':text}
    r=agent._tool_record_patient_facts('semantic',s,{'complaint':text,'semantic_route':route()})
    assert r['profile_status']=='profile'
    assert s['complaint']==text

@pytest.mark.parametrize('extra',[
    {'domain':'стоматология'}, {'evidence':'Болит поясница'}, {'confidence':0.4},
    {'status':'EXPAND_PROFILE'}, {'confidence':True}])
def test_invalid_or_unverifiable_semantic_claim_cannot_admit(extra):
    text='Тянет ниже ремня после долгого сидения'; s={'last_user_text':text}
    r=agent._tool_record_patient_facts('semantic',s,{'complaint':text,'semantic_route':{**route(),**extra}})
    assert r['profile_status']=='unclear'

def test_semantic_extraction_cannot_erase_explicit_excluded_complaint():
    s={'last_user_text':'Болит зуб и поясница'}
    r=agent._tool_record_patient_facts('semantic',s,{'complaint':'поясница','semantic_route':route(evidence='поясница')})
    assert r['profile_status']=='unclear'

def test_semantic_urgent_blocks_booking():
    text='Внезапно перестала слушаться правая рука'; s={'last_user_text':text}
    r=agent._tool_record_patient_facts('semantic',s,{'complaint':text,'semantic_route':route('URGENT','неврология',text)})
    assert r['profile_status']=='urgent'
    assert agent._medical_gate_error(s)=='urgent_requires_handoff'

def test_missing_model_route_remains_uncertain():
    s={'last_user_text':'Мне нехорошо'}
    assert agent._tool_record_patient_facts('semantic',s,{'complaint':'Мне нехорошо'})['profile_status']=='unclear'

def test_patient_reset_discards_semantic_authorization():
    text='Тянет ниже ремня после долгого сидения'; s={'last_user_text':text}
    agent._tool_record_patient_facts('semantic',s,{'complaint':text,'semantic_route':route()})
    agent._reset_patient_context(s)
    assert not s.get('semantic_route_evidence')


def test_model_complaint_does_not_serve_as_its_own_evidence():
    import semantic_router
    result=semantic_router.classify('Тянет ниже ремня после долгого сидения','Мне нехорошо',route())
    assert result['profile_status']=='unclear'

def test_urgent_semantics_take_precedence_over_excluded_domain():
    s={'last_user_text':'Болит зуб, внезапно потерял сознание'}
    result=agent._tool_record_patient_facts('semantic',s,{'complaint':s['last_user_text'],
        'semantic_route':route('URGENT','не определено','внезапно потерял сознание')})
    assert result['profile_status']=='urgent'


def test_urgent_route_cannot_end_in_ordinary_booking_reply():
    s={'profile_status':'urgent','language':'ru'}
    result=agent.AgentResult()
    reply=agent._enforce_profile_gate_reply(s,result,'Внезапно потерял сознание','На какой день записать?')
    assert '103' in reply and '112' in reply
    assert 'записать' not in reply.lower()


@pytest.mark.parametrize("confirmed", [True, False])
def test_dialog_urgent_escalation_keeps_emergency_advice(tmp_path, monkeypatch, confirmed):
    import asyncio
    import crm, dialog, state
    from config import get_settings
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "urgent.sqlite3"))
    get_settings.cache_clear()
    state.init_db()
    async def lookup(phone):
        return {"ok": True, "found": False, "isNew": True}
    async def escalate(**kwargs):
        return {"ok": confirmed}
    async def run(**kwargs):
        kwargs["session"]["profile_status"] = "urgent"
        return agent.AgentResult(used=True, escalate=True, reply="Нужна срочная помощь: 103 или 112.")
    monkeypatch.setattr(crm, "lookup_active_appointments_by_phone", lookup)
    monkeypatch.setattr(crm, "escalate_to_operator", escalate)
    monkeypatch.setattr(agent, "run_agent_turn", run)
    answer = asyncio.run(dialog.handle_message("synthetic-urgent", "77010000009", "Внезапно перестала слушаться правая рука"))
    assert "103" in answer and "112" in answer
    if not confirmed:
        assert state.get_session("synthetic-urgent")["operator_handoff_confirmed"] is False
    get_settings.cache_clear()


def test_generic_trunk_location_cannot_be_rewritten_into_back_pain():
    text = "Когда долго сижу, в нижней части туловища словно тянет струну, особенно при наклоне."
    s = {"last_user_text": text}
    result = agent._tool_record_patient_facts("synthetic-vague", s,
        {"complaint": "боль в спине", "semantic_route": route(evidence=text)})
    assert result["profile_status"] == "unclear"
