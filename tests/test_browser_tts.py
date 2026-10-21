"""Browser plugin not available: real Chromium playback with isolated mock APIs."""
import io
import json
import os
import wave
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect, sync_playwright

ROOT=Path(__file__).resolve().parents[1]
ARTICLES=[{'id':i,'title':title,'source':'科技观察','feed_source':'科技观察','thumb':'',
           'date':'2026-10-20','time':'09:30','timestamp':1792488600+i,
           'body_html':'<p>城市交通正在发生变化。新的技术为市民出行带来更多选择。</p>'*6}
          for i,title in [(1,'城市里的无人驾驶，驶入日常生活'),(2,'第二篇新闻：让城市出行更加便捷')]]


def sample_audio():
    output=io.BytesIO()
    with wave.open(output,'wb') as wav:
        wav.setnchannels(1);wav.setsampwidth(2);wav.setframerate(8000)
        wav.writeframes(b'\x00\x00'*8000*60)
    return output.getvalue()


@pytest.fixture
def browser():
    with sync_playwright() as pw:
        browser=pw.chromium.launch()
        yield browser
        browser.close()


def setup_page(browser,viewport,theme='dark',role='user',failure=False,articles=None,ai_result=None,preferences=None):
    context=browser.new_context(viewport=viewport,service_workers='block',color_scheme=theme)
    user={'id':7,'role':role,'nickname':'Reader','email':'reader@example.test'}
    context.add_init_script("localStorage.setItem('raynews_token','mock');localStorage.setItem('raynews_user',JSON.stringify(%s));localStorage.setItem('raynews_theme','%s');"%(json.dumps(user),theme))
    article_items=articles or ARTICLES
    enabled={'tts':True,'summary':True,**(preferences or {})};calls=[];control={'ready':True,'busy_for_2':False}
    control['ai_result']=ai_result or {'summary':'无人驾驶从试点逐渐走向日常应用。'}
    control['tts_config']={'enabled':False,'endpoint':'','model':'kokoro',
                           'voice':'zf_001(0.5)+zf_022(0.5)','lang_code':'z','speed':1.3,
                           'response_format':'mp3','stream':False,'volume_multiplier':None,
                           'return_download_link':False,'download_format':None,
                           'normalization_options':None,'auth_type':'basic','username':'',
                           'has_password':False,'has_authorization':False,'available':False}
    def access():
        return {'permissions':{key:True for key in ('summary','translation','title_shortening','daily_digest','tts')},
                'services':{'tts':True},'view_tts':enabled['tts'],'view_summary':enabled['summary'],
                'view_translation':enabled.get('translation',False),'view_title_translation':enabled.get('title_translation',False),
                'view_title_shortening':enabled.get('shorten',False)}
    def route(r):
        path=urlsplit(r.request.url).path;calls.append((r.request.method,path))
        if path in ('/','/index.html'):
            r.fulfill(body=(ROOT/'frontend/index.html').read_text(),content_type='text/html');return
        asset=ROOT/'frontend'/path.lstrip('/')
        if path.endswith(('.png','.ico')) and asset.is_file():
            r.fulfill(body=asset.read_bytes(),content_type='image/png');return
        data={}
        if path=='/auth/me':data={'user':user}
        elif path=='/settings':data={'access':access(),'theme_preference':theme,'notification_config':{}}
        elif path=='/api/news':data={'items':article_items,'total':2,'page':1,'size':30,'total_pages':1}
        elif path.startswith('/api/news/') and path.rsplit('/',1)[-1].isdigit():data=article_items[int(path.rsplit('/',1)[-1])-1]
        elif path.startswith('/ai/tts/audio/'):
            assert r.request.headers.get('authorization')=='Bearer mock'
            r.fulfill(body=sample_audio(),content_type='audio/wav');return
        elif path.startswith('/ai/tts/jobs/'):
            if control.get('expired'):
                r.fulfill(status=410,json={'code':'TTS_EXPIRED','error':'文章或译文已更新，请重新生成'});return
            data={'id':'job-1','status':'failed' if control.get('job_failed') else 'ready' if control['ready'] else 'generating','has_summary':True}
            if control.get('job_failed'):data['error']='语音生成超时，请重试'
        elif path.startswith('/ai/tts/'):
            if failure:
                r.fulfill(status=502,json={'code':'TTS_UPSTREAM_ERROR','error':'语音服务暂时不可用，请稍后重试'});return
            if path=='/ai/tts/2' and control['busy_for_2']:
                r.fulfill(status=429,json={'code':'TTS_BUSY','error':'已有语音正在生成，请稍后再试'});return
            control['expired']=False
            article=dict(article_items[int(path.rsplit('/',1)[-1])-1])
            if control.get('tts_title'):article['title']=control['tts_title']
            data={'id':'job-'+str(article['id']),'status':'ready' if control['ready'] else 'queued','article':article,'has_summary':True}
            data.update(control.get('reading_selection',{}))
            if control.get('stream'):data['stream_url']='/ai/tts/stream/'+data['id']
        elif path.startswith('/ai/result/'):
            data={**control['ai_result'],'access':access()}
        elif path=='/ai/results':data={'items':{str(item['id']):control['ai_result'] for item in article_items},'access':access()}
        elif path=='/ai/result-updates':data={'items':[],'cursor':'test|0','has_more':False,'access':access()}
        elif path in ('/sources','/api/sources'):data={'sources':[],'categories':[]}
        elif path=='/notifications':data={'items':[],'unread':0}
        elif path=='/admin/tts-config':
            if r.request.method=='PUT':
                control['saved_tts']=r.request.post_data_json
                control['tts_config'].update({key:value for key,value in control['saved_tts'].items()
                                              if key not in ('password','authorization','clear_password','clear_authorization')})
            data=control['tts_config']
        elif path=='/admin/tts-test':r.fulfill(body=sample_audio(),content_type='audio/wav');return
        r.fulfill(json=data)
    context.route('**/*',route)
    page=context.new_page();errors=[]
    page.on('pageerror',lambda error:errors.append(str(error)))
    control['console_logs']=[]
    page.on('console',lambda message:control['console_logs'].append(message.text) if message.type in {'error','warning'} else None)
    page.goto('http://raynews.test/')
    page.wait_for_function('news.length===2 && userAutoSettings?.view_tts')
    return context,page,enabled,calls,errors,control


@pytest.mark.parametrize('viewport,theme', [({'width':390,'height':844},'dark'),({'width':390,'height':844},'light'),({'width':1280,'height':900},'dark')])
def test_playback_controls_navigation_queue_and_logout(browser,viewport,theme,tmp_path):
    context,page,enabled,calls,errors,_=setup_page(browser,viewport,theme)
    try:
        assert page.title()=='RayNews'
        page.locator('#list .item[data-id="1"]').click()
        expect(page.locator('#articleBody')).to_contain_text('城市交通')
        actions=page.locator('#aiActions button')
        expect(actions.nth(0)).to_have_attribute('aria-label','语音阅读')
        expect(actions.nth(1)).to_have_text('摘要')
        actions.nth(0).click()
        page.wait_for_function('document.getElementById("ttsAudio").src.startsWith("blob:")')
        expect(page.locator('#ttsDisc')).to_be_visible()
        page.locator('#ttsDisc').click()
        expect(page.get_by_role('dialog',name=ARTICLES[0]['title'])).to_be_visible()
        expect(page.locator('#ttsDuration')).to_have_text('01:00')
        expect(page.locator('#ttsPrevious')).to_be_disabled()
        expect(page.locator('#ttsNext')).to_be_enabled()
        if page.evaluate('document.getElementById("ttsAudio").paused'):page.locator('#ttsPlay').click()
        expect(page.locator('#ttsPlay')).to_have_attribute('aria-label','暂停')
        page.locator('#ttsPlay').click()
        expect(page.locator('#ttsPlay')).to_have_attribute('aria-label','播放')
        page.locator('#ttsSeek').fill('500')
        assert 29 <= page.evaluate('document.getElementById("ttsAudio").currentTime') <= 31
        page.locator('#ttsSpeed').select_option('1.2')
        assert page.evaluate('document.getElementById("ttsAudio").playbackRate')==1.2
        page.locator('#ttsPlay').click()
        expect(page.locator('#ttsPlay')).to_have_attribute('aria-label','暂停')
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        panel=page.locator('.tts-panel').bounding_box()
        assert panel['x']>=0 and panel['y']>=0 and panel['y']+panel['height']<=viewport['height']+1
        screenshot_dir=Path(os.environ.get('RAYNEWS_QA_SCREENSHOT_DIR',str(tmp_path)))
        screenshot_dir.mkdir(parents=True,exist_ok=True)
        page.screenshot(path=str(screenshot_dir/f'tts-player-{viewport["width"]}-{theme}.png'))
        page.locator('#ttsCollapse').click()
        expect(page.locator('#ttsDisc')).to_have_class('tts-disc playing')
        expect(page.locator('#overlay')).to_have_css('transform','none')
        page.screenshot(path=str(screenshot_dir/f'tts-disc-{viewport["width"]}-{theme}.png'))
        page.locator('#backBtn').click()
        expect(page.locator('#overlay')).not_to_have_class('overlay open')
        assert not page.evaluate('document.getElementById("ttsAudio").paused')
        page.locator('#list .item[data-id="2"]').click()
        assert page.locator('#ttsDisc').get_attribute('title').endswith(ARTICLES[0]['title'])
        page.locator('#ttsDisc').click()
        page.locator('#ttsNext').click()
        expect(page.locator('#ttsTitle')).to_have_text(ARTICLES[1]['title'])
        expect(page.locator('#ttsNext')).to_be_disabled()
        page.wait_for_function('document.getElementById("ttsAudio").src.startsWith("blob:")')
        page.evaluate('logout()')
        expect(page.locator('#ttsDisc')).to_be_hidden()
        expect(page.locator('#ttsOverlay')).to_be_hidden()
        assert not page.locator('#ttsAudio').get_attribute('src')
        assert len([p for method,p in calls if method=='POST' and p.startswith('/ai/tts/')])==2
        assert errors==[]
    finally:context.close()


def test_error_retry_and_revoked_summary(browser):
    context,page,enabled,calls,errors,_=setup_page(browser,{'width':390,'height':844},failure=True)
    try:
        page.locator('#list .item[data-id="1"]').click()
        page.locator('.tts-speaker').click()
        expect(page.locator('#ttsDisc')).to_be_visible()
        page.locator('#ttsDisc').click()
        expect(page.locator('#ttsStatus')).to_contain_text('语音服务暂时不可用')
        expect(page.locator('#ttsRetry')).to_be_visible()
        expect(page.locator('#ttsPlay')).to_be_disabled()
        expect(page.locator('#ttsDisc')).not_to_have_class('tts-disc busy')
        page.locator('#ttsRetry').click()
        expect(page.locator('#ttsStatus')).to_contain_text('语音服务暂时不可用')
        enabled['tts']=False
        page.evaluate('loadUserSettings()')
        expect(page.locator('#ttsOverlay')).to_be_hidden()
        expect(page.locator('#ttsDisc')).to_be_hidden()
        assert errors==[]
    finally:context.close()


def test_admin_config_defaults_and_secret_fields(browser):
    context,page,_,calls,errors,control=setup_page(browser,{'width':390,'height':844},role='admin')
    try:
        page.evaluate('openAdminSettings();switchAdminTab("ai");switchAdminAiTab("tts")')
        expect(page.locator('#ttsEndpoint')).to_have_value('')
        expect(page.locator('#ttsUsername')).to_have_value('')
        expect(page.locator('#ttsLang')).to_have_value('z')
        expect(page.locator('#ttsVoice')).to_have_value('zf_001(0.5)+zf_022(0.5)')
        expect(page.locator('#ttsResponseFormat')).to_have_value('MP3')
        expect(page.locator('#ttsResponseFormat')).to_have_attribute('readonly','')
        expect(page.locator('#ttsPassword')).to_have_value('')
        expect(page.locator('#ttsPassword')).to_have_attribute('placeholder','尚未配置')
        expect(page.locator('#ttsCustomAuth')).to_be_hidden()
        page.locator('#ttsAuthType').select_option('authorization')
        expect(page.locator('#ttsCustomAuth')).to_be_visible()
        expect(page.locator('#ttsPassword')).to_be_hidden()
        page.locator('#ttsAuthType').select_option('basic')
        page.locator('#ttsSynthesisSpeed').fill('3.5')
        page.locator('#ttsVolumeMultiplier').fill('0.8')
        page.locator('#ttsLang').fill('')
        page.locator('#ttsStream').check()
        page.locator('#ttsReturnDownloadLink').check()
        expect(page.locator('#ttsDownloadFormat')).to_have_value('MP3')
        expect(page.locator('#ttsDownloadFormat')).to_have_attribute('readonly','')
        page.locator('.tts-normalization summary').click()
        page.locator('#ttsNormalizationDefault').uncheck()
        page.locator('#ttsUnitNormalization').check()
        page.locator('#ttsUrlNormalization').uncheck()
        page.locator('#ttsSaveConfig').click()
        expect(page.locator('#ttsConfigStatus')).to_have_text('语音配置已保存。')
        assert control['saved_tts']['voice']=='zf_001(0.5)+zf_022(0.5)'
        assert control['saved_tts']['response_format']=='mp3'
        assert control['saved_tts']['speed']==3.5
        assert control['saved_tts']['lang_code'] is None
        assert control['saved_tts']['stream'] is True
        assert control['saved_tts']['volume_multiplier']==0.8
        assert control['saved_tts']['download_format']=='mp3'
        assert control['saved_tts']['normalization_options']['unit_normalization'] is True
        assert control['saved_tts']['normalization_options']['url_normalization'] is False
        expect(page.locator('#ttsResponseFormat')).to_have_value('MP3')
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        assert errors==[]
        assert control['console_logs']==[]
    finally:context.close()


def test_queued_generation_then_summary_revocation(browser):
    context,page,enabled,calls,errors,control=setup_page(browser,{'width':390,'height':844})
    try:
        control['ready']=False
        page.locator('#list .item[data-id="1"]').click()
        page.locator('.tts-speaker').click()
        expect(page.locator('#ttsDisc')).to_have_class('tts-disc busy')
        page.locator('#ttsDisc').click()
        expect(page.locator('#ttsStatus')).to_contain_text('正在整理文章并生成语音')
        expect(page.locator('#ttsPlay')).to_be_disabled()
        control['ready']=True
        page.wait_for_function('document.getElementById("ttsAudio").src.startsWith("blob:")')
        expect(page.locator('#ttsPlay')).to_be_enabled()
        enabled['summary']=False
        page.evaluate('loadUserSettings()')
        expect(page.locator('#ttsOverlay')).to_be_hidden()
        expect(page.locator('#ttsDisc')).to_be_hidden()
        assert not page.locator('#ttsAudio').get_attribute('src')
        assert errors==[]
    finally:context.close()


@pytest.mark.parametrize('stop_first',[False,True])
def test_switch_during_generation_waits_for_server_slot(browser,stop_first):
    context,page,_,calls,errors,control=setup_page(browser,{'width':390,'height':844})
    try:
        control['ready']=False
        control['busy_for_2']=True
        page.evaluate('void RayNewsTTS.start(1)')
        expect(page.locator('#ttsDisc')).to_have_class('tts-disc busy')
        page.locator('#ttsDisc').click()
        if stop_first:
            page.locator('#ttsStop').click()
            expect(page.locator('#ttsDisc')).to_be_hidden()
            page.evaluate('void RayNewsTTS.start(2)')
            page.locator('#ttsDisc').click()
        else:
            page.locator('#ttsNext').click()
        expect(page.locator('#ttsStatus')).to_contain_text('空出后会自动开始当前文章')
        expect(page.locator('#ttsRetry')).to_be_hidden()
        control['busy_for_2']=False
        control['ready']=True
        page.wait_for_function('document.getElementById("ttsAudio").src.startsWith("blob:")',timeout=10000)
        expect(page.locator('#ttsTitle')).to_have_text(ARTICLES[1]['title'])
        assert sum(method=='POST' and path=='/ai/tts/2' for method,path in calls)>=2
        assert errors==[]
    finally:context.close()


def enable_stream(page, control):
    control.update(ready=False, stream=True)
    page.evaluate("""() => {
      const original=window.fetch;
      window.streamProbe={requests:0,cancelled:0,authorization:''};
      window.fetch=(url,options)=>{
        if (!String(url).startsWith('/ai/tts/stream/')) return original(url,options);
        streamProbe.requests++;streamProbe.authorization=options.headers.Authorization;
        return Promise.resolve(new Response(new ReadableStream({
          start(controller){
            streamProbe.push=seconds=>{
              // Valid silent MPEG-1 Layer III frames: 128 kbit/s, 44.1 kHz mono.
              const frames=Math.ceil(seconds*44100/1152),bytes=new Uint8Array(frames*417);
              for(let i=0;i<frames;i++)bytes.set([0xff,0xfb,0x90,0xc0],i*417);
              controller.enqueue(bytes);
            };
            streamProbe.close=()=>controller.close();
            options.signal.addEventListener('abort',()=>{
              streamProbe.cancelled++;
              try{controller.error(new DOMException('Aborted','AbortError'));}catch(_){}
            },{once:true});
          },
          cancel(){streamProbe.cancelled++;}
        }),{headers:{'Content-Type':'audio/mpeg'}}));
      };
    }""")


@pytest.mark.parametrize('width',[390,1280])
def test_stream_starts_after_five_seconds_before_eof(browser,width,tmp_path):
    context,page,_,calls,errors,control=setup_page(browser,{'width':width,'height':900})
    try:
        enable_stream(page,control)
        page.locator('#list .item[data-id="1"]').click()
        page.locator('.tts-speaker').click()
        page.locator('#ttsDisc').click()
        page.wait_for_function('streamProbe.requests===1')
        expect(page.locator('#ttsStatus')).to_contain_text('约 5 秒音频就绪')
        assert page.evaluate('streamProbe.authorization')=='Bearer mock'
        page.evaluate('streamProbe.push(2)')
        page.wait_for_function('document.getElementById("ttsAudio").buffered.length>0')
        assert page.evaluate('document.getElementById("ttsAudio").paused')
        expect(page.locator('#ttsPlay')).to_be_disabled()
        page.evaluate('streamProbe.push(4)')
        expect(page.locator('#ttsPlay')).to_have_attribute('aria-label','暂停')
        expect(page.locator('#ttsStatus')).to_contain_text('边生成边播放')
        expect(page.locator('#ttsSeek')).to_be_disabled()
        expect(page.locator('#ttsDuration')).to_have_text('--:--')
        assert control['ready'] is False
        assert not any('/ai/tts/audio/' in path for _,path in calls)
        screenshot_dir=Path(os.environ.get('RAYNEWS_QA_SCREENSHOT_DIR',str(tmp_path)))
        screenshot_dir.mkdir(parents=True,exist_ok=True)
        page.screenshot(path=str(screenshot_dir/f'tts-stream-{width}.png'),animations='disabled')
        page.locator('#ttsPlay').click()
        expect(page.locator('#ttsPlay')).to_have_attribute('aria-label','播放')
        page.evaluate('streamProbe.push(2)')
        expect(page.locator('#ttsPlay')).to_have_attribute('aria-label','播放')
        control['ready']=True
        page.evaluate('streamProbe.close()')
        expect(page.locator('#ttsStatus')).not_to_contain_text('边生成边播放')
        expect(page.locator('#ttsSeek')).to_be_enabled()
        expect(page.locator('#ttsPlay')).to_have_attribute('aria-label','播放')
        page.locator('#ttsSeek').fill('500')
        assert 3.5 <= page.evaluate('document.getElementById("ttsAudio").currentTime') <= 4.5
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        assert errors==[]
        assert control['console_logs']==[]
    finally:context.close()


@pytest.mark.parametrize('outcome',['short','failed','stop','switch','logout'])
def test_stream_completion_failure_and_cancellation(browser,outcome):
    context,page,_,calls,errors,control=setup_page(browser,{'width':390,'height':844})
    try:
        enable_stream(page,control)
        page.locator('#list .item[data-id="1"]').click()
        page.locator('.tts-speaker').click()
        page.locator('#ttsDisc').click()
        page.wait_for_function('streamProbe.requests===1')
        page.evaluate('streamProbe.push(1)')
        page.wait_for_function('document.getElementById("ttsAudio").buffered.length>0')
        if outcome=='short':
            control['ready']=True
            page.evaluate('streamProbe.close()')
            expect(page.locator('#ttsPlay')).to_have_attribute('aria-label','暂停')
            expect(page.locator('#ttsSeek')).to_be_enabled()
        elif outcome=='failed':
            control['job_failed']=True
            page.evaluate('streamProbe.close()')
            expect(page.locator('#ttsStatus')).to_contain_text('语音生成超时')
            expect(page.locator('#ttsRetry')).to_be_visible()
            expect(page.locator('#ttsPlay')).to_be_disabled()
            assert not page.locator('#ttsAudio').get_attribute('src')
        else:
            if outcome=='stop':page.locator('#ttsStop').click()
            elif outcome=='logout':page.evaluate('logout()')
            else:
                control['ready']=True
                page.locator('#ttsNext').click()
                expect(page.locator('#ttsTitle')).to_have_text(ARTICLES[1]['title'])
                expect(page.locator('#ttsPlay')).to_be_enabled()
            page.wait_for_function('streamProbe.cancelled>0')
            if outcome!='switch':
                expect(page.locator('#ttsDisc')).to_be_hidden()
                assert not page.locator('#ttsAudio').get_attribute('src')
        assert errors==[]
    finally:context.close()


def test_stream_unsupported_browser_uses_complete_audio(browser):
    context,page,_,calls,errors,control=setup_page(browser,{'width':390,'height':844})
    try:
        control.update(ready=False,stream=True)
        page.evaluate('window.MediaSource=undefined')
        page.locator('#list .item[data-id="1"]').click()
        page.locator('.tts-speaker').click()
        page.locator('#ttsDisc').click()
        expect(page.locator('#ttsPlay')).to_be_disabled()
        control['ready']=True
        expect(page.locator('#ttsPlay')).to_be_enabled()
        assert any('/ai/tts/audio/' in path for _,path in calls)
        assert not any('/ai/tts/stream/' in path for _,path in calls)
        assert errors==[]
    finally:context.close()


def test_stream_recovers_audio_quota_and_restores_seek_history(browser):
    context,page,_,calls,errors,control=setup_page(browser,{'width':390,'height':844})
    try:
        enable_stream(page,control)
        page.locator('#list .item[data-id="1"]').click()
        page.locator('.tts-speaker').click()
        page.locator('#ttsDisc').click()
        page.wait_for_function('streamProbe.requests===1')
        page.evaluate('streamProbe.push(8)')
        expect(page.locator('#ttsPlay')).to_have_attribute('aria-label','暂停')
        page.locator('#ttsPlay').click()
        page.evaluate("""() => {
          document.getElementById('ttsAudio').currentTime=5;
          const append=SourceBuffer.prototype.appendBuffer;
          let first=true;
          SourceBuffer.prototype.appendBuffer=function(bytes){
            if(first){first=false;throw new DOMException('Audio buffer full','QuotaExceededError');}
            return append.call(this,bytes);
          };
          streamProbe.push(2);
        }""")
        page.wait_for_function('document.getElementById("ttsAudio").buffered.start(0)>0')
        control['ready']=True
        page.evaluate('streamProbe.close()')
        expect(page.locator('#ttsSeek')).to_be_enabled()
        expect(page.locator('#ttsDuration')).to_have_text('01:00')
        assert 4.5 <= page.evaluate('document.getElementById("ttsAudio").currentTime') <= 5.5
        expect(page.locator('#ttsPlay')).to_have_attribute('aria-label','播放')
        page.locator('#ttsSeek').fill('0')
        assert page.evaluate('document.getElementById("ttsAudio").currentTime')==0
        assert any('/ai/tts/audio/' in path for _,path in calls)
        assert errors==[]
    finally:context.close()


TRANSLATED_ARTICLES=[{**ARTICLES[0],
    'title':'A very long original English headline describing modern city transportation technology',
    'body_html':'<p>The original English article describes transportation technology.</p>'},ARTICLES[1]]
TRANSLATED_RESULT={
    'summary':'城市交通技术带来了新的变化。',
    'title_summary':'Short original headline',
    'title_translation':'关于现代城市交通技术发展与影响的一篇完整中文译名',
    'translated_title_summary':'城市交通技术新进展',
    'translation':json.dumps({'html':'<p>这是已经翻译完成的中文正文，包含 1.3% 的增长数据。</p>'}),
}


def translated_page(browser,viewport,theme='dark'):
    result=setup_page(browser,viewport,theme,articles=TRANSLATED_ARTICLES,ai_result=TRANSLATED_RESULT,
                      preferences={'translation':True,'title_translation':True,'shorten':True})
    control=result[-1]
    control['tts_title']=TRANSLATED_RESULT['translated_title_summary']
    control['reading_selection']={'has_translation':True,'title_kind':'translated_title_summary',
        'required_preferences':['view_translation','view_title_translation','view_title_shortening']}
    return result


@pytest.mark.parametrize('width,theme',[(390,'dark'),(390,'light'),(1280,'dark')])
def test_tts_title_matches_translated_page_and_action_buttons_are_uniform(browser,width,theme,tmp_path):
    context,page,_,calls,errors,control=translated_page(browser,{'width':width,'height':844},theme)
    try:
        assert page.title()=='RayNews'
        page.locator('#list .item[data-id="1"]').click()
        heading=page.locator('#articleWrap > h1')
        expect(heading).to_have_text(TRANSLATED_RESULT['translated_title_summary'])
        expect(page.locator('#articleBody')).to_contain_text('已经翻译完成的中文正文')
        expect(page.locator('#articleBody')).not_to_contain_text('original English article')
        buttons=page.locator('#aiActions button')
        sizes=[button.bounding_box() for button in buttons.all()]
        assert len(sizes)==3
        assert max(size['height'] for size in sizes)-min(size['height'] for size in sizes)<=1
        assert max(size['width'] for size in sizes)-min(size['width'] for size in sizes)<=1
        assert all(size['height']==24 for size in sizes)
        styles=buttons.evaluate_all("nodes=>nodes.map(node=>{const s=getComputedStyle(node);return [s.color,s.backgroundColor,s.borderRadius,s.padding].join('|')})")
        assert len(set(styles))==1
        assert page.locator('.tts-speaker svg').bounding_box()['height']==14
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        screenshot_dir=Path(os.environ.get('RAYNEWS_QA_SCREENSHOT_DIR',str(tmp_path)))
        screenshot_dir.mkdir(parents=True,exist_ok=True)
        page.screenshot(path=str(screenshot_dir/f'tts-actions-{width}-{theme}.png'),animations='disabled')
        buttons.nth(0).click()
        page.wait_for_function('document.getElementById("ttsAudio").src.startsWith("blob:")')
        page.locator('#ttsDisc').click()
        expect(page.locator('#ttsTitle')).to_have_text(heading.inner_text())
        expect(page.locator('#ttsSource')).to_contain_text('译文朗读')
        expect(page.locator('#ttsPlay')).to_be_enabled()
        assert errors==[] and control['console_logs']==[]
    finally:context.close()


@pytest.mark.parametrize('preference',['translation','title_translation','shorten'])
def test_selected_translation_and_title_preferences_stop_old_audio(browser,preference):
    context,page,enabled,_,errors,_=translated_page(browser,{'width':390,'height':844})
    try:
        page.locator('#list .item[data-id="1"]').click()
        page.locator('.tts-speaker').click()
        page.wait_for_function('document.getElementById("ttsAudio").src.startsWith("blob:")')
        enabled[preference]=False
        page.evaluate('loadUserSettings()')
        expect(page.locator('#ttsDisc')).to_be_hidden()
        expect(page.locator('#ttsOverlay')).to_be_hidden()
        assert not page.locator('#ttsAudio').get_attribute('src')
        assert errors==[]
    finally:context.close()


def test_clicking_current_article_replaces_expired_audio_after_content_changes(browser):
    context,page,_,calls,errors,control=translated_page(browser,{'width':390,'height':844})
    try:
        page.locator('#list .item[data-id="1"]').click()
        page.locator('.tts-speaker').click()
        page.wait_for_function('document.getElementById("ttsAudio").src.startsWith("blob:")')
        old_url=page.locator('#ttsAudio').get_attribute('src')
        control['ai_result']={**control['ai_result'],'translated_title_summary':'更新后的交通标题'}
        control['tts_title']='更新后的交通标题'
        page.evaluate('autoDisplaySummary(1)')
        expect(page.locator('#articleWrap > h1')).to_have_text('更新后的交通标题')
        control['expired']=True
        page.locator('.tts-speaker').click()
        page.wait_for_function('old=>document.getElementById("ttsAudio").src.startsWith("blob:") && document.getElementById("ttsAudio").src!==old',arg=old_url)
        expect(page.locator('#ttsTitle')).to_have_text('更新后的交通标题')
        assert sum(method=='POST' and path=='/ai/tts/1' for method,path in calls)==2
        assert errors==[]
    finally:context.close()


@pytest.mark.parametrize('paused',[False,True])
def test_translation_arriving_during_playback_replaces_original_and_preserves_pause(browser,paused):
    initial={key:value for key,value in TRANSLATED_RESULT.items() if key!='translation'}
    context,page,_,calls,errors,control=setup_page(browser,{'width':390,'height':844},
        articles=TRANSLATED_ARTICLES,ai_result=initial,
        preferences={'translation':True,'title_translation':True,'shorten':True})
    try:
        control['tts_title']=initial['translated_title_summary']
        control['reading_selection']={'has_translation':False,'title_kind':'translated_title_summary',
            'required_preferences':['view_title_translation','view_title_shortening']}
        page.locator('#list .item[data-id="1"]').click()
        expect(page.locator('#articleBody')).to_contain_text('original English article')
        page.locator('.tts-speaker').click()
        page.wait_for_function('document.getElementById("ttsAudio").src.startsWith("blob:")')
        page.locator('#ttsDisc').click()
        if page.evaluate('document.getElementById("ttsAudio").paused'):page.locator('#ttsPlay').click()
        expect(page.locator('#ttsPlay')).to_have_attribute('aria-label','暂停')
        if paused:page.locator('#ttsPlay').click()
        old_url=page.locator('#ttsAudio').get_attribute('src')
        control['ai_result']={**TRANSLATED_RESULT}
        control['reading_selection']={'has_translation':True,'title_kind':'translated_title_summary',
            'required_preferences':['view_translation','view_title_translation','view_title_shortening']}
        control['expired']=True
        page.evaluate('autoDisplaySummary(1)')
        expect(page.locator('#articleBody')).to_contain_text('已经翻译完成的中文正文')
        page.wait_for_function('old=>document.getElementById("ttsAudio").src.startsWith("blob:") && document.getElementById("ttsAudio").src!==old',arg=old_url)
        expect(page.locator('#ttsSource')).to_contain_text('译文朗读')
        expect(page.locator('#ttsPlay')).to_have_attribute('aria-label','播放' if paused else '暂停')
        assert sum(method=='POST' and path=='/ai/tts/1' for method,path in calls)==2
        assert errors==[]
    finally:context.close()


@pytest.mark.parametrize('paused',[False,True])
def test_translation_refresh_preserves_pause_with_streaming_audio(browser,paused):
    initial={key:value for key,value in TRANSLATED_RESULT.items() if key!='translation'}
    context,page,_,calls,errors,control=setup_page(browser,{'width':390,'height':844},
        articles=TRANSLATED_ARTICLES,ai_result=initial,
        preferences={'translation':True,'title_translation':True,'shorten':True})
    try:
        control['tts_title']=initial['translated_title_summary']
        page.locator('#list .item[data-id="1"]').click()
        page.locator('.tts-speaker').click()
        page.wait_for_function('document.getElementById("ttsAudio").src.startsWith("blob:")')
        page.locator('#ttsDisc').click()
        if page.evaluate('document.getElementById("ttsAudio").paused'):page.locator('#ttsPlay').click()
        if paused:page.locator('#ttsPlay').click()
        enable_stream(page,control)
        control['ai_result']={**TRANSLATED_RESULT}
        control['reading_selection']={'has_translation':True,'title_kind':'translated_title_summary',
            'required_preferences':['view_translation','view_title_translation','view_title_shortening']}
        control['expired']=True
        page.evaluate('autoDisplaySummary(1)')
        page.wait_for_function('streamProbe.requests===1')
        page.evaluate('streamProbe.push(6)')
        expect(page.locator('#ttsPlay')).to_be_enabled()
        expect(page.locator('#ttsPlay')).to_have_attribute('aria-label','播放' if paused else '暂停')
        expect(page.locator('#ttsSource')).to_contain_text('译文朗读')
        control['ready']=True
        page.evaluate('streamProbe.close()')
        expect(page.locator('#ttsSeek')).to_be_enabled()
        expect(page.locator('#ttsPlay')).to_have_attribute('aria-label','播放' if paused else '暂停')
        assert sum(method=='POST' and path=='/ai/tts/1' for method,path in calls)==2
        assert errors==[]
    finally:context.close()
