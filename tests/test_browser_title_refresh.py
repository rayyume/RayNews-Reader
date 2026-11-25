"""Browser plugin not available: verify pagination with real Chromium DOM updates."""
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
USER = {'id': 7, 'role': 'user', 'nickname': 'Reader', 'email': 'reader@example.test'}


@pytest.fixture
def title_page():
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        context = browser.new_context(service_workers='block')
        context.add_init_script("localStorage.setItem('raynews_token','mock');localStorage.setItem('raynews_user',JSON.stringify(%s));" % json.dumps(USER))
        control = {'pending': [], 'hold_first': False, 'revision': 0, 'titles': {}, 'requests': [], 'enabled': True}
        articles = [{'id': i, 'title': f'Original headline {i}', 'source': 'Example',
                     'timestamp': 1792065600-i, 'date': '2026-10-15', 'time': '12:00',
                     'body_html': f'<p>Original body {i}</p>'} for i in range(1, 91)]

        def access():
            return {'permissions': dict.fromkeys(('summary', 'translation', 'title_shortening', 'daily_digest', 'tts'), True),
                    'services': {}, 'ai_active': True, 'view_title_translation': control['enabled'],
                    'view_summary': False, 'view_translation': False, 'view_title_shortening': False,
                    'view_daily_digest': False}

        def titles(ids):
            return {'items': {str(i): {'title_translation': control['titles'].get(i, f'翻译后的新闻标题 {i}')} for i in ids}, 'access': access()}

        def route(r):
            url = urlsplit(r.request.url)
            params = parse_qs(url.query)
            path = url.path
            data = {}
            if path == '/':
                r.fulfill(body=(ROOT/'frontend/index.html').read_text(), content_type='text/html')
                return
            if path == '/auth/me':
                data = {'user': USER}
            elif path == '/settings':
                data = {'access': access()}
            elif path == '/api/news':
                p = int(params.get('page', ['1'])[0])
                data = {'items': articles[(p-1)*30:p*30], 'page': p, 'size': 30, 'total': 90}
            elif path.startswith('/api/news/') and path.rsplit('/', 1)[-1].isdigit():
                data = articles[int(path.rsplit('/', 1)[-1])-1]
            elif path == '/ai/results':
                assert params.get('fields') == ['titles']
                ids = list(map(int, params['ids'][0].split(',')))
                control['requests'].append(ids)
                if control['hold_first'] or any(i > 30 for i in ids):
                    control['pending'].append((r, ids))
                    return
                data = titles(ids)
            elif path.startswith('/ai/result/'):
                data = titles([int(path.rsplit('/', 1)[-1])])['items'][path.rsplit('/', 1)[-1]]
            elif path == '/ai/result-updates':
                data = {'items': [{'id': 32}] if control['revision'] else [],
                        'cursor': f'epoch|{control["revision"]}', 'has_more': False, 'access': access()}
            elif path == '/ai/translation-updates':
                data = {'items': [], 'cursor': '2026-10-15 12:00:00.000|0'}
            elif path in ('/sources', '/api/sources'):
                data = {'sources': [], 'categories': ['News', 'Tech'],
                        'category_names': {'News': '政经', 'Tech': '科技'}}
            elif path.endswith(('.png', '.ico')):
                asset = ROOT/'frontend'/path.lstrip('/')
                r.fulfill(body=asset.read_bytes(), content_type='image/png')
                return
            r.fulfill(json=data)

        context.route('**/*', route)
        page = context.new_page()
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('console', lambda message: errors.append(message.text) if message.type == 'error' else None)

        def release(predicate=lambda ids: True):
            for r, ids in control['pending'][:]:
                if predicate(ids):
                    r.fulfill(json=titles(ids))
                    control['pending'].remove((r, ids))

        yield page, control, release
        assert errors == []
        context.close()
        browser.close()


@pytest.mark.parametrize('width,height', [(1280, 800), (390, 844)])
def test_page_first_paint_uses_titles_and_open_return_keeps_cards(title_page, width, height, tmp_path):
    page, control, release = title_page
    page.set_viewport_size({'width': width, 'height': height})
    page.goto('http://raynews.test/')
    assert 'RayNews' in page.title()
    expect(page.locator('#list .item-title').first).to_have_text('翻译后的新闻标题 1')
    page.wait_for_function('titleReadsInFlight.has(31)')
    page.evaluate('''() => {
      window.firstPageTwoTitles = null;
      new MutationObserver(() => {
        if (!window.firstPageTwoTitles && document.querySelector('#list .item')?.dataset.id === '31')
          window.firstPageTwoTitles = [...document.querySelectorAll('#list .item-title')].map(el => el.textContent);
      }).observe(document.getElementById('list'), {childList:true, subtree:true});
    }''')
    page.get_by_role('button', name='下一页', exact=True).click()
    page.wait_for_function('pageNavigationPending')
    assert page.evaluate('currentPage') == 1
    release(lambda ids: 31 in ids)
    page.wait_for_function('currentPage === 2 && !pageNavigationPending')
    assert page.evaluate('window.firstPageTwoTitles') == [f'翻译后的新闻标题 {i}' for i in range(31, 61)]
    assert sum(31 in ids for ids in control['requests']) == 1
    page.evaluate('window.cardsBefore = [...document.querySelectorAll("#list .item")]; window.titleBefore = document.querySelector("#list .item-title")')
    page.locator('#list .item[data-id="31"]').click()
    expect(page.locator('#articleBody')).to_contain_text('Original body 31')
    page.evaluate('closeArticle()')
    page.wait_for_function('!document.getElementById("overlay").classList.contains("open")')
    page.evaluate('loadUserSettings()')
    assert page.evaluate('window.cardsBefore.every((node, i) => node === document.querySelectorAll("#list .item")[i])')
    assert page.evaluate('window.titleBefore === document.querySelector("#list .item-title")')

    control['titles'][32] = '更新后的新闻标题 32'
    control['revision'] = 1
    page.evaluate('void pollAIResultUpdates(true)')
    page.wait_for_function('pendingTitleReads.has(32) && titleReadsInFlight.has(32)')
    expect(page.locator('#list .item[data-id="32"] .item-title')).to_have_text('翻译后的新闻标题 32')
    release(lambda ids: 32 in ids)
    expect(page.locator('#list .item[data-id="32"] .item-title')).to_have_text('更新后的新闻标题 32')
    assert page.evaluate('window.cardsBefore.every((node, i) => node === document.querySelectorAll("#list .item")[i])')
    page.screenshot(path=str(tmp_path/f'translated-list-{width}.png'))


def test_late_page_revalidation_warms_cache_without_replacing_visible_cards(title_page, tmp_path):
    page, control, release = title_page
    control['enabled'] = False
    page.set_viewport_size({'width': 390, 'height': 844})
    page.goto('http://raynews.test/')
    page.wait_for_function("pageMemoryBuffer.has(newsPageCacheKey(2, 'all'))")
    delayed = []

    def hold_page_two(route):
        if parse_qs(urlsplit(route.request.url).query).get('page') == ['2']:
            delayed.append(route)
        else:
            route.fallback()

    page.route('**/api/news?*', hold_page_two)
    page.get_by_role('button', name='下一页', exact=True).click()
    expect(page.locator('#list .item').first).to_have_attribute('data-id', '31')
    page.wait_for_function("currentPage === 2 && !pageNavigationPending")
    assert len(delayed) == 1
    page.evaluate("window.beforeLateResponse = document.querySelector('#list .item')")
    delayed[0].fulfill(json={'page': 2, 'total': 90, 'items': [
        {'id': 991, 'title': '新的缓存页面', 'source': 'Example', 'timestamp': 1792065800}
    ]})
    page.wait_for_function("pageMemoryBuffer.get(newsPageCacheKey(2, 'all')).data.items[0].id === 991")
    assert page.evaluate("window.beforeLateResponse === document.querySelector('#list .item')")
    page.evaluate("pendingNewItems = [{id: 991}]; pendingNewArticleCount = 1; lastUserActivityAt = 0")
    page.evaluate("showLatestAfterIdle()")
    assert page.evaluate("currentPage") == 2
    expect(page.locator('#list .item').first).to_have_attribute('data-id', '31')
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.screenshot(path=str(tmp_path / 'stable-page-2.png'))


def install_swipe_probe(page):
    page.evaluate("""() => {
      window.categorySweeps = [];
      document.getElementById('topCatBar').addEventListener('animationstart', event => {
        if (event.animationName === 'category-sweep') categorySweeps.push(event.target.dataset.f);
      });
      window.swipeCategory = (x, endX, y, endY, selector = '#list .item') => {
        const target = document.querySelector(selector);
        return [['touchstart', x, y], ['touchmove', endX, endY], ['touchend', endX, endY]].map(([type, clientX, clientY]) => {
          const point = new Touch({target, identifier:7, clientX, clientY});
          const handled = !target.dispatchEvent(new TouchEvent(type, {bubbles:true, cancelable:true,
            touches:type === 'touchend' ? [] : [point], changedTouches:[point]}));
          if (type === 'touchmove') window.listDrag = document.getElementById('list').style.transform;
          return handled;
        });
      };
    }""")


@pytest.mark.parametrize('standalone', [False, True])
def test_home_swipe_switches_categories_and_reserves_browser_edges(title_page, tmp_path, standalone):
    page, control, _ = title_page
    control['enabled'] = False
    page.set_viewport_size({'width': 430, 'height': 932})
    page.add_init_script(f"Object.defineProperty(navigator, 'standalone', {{value: {str(standalone).lower()}}})")
    page.goto('http://raynews.test/')
    expect(page.locator('#list .item').first).to_be_visible()
    page.wait_for_function("CATEGORY_ORDER[0] === 'News'")
    initial_length = page.evaluate('history.length')
    assert page.evaluate("document.documentElement.classList.contains('home-gestures')") == standalone
    if standalone:
        assert page.evaluate("getComputedStyle(document.querySelector('.header')).backdropFilter") == 'none'
        assert page.evaluate("getComputedStyle(document.querySelector('.header')).backgroundColor") == page.evaluate(
            "getComputedStyle(document.documentElement).backgroundColor"
        )
    install_swipe_probe(page)
    assert page.evaluate('swipeCategory(425, 385, 250, 258)') == [False, standalone, standalone]
    if not standalone:
        assert page.evaluate('swipeCategory(5, 320, 250, 248)') == [False] * 3
        expect(page.locator('#topCatBar .active')).to_have_text('All')
        page.evaluate("swipeCategory(325, 285, 250, 258, 'body')")
    assert page.evaluate("listDrag.startsWith('translateX(-')")
    expect(page.locator('#topCatBar .active')).to_have_text('政经')
    page.wait_for_function("categorySweeps.includes('cat:News') && !pageNavigationPending")
    assert page.evaluate('history.length') == initial_length
    assert 'category=News' in page.url
    page.evaluate('swipeCategory(150, 161, 250, 480)')
    expect(page.locator('#topCatBar .active')).to_have_text('政经')
    page.screenshot(path=str(tmp_path / ('category-mobile-pwa.png' if standalone else 'category-mobile-browser.png')))
    page.evaluate('swipeCategory(75, 320, 250, 248)')
    expect(page.locator('#topCatBar .active')).to_have_text('All')
    assert page.evaluate('history.length') == initial_length


def test_horizontal_swipe_shows_pending_category_and_accepts_another_swipe(title_page):
    page, control, release = title_page
    page.set_viewport_size({'width': 430, 'height': 932})
    page.goto('http://raynews.test/')
    page.wait_for_function("pageMemoryBuffer.has(newsPageCacheKey(1, 'cat:News'))")
    page.evaluate("async () => { pageMemoryBuffer.clear(); await clearCachedNewsPages(); window.scrollTo(0, 670); }")
    page.wait_for_function('scrollY >= 650')
    scroll_y = page.evaluate('scrollY')
    held = {}

    def route(request):
        query = parse_qs(urlsplit(request.request.url).query)
        category = query.get('category', [''])[0]
        if query.get('page') == ['1'] and category in ('News', 'Tech'):
            held[category] = request
        else:
            request.fallback()

    page.route('**/api/news?*', route)
    install_swipe_probe(page)
    assert page.evaluate("swipeCategory(325, 285, 470, 488, '#pagination button')") == [False, True, True]
    expect(page.locator('#topCatBar .pending')).to_have_text('政经')
    expect(page.locator('#topCatBar .active')).to_have_text('All')
    assert page.evaluate('scrollY') == scroll_y
    assert page.evaluate("!document.getElementById('overlay').classList.contains('open')")
    page.evaluate("swipeCategory(325, 285, 475, 489, '#footer')")
    expect(page.locator('#topCatBar .pending')).to_have_text('科技')
    for _ in range(100):
        if 'Tech' in held:
            break
        page.wait_for_timeout(20)
    held['Tech'].fulfill(json={'items': [{
        'id': 901, 'title': 'Technology headline', 'source': 'Example', 'timestamp': 1792066100
    }], 'page': 1, 'total': 1})
    expect(page.locator('#topCatBar .active')).to_have_text('科技')
    expect(page.locator('#list .item-title')).to_have_text('Technology headline')
    expect(page.locator('#topCatBar .pending')).to_have_count(0)
    page.wait_for_function('titleReadsInFlight.has(901)')
    release(lambda ids: 901 in ids)
    expect(page.locator('#list .item-title')).to_have_text('翻译后的新闻标题 901')
    assert page.evaluate('scrollY') == 0


def test_touch_scrolling_does_not_latch_article_hover_and_tap_still_opens(title_page):
    page, control, _ = title_page
    control['enabled'] = False
    page.set_viewport_size({'width': 430, 'height': 932})
    page.add_init_script("Object.defineProperty(navigator, 'standalone', {value: true})")
    page.goto('http://raynews.test/')
    articles = page.locator('#list .item')
    expect(articles.first).to_be_visible()
    normal = articles.first.evaluate("el => getComputedStyle(el).backgroundColor")
    articles.first.hover()
    expect(articles.first).not_to_have_css('background-color', normal)

    touch = page.context.new_cdp_session(page)
    touch.send('Emulation.setTouchEmulationEnabled', {'enabled': True, 'maxTouchPoints': 1})
    touch.send('Input.dispatchTouchEvent', {'type': 'touchStart', 'touchPoints': [{'x': 210, 'y': 475}]})
    for y in (440, 395, 345, 295, 245):
        touch.send('Input.dispatchTouchEvent', {'type': 'touchMove', 'touchPoints': [{'x': 210, 'y': y}]})
    touch.send('Input.dispatchTouchEvent', {'type': 'touchEnd', 'touchPoints': []})
    page.wait_for_function('scrollY >= 100')
    expect(page.locator('#topCatBar .active')).to_have_text('All')
    assert "#/article/" not in page.url
    assert articles.evaluate_all(
        "(items, color) => items.every(item => getComputedStyle(item).backgroundColor === color)", normal
    )
    page.evaluate('window.scrollTo(0, 0)')
    touch.send('Input.dispatchTouchEvent', {'type': 'touchStart', 'touchPoints': [{'x': 300, 'y': 300}]})
    for x in (285, 270, 255):
        touch.send('Input.dispatchTouchEvent', {'type': 'touchMove', 'touchPoints': [{'x': x, 'y': 308}]})
    touch.send('Input.dispatchTouchEvent', {'type': 'touchEnd', 'touchPoints': []})
    expect(page.locator('#topCatBar .active')).to_have_text('政经')
    assert "#/article/" not in page.url
    page.wait_for_function('!pageNavigationPending && !document.querySelector("#list").getAnimations().length')
    box = articles.first.bounding_box()
    point = {'x': box['x'] + box['width'] / 2, 'y': box['y'] + box['height'] / 2}
    touch.send('Input.dispatchTouchEvent', {'type': 'touchStart', 'touchPoints': [point]})
    touch.send('Input.dispatchTouchEvent', {'type': 'touchEnd', 'touchPoints': []})
    expect(page.locator('#articleBody')).to_contain_text('Original body 1')


def test_slow_titles_do_not_block_navigation_and_arrive_without_replacing_cards(title_page):
    page, _, release = title_page
    page.goto('http://raynews.test/')
    page.wait_for_function('titleReadsInFlight.has(31)')
    page.evaluate('void goToPage(2)')
    page.wait_for_function('currentPage === 2 && !pageNavigationPending', timeout=2500)
    expect(page.locator('#list .item-title').first).to_have_text('Original headline 31')
    page.evaluate('''async () => {
      await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(() => requestAnimationFrame(resolve))));
      window.cardBefore = document.querySelector('#list .item');
      window.scrollTo({top:500, behavior:'instant'});
    }''')
    page.wait_for_function('window.scrollY >= 450')
    release(lambda ids: 31 in ids)
    expect(page.locator('#list .item-title').first).to_have_text('翻译后的新闻标题 31')
    assert page.evaluate('window.cardBefore === document.querySelector("#list .item")')
    assert page.evaluate('window.scrollY') >= 450


def test_next_page_titles_are_independent_of_slow_previous_batch_and_logout(title_page):
    page, control, release = title_page
    control['hold_first'] = True
    page.goto('http://raynews.test/')
    page.wait_for_function('titleReadsInFlight.has(1) && titleReadsInFlight.has(31)')
    page.evaluate('void goToPage(2)')
    release(lambda ids: 31 in ids)
    page.wait_for_function('currentPage === 2 && !pageNavigationPending')
    expect(page.locator('#list .item-title').first).to_have_text('翻译后的新闻标题 31')
    assert page.evaluate('titleReadsInFlight.has(1)')
    page.evaluate('logout()')
    release(lambda ids: 1 in ids)
    expect(page.locator('#list .item-title').first).to_have_text('Original headline 31')
    assert page.evaluate('serverTitleResults.size') == 0


def test_title_refresh_and_preference_changes_preserve_source_selection_and_favorites(title_page):
    page, control, release = title_page
    page.goto('http://raynews.test/')
    page.wait_for_function('titleReadsInFlight.has(31)')
    page.evaluate('''() => {
      authUser.role = 'admin';
      sourceArticlesState.items = [{id:31, title:'Original headline 31', source:'Example'}];
      renderSourceArticles(sourceArticlesState.items);
      document.getElementById('sourceArticlesOverlay').classList.add('open');
      renderFavorites({items:[{article_id:31, title:'Original headline 31', source:'Example'}]});
      window.sourceCheck = document.querySelector('.source-article-check[data-article-id="31"]');
      window.sourceCheck.checked = true;
      window.favoriteCard = document.querySelector('#favBody .fav-item');
    }''')
    release(lambda ids: 31 in ids)
    expect(page.locator('#sourceArticlesBody .fav-item-title')).to_have_text('翻译后的新闻标题 31')
    expect(page.locator('#favBody .fav-item-title')).to_have_text('翻译后的新闻标题 31')
    control['enabled'] = False
    page.evaluate('loadUserSettings()')
    expect(page.locator('#sourceArticlesBody .fav-item-title')).to_have_text('Original headline 31')
    expect(page.locator('#favBody .fav-item-title')).to_have_text('Original headline 31')
    assert page.evaluate('window.sourceCheck === document.querySelector(".source-article-check[data-article-id=\\"31\\"]") && window.sourceCheck.checked')
    assert page.evaluate('window.favoriteCard === document.querySelector("#favBody .fav-item")')


def test_slow_page_navigation_retains_focus_and_recovers_from_deleted_page(title_page):
    page, control, _ = title_page
    control['enabled'] = False
    page.set_viewport_size({'width': 390, 'height': 844})
    page.goto('http://raynews.test/')
    page.wait_for_function("pageMemoryBuffer.has(newsPageCacheKey(2, 'all'))")
    page.evaluate("async () => { pageMemoryBuffer.clear(); await clearCachedNewsPages(); }")
    deleted, recovered = [], []

    def hold_page(route):
        params = parse_qs(urlsplit(route.request.url).query)
        if params.get('page') == ['2']:
            deleted.append(route)
        elif params.get('page') == ['1']:
            recovered.append(route)
        else:
            route.fallback()

    page.route('**/api/news?*', hold_page)
    next_button = page.get_by_role('button', name='下一页', exact=True)
    next_button.focus()
    next_button.press('Enter')
    expect(next_button).to_be_focused()
    expect(next_button).to_be_disabled()
    expect(page.locator('#pagination [role=status]')).to_have_text('1 / 3 · 加载中…')
    expect(page.locator('#list .item')).to_have_count(30)
    for _ in range(100):
        if deleted:
            break
        page.wait_for_timeout(20)
    assert len(deleted) == 1
    deleted[0].fulfill(json={'items': [], 'page': 2, 'total': 12})
    for _ in range(100):
        if recovered:
            break
        page.wait_for_timeout(20)
    assert len(recovered) == 1
    expect(page.locator('#list .item')).to_have_count(30)
    recovered[0].fulfill(json={'page': 1, 'total': 12, 'items': [
        {'id': index, 'title': f'Remaining headline {index}', 'source': 'Example'}
        for index in range(1, 13)
    ]})
    expect(page.locator('#list .item')).to_have_count(12)
    expect(page.locator('#list')).to_be_focused()
    expect(page.locator('#list')).to_have_attribute('aria-busy', 'false')
    assert page.evaluate('currentPage') == 1
    assert 'page=2' not in page.url


def test_search_labels_cached_results_and_updates_on_reconnect(title_page):
    page, control, _ = title_page
    control['enabled'] = False
    page.goto('http://raynews.test/')
    searches = []

    def route(request):
        params = parse_qs(urlsplit(request.request.url).query)
        if params.get('q') != ['城市交通']:
            request.fallback()
            return
        searches.append(request)
        cached = len(searches) == 1
        request.fulfill(headers={'X-SW-Fallback': '1'} if cached else {}, json={
            'items': [{'id': 111, 'title': '缓存摘要' if cached else '在线搜索结果', 'source': 'Example',
                       'date': '2026-11-24', 'time': '12:05', 'timestamp': 1795512300}],
            'page': 1, 'total': 1,
        })

    page.route('**/api/news?*', route)
    page.evaluate('openSearch()')
    page.locator('#articleSearchInput').fill('城市交通')
    expect(page.locator('#searchStatus')).to_contain_text('缓存结果')
    expect(page.locator('#searchResults .item-title')).to_have_text('缓存摘要')
    expect(page.locator('#searchResults').get_by_role('button', name='重新搜索')).to_be_visible()
    page.evaluate("window.dispatchEvent(new Event('online'))")
    expect(page.locator('#searchResults .item-title')).to_have_text('在线搜索结果')
    expect(page.locator('#searchStatus')).to_have_text('已显示 1 / 1 条与「城市交通」相关的新闻')
    assert len(searches) == 2


def test_category_background_refresh_waits_for_reader_to_reveal_news(title_page):
    page, control, _ = title_page
    control['enabled'] = False
    page.set_viewport_size({'width': 390, 'height': 844})
    page.goto('http://raynews.test/')
    page.wait_for_function("pageMemoryBuffer.has(newsPageCacheKey(2, 'all'))")
    page.evaluate("void selectFilter('cat:News')")
    page.wait_for_function("filter === 'cat:News' && !pageNavigationPending")
    page.evaluate("""() => {
      loadSourceCategories = async () => true;
      sourceMeta['Example'] = {category:'News', label:'Example'};
    }""")
    held = []
    cursor = 1792066000
    newest = {'id': 888, 'title': '后台新增新闻', 'source': 'Example', 'timestamp': cursor + 100}
    refreshed = {'page': 1, 'total': 91, 'items': [newest] + [
        {'id': index, 'title': f'Original headline {index}', 'source': 'Example', 'timestamp': cursor - index}
        for index in range(1, 30)
    ]}

    def route(request):
        params = parse_qs(urlsplit(request.request.url).query)
        if 'since' in params:
            request.fulfill(json={'items': [newest]})
        elif params.get('page') == ['1'] and params.get('category') == ['News']:
            if not held:
                held.append(request)
            else:
                request.fulfill(json=refreshed)
        else:
            request.fallback()

    page.route('**/api/news?*', route)
    page.evaluate(f"void loadSince({cursor})")
    for _ in range(100):
        if held:
            break
        page.wait_for_timeout(20)
    assert held
    page.evaluate('window.scrollTo(0, 720)')
    page.wait_for_function('scrollY >= 700')
    before = page.evaluate('scrollY')
    held[0].fulfill(json=refreshed)
    prompt = page.locator('#newArticlesPrompt')
    expect(prompt).to_have_class('new-articles-prompt show')
    expect(page.locator('#list .item').first).to_have_attribute('data-id', '1')
    assert page.evaluate('scrollY') == before
    prompt.click()
    expect(page.locator('#list .item').first).to_have_attribute('data-id', '888')
    expect(prompt).to_have_class('new-articles-prompt')
