"""Synthetic webhook contracts, no real providers or patient data."""
import asyncio

import pytest
from fastapi.testclient import TestClient

import main
import state
from config import get_settings


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv('SQLITE_PATH', str(tmp_path / 'app.sqlite3'))
    monkeypatch.setenv('WAZZUP_CHANNEL_ID', 'synthetic-channel')
    monkeypatch.setenv('WAZZUP_ALLOWED_CHANNEL_IDS', 'synthetic-channel,second-channel')
    get_settings.cache_clear()
    state.init_db()
    monkeypatch.setattr(main, 'is_bot_work_time', lambda: True)
    yield
    get_settings.cache_clear()


def payload(**kwargs):
    return {'chatId': '77010000009', 'channelId': 'synthetic-channel',
            'chatType': 'whatsapp', 'direction': 'incoming', 'messageId': 'synthetic-event', **kwargs}


def test_attachment_identity_and_caption_survive_normalization():
    raw = payload(type='document', text='МРТ мамы', contentUri='https://cdn.wazzup24.com/report.pdf', mimeType='application/pdf')
    message = main._normalize_wazzup_message({}, raw)
    assert message['text'] == 'МРТ мамы'
    assert message.get('attachments') and message['attachments'][0]['media_url'].endswith('report.pdf')


def test_no_id_distinct_attachments_do_not_collide():
    a = payload(messageId='', type='image', contentUri='https://cdn.wazzup24.com/a.jpg')
    b = {**a, 'contentUri': 'https://cdn.wazzup24.com/b.jpg'}
    assert main._normalize_wazzup_message({}, a)['message_key'] != main._normalize_wazzup_message({}, b)['message_key']
    assert main._normalize_wazzup_message({}, a)['message_key'] == main._normalize_wazzup_message({}, dict(a))['message_key']


@pytest.mark.parametrize('kind,mime', [('voice', 'audio/ogg'), ('image', 'image/png'), ('document', 'application/pdf')])
def test_media_without_caption_reaches_pipeline(monkeypatch, kind, mime):
    seen = []
    async def handler(message):
        seen.append(message)
        return {'answer': '', 'should_send_wazzup': False}
    monkeypatch.setattr(main, 'handle_incoming_message', handler)
    response = TestClient(main.app).post('/webhook/wazzup', json=payload(type=kind, mimeType=mime, contentUri='https://cdn.wazzup24.com/synthetic'))
    assert response.status_code == 200
    assert len(seen) == 1 and seen[0]['attachments']


@pytest.mark.parametrize('change', ['daytime', 'takeover', 'disabled'])
def test_last_moment_send_gate(monkeypatch, change):
    sent = []
    async def handler(message):
        if change == 'daytime':
            monkeypatch.setattr(main, 'is_bot_work_time', lambda: False)
        elif change == 'disabled':
            get_settings().bot_auto_reply_enabled = False
        else:
            session = state.get_session(message['chat_id'])
            session['manual_admin_intervention'] = True
            state.save_session(message['chat_id'], session)
        return {'answer': 'Тестовый ответ', 'should_send_wazzup': True}
    async def sender(**kwargs):
        sent.append(kwargs)
        return {'ok': True}
    monkeypatch.setattr(main, 'handle_incoming_message', handler)
    monkeypatch.setattr(main, 'send_wazzup_message', sender)
    result = TestClient(main.app).post('/webhook/wazzup', json=payload(text='Тест')).json()
    assert sent == []
    assert result.get('inbox_status') in {'handoff_pending', 'handoff_confirmed'}


def test_staff_takeover_is_saved_before_fifo_processing(monkeypatch):
    monkeypatch.setenv('DURABLE_WEBHOOK_ENABLED', 'true')
    get_settings.cache_clear()
    response = TestClient(main.app).post('/webhook/wazzup', json=payload(text='Сотрудник подключился', direction='outgoing', fromMe=True, source='operator'))
    assert response.status_code == 200
    assert state.get_session('77010000009').get('manual_admin_intervention') is True


def test_status_only_receipt_is_not_staff_takeover():
    main._mark_manual_admin_interventions({'messages': [payload(direction='', status='sent', messageId='provider-receipt')]})
    assert not state.get_session('77010000009').get('manual_admin_intervention')


def test_failed_handoff_remains_attention_even_when_reply_sent(monkeypatch):
    async def handler(message):
        session = state.get_session(message['chat_id'])
        session.update(operator_handoff_confirmed=False, operator_handoff_error='TimeoutError')
        state.save_session(message['chat_id'], session)
        return {'answer': 'Связаться с сотрудником пока не удалось.', 'should_send_wazzup': True}
    async def sender(**kwargs):
        return {'ok': True}
    monkeypatch.setattr(main, 'handle_incoming_message', handler)
    monkeypatch.setattr(main, 'send_wazzup_message', sender)
    response = TestClient(main.app).post('/webhook/wazzup', json=payload(text='Нужен человек'))
    assert response.json().get('inbox_status') == 'handoff_pending'


def test_stale_ai_session_cannot_erase_staff_takeover():
    stale = state.get_session('synthetic-race')
    human = dict(stale, manual_admin_intervention=True, manual_takeover=True, ai_muted=True)
    state.save_session('synthetic-race', human)
    state.save_session('synthetic-race', stale)
    assert state.get_session('synthetic-race')['manual_admin_intervention'] is True
