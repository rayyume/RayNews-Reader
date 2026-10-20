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


def setup_page(browser,viewport,theme='dark',role='user',failure=False):
    context=browser.new_context(viewport=viewport,service_workers='block',color_scheme=theme)
    user={'id':7,'role':role,'nickname':'Reader','email':'reader@example.test'}
    context.add_init_script("localStorage.setItem('raynews_token','mock');localStorage.setItem('raynews_user',JSON.stringify(%s));localStorage.setItem('raynews_theme','%s');"%(json.dumps(user),theme))
    enabled={'tts':True,'summary':True};calls=[];control={'ready':True,'busy_for_2':False}
    def access():
        return {'permissions':{key:True for key in ('summary','translation','title_shortening','daily_digest','tts')},
                'services':{'tts':True},'view_tts':enabled['tts'],'view_summary':enabled['summary'],
                'view_translation':False,'view_title_translation':False}
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
        elif path=='/api/news':data={'items':ARTICLES,'total':2,'page':1,'size':30,'total_pages':1}
        elif path.startswith('/api/news/') and path.rsplit('/',1)[-1].isdigit():data=ARTICLES[int(path.rsplit('/',1)[-1])-1]
        elif path.startswith('/ai/tts/audio/'):
            assert r.request.headers.get('authorization')=='Bearer mock'
            r.fulfill(body=sample_audio(),content_type='audio/wav');return
        elif path.startswith('/ai/tts/jobs/'):
            data={'id':'job-1','status':'ready' if control['ready'] else 'generating','has_summary':True}
        elif path.startswith('/ai/tts/'):
            if failure:
                r.fulfill(status=502,json={'code':'TTS_UPSTREAM_ERROR','error':'语音服务暂时不可用，请稍后重试'});return
            if path=='/ai/tts/2' and control['busy_for_2']:
                r.fulfill(status=429,json={'code':'TTS_BUSY','error':'已有语音正在生成，请稍后再试'});return
            article=ARTICLES[int(path.rsplit('/',1)[-1])-1]
            data={'id':'job-'+str(article['id']),'status':'ready' if control['ready'] else 'queued','article':article,'has_summary':True}
        elif path.startswith('/ai/result/'):
            data={'summary':'无人驾驶从试点逐渐走向日常应用。','access':access()}
        elif path=='/ai/results':data={'items':{},'access':access()}
        elif path=='/ai/result-updates':data={'items':[],'cursor':'test|0','has_more':False,'access':access()}
        elif path in ('/sources','/api/sources'):data={'sources':[],'categories':[]}
        elif path=='/notifications':data={'items':[],'unread':0}
        elif path=='/admin/tts-config':data={'enabled':False,'endpoint':'','model':'kokoro','voice':'zf_094','lang_code':'z','speed':1.3,'auth_type':'basic','username':'','has_password':False,'has_authorization':False,'available':False}
        elif path=='/admin/tts-test':r.fulfill(body=sample_audio(),content_type='audio/wav');return
        r.fulfill(json=data)
    context.route('**/*',route)
    page=context.new_page();errors=[]
    page.on('pageerror',lambda error:errors.append(str(error)))
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
    context,page,_,calls,errors,_=setup_page(browser,{'width':390,'height':844},role='admin')
    try:
        page.evaluate('openAdminSettings();switchAdminTab("ai");switchAdminAiTab("tts")')
        expect(page.locator('#ttsEndpoint')).to_have_value('')
        expect(page.locator('#ttsUsername')).to_have_value('')
        expect(page.locator('#ttsLang')).to_have_value('z')
        expect(page.locator('#ttsPassword')).to_have_value('')
        expect(page.locator('#ttsPassword')).to_have_attribute('placeholder','尚未配置')
        expect(page.locator('#ttsCustomAuth')).to_be_hidden()
        page.locator('#ttsAuthType').select_option('authorization')
        expect(page.locator('#ttsCustomAuth')).to_be_visible()
        expect(page.locator('#ttsPassword')).to_be_hidden()
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        assert errors==[]
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
