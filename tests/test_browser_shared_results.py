"""Rendered desktop/mobile flows against the actual app shell and controlled APIs.

Browser plugin not available: use Playwright Chromium. Authentication here is
an isolated mock token, never a token accepted by a real RayNews server.
"""
import json
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[1]
USER = {'id': 7, 'role': 'user', 'nickname': 'Local QA', 'email': 'qa@example.test'}
ARTICLES = [{'id': i, 'title': f'Original article {i}', 'date': '2026-10-15', 'time': '12:00',
             'timestamp': 1792065600+i, 'source': 'Example', 'feed_source': 'Example', 'thumb': '',
             'body_html': f'<p>Readable original body {i}</p>'} for i in (1, 2)]


@pytest.fixture
def browser():
    with sync_playwright() as playwright:
        instance = playwright.chromium.launch()
        yield instance
        instance.close()


@pytest.mark.parametrize('viewport', [{'width':1280,'height':800}, {'width':390,'height':844}])
def test_body_first_live_updates_original_toggle_and_logout(browser, viewport, tmp_path):
    context = browser.new_context(viewport=viewport, service_workers='block')
    context.add_init_script('''localStorage.setItem('raynews_token','local-mock-token');
        localStorage.setItem('raynews_user',JSON.stringify(%s));
        window.qaLongTasks=[]; window.qaLayoutShifts=[];
        new PerformanceObserver(list=>window.qaLongTasks.push(...list.getEntries().map(e=>e.duration))).observe({type:'longtask',buffered:true});
        new PerformanceObserver(list=>window.qaLayoutShifts.push(...list.getEntries().filter(e=>!e.hadRecentInput).map(e=>e.value))).observe({type:'layout-shift',buffered:true});
    ''' % json.dumps(USER))
    result = {}
    revision = 0
    requests = []
    pending_baselines = []
    def route(request_route):
        nonlocal revision
        request = request_route.request
        path = urlsplit(request.url).path
        requests.append(path)
        data = {}
        if path in ('/', '/index.html'):
            request_route.fulfill(body=(ROOT/'frontend/index.html').read_text(), content_type='text/html')
            return
        if path.startswith('/api/news/') and path.rsplit('/',1)[-1].isdigit():
            article_id = int(path.rsplit('/',1)[-1])
            data = ARTICLES[article_id-1]
        elif path == '/api/news':
            data = {'items':ARTICLES,'total':2,'page':1,'size':30,'total_pages':1}
        elif path == '/auth/me':
            data = {'user':USER}
        elif path == '/settings':
            data = {'share_active':True,'share_ai_results':True,'share_view_summary':True,
                    'share_view_translation':True,'share_view_title':True}
        elif path == '/ai/result-updates':
            data = {'cursor':f'local|{revision}','has_more':False,
                    'items':[{'id':1,'version':revision}] if revision else []}
        elif path == '/ai/translation-updates':
            pending_baselines.append(request_route)
            return
        elif path.startswith('/ai/result/'):
            data = result if path.endswith('/1') else {}
        elif path in ('/sources','/api/sources'):
            data = {'sources':[],'categories':['News','Tech','Biz','Info']}
        elif path == '/notifications':
            data = {'items':[],'unread_count':0,'total':0}
        elif path.endswith('/status'):
            data = {'unread_count':0}
        elif path.startswith('/ai/daily-summary'):
            data = {'status':'scheduled'}
        elif path == '/manifest.json':
            data = {'name':'RayNews','start_url':'/'}
        elif not path.startswith(('/api/','/auth/','/ai/','/favorites','/notifications','/settings')):
            request_route.fulfill(body='',status=200)
            return
        request_route.fulfill(json=data)
    context.route('**/*', route)
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        page.goto('http://raynews.test/')
        assert 'RayNews' in page.title()
        page.wait_for_function('news.length === 2 && userAutoSettings?.share_active')
        page.locator('#list .item[data-id="1"]').click()
        expect(page.locator('#articleBody')).to_contain_text('Readable original body 1')
        expect(page.locator('#sharedResultStatus')).to_contain_text('暂无共享结果')
        assert pending_baselines, 'body must render while the compatibility baseline is still pending'
        for pending in pending_baselines:
            pending.fulfill(json={'cursor':'2026-10-15 12:00:00.000|0','items':[]})
        pending_baselines.clear()
        page.evaluate('pollAIResultUpdates(true)')
        result['summary'] = 'Summary completed while reading'
        revision = 1
        page.evaluate('pollAIResultUpdates(true)')
        expect(page.locator('#aiSummaryTop')).to_contain_text('Summary completed while reading',timeout=15000)
        expect(page.locator('#articleBody')).to_contain_text('Readable original body 1')
        result['translation'] = json.dumps({'title':'Translated title','html':'<p>Translated body one</p>'})
        revision = 2
        page.evaluate('pollAIResultUpdates(true)')
        expect(page.locator('#articleBody')).to_have_text('Translated body one')
        expect(page.locator('#articleWrap h1')).to_have_text('Translated title')
        expect(page.locator('#list .item[data-id="1"] .item-title')).to_have_text('Translated title')
        expect(page.locator('.ai-translate-btn')).to_have_text('原文')
        page.locator('.ai-translate-btn').click()
        expect(page.locator('#articleBody')).to_contain_text('Readable original body 1')
        result['translation'] = json.dumps({'title':'New translated title','html':'<p>Updated translation</p>'})
        revision = 3
        page.evaluate('pollAIResultUpdates(true)')
        expect(page.locator('#articleBody')).to_contain_text('Readable original body 1')
        page.locator('.ai-translate-btn').click()
        expect(page.locator('#articleBody')).to_have_text('Updated translation')
        page.screenshot(path=str(tmp_path/'reading.png'))
        page.evaluate('logout()')
        expect(page.locator('#aiSummaryTop')).to_have_count(0)
        expect(page.locator('#articleBody')).to_contain_text('Readable original body 1')
        assert not any(path == '/ai/chat' for path in requests)
        assert errors == []
        timings = page.evaluate("performance.getEntriesByType('measure').filter(e=>e.name.startsWith('raynews-')).map(e=>({name:e.name,duration:e.duration}))")
        assert {e['name'] for e in timings} >= {'raynews-body-visible','raynews-shared-visible'}
    finally:
        context.close()


@pytest.mark.parametrize('delayed_shared', [False, True])
def test_shared_translation_applies_when_ready_and_preserves_original_toggle(browser, delayed_shared, tmp_path):
    context = browser.new_context(viewport={'width':390,'height':844}, service_workers='block')
    context.add_init_script("localStorage.setItem('raynews_token','local-mock-token');localStorage.setItem('raynews_user',JSON.stringify(%s));" % json.dumps(USER))
    pending_shared = []
    translation = {'translation':json.dumps({'title':'中文标题','html':'<p>中文译文正文</p>'})}
    def route(request_route):
        path = urlsplit(request_route.request.url).path
        if path == '/':
            request_route.fulfill(body=(ROOT/'frontend/index.html').read_text(), content_type='text/html')
            return
        if path.endswith(('.png', '.ico')):
            asset = ROOT / 'frontend' / path.lstrip('/')
            request_route.fulfill(body=asset.read_bytes(), content_type='image/png')
            return
        if path == '/api/news':
            data = {'items':ARTICLES,'total':2,'page':1,'size':30,'total_pages':1}
        elif path == '/api/news/1':
            data = ARTICLES[0]
        elif path == '/auth/me':
            data = {'user':USER}
        elif path == '/settings':
            data = {'share_active':True,'share_view_translation':True,'share_view_title':True}
        elif path == '/ai/result/1':
            if delayed_shared:
                pending_shared.append(request_route)
                return
            data = translation
        elif path in ('/ai/result-updates','/ai/translation-updates'):
            data = {'cursor':'local|0','has_more':False,'items':[]}
        elif path in ('/sources','/api/sources'):
            data = {'sources':[],'categories':[]}
        else:
            data = {}
        request_route.fulfill(json=data)
    context.route('**/*', route)
    page = context.new_page()
    try:
        page.goto('http://raynews.test/')
        page.wait_for_function('news.length === 2 && userAutoSettings?.share_view_translation')
        page.evaluate("""() => {
          window.firstReadableArticleBody = null;
          window.firstReadableArticleTitle = null;
          const check = () => {
            const body = document.getElementById('articleBody');
            const text = body?.innerText?.trim() || '';
            if (text && text !== '加载中...') {
              window.firstReadableArticleBody = text;
              window.firstReadableArticleTitle = document.querySelector('#articleWrap h1')?.innerText;
            }
            else requestAnimationFrame(check);
          };
          requestAnimationFrame(check);
        }""")
        page.locator('#list .item[data-id="1"]').click()
        page.wait_for_function('window.firstReadableArticleBody !== null')
        if delayed_shared:
            assert page.evaluate('window.firstReadableArticleBody') == 'Readable original body 1'
            assert page.evaluate('window.firstReadableArticleTitle') == 'Original article 1'
            assert pending_shared
            for pending in pending_shared:
                pending.fulfill(json=translation)
            expect(page.locator('#articleBody')).to_have_text('中文译文正文')
            expect(page.locator('#articleWrap h1')).to_have_text('中文标题')
            expect(page.locator('#list .item[data-id="1"] .item-title')).to_have_text('中文标题')
            expect(page.locator('.ai-translate-btn')).to_have_text('原文')
            page.wait_for_timeout(400)
            page.screenshot(path=str(tmp_path/'late-translation-applied.png'))
            page.locator('.ai-translate-btn').click()
            expect(page.locator('#articleBody')).to_have_text('Readable original body 1')
        else:
            assert page.evaluate('window.firstReadableArticleBody') == '中文译文正文'
            assert page.evaluate('window.firstReadableArticleTitle') == '中文标题'
            expect(page.locator('#articleWrap h1')).to_have_text('中文标题')
            expect(page.locator('.ai-translate-btn')).to_have_text('原文')
            page.wait_for_timeout(400)
            page.screenshot(path=str(tmp_path/'translation-first-paint.png'))
            page.locator('.ai-translate-btn').click()
            expect(page.locator('#articleBody')).to_have_text('Readable original body 1')
    finally:
        context.close()


def test_refresh_shows_committed_content_before_completion(browser):
    context=browser.new_context(viewport={'width':1280,'height':800},service_workers='block')
    context.add_init_script("localStorage.setItem('raynews_token','local-mock-token');localStorage.setItem('raynews_user',JSON.stringify(%s));" % json.dumps(USER))
    committed=False
    completed=False
    new_article={**ARTICLES[0],'id':3,'title':'Newly committed article','timestamp':1792065700}
    def route(request_route):
        nonlocal committed
        path=urlsplit(request_route.request.url).path
        if path=='/':
            request_route.fulfill(body=(ROOT/'frontend/index.html').read_text(),content_type='text/html')
            return
        data={}
        if path=='/api/news':
            items=([new_article] if committed else [])+ARTICLES
            data={'items':items,'total':len(items),'page':1,'total_pages':1}
        elif path=='/auth/me':
            data={'user':USER}
        elif path=='/settings':
            data={'share_active':False}
        elif path=='/auth/refresh':
            data={'job_id':'local-refresh','status':'running'}
        elif path=='/auth/refresh/status':
            committed=True
            data={'job_id':'local-refresh','status':'completed' if completed else 'running',
                  'new_ids_so_far':[3],'new_count_so_far':1,'new_ids':[3] if completed else [],
                  'new_count':1 if completed else 0,'stage':'completed' if completed else 'fulltext_and_ingest'}
        elif path=='/ai/result-updates':
            data={'cursor':'local|0','items':[],'has_more':False}
        elif path in ('/sources','/api/sources'):
            data={'sources':[],'categories':['News','Tech','Biz','Info']}
        elif path.startswith('/ai/daily-summary'):
            data={'status':'scheduled'}
        request_route.fulfill(json=data)
    context.route('**/*',route)
    page=context.new_page()
    errors=[]
    page.on('pageerror',lambda error:errors.append(str(error)))
    try:
        page.goto('http://raynews.test/')
        expect(page.locator('#list .item')).to_have_count(2)
        page.locator('#refreshBtn').click()
        expect(page.locator('#refreshBtn')).to_be_disabled()
        expect(page.locator('#list .item[data-id="3"]')).to_be_visible(timeout=5000)
        assert completed is False
        expect(page.locator('#refreshBtn')).to_contain_text('+1')
        before=page.evaluate('window.raynewsRefreshTiming')
        assert before['first_visible_ms'] is not None
        assert before['completion_observed_ms'] is None
        completed=True
        expect(page.locator('#refreshBtn')).to_be_enabled(timeout=5000)
        observation=page.evaluate('window.raynewsRefreshTiming')
        assert observation['accepted_ms'] <= observation['first_visible_ms'] <= observation['completion_observed_ms']
        assert observation['job_id']=='local-refresh'
        assert errors==[]
    finally:
        context.close()
