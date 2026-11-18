import pytest
from config import get_settings
import state
from fastapi.testclient import TestClient
import main

@pytest.fixture(autouse=True)
def isolated(tmp_path,monkeypatch):
 monkeypatch.setenv('SQLITE_PATH',str(tmp_path/'delivery.sqlite3'))
 monkeypatch.setenv('WAZZUP_CHANNEL_ID','channel-a')
 get_settings.cache_clear(); state.init_db()
 yield
 get_settings.cache_clear()

def bind():
 state.claim_outgoing_message('reply:test','internal-chat')
 state.bind_provider_message('reply:test',provider_id='provider-1',channel_id='channel-a',provider_chat_id='synthetic-chat')

@pytest.mark.parametrize('status,want',[('sent','PROVIDER_ACCEPTED'),('delivered','DELIVERED'),('read','READ'),('error','FAILED_NEEDS_ATTENTION')])
def test_receipt_maps_to_real_claim(status,want):
 bind(); state.record_delivery_receipt({'messageId':'provider-1','status':status})
 assert state.outgoing_delivery_status('reply:test')==want

def test_early_receipt_survives_restart():
 state.record_delivery_receipt({'messageId':'provider-1','status':'read'})
 state.init_db(); bind()
 assert state.outgoing_delivery_status('reply:test')=='READ'

def test_duplicate_and_out_of_order_receipts_do_not_downgrade():
 bind()
 for s in ['read','read','sent','delivered','error']:
  state.record_delivery_receipt({'messageId':'provider-1','status':s})
 assert state.outgoing_delivery_status('reply:test')=='READ'

@pytest.mark.parametrize('extra',[{'channelId':'foreign'},{'chatId':'wrong'}])
def test_wrong_route_receipt_cannot_change_claim(extra):
 bind(); state.record_delivery_receipt({'messageId':'provider-1','status':'delivered',**extra})
 assert state.outgoing_delivery_status('reply:test')=='PROVIDER_ACCEPTED'

def test_receipts_bypass_dialog_and_inbox(monkeypatch):
 bind()
 async def forbidden(*args,**kwargs): pytest.fail('Receipt reached dialog')
 monkeypatch.setattr(main,'handle_incoming_message',forbidden)
 result=TestClient(main.app).post('/webhook/wazzup',json={'statuses':[{'messageId':'provider-1','status':'delivered'}]})
 assert result.status_code==200
 assert state.outgoing_delivery_status('reply:test')=='DELIVERED'
 assert not state.get_session('synthetic-chat').get('manual_takeover')

def test_failed_delivery_visible_in_aggregate_without_identifiers():
 bind(); state.record_delivery_receipt({'messageId':'provider-1','status':'error'})
 assert state.delivery_status_summary()=={'FAILED_NEEDS_ATTENTION':1}
 assert state.outgoing_message_status('reply:test')=='sending'
 # Receipt interpretation never reclaims or retries the outbound operation.
 assert not state.claim_outgoing_message('reply:test','internal-chat')
