"""Bounded opt-in OpenAI tests on synthetic media; no production imports by default."""
from __future__ import annotations
import argparse
import asyncio
from contextlib import ExitStack
import io
import json
import logging
import os
from pathlib import Path
import random
import re
import sys
import tempfile
import wave
from unittest.mock import patch

class SafetyStop(RuntimeError):
    pass

class RequestGuard:
    def __init__(self, *, live, max_calls=20, max_usd=1):
        if not 1 <= max_calls <= 20 or not 0 < max_usd <= 1:
            raise ValueError('invalid_budget')
        self.live, self.max_calls, self.max_usd = live, max_calls, max_usd
        self.calls, self.reserved_usd = 0, 0.0

    def authorize(self, request):
        url = request.url
        if (not self.live or url.scheme != 'https' or url.host != 'api.openai.com' or
            url.port not in (None,443) or request.method != 'POST' or url.path not in {
            '/v1/audio/speech','/v1/audio/transcriptions','/v1/chat/completions'}):
            raise SafetyStop('forbidden_transport')
        if url.path == '/v1/audio/speech':
            reserve = .04
        elif url.path == '/v1/audio/transcriptions':
            reserve = .015
        else:
            payload = json.loads(request.content)
            if payload.get('model') == 'gpt-5.6-terra':
                if not 1 <= payload.get('max_completion_tokens',0) <= 2000:
                    raise SafetyStop('unbounded_output')
                reserve = max(.12, len(request.content)*2/1000000 + .024)
            elif payload.get('model') == 'gpt-4o-mini':
                reserve = .02
            else:
                raise SafetyStop('unquoted_model')
        if self.calls >= self.max_calls or self.reserved_usd+reserve > self.max_usd+1e-9:
            raise SafetyStop('budget_exhausted')
        self.calls += 1
        self.reserved_usd = round(self.reserved_usd+reserve,8)


def configure_isolation(directory):
    os.environ.update(SQLITE_PATH=str(Path(directory)/'synthetic.sqlite3'),
        CRM_WRITE_ENABLED='false', CRM_BOT_SECRET='', WAZZUP_API_KEY='',
        WAZZUP_CHANNEL_ID='', CRM_BASE_URL='https://crm.invalid', WAZZUP_API_URL='https://wazzup.invalid',
        PUBLIC_BASE_URL='https://acceptance.invalid', MEDICAL_DOCUMENT_PROCESSING_ENABLED='true',
        AI_BRAIN_MODEL='gpt-5.6-terra', AI_BRAIN_MAX_COMPLETION_TOKENS='2000',
        OPENAI_VOICE_MODEL='gpt-transcribe', OPENAI_DOCUMENT_MODEL='gpt-4o-mini',
        AI_ENABLED='true', OPENAI_BRAIN_ENABLED='true', MONTHLY_AI_BUDGET_USD='1',
        AI_MAX_CLASSIFIER_CALLS_PER_DAY='30')


def install_transport_fence(stack, guard):
    import httpx
    original = httpx.AsyncClient.send
    async def send(client,request,*args,**kwargs):
        await request.aread()
        guard.authorize(request)
        kwargs['follow_redirects'] = False
        return await original(client,request,*args,**kwargs)
    def forbidden(*args,**kwargs):
        raise SafetyStop('sync_transport_forbidden')
    stack.enter_context(patch.object(httpx.AsyncClient,'send',send))
    stack.enter_context(patch.object(httpx.Client,'send',forbidden))


def normalize_wav(data):
    # Streaming WAV headers can advertise UINT32_MAX frames. Bound the actual
    # payload, decode only bytes present, then write truthful RIFF frame counts.
    if len(data)>4_000_000:
        raise SafetyStop('unexpected_audio')
    with wave.open(io.BytesIO(data),'rb') as source:
        params=source.getparams()
        if params.sampwidth!=2 or params.nchannels not in (1,2) or not 8000<=params.framerate<=48000:
            raise SafetyStop('unexpected_audio')
        frames=source.readframes(20*params.framerate+1)
        frame_size=params.sampwidth*params.nchannels
        if not frames or len(frames)%frame_size or len(frames)/frame_size/params.framerate>20:
            raise SafetyStop('unexpected_audio')
    out=io.BytesIO()
    with wave.open(out,'wb') as target:
        target.setparams(params._replace(nframes=len(frames)//frame_size))
        target.writeframes(frames)
    return out.getvalue()


def noisy_wav(data):
    import array
    with wave.open(io.BytesIO(normalize_wav(data)),'rb') as source:
        params=source.getparams()
        samples=array.array('h',source.readframes(source.getnframes()))
    rng = random.Random(241)
    for i,value in enumerate(samples):
        samples[i] = max(-32768,min(32767,value+rng.randint(-350,350)))
    out = io.BytesIO()
    with wave.open(out,'wb') as target:
        target.setparams(params)
        target.writeframes(samples.tobytes())
    return out.getvalue()


def create_documents(output):
    from PIL import Image,ImageDraw,ImageFont
    fontfile = Path(os.environ.get('WINDIR','C:/Windows'))/'Fonts'/'arial.ttf'
    font = ImageFont.truetype(str(fontfile),30) if fontfile.exists() else ImageFont.load_default(size=30)
    pages = []
    for text in ['SYNTHETIC TEST ONLY - PHYSICIAN REPORT\nMRI lumbar spine. Age: 42.\nConclusion: L4-L5 disc protrusion, 3 mm.\nNo fracture. Signed: Dr Synthetic.',
                 'SYNTHETIC TEST ONLY - PHYSICIAN REPORT\nPage 2: follow-up in 14 days.\nNo surgical recommendation.\nSigned: Dr Synthetic.']:
        image = Image.new('RGB',(1000,650),'white')
        ImageDraw.Draw(image).multiline_text((40,60),text,fill='black',font=font,spacing=20)
        pages.append(image)
    pages[0].save(output/'report.png')
    pages[0].save(output/'report.pdf',save_all=True,append_images=pages[1:],resolution=120)
    Image.new('RGB',(500,500),'gray').save(output/'unreadable.png')


def matches(text,groups):
    text = text.casefold().replace('ё','е')
    return all(any(variant in text for variant in group) for group in groups)


def assess_semantic(result,session,text,expected,raw_routes=None):
    saved=session.get('semantic_route_evidence') or {}
    route=saved.get('interpretation') or {}
    evidence=bool(route.get('evidence') and route['evidence'] in text and saved.get('source')==route['evidence'])
    repeat=bool(re.search(r'сколько.{0,20}лет|ваш.{0,8}возраст|жасыңыз.{0,12}(қанша|нешеде)',result.reply,re.I))
    raw_route=(raw_routes or [route])[-1] or {}
    raw_rejected=bool(raw_route.get('status') and raw_route.get('status').lower()!=session.get('profile_status'))
    clarifies=bool('?' in result.reply and re.search(r'уточн|где|како[йея].{0,15}(част|област|мест)|что.{0,15}(беспоко|именно)',result.reply,re.I))
    route_match=session.get('profile_status')==expected.lower()
    interpretation_ok=(evidence and route.get('status')==expected) if expected!='UNCLEAR' else clarifies
    age_retained=session.get('age')==42
    return {'passed':bool(result.used and result.reply and interpretation_ok and route_match and age_retained and not repeat and not result.booked),
        'literal_evidence_valid':evidence,'route_status_matches':route_match,'expected_status':expected,
        'raw_model_route_rejected':raw_rejected,'appropriate_clarifier':clarifies,'actual_status':raw_route.get('status'),'stored_profile_status':session.get('profile_status'),
        'known_age_retained':age_retained,'repeated_known_age':repeat,'real_model_used':result.used}


async def execute_case(report,guard,selected,name,operation):
    if selected and name not in selected:
        return
    before=guard.calls
    try:
        details=await operation()
        report['cases'].append({'case':name,'calls':guard.calls-before,**details})
    except Exception as exc:
        record={'case':name,'passed':False,'calls':guard.calls-before,'error_type':type(exc).__name__}
        allowed={'unexpected_audio','forbidden_transport','sync_transport_forbidden','unbounded_output',
            'unquoted_model','budget_exhausted','crm_wazzup_forbidden','nonfixture_media',
            'ffmpeg_or_fixture_missing','ogg_encoding_failed','ogg_decoding_failed','synthetic_fixture_source_required',
            'document_processing_disabled','physician_report_required','document_unreadable','document_budget_exhausted'}
        if str(exc) in allowed:
            record['reason_code']=str(exc)
        report['cases'].append(record)


def convert_ogg(ffmpeg,source,target):
    import subprocess
    if ffmpeg is None or not Path(ffmpeg).is_file() or not Path(source).is_file():
        raise SafetyStop('ffmpeg_or_fixture_missing')
    executable=str(Path(ffmpeg).resolve())
    source=Path(source).resolve()
    target=Path(target).resolve()
    if source.suffix.lower()!='.wav' or target.suffix.lower()!='.ogg' or source==target:
        raise SafetyStop('nonfixture_media')
    # Fixed local-only conversion; no shell interpolation or media network access.
    common=[executable,'-v','error','-nostdin','-protocol_whitelist','file,pipe']
    encoded=subprocess.run(common+['-i',str(source),'-c:a','libopus','-y',str(target)],
        stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,timeout=30,check=False)
    if encoded.returncode!=0:
        raise SafetyStop('ogg_encoding_failed')
    decoded=subprocess.run(common+['-i',str(target),'-f','null','-'],
        stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,timeout=30,check=False)
    if decoded.returncode!=0:
        raise SafetyStop('ogg_decoding_failed')
    data=target.read_bytes()
    if not data.startswith(b'OggS') or b'OpusHead' not in data[:100]:
        raise SafetyStop('unexpected_audio')
    return data


def assess_multiple_questions(result,session):
    text=result.reply.casefold()
    price=bool(re.search(r'(?<![0-9])5[\s\u00a0\u202f]*000(?![0-9])',text))
    address=bool('астан' in text and ('туран' in text or 'тұран' in text) and '59/2' in text)
    repeat=bool(re.search(r'сколько.{0,20}лет|ваш.{0,8}возраст|жасыңыз.{0,12}(қанша|нешеде)',text,re.I))
    return {'passed':bool(result.used and price and address and session.get('age')==42 and not repeat and not result.booked),
        'confirmed_price_answered':price,'confirmed_full_address_answered':address,
        'known_age_retained':session.get('age')==42,'repeated_known_age':repeat,'real_model_used':result.used}


def read_fixture(source,language,reference):
    source=Path(source).resolve()
    if language not in {'ru','kk','mixed'}:
        raise SafetyStop('nonfixture_media')
    manifest=source/'references.json'
    if manifest.stat().st_size>100000:
        raise SafetyStop('nonfixture_media')
    entries=json.loads(manifest.read_text(encoding='utf-8'))
    if not any(item.get('case')==language and item.get('reference')==reference and item.get('source')=='synthetic OpenAI TTS' for item in entries):
        raise SafetyStop('nonfixture_media')
    path=source/(language+'.wav')
    if path.stat().st_size>4000000:
        raise SafetyStop('unexpected_audio')
    return normalize_wav(path.read_bytes())


def describe_vision(content):
    try:
        parsed=json.loads(content)
    except (ValueError,TypeError):
        return {'json_valid':False}
    if not isinstance(parsed,dict):
        return {'json_valid':True,'root_type':type(parsed).__name__}
    value=parsed.get('text')
    return {'json_valid':True,'kind':parsed.get('kind'),'keys':sorted(parsed),
        'text_type':type(value).__name__,'text_length':len(value) if isinstance(value,(str,list,dict)) else None}


async def run_live(output,guard,report,selected=None,fixture_source=None,ffmpeg=None):
    import openai
    import config
    config.get_settings.cache_clear()
    import state
    state.init_db()
    import agent,crm,wazzup,media,voice,ai
    def forbidden(*args,**kwargs):
        raise SafetyStop('crm_wazzup_forbidden')
    async def empty(*args,**kwargs):
        return {'ok':True,'doctors':[],'slots':[],'appointments':[]}
    async def case(name,operation):
        await execute_case(report,guard,selected,name,operation)
    with ExitStack() as stack:
        stack.enter_context(patch.object(crm,'_client',forbidden))
        stack.enter_context(patch.object(wazzup,'_client',forbidden))
        for name in ['get_doctors','check_slots','lookup_active_appointments_by_phone']:
            stack.enter_context(patch.object(crm,name,empty))
        for name in ['book_appointment','cancel_appointment','reschedule_appointment']:
            stack.enter_context(patch.object(crm,name,forbidden))
        clients=[]
        def agent_client(key):
            client = openai.AsyncOpenAI(api_key=key,max_retries=0,timeout=45)
            clients.append(client)
            return client
        stack.enter_context(patch.object(ai,'_openai_client',agent_client))
        client = agent_client(config.get_settings().openai_api_key)
        try:
            fixtures=[('ru','Мне сорок два года. У меня болит поясница.',[('сорок два','42'),('поясниц',)]),
                ('kk','Мен қырық екі жастамын. Менің белім ауырады.',[('қырық екі','42'),('бел',)]),
                ('mixed','Здравствуйте, мен қырық екі жастамын, болит поясница.',[('қырық екі','сорок два','42'),('поясниц',)])]
            references=[]
            for language,text,terms in fixtures:
                async def speech(language=language,text=text,terms=terms):
                    if fixture_source:
                        data=read_fixture(fixture_source,language,text)
                    else:
                        response=await client.audio.speech.create(model='gpt-4o-mini-tts',voice='alloy',input=text,response_format='wav')
                        data=normalize_wav(response.content)
                    (output/f'{language}.wav').write_bytes(data)
                    references.append({'case':language,'reference':text,'source':'synthetic OpenAI TTS'})
                    transcript=await voice.transcribe_bytes(data,filename=f'{language}.wav')
                    (output/f'{language}_transcript.txt').write_text(transcript.text,encoding='utf-8')
                    return {'passed':matches(transcript.text,terms),'synthetic_transcript_file':f'{language}_transcript.txt','fixture_reused':bool(fixture_source)}
                await case('voice_'+language,speech)
            (output/'references.json').write_text(json.dumps(references,ensure_ascii=False,indent=2),encoding='utf-8')
            async def noise():
                clean=(output/'ru.wav').read_bytes() if (output/'ru.wav').exists() else read_fixture(fixture_source,'ru',fixtures[0][1])
                data=noisy_wav(clean)
                (output/'ru_noisy.wav').write_bytes(data)
                result=await voice.transcribe_bytes(data,filename='ru_noisy.wav')
                return {'passed':matches(result.text,[('сорок два','42'),('поясниц',)])}
            await case('voice_ru_synthetic_noise',noise)
            async def ogg_voice():
                if fixture_source is None:
                    raise SafetyStop('synthetic_fixture_source_required')
                data=read_fixture(fixture_source,'ru',fixtures[0][1])
                source=output/'ogg_source.wav'
                source.write_bytes(data)
                encoded=convert_ogg(ffmpeg,source,output/'synthetic.ogg')
                transcript=await voice.transcribe_bytes(encoded,filename='synthetic.ogg')
                (output/'ogg_transcript.txt').write_text(transcript.text,encoding='utf-8')
                return {'passed':matches(transcript.text,[('сорок два','42'),('поясниц',)]),
                    'opus_decoded':True,'synthetic_transcript_file':'ogg_transcript.txt','fixture_reused':True}
            await case('voice_ogg',ogg_voice)
            create_documents(output)
            for filename,terms in [('report.png',[('no fracture',),('42',),('l4-l5',),('3 mm',)]),
                ('report.pdf',[('no fracture',),('42',),('l4-l5',),('3 mm',),('14',),('no surgical',)]),('unreadable.png',None)]:
                async def document(filename=filename,terms=terms):
                    async def download(*,media_url=None,file_id=None):
                        if media_url!='synthetic:'+filename or file_id:
                            raise SafetyStop('nonfixture_media')
                        return (output/filename).read_bytes(),'application/octet-stream'
                    vision_client=agent_client(config.get_settings().openai_api_key)
                    original_create=vision_client.chat.completions.create
                    async def observed_create(**kwargs):
                        response=await original_create(**kwargs)
                        content=response.choices[0].message.content or ''
                        diagnostic={'case':filename,'structure':describe_vision(content),'synthetic_fixture_response':content}
                        (output/(filename+'.vision.json')).write_text(json.dumps(diagnostic,ensure_ascii=False,indent=2),encoding='utf-8')
                        return response
                    with patch.object(media,'download_media',download),patch.object(media,'AsyncOpenAI',lambda **kwargs:vision_client),patch.object(vision_client.chat.completions,'create',observed_create):
                        try:
                            result=await media.extract_document({'media_url':'synthetic:'+filename})
                        except media.DocumentNeedsReview:
                            if terms is None:
                                return {'passed':True,'refused':True}
                            raise
                    return {'passed':terms is not None and matches(result,terms)}
                await case('document_'+filename,document)
            for language,text in [
                ('ru','Мне 42. Когда долго сижу, в нижней части туловища словно тянет струну, особенно при наклоне.'),
                ('kk','Мен 42 жастамын. Ұзақ отырсам, бел тұсым тартылып, еңкейгенде мазам кетеді.'),
                ('mixed','Мне 42, белім ұзақ отырғанда ауырады, хочу понять к кому обращаться.')]:
                async def semantic(language=language,text=text):
                    session={'language':'kk' if language=='kk' else 'ru','age':42,'patient_age':42,'step':'complaint'}
                    raw_routes=[]
                    original_record=agent._tool_record_patient_facts
                    def record(*args,**kwargs):
                        facts=args[2] if len(args)>2 else kwargs.get('args',{})
                        if isinstance(facts.get('semantic_route'),dict):
                            raw_routes.append(facts['semantic_route'])
                        return original_record(*args,**kwargs)
                    with patch.object(agent,'_tool_record_patient_facts',record):
                        result=await agent.run_agent_turn(chat_id='synthetic-'+language,phone='',session=session,user_text=text)
                    (output/('semantic_'+language+'_reply.txt')).write_text(result.reply,encoding='utf-8')
                    assessment=assess_semantic(result,session,text,'UNCLEAR' if language=='ru' else 'PROFILE',raw_routes=raw_routes)
                    assessment['tool_names']=[x.get('tool') for x in result.tool_calls]
                    return assessment
                await case('semantic_'+language,semantic)
            async def multiple_questions():
                session={'language':'ru','age':42,'patient_age':42,'complaint':'болит поясница',
                    'profile_status':'profile','step':'complaint'}
                result=await agent.run_agent_turn(chat_id='synthetic-multiple',phone='',session=session,
                    user_text='Мне 42, болит поясница. Сколько стоит первичный приём и где находится клиника?')
                (output/'multiple_questions_reply.txt').write_text(result.reply,encoding='utf-8')
                assessment=assess_multiple_questions(result,session)
                assessment['tool_names']=[x.get('tool') for x in result.tool_calls]
                return assessment
            await case('multiple_questions',multiple_questions)
        finally:
            for item in clients:
                await item.close()


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live',action='store_true')
    parser.add_argument('--only',nargs='+',choices=['voice_ru','voice_kk','voice_mixed','voice_ru_synthetic_noise','document_report.png','document_report.pdf','document_unreadable.png','semantic_ru','semantic_kk','semantic_mixed','voice_ogg','multiple_questions'])
    parser.add_argument('--output',required=True,type=Path)
    parser.add_argument('--fixture-source',type=Path)
    parser.add_argument('--ffmpeg',type=Path)
    parser.add_argument('--voice-model',choices=['whisper-1','gpt-transcribe'],default='gpt-transcribe')
    parser.add_argument('--max-calls',type=int,default=20)
    parser.add_argument('--max-usd',type=float,default=1)
    args=parser.parse_args(argv)
    guard=RequestGuard(live=args.live,max_calls=args.max_calls,max_usd=args.max_usd)
    output=args.output.resolve()
    output.mkdir(parents=True,exist_ok=True)
    report={'mode':'live' if args.live else 'dry_run','cases':[],'production_transport_blocked':True,'selected_cases':args.only or 'all',
        'limitations':['Synthetic speech; native-speaker accuracy not established.',
        'No patient/provider roundtrip or real CRM writes.',
        'Reserved USD is a conservative projection, not a settled invoice.',
        'Unreadable synthetic image does not establish clinical scan recognition.']}
    try:
        if args.live:
            if any(name in sys.modules for name in ['config','agent','state']):
                raise SafetyStop('fresh_child_process_required')
            logging.disable(logging.CRITICAL)
            sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
            with tempfile.TemporaryDirectory(prefix='neuro2-synthetic-') as temp:
                configure_isolation(temp)
                os.environ['OPENAI_VOICE_MODEL']=args.voice_model
                report['voice_model']=args.voice_model
                with ExitStack() as stack:
                    install_transport_fence(stack,guard)
                    try:
                        asyncio.run(run_live(output,guard,report,set(args.only or []),args.fixture_source,args.ffmpeg))
                    finally:
                        # sqlite3 connection context managers commit but close only
                        # on collection; release before Windows directory cleanup.
                        import gc
                        gc.collect()
    except Exception as exc:
        report['fatal_error_type']=type(exc).__name__
    report.update(calls=guard.calls,reserved_usd=guard.reserved_usd)
    report['all_executed_passed']=bool(report['cases']) and all(x['passed'] for x in report['cases'])
    (output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'mode':report['mode'],'calls':guard.calls,'reserved_usd':guard.reserved_usd,
        'passed':sum(x['passed'] for x in report['cases']),'cases':len(report['cases'])}))
    return 0 if not args.live or (report['all_executed_passed'] and 'fatal_error_type' not in report) else 1

if __name__=='__main__':
    raise SystemExit(main())
