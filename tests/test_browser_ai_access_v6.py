"""Browser plugin not available: exercise the real page with Playwright Chromium."""
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import sync_playwright, expect

ASSETS = Path(__file__).resolve().parents[1] / 'frontend'
HTML = (ASSETS / 'index.html').read_text().replace('{{FULL_BUILD_VERSION}}','v6.0.0-beta.1-local')


@pytest.mark.parametrize('viewport', [{'width':1280,'height':800}, {'width':390,'height':844}])
def test_user_menus_grants_and_server_results(viewport, tmp_path):
    from ai_access import SERVICES, PREFERENCES
    grants = dict.fromkeys(SERVICES, False)
    stored = dict.fromkeys(PREFERENCES, False)
    calls=[]
    user={'id':7,'role':'user','nickname':'Reader','email':'reader@example.test'}
    def access():
        return {'permissions':grants.copy(),'services':{key:key!='tts' for key in SERVICES},**{key:stored[key] and grants[value] and value!='tts' for key,value in PREFERENCES.items()}}
    def route(r):
        request=r.request;path=urlsplit(request.url).path;calls.append((request.method,path))
        data={}
        if (ASSETS/path.lstrip('/')).is_file() and path.endswith(('.png','.ico')):
            r.fulfill(body=(ASSETS/path.lstrip('/')).read_bytes(),content_type='image/png');return
        if path in ('/','/index.html'):
            r.fulfill(body=HTML,content_type='text/html');return
        if path=='/auth/me':data={'user':user}
        elif path=='/settings':
            if request.method=='PUT': stored.update(request.post_data_json)
            data={**stored,'access':access(),'theme_preference':'system','notification_config':{},'daily_summary_enabled':False,'daily_summary_inapp_enabled':True}
        elif path=='/api/news':data={'items':[{'id':1,'title':'Original headline','source':'Example','body_html':'<p>Original body</p>','timestamp':1792065600}],'total':1,'page':1,'size':30}
        elif path=='/api/news/1':data={'id':1,'title':'Original headline','source':'Example','body_html':'<p>Original body</p>'}
        elif path=='/ai/results':data={'items':{'1':{'title_translation':'完整译名','title_summary':'Short original','translated_title_summary':'简写译名'}},'access':access()}
        elif path=='/ai/result/1':data={'summary':'Server summary','translation':json.dumps({'html':'<p>Server translation</p>'}),'access':access()}
        elif path=='/ai/result-updates':data={'items':[],'cursor':'test|0','has_more':False,'access':access()}
        elif path=='/ai/translation-updates':data={'items':[],'cursor':'2026-10-19 00:00:00.000|0'}
        elif path in ('/sources','/api/sources'):data={'sources':[],'categories':[]}
        elif path=='/notifications':data={'items':[],'unread':0}
        elif path.startswith('/ai/daily-summary'):data={'status':'scheduled'}
        elif path.endswith('/status'):data={'status':'idle'}
        r.fulfill(json=data)
    with sync_playwright() as pw:
        browser=pw.chromium.launch()
        context=browser.new_context(viewport=viewport,service_workers='block')
        context.add_init_script("localStorage.setItem('raynews_token','mock');localStorage.setItem('raynews_user',JSON.stringify(%s));" % json.dumps(user))
        context.route('**/*',route)
        page=context.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)));page.on('console',lambda message:errors.append(message.text) if message.type=='error' else None)
        page.goto('http://raynews.test/')
        page.wait_for_function('userAutoSettings?.permissions && news.length===1')
        assert 'RayNews' in page.title()
        page.evaluate('openSettings()')
        expect(page.locator('#settingsBody .settings-tabs button')).to_have_text(['账号','通知','主题','AI 功能'])
        page.get_by_role('button',name='AI 功能',exact=True).click()
        expect(page.locator('#aiEndpoint')).to_have_count(0)
        page.locator('#view_summary').click()
        expect(page.locator('#settingsStatus')).to_contain_text('无权限，请联系管理员')
        expect(page.locator('#view_summary')).to_have_attribute('aria-pressed','false')
        expect(page.locator('#view_summary')).to_have_text('已关闭')
        grants['summary']=True;grants['translation']=True;grants['tts']=True
        page.evaluate('loadAIFunctions()')
        expect(page.locator('#view_summaryState')).to_have_text('已授权')
        page.locator('#view_summary').click()
        expect(page.locator('#view_summary')).to_have_attribute('aria-pressed','true')
        expect(page.locator('#view_summary')).to_have_text('已开启')
        expect(page.locator('#view_summary')).to_be_enabled()
        page.locator('#view_summary').click()
        expect(page.locator('#view_summary')).to_have_text('已关闭')
        expect(page.locator('#view_summary')).to_be_enabled()
        page.locator('#view_summary').click()
        expect(page.locator('#view_summary')).to_have_attribute('aria-pressed','true')
        page.locator('#view_tts').click()
        expect(page.locator('#settingsStatus')).to_contain_text('语音服务暂未开放')
        expect(page.locator('#view_tts')).to_have_attribute('aria-pressed','false')
        expect(page.locator('#view_tts')).to_have_text('已关闭')
        expect(page.locator('#settingsTabAi input[type=checkbox]')).to_have_count(0)
        expect(page.locator('#view_title_shorteningState')).to_have_css('color','rgb(198, 108, 122)')
        expect(page.locator('#view_summaryState')).to_have_css('color','rgb(80, 155, 114)')
        if os.environ.get('RAYNEWS_QA_SCREENSHOT_DIR'):
            page.screenshot(path=str(Path(os.environ['RAYNEWS_QA_SCREENSHOT_DIR']) / f'user-ai-{viewport["width"]}.png'))
        page.evaluate('closeAISettings()')
        page.locator('#list .item[data-id="1"]').click()
        expect(page.locator('#articleBody')).to_contain_text('Original body')
        expect(page.locator('#aiSummaryTop')).to_contain_text('Server summary')
        grants['summary']=False;stored['view_summary']=False
        page.evaluate('loadUserSettings()')
        expect(page.locator('#aiSummaryTop')).to_have_count(0)
        assert not any(path in ('/ai/chat','/ai/config','/ai/config/client','/ai/test-connection') or (method=='POST' and path.startswith('/ai/result/')) for method,path in calls)
        assert errors==[]
        browser.close()


def test_admin_grouped_menus_and_grant_controls():
    from ai_access import SERVICES,PREFERENCES
    admin={'id':1,'role':'admin','nickname':'Admin','email':'admin@example.test'}
    with sync_playwright() as pw:
        browser=pw.chromium.launch();context=browser.new_context(service_workers='block')
        context.add_init_script("localStorage.setItem('raynews_token','mock');localStorage.setItem('raynews_user',JSON.stringify(%s));" % json.dumps(admin))
        def route(r):
            path=urlsplit(r.request.url).path
            if (ASSETS/path.lstrip('/')).is_file() and path.endswith(('.png','.ico')):
                r.fulfill(body=(ASSETS/path.lstrip('/')).read_bytes(),content_type='image/png');return
            if path in ('/','/index.html'):r.fulfill(body=HTML,content_type='text/html');return
            data={}
            if path=='/auth/me':data={'user':admin}
            elif path=='/settings':data={'access':{'permissions':dict.fromkeys(SERVICES,True),'services':{key:key!='tts' for key in SERVICES},**dict.fromkeys(PREFERENCES,False)}}
            elif path=='/auth/users':data={'users':[{**admin,'ai_permissions':dict.fromkeys(SERVICES,True)},{'id':2,'role':'user','email':'u@example.test','ai_permissions':dict.fromkeys(SERVICES,False)}],'total':2}
            elif path=='/auth/pending-invitations':data={'invitations':[]}
            elif path=='/api/news':data={'items':[],'total':0}
            elif path=='/notifications':data={'items':[],'unread':0}
            elif path=='/ai/result-updates':data={'items':[],'cursor':'test|0','has_more':False}
            r.fulfill(json=data)
        context.route('**/*',route);page=context.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)));page.on('console',lambda message:errors.append(message.text) if message.type=='error' else None)
        page.goto('http://raynews.test/');page.wait_for_function('userAutoSettings?.permissions')
        page.evaluate('openAdminSettings()')
        expect(page.locator('#adminSettingsBody > .settings-tabs button')).to_have_text(['用户管理','AI 服务','订阅源','通知发布','系统维护'])
        expect(page.locator('#adminBody input[type=checkbox]')).to_have_count(10)
        expect(page.locator('#adminBody input[disabled]')).to_have_count(5)
        if os.environ.get('RAYNEWS_QA_SCREENSHOT_DIR'):
            expect(page.locator('#adminSettingsOverlay')).to_have_css('opacity','1')
            page.screenshot(path=str(Path(os.environ['RAYNEWS_QA_SCREENSHOT_DIR']) / 'admin-ai-grants.png'))
        page.get_by_role('button',name='AI 服务',exact=True).click()
        expect(page.locator('#adminTabAi > .settings-tabs button')).to_have_text(['接口配置','摘要','翻译','标题简写','每日总结','语音阅读'])
        page.get_by_role('button',name='语音阅读',exact=True).click()
        expect(page.locator('#adminTabTts')).to_contain_text('启用语音阅读服务')
        expect(page.locator('#ttsPassword')).to_have_attribute('type','password')
        assert errors==[]
        browser.close()


@pytest.mark.parametrize('viewport', [{'width':1280,'height':800}, {'width':390,'height':844}])
@pytest.mark.parametrize('original', ['Old snapshot', None])
@pytest.mark.parametrize('failure_status', [401, 403])
def test_source_title_survives_ai_toggle_and_logout(viewport, original, failure_status):
    from ai_access import SERVICES, PREFERENCES
    stored = dict.fromkeys(PREFERENCES, False)
    result_request = {'status':200}
    user = {'id':7, 'role':'user', 'nickname':'Reader', 'email':'reader@example.test'}
    article = {'id':1, 'title':'Current source headline', 'original_title':original,
               'title_source':None, 'source':'Example', 'timestamp':1792065600,
               'body_html':'<p>Current source body</p>'}

    def access():
        return {'permissions':dict.fromkeys(SERVICES, True),
                'services':{key:key!='tts' for key in SERVICES}, **stored}

    def route(r):
        path = urlsplit(r.request.url).path
        if path in ('/', '/index.html'):
            r.fulfill(body=HTML, content_type='text/html')
            return
        if (ASSETS/path.lstrip('/')).is_file() and path.endswith(('.png', '.ico')):
            r.fulfill(body=(ASSETS/path.lstrip('/')).read_bytes(), content_type='image/png')
            return
        data = {}
        if path == '/auth/me':
            data = {'user':user}
        elif path == '/settings':
            data = {**stored, 'access':access(), 'theme_preference':'system'}
        elif path == '/api/news':
            data = {'items':[article], 'total':1, 'page':1, 'size':30}
        elif path == '/api/news/1':
            data = article
        elif path == '/ai/result/1':
            if result_request['status'] != 200:
                r.fulfill(status=result_request['status'], json={'error':'Access unavailable'})
                return
            data = {'access':access()}
            if stored['view_title_translation']:
                data['title_translation'] = 'Protected translated headline'
            if stored['view_translation']:
                data['translation'] = json.dumps({'html':'<p>Protected translated body</p>'})
        elif path == '/ai/results':
            data = {'items':{'1':{'title_translation':'Protected translated headline'}}, 'access':access()}
        elif path == '/ai/result-updates':
            data = {'items':[], 'cursor':'epoch|0', 'has_more':False, 'access':access()}
        elif path == '/ai/translation-updates':
            data = {'items':[], 'cursor':'2026-10-19 00:00:00.000|0'}
        elif path in ('/sources', '/api/sources'):
            data = {'sources':[], 'categories':[]}
        elif path == '/notifications':
            data = {'items':[], 'unread':0}
        r.fulfill(json=data)

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        context = browser.new_context(viewport=viewport, service_workers='block')
        context.add_init_script("localStorage.setItem('raynews_token','mock');localStorage.setItem('raynews_user',JSON.stringify(%s));" % json.dumps(user))
        context.route('**/*', route)
        page = context.new_page()
        errors = []
        console_errors = []
        page.on('pageerror', lambda error:errors.append(str(error)))
        page.on('console', lambda message:console_errors.append((message.text, message.location)) if message.type=='error' else None)
        try:
            page.goto('http://raynews.test/')
            assert page.url == 'http://raynews.test/'
            assert 'RayNews' in page.title()
            page.wait_for_function('userAutoSettings?.permissions && news.length===1')
            expect(page.locator('#list .item-title')).to_have_text('Current source headline')
            page.locator('#list .item[data-id="1"]').click()
            expect(page.locator('#overlay')).to_have_css('transform', 'none')
            expect(page.locator('#articleWrap h1')).to_have_text('Current source headline')
            expect(page.locator('#articleBody')).to_contain_text('Current source body')
            for _ in range(2):
                stored.update(view_title_translation=True, view_translation=True)
                page.evaluate('loadUserSettings()')
                expect(page.locator('#articleWrap h1')).to_have_text('Protected translated headline')
                expect(page.locator('#articleBody')).to_have_text('Protected translated body')
                if _ == 0:
                    stored.update(view_title_translation=False, view_translation=False)
                    page.evaluate('loadUserSettings()')
                else:
                    result_request['status'] = failure_status
                    page.evaluate('autoDisplaySummary(1)')
                    expect(page.locator('#articleWrap h1')).to_have_text('Current source headline')
                    expect(page.locator('#articleBody')).to_contain_text('Current source body')
                    assert not page.evaluate('userAutoSettings.view_title_translation || userAutoSettings.view_translation')
                    result_request['status'] = 200
                    page.evaluate('loadUserSettings()')
                    expect(page.locator('#articleWrap h1')).to_have_text('Protected translated headline')
                    if original is None:
                        # Deep-linked article metadata can leave the memory cache
                        # before logout; its saved source title must remain usable.
                        page.evaluate('articleBodyCache={}; news=[]; searchItems=[]; sourceArticlesState.items=[]')
                    page.evaluate('logout()')
                expect(page.locator('#articleWrap h1')).to_have_text('Current source headline')
                expect(page.locator('#articleBody')).to_contain_text('Current source body')
            if original is not None:
                expect(page.locator('#list .item-title')).to_have_text('Current source headline')
            if os.environ.get('RAYNEWS_QA_SCREENSHOT_DIR'):
                page.screenshot(path=str(Path(os.environ['RAYNEWS_QA_SCREENSHOT_DIR']) / f'source-title-{viewport["width"]}-{original is None}.png'))
            assert errors == []
            assert all(
                text.startswith(f'Failed to load resource: the server responded with a status of {failure_status} ')
                and urlsplit(location.get('url', '')).path == '/ai/result/1'
                for text, location in console_errors
            )
        finally:
            context.close()
            browser.close()
