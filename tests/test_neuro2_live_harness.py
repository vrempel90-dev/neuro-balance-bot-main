from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts' / 'live_acceptance.py'


def harness():
    assert SCRIPT.exists(), 'bounded live acceptance runner is missing'
    spec = importlib.util.spec_from_file_location('neuro2_live_acceptance', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_default_execution_cannot_make_live_calls(tmp_path):
    result = subprocess.run([sys.executable, str(SCRIPT), '--output', str(tmp_path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    report = json.loads((tmp_path / 'report.json').read_text(encoding='utf-8'))
    assert report['mode'] == 'dry_run'
    assert report['calls'] == 0
    assert report['production_transport_blocked'] is True


def test_production_and_redirect_destinations_blocked_before_budget():
    module = harness()
    guard = module.RequestGuard(live=True, max_calls=20, max_usd=1)
    for url in ['https://api.wazzup24.com/v3/message', 'https://neuro-balance-crm.vercel.app/api',
                'http://api.openai.com/v1/chat/completions', 'https://api.openai.com.evil.test/v1/chat/completions',
                'https://api.openai.com/v1/files']:
        with pytest.raises(module.SafetyStop):
            guard.authorize(httpx.Request('POST', url, json={'model': 'gpt-5.6-terra'}))
    assert guard.calls == 0


def test_budget_refuses_twenty_first_call_and_does_not_spend_reservation():
    module = harness()
    guard = module.RequestGuard(live=True, max_calls=20, max_usd=1)
    request = httpx.Request('POST', 'https://api.openai.com/v1/audio/speech', json={'model': 'gpt-4o-mini-tts', 'input': 'short'})
    for _ in range(20):
        guard.authorize(request)
    with pytest.raises(module.SafetyStop):
        guard.authorize(request)
    assert guard.calls == 20
    assert guard.reserved_usd <= 1


def test_cost_cap_checked_before_sending_even_without_usage():
    module = harness()
    guard = module.RequestGuard(live=True, max_calls=20, max_usd=0.03)
    request = httpx.Request('POST', 'https://api.openai.com/v1/audio/speech', json={'model': 'gpt-4o-mini-tts', 'input': 'short'})
    with pytest.raises(module.SafetyStop):
        guard.authorize(request)
    assert guard.calls == 0
    assert guard.reserved_usd == 0


def test_dry_guard_never_authorizes_openai():
    module = harness()
    guard = module.RequestGuard(live=False, max_calls=20, max_usd=1)
    with pytest.raises(module.SafetyStop):
        guard.authorize(httpx.Request('POST', 'https://api.openai.com/v1/audio/speech'))
    assert guard.calls == 0


def test_isolation_overwrites_production_database_before_import(monkeypatch, tmp_path):
    module = harness()
    monkeypatch.setenv('SQLITE_PATH', '/production/patients.sqlite3')
    monkeypatch.setenv('CRM_WRITE_ENABLED', 'true')
    import os
    # Register every environment override with pytest so the harness test
    # cannot leak its synthetic routing into later application contracts.
    before = dict(os.environ)
    module.configure_isolation(tmp_path)
    changed = {key: value for key, value in os.environ.items() if before.get(key) != value}
    os.environ.clear()
    os.environ.update(before)
    for key, value in changed.items():
        monkeypatch.setenv(key, value)
    assert Path(os.environ['SQLITE_PATH']).parent == tmp_path
    assert os.environ['CRM_WRITE_ENABLED'] == 'false'
    assert os.environ['WAZZUP_API_KEY'] == ''
    assert os.environ['CRM_BOT_SECRET'] == ''


def test_invalid_budget_cannot_raise_hard_limits():
    module = harness()
    for kwargs in [dict(max_calls=21,max_usd=1),dict(max_calls=20,max_usd=1.01),dict(max_calls=0,max_usd=1)]:
        with pytest.raises(ValueError):
            module.RequestGuard(live=True, **kwargs)



def test_terra_reservation_bounds_prompt_and_completion_before_request():
    module = harness()
    guard = module.RequestGuard(live=True,max_calls=20,max_usd=.1)
    with pytest.raises(module.SafetyStop):
        guard.authorize(httpx.Request('POST','https://api.openai.com/v1/chat/completions',
            json={'model':'gpt-5.6-terra','max_completion_tokens':2000,'messages':[]}))
    assert guard.calls == 0
    with pytest.raises(module.SafetyStop):
        guard.authorize(httpx.Request('POST','https://api.openai.com/v1/chat/completions',
            json={'model':'gpt-5.6-terra','messages':[]}))
    assert guard.calls == 0


def test_noise_fixture_is_decodable_and_preserves_duration():
    import io
    import wave
    module=harness()
    buffer=io.BytesIO()
    with wave.open(buffer,'wb') as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(24000)
        target.writeframes(b'\0\0'*24000)
    noisy=module.noisy_wav(buffer.getvalue())
    with wave.open(io.BytesIO(noisy),'rb') as decoded:
        assert decoded.getnframes() == 24000
        assert decoded.getframerate() == 24000
        assert decoded.readframes(24000) != b'\0\0'*24000


def test_semantic_evidence_does_not_excuse_wrong_routing():
    from types import SimpleNamespace
    module=harness()
    result=SimpleNamespace(used=True,reply='Следующий вопрос',booked=False)
    session={'age':42,'profile_status':'non_profile','semantic_route_evidence':{
        'source':'белім ауырады','interpretation':{'status':'NON_PROFILE','evidence':'белім ауырады'}}}
    assessment=module.assess_semantic(result,session,'Мен 42, белім ауырады','PROFILE')
    assert assessment['literal_evidence_valid'] is True
    assert assessment['passed'] is False
    assert assessment['route_status_matches'] is False


def test_semantic_admission_requires_saved_classification_and_known_age():
    from types import SimpleNamespace
    module=harness()
    result=SimpleNamespace(used=True,reply='Қай уақыттан бері ауырады?',booked=False)
    session={'age':42,'profile_status':'profile','semantic_route_evidence':{
        'source':'белім ауырады','interpretation':{'status':'PROFILE','evidence':'белім ауырады'}}}
    assert module.assess_semantic(result,session,'Мен 42, белім ауырады','PROFILE')['passed'] is True
    session['age']=24
    assert module.assess_semantic(result,session,'Мен 42, белім ауырады','PROFILE')['passed'] is False


def test_streaming_wav_sentinel_uses_decoded_payload_duration():
    import io,struct,wave
    module=harness()
    buffer=io.BytesIO()
    with wave.open(buffer,'wb') as target:
        target.setnchannels(1); target.setsampwidth(2); target.setframerate(24000)
        target.writeframes(b'\0\0'*24000)
    streamed=bytearray(buffer.getvalue())
    struct.pack_into('<I',streamed,4,0xffffffff)
    struct.pack_into('<I',streamed,40,0xffffffff)
    normalized=module.normalize_wav(bytes(streamed))
    with wave.open(io.BytesIO(normalized),'rb') as decoded:
        assert decoded.getnframes()==24000
        assert decoded.getframerate()==24000
    assert module.noisy_wav(bytes(streamed)).startswith(b'RIFF')


def test_case_filter_prevents_unselected_operation_and_does_not_claim_pass():
    import asyncio
    module=harness()
    guard=module.RequestGuard(live=False)
    report={'cases':[]}
    async def forbidden():
        raise AssertionError('unselected provider operation executed')
    asyncio.run(module.execute_case(report,guard,{'document_report.png'},'voice_ru',forbidden))
    assert report['cases']==[]


def test_case_filter_executes_selected_case_and_sanitizes_reason():
    import asyncio
    module=harness()
    report={'cases':[]}
    guard=module.RequestGuard(live=False)
    async def failure():
        raise module.SafetyStop('unexpected_audio')
    asyncio.run(module.execute_case(report,guard,{'voice_ru'},'voice_ru',failure))
    assert report['cases']==[{'case':'voice_ru','passed':False,'calls':0,'error_type':'SafetyStop','reason_code':'unexpected_audio'}]


def test_unclear_backend_veto_requires_clarification_not_profile_evidence():
    from types import SimpleNamespace
    module=harness()
    result=SimpleNamespace(used=True,reply='Уточните, в какой области болит?',booked=False)
    assessment=module.assess_semantic(result,{'age':42,'profile_status':'unclear'},'тянет в туловище','UNCLEAR',raw_routes=[{'status':'PROFILE','evidence':'тянет в туловище'}])
    assert assessment['passed'] is True
    assert assessment['raw_model_route_rejected'] is True
    result.reply='Хорошо, можем записать.'
    assert module.assess_semantic(result,{'age':42,'profile_status':'unclear'},'тянет в туловище','UNCLEAR')['passed'] is False


def test_fixture_source_requires_matching_synthetic_reference(tmp_path):
    import io,wave
    module=harness()
    buffer=io.BytesIO()
    with wave.open(buffer,'wb') as target:
        target.setnchannels(1); target.setsampwidth(2); target.setframerate(24000); target.writeframes(b'\0\0'*24000)
    (tmp_path/'kk.wav').write_bytes(buffer.getvalue())
    (tmp_path/'references.json').write_text(json.dumps([{'case':'kk','reference':'known synthetic reference','source':'synthetic OpenAI TTS'}]),encoding='utf-8')
    assert module.read_fixture(tmp_path,'kk','known synthetic reference').startswith(b'RIFF')
    with pytest.raises(module.SafetyStop):
        module.read_fixture(tmp_path,'kk','different reference')


def test_vision_diagnostic_distinguishes_text_array_from_text_string():
    module=harness()
    metadata=module.describe_vision('{"kind":"report","text":["line1","line2"]}')
    assert metadata['kind']=='report'
    assert metadata['text_type']=='list'
    assert metadata['text_length']==2


def test_multiple_questions_requires_price_and_full_address_without_age_repeat():
    from types import SimpleNamespace
    module=harness()
    result=SimpleNamespace(used=True,reply='Приём 5 000 тг. Астана, проспект Туран, 59/2.',booked=False)
    assert module.assess_multiple_questions(result,{'age':42})['passed'] is True
    result.reply='Приём 5 000 тг. Сколько Вам лет?'
    assert module.assess_multiple_questions(result,{'age':42})['passed'] is False
    result.reply='Астана, Туран, 59/2. Стоимость уточните у врача.'
    assert module.assess_multiple_questions(result,{'age':42})['passed'] is False


def test_ogg_conversion_requires_explicit_existing_ffmpeg_before_creating_output(tmp_path):
    module=harness()
    with pytest.raises(module.SafetyStop):
        module.convert_ogg(None,tmp_path/'source.wav',tmp_path/'synthetic.ogg')
    assert not (tmp_path/'synthetic.ogg').exists()


def test_ogg_fixture_is_encoded_and_fully_decoded_with_real_ffmpeg(tmp_path):
    import io,wave
    module=harness()
    ffmpeg=Path('C:/Users/user/AppData/Local/Programs/Python/Python312/Lib/site-packages/imageio_ffmpeg/binaries/ffmpeg-win-x86_64-v7.1.exe')
    if not ffmpeg.is_file():
        pytest.skip('explicit local ffmpeg absent')
    source=tmp_path/'source.wav'
    with wave.open(str(source),'wb') as target:
        target.setnchannels(1); target.setsampwidth(2); target.setframerate(24000); target.writeframes(b'\0\0'*24000)
    result=module.convert_ogg(ffmpeg,source,tmp_path/'synthetic.ogg')
    assert result.startswith(b'OggS')
    assert b'OpusHead' in result[:100]
