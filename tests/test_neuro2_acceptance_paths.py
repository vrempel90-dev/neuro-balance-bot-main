"""Real HTTP ingress, durable FIFO, media/dialog boundaries and outbound claims."""
import asyncio
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from config import get_settings
import crm
import main
import media
import production_inbox as inbox
import state


@pytest.mark.parametrize("kind", ["voice", "document"])
def test_ordered_media_traverse_durable_pipeline(tmp_path, monkeypatch, kind):
    monkeypatch.setenv('SQLITE_PATH', str(tmp_path / 'voice-fifo.sqlite3'))
    monkeypatch.setenv('DURABLE_WEBHOOK_ENABLED', 'true')
    monkeypatch.setenv('WAZZUP_CHANNEL_ID', 'synthetic-channel')
    monkeypatch.setenv('WAZZUP_ALLOWED_CHANNEL_IDS', '')
    monkeypatch.setenv('WEBHOOK_SECRET', '')
    monkeypatch.setenv('MEDICAL_DOCUMENT_PROCESSING_ENABLED', 'true')
    monkeypatch.setenv('BOT_AUTO_REPLY_ENABLED', 'true')
    get_settings.cache_clear()
    state.init_db()
    inbox.init_db()
    monkeypatch.setattr(main, 'is_bot_work_time', lambda: True)
    observed, sent = [], []
    async def lookup(phone):
        return {'ok': True, 'found': False, 'isNew': True}
    async def transcribe(part, **kwargs):
        return SimpleNamespace(text={'voice-1':'Болит поясница', 'voice-2':'Мне 42 года'}[part['file_id']])
    async def dialog(chat_id, phone, user_text):
        observed.append(user_text)
        state.add_message(chat_id, 'user', user_text)
        return f'Уточнение {len(observed)}'
    async def humanize(chat_id, user_text, answer, **kwargs):
        return answer
    async def sender(**kwargs):
        sent.append(kwargs)
        return {'ok': True, 'messageId': f'synthetic-provider-{len(sent)}'}
    monkeypatch.setattr(crm, 'lookup_active_appointments_by_phone', lookup)
    monkeypatch.setattr(media.voice, 'transcribe_wazzup_voice', transcribe)
    async def document(part):
        return (await transcribe(part)).text
    monkeypatch.setattr(media, 'extract_document', document)
    monkeypatch.setattr(main, 'handle_message', dialog)
    monkeypatch.setattr(main, '_maybe_humanize_answer', humanize)
    monkeypatch.setattr(main, 'send_wazzup_message', sender)
    payload = {'messages': [{'chatId':'77010000009', 'channelId':'synthetic-channel',
        'messageId': f'event-{n}', 'type':kind, 'mimeType':'audio/ogg' if kind == 'voice' else 'application/pdf',
        'fileId': f'voice-{n}', 'fromMe':False, 'timestamp':state.now_iso()} for n in (1,2)]}
    result = TestClient(main.app).post('/webhook/wazzup', json=payload)
    assert result.status_code == 200 and result.json()['accepted_count'] == 2
    async def process():
        worker = inbox.InboxWorker(main._process_inbox_job)
        await worker._process(inbox.claim_next())
        await worker._process(inbox.claim_next())
    asyncio.run(process())
    assert len(observed) == 2
    assert 'Болит поясница' in observed[0] and '42 года' in observed[1]
    assert len(sent) == 2
    assert inbox.status_summary()['counts']['done'] == 2
    history = state.get_history('77010000009')
    assert len(history) == 2 and '42 года' in history[-1]['content']
    get_settings.cache_clear()
