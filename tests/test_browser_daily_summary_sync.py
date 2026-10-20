"""Browser plugin unavailable: exercise login/resume against delayed settings."""
import json
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect, sync_playwright

ROOT=Path(__file__).resolve().parents[1]
USER={'id':7,'role':'user','nickname':'Reader','email':'reader@example.test'}


@pytest.mark.parametrize('entry',['stored_login','login','resume'])
def test_digest_syncs_immediately_after_delayed_access_settings(entry):
    with sync_playwright() as pw:
        browser=pw.chromium.launch()
        context=browser.new_context(viewport={'width':390,'height':844},service_workers='block')
        if entry!='login':
            context.add_init_script("localStorage.setItem('raynews_token','mock');localStorage.setItem('raynews_user',JSON.stringify(%s));"%json.dumps(USER))
        pending=[];digest_requests=[];errors=[]
        state={'hold':entry!='resume','enabled':entry!='resume'}
        def settings():
            return {'access':{'permissions':{'daily_digest':True},'services':{},'view_daily_digest':state['enabled']},'theme_preference':'dark','notification_config':{}}
        def route(r):
            path=urlsplit(r.request.url).path
            if path in ('/','/index.html'):
                r.fulfill(body=(ROOT/'frontend/index.html').read_text(),content_type='text/html');return
            asset=ROOT/'frontend'/path.lstrip('/')
            if path.endswith(('.png','.ico')) and asset.is_file():
                r.fulfill(body=asset.read_bytes(),content_type='image/png');return
            data={}
            if path=='/auth/me':data={'user':USER}
            elif path=='/auth/login':data={'token':'mock','user':USER}
            elif path=='/settings':
                if state['hold']:
                    pending.append(r);return
                data=settings()
            elif path=='/api/news':data={'items':[],'total':0,'total_pages':1}
            elif path in ('/sources','/api/sources'):data={'sources':[],'categories':[]}
            elif path=='/notifications':data={'items':[],'unread':0}
            elif path=='/ai/daily-summary/today':
                digest_requests.append(r.request.headers.get('authorization'))
                data={'status':'completed','summary':'## 今日要闻\n一条最新总结。','updated_at':'2026-10-20 13:00:00','article_count':1}
            elif path=='/ai/result-updates':data={'items':[],'cursor':'test|0','has_more':False}
            r.fulfill(json=data)
        context.route('**/*',route)
        page=context.new_page();page.on('pageerror',lambda error:errors.append(str(error)))
        try:
            page.goto('http://raynews.test/')
            if entry=='login':
                page.locator('#loginBtn').click()
                page.locator('#authLogin').fill(USER['email'])
                page.locator('#authPassword').fill('local-test-password')
                page.locator('#authSubmitBtn').click()
            elif entry=='resume':
                page.wait_for_function('userAutoSettings?.permissions?.daily_digest && !userAutoSettings.view_daily_digest')
                state.update(hold=True,enabled=True)
                page.evaluate('lastForegroundSyncAt=0;onReturnToForeground()')
            # Keep dispatching Playwright events until the HTTP route is held;
            # a fixed sleep races token verification on loaded CI machines.
            for _ in range(500):
                if pending:
                    break
                page.wait_for_timeout(10)
            assert pending and not digest_requests
            state['hold']=False
            for response in pending:
                response.fulfill(json=settings())
            expect(page.locator('#dailySummaryBtn')).to_have_class('icon-btn daily-summary-btn ready',timeout=5000)
            assert digest_requests==['Bearer mock']
            assert page.evaluate('dailySummaryState.result.article_count')==1
            assert errors==[]
        finally:
            context.close();browser.close()
