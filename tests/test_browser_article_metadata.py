"""Browser plugin not available: exercise metadata freshness in Chromium."""
import pytest
from playwright.sync_api import expect

from test_browser_tts import ARTICLES, browser, setup_page


def source_articles(label, read_at):
    return [{**item, 'display_source': label, 'display_source_key': '科技观察',
             'source_curated': True, 'source_labels_read_at': read_at} for item in ARTICLES]


def close_article(page):
    page.evaluate('closeArticle()')
    page.wait_for_function('!document.getElementById("overlay").classList.contains("open")')


@pytest.mark.parametrize('guest', [False, True])
def test_older_source_metadata_does_not_override_fresh_articles_or_details(browser, guest):
    context, page, _, _, errors, control = setup_page(
        browser, {'width': 390, 'height': 844}, guest=guest,
        articles=source_articles('新的管理员名称', 200),
        sources=[{'source': '科技观察', 'label': '旧缓存名称', 'category': 'Tech', 'status': 'manual'}])
    try:
        expect(page.locator('#list .item[data-id="1"] .badge')).to_have_text('新的管理员名称')
        control['article_detail_override'] = {'display_source': '更新的详情名称', 'source_labels_read_at': 300}
        page.locator('#list .item[data-id="1"]').click()
        expect(page.locator('#articleWrap .article-meta .badge')).to_have_text('更新的详情名称')
        expect(page.locator('#list .item[data-id="1"] .badge')).to_have_text('更新的详情名称')
        if not guest:
            page.locator('.tts-speaker').click()
            page.wait_for_function('document.getElementById("ttsAudio").src.startsWith("blob:")')
            page.locator('#ttsDisc').click()
            expect(page.locator('#ttsSource')).to_contain_text('更新的详情名称')
            assert page.evaluate('navigator.mediaSession.metadata.artist') == '更新的详情名称'
        assert errors == []
    finally:
        context.close()


@pytest.mark.parametrize('guest', [False, True])
def test_cached_body_does_not_roll_back_newer_list_sources(browser, guest):
    articles = source_articles('旧管理员名称', 100)
    context, page, _, calls, errors, _ = setup_page(
        browser, {'width': 1280, 'height': 800}, guest=guest,
        articles=articles, sources_available=False)
    try:
        context.route('**/ai/translation-updates**', lambda route: route.fulfill(
            json={'items': [], 'cursor': '2026-10-21 00:00:00.000|0'}))
        if not guest:
            page.evaluate('pollTranslationUpdates()')
        page.locator('#list .item[data-id="1"]').click()
        expect(page.locator('#articleBody')).to_contain_text('城市交通')
        page.wait_for_function('!!articleBodyCache[1]')
        close_article(page)
        detail_reads = sum(path == '/api/news/1' for _, path in calls)
        articles[0].update(display_source='最新管理员名称', source_labels_read_at=200)
        page.evaluate('loadNewsPage(1,{useCache:false,forceNetwork:true,userInitiated:true})')
        expect(page.locator('#list .item[data-id="1"] .badge')).to_have_text('最新管理员名称')
        page.locator('#list .item[data-id="1"]').click()
        expect(page.locator('#articleBody')).to_contain_text('城市交通')
        expect(page.locator('#articleWrap .article-meta .badge')).to_have_text('最新管理员名称')
        expect(page.locator('#list .item[data-id="1"] .badge')).to_have_text('最新管理员名称')
        assert page.evaluate('articleBodyCache[1].display_source') == '最新管理员名称'
        assert sum(path == '/api/news/1' for _, path in calls) == detail_reads
        assert errors == []
    finally:
        context.close()


def test_newer_source_metadata_can_update_cached_articles_and_active_audio(browser):
    context, page, _, _, errors, _ = setup_page(
        browser, {'width': 390, 'height': 844}, articles=source_articles('旧管理员名称', 100))
    try:
        page.locator('#list .item[data-id="1"]').click()
        expect(page.locator('#articleBody')).to_contain_text('城市交通')
        page.locator('.tts-speaker').click()
        page.wait_for_function('document.getElementById("ttsAudio").src.startsWith("blob:")')
        page.locator('#ttsDisc').click()
        page.evaluate('''() => {
          rebuildCategoryMap([{source:'科技观察',label:'新分类名称',category:'Tech',status:'classified'}],
            {source_labels_read_at:200});
        }''')
        expect(page.locator('#list .item[data-id="1"] .badge')).to_have_text('新分类名称')
        expect(page.locator('#articleWrap .article-meta .badge')).to_have_text('新分类名称')
        expect(page.locator('#ttsSource')).to_contain_text('新分类名称')
        assert page.evaluate('navigator.mediaSession.metadata.artist') == '新分类名称'
        page.evaluate('''() => {
          rebuildCategoryMap([{source:'科技观察',label:'迟到的旧名称',category:'Tech',status:'manual'}],
            {source_labels_read_at:150});
        }''')
        expect(page.locator('#list .item[data-id="1"] .badge')).to_have_text('新分类名称')
        expect(page.locator('#ttsSource')).to_contain_text('新分类名称')
        assert errors == []
    finally:
        context.close()


def test_source_cache_keeps_newer_metadata_when_an_older_response_finishes_last(browser):
    context, page, _, _, errors, _ = setup_page(browser, {'width': 390, 'height': 844})
    try:
        cached = page.evaluate('''async () => {
          const newer={source_labels_read_at:300,sources:[{source:'科技观察',label:'新名称'}]};
          const older={source_labels_read_at:200,sources:[{source:'科技观察',label:'旧名称'}]};
          await writeNewsCacheEntry({key:'source-metadata',kind:'metadata',updatedAt:Date.now(),data:newer});
          await writeNewsCacheEntry({key:'source-metadata',kind:'metadata',updatedAt:Date.now(),data:older});
          return (await readNewsCacheEntry('source-metadata')).data;
        }''')
        assert cached['source_labels_read_at'] == 300
        assert cached['sources'][0]['label'] == '新名称'
        assert errors == []
    finally:
        context.close()


def test_queued_audio_preserves_the_known_source_freshness(browser):
    articles = source_articles('新的管理员名称', 200)
    context, page, _, _, errors, _ = setup_page(browser, {'width': 390, 'height': 844}, articles=articles)
    pending = []
    try:
        page.locator('#list .item[data-id="1"]').click()
        expect(page.locator('#articleBody')).to_contain_text('城市交通')
        page.evaluate('''() => rebuildCategoryMap(
          [{source:'科技观察',label:'旧缓存名称',category:'Tech',status:'manual'}],
          {source_labels_read_at:100})''')
        context.route('**/ai/tts/1', lambda route: pending.append(route))
        page.locator('.tts-speaker').click()
        expect(page.locator('#ttsSource')).to_contain_text('新的管理员名称')
        page.wait_for_function('document.getElementById("ttsDisc").hidden === false')
        assert len(pending) == 1
        pending.pop().fulfill(json={'id':'job-1','status':'ready','article':articles[0]})
        page.wait_for_function('document.getElementById("ttsAudio").src.startsWith("blob:")')
        assert errors == []
    finally:
        context.close()


@pytest.mark.parametrize('stale_view', ['favorites', 'source_history', 'search'])
def test_settings_refresh_keeps_each_views_own_original_title(browser, stale_view):
    articles = [{**item, 'title': f'旧原题 {item["id"]}'} for item in ARTICLES]
    context, page, _, _, errors, _ = setup_page(browser, {'width': 1280, 'height': 800}, articles=articles)
    try:
        page.evaluate('''view => {
          const item={id:1,article_id:1,title:'旧原题 1',source:'科技观察'};
          if(view==='favorites') renderFavorites({items:[item]});
          if(view==='source_history') {
            sourceArticlesState.items=[item];
            renderSourceArticles(sourceArticlesState.items);
          }
          if(view==='search') {
            searchItems=[item];searchTotal=1;searchQuery='原题';renderServerSearchItems();
          }
        }''', stale_view)
        articles[0]['title'] = '新原题 1'
        page.evaluate('loadNewsPage(1,{useCache:false,forceNetwork:true,userInitiated:true})')
        expect(page.locator('#list .item[data-id="1"] .item-title')).to_have_text('新原题 1')
        page.evaluate('loadUserSettings()')
        expect(page.locator('#list .item[data-id="1"] .item-title')).to_have_text('新原题 1')
        selector = {'favorites': '#favBody .fav-item-title', 'source_history': '#sourceArticlesBody .fav-item-title',
                    'search': '#searchResults .item-title'}[stale_view]
        expect(page.locator(selector)).to_have_text('旧原题 1')
        assert errors == []
    finally:
        context.close()
