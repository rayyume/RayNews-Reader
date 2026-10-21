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
                data = {'sources': [], 'categories': []}
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
