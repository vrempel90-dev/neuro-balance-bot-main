from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

import admission
import agent
import crm
import dialog
import state
from config import get_settings

PHONE = '77010000001'


@pytest.fixture(autouse=True)
def database(monkeypatch, tmp_path):
    monkeypatch.setenv('SQLITE_PATH', str(tmp_path / 'audit.sqlite3'))
    get_settings.cache_clear()
    state.init_db()
    yield
    get_settings.cache_clear()


def fresh(**extra):
    return {'ok': True, 'found': True, 'isNew': True,
            'lead': {'id': 'synthetic-lead', 'status': 'NEW'},
            'hasActiveAppointment': False, **extra}


@pytest.mark.parametrize('blocker', [
    {'found': True, 'isNew': False},
    {'found': True, 'isNew': True, 'lead': {'id': 'old', 'status': 'IN PROGRESS'}},
    {'found': False, 'isNew': True, 'hasActiveAppointment': 'unknown'},
    {'found': False, 'isNew': True, 'patient': []},
])
def test_any_phone_variant_exclusion_blocks_new_lead(monkeypatch, blocker):
    async def lookup(phone):
        return blocker if phone == 'blocked' else fresh()
    monkeypatch.setattr(crm, 'phone_lookup_variants', lambda phone: ['fresh', 'blocked'])
    monkeypatch.setattr(crm, 'patient_lookup', lookup)
    result = asyncio.run(crm.CRMClient().lookup_active_appointments_by_phone(PHONE))
    assert admission.classify(PHONE, result).bot_may_reply is False


def test_patient_switch_does_not_restore_clearance_or_slots():
    state.save_session('switch', {'patient_context_version': 0, 'contraindications_ok': True,
        'step': 'time', 'last_slots': [{'time': '14:00'}]})
    state.save_session('switch', {'patient_context_version': 1, 'contraindications_ok': False,
        'step': 'time', 'last_slots': []})
    actual = state.get_session('switch')
    assert actual['contraindications_ok'] is False
    assert actual['last_slots'] == []


def test_reset_does_not_resurrect_medical_clearance():
    state.save_session('reset', {'contraindications_ok': True})
    state.reset_session('reset')
    assert state.get_session('reset')['contraindications_ok'] is not True


def test_same_patient_still_preserves_valid_clearance_and_slots():
    state.save_session('same', {'contraindications_ok': True, 'step': 'time',
        'last_slots': [{'time': '14:00'}]})
    state.save_session('same', {'contraindications_ok': False, 'step': 'time', 'last_slots': []})
    actual = state.get_session('same')
    assert actual['contraindications_ok'] is True
    assert actual['last_slots'] == [{'time': '14:00'}]


def test_emergency_is_handled_before_model_or_booking(monkeypatch):
    async def lookup(phone):
        return fresh()
    async def forbidden_agent(**kwargs):
        pytest.fail('emergency must not reach model or booking tools')
    async def escalate(**kwargs):
        return {'ok': True}
    monkeypatch.setattr(crm, 'lookup_active_appointments_by_phone', lookup)
    monkeypatch.setattr(crm, 'escalate_to_operator', escalate)
    monkeypatch.setattr(agent, 'run_agent_turn', forbidden_agent)
    reply = asyncio.run(dialog.handle_message('emergency', PHONE, 'Не могу дышать, сильная боль в груди'))
    assert '103' in reply
    assert state.get_session('emergency')['handoff_reason'] == 'red_flag'


def classify(monkeypatch, payload, session):
    async def lookup(phone):
        return deepcopy(payload)
    monkeypatch.setattr(crm, 'lookup_active_appointments_by_phone', lookup)
    return asyncio.run(dialog._classify_lead('continuation', PHONE, session))


def test_verified_initial_new_lead_can_complete_same_funnel(monkeypatch):
    session = {}
    assert classify(monkeypatch, fresh(), session).bot_may_reply
    state.save_session('continuation', session)
    restored = state.get_session('continuation')
    assert classify(monkeypatch, fresh(isNew=False), restored).bot_may_reply


@pytest.mark.parametrize('change', [
    {'patient': {'id': 'existing'}},
    {'lastAppointment': {'id': 'past', 'status': 'cancelled'}},
    {'appointments': [{'id': 'past', 'status': 'cancelled'}]},
    {'hasActiveAppointment': True},
    {'activeAppointment': {'id': 'past', 'status': 'cancelled'}},
    {'lead': {'id': 'other', 'status': 'NEW'}},
    {'lead': {'id': 'synthetic-lead', 'status': 'IN PROGRESS'}},
])
def test_continuation_never_overrides_fresh_exclusions(monkeypatch, change):
    session = {}
    classify(monkeypatch, fresh(), session)
    assert not classify(monkeypatch, fresh(isNew=False, **change), session).bot_may_reply


@pytest.mark.parametrize('change', [
    {'manual_admin_intervention': True}, {'manual_takeover': True},
    {'ai_admission_closed_at': 'closed'}, {'booking_confirmed': True},
    {'ai_admission_started_at': (datetime.now(timezone.utc)-timedelta(days=2)).isoformat()},
    {'phone_normalized': '77019999999'},
])
def test_continuation_requires_open_current_dialog(monkeypatch, change):
    session = {}
    classify(monkeypatch, fresh(), session)
    # Classification records the provenance start; the normal turn opens admission.
    dialog._activate_ai_admission_lease(session)
    session.update(change)
    if 'phone_normalized' in change:
        # A different sender must not reuse the initial identity binding.
        session['ai_admission_phone'] = change['phone_normalized']
    assert not classify(monkeypatch, fresh(isNew=False), session).bot_may_reply


def test_initial_new_without_lead_id_does_not_create_a_continuation_lease(monkeypatch):
    session = {}
    classify(monkeypatch, fresh(lead=None, found=False), session)
    assert not classify(monkeypatch, fresh(isNew=False), session).bot_may_reply


def test_continuation_roundtrip_through_real_dialog(monkeypatch):
    responses = iter([fresh(), fresh(isNew=False)])
    turns = []
    async def lookup(phone):
        return next(responses)
    async def model(**kwargs):
        turns.append(kwargs['user_text'])
        return agent.AgentResult(used=True, reply='Подскажите, что беспокоит?')
    monkeypatch.setattr(crm, 'lookup_active_appointments_by_phone', lookup)
    monkeypatch.setattr(agent, 'run_agent_turn', model)
    assert asyncio.run(dialog.handle_message('roundtrip', PHONE, 'Здравствуйте'))
    assert asyncio.run(dialog.handle_message('roundtrip', PHONE, 'Болит спина'))
    assert len(turns) == 2
    assert state.get_session('roundtrip')['crm_state_reason'] == 'verified_initial_new_lead'


def test_existing_sender_requesting_relative_remains_silent(monkeypatch):
    async def lookup(phone):
        return fresh(isNew=False, patient={'id': 'existing-sender'})
    async def forbidden_agent(**kwargs):
        pytest.fail('a relative request cannot bypass sender admission')
    async def escalate(**kwargs):
        return {'ok': True}
    monkeypatch.setattr(crm, 'lookup_active_appointments_by_phone', lookup)
    monkeypatch.setattr(crm, 'escalate_to_operator', escalate)
    monkeypatch.setattr(agent, 'run_agent_turn', forbidden_agent)
    assert asyncio.run(dialog.handle_message('relative', PHONE, 'Хочу записать маму, ей 54')) == ''
    assert state.get_session('relative')['crm_patient_is_new'] is False


def test_conflicting_variant_cannot_use_valid_continuation_provenance(monkeypatch):
    session = {}
    classify(monkeypatch, fresh(), session)
    async def lookup(phone):
        return {'found': True, 'isNew': False} if phone == 'blocked' else fresh(isNew=False)
    monkeypatch.setattr(crm, 'phone_lookup_variants', lambda phone: ['fresh', 'blocked'])
    monkeypatch.setattr(crm, 'patient_lookup', lookup)
    result = asyncio.run(crm.CRMClient().lookup_active_appointments_by_phone(PHONE))
    assert not classify(monkeypatch, result, session).bot_may_reply
