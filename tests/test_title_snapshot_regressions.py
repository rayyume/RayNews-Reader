"""Fresh feed titles must supersede snapshots left by legacy AI rewriting."""
import json
import sqlite3

import pytest

import fetcher
import refresh_server
import web_server
from server_ai_store import migrate_results
from test_frontend_refresh_behavior import run_node, source_between


@pytest.fixture
def news(tmp_path, monkeypatch):
    monkeypatch.setattr(fetcher, 'OUTPUT_DIR', tmp_path)
    monkeypatch.setattr(fetcher, 'DB_FILE', tmp_path / 'news.db')
    monkeypatch.setattr(refresh_server, 'DB_FILE', fetcher.DB_FILE)
    monkeypatch.setattr(refresh_server, '_schema_ready', False)
    refresh_server._schema_ready_event.clear()
    refresh_server.clear_article_cache()
    conn = fetcher.init_db()
    yield conn
    conn.close()
    refresh_server.clear_article_cache()
    refresh_server._schema_ready_event.clear()


def seed_legacy_ai_title(conn):
    fetcher.upsert_articles(conn, [{'id': 1, 'title': 'First source title', 'timestamp': 100}])
    conn.execute("UPDATE articles SET title='AI shortened title', original_title='First source title', "
                 "title_source='title_summary', title_updated_at='2026-01-01 00:00:00'")
    conn.execute('CREATE TABLE ai_results(article_id INTEGER PRIMARY KEY, title_summary TEXT)')
    conn.execute("INSERT INTO ai_results VALUES(1, 'AI shortened title')")
    conn.commit()


@pytest.mark.parametrize('upgrade_first', [False, True])
def test_real_reingest_supersedes_legacy_title_before_or_after_upgrade(news, upgrade_first):
    seed_legacy_ai_title(news)
    if upgrade_first:
        with news:
            migrate_results(news)
    fetcher.upsert_articles(news, [{'id': 1, 'title': 'Latest source headline', 'timestamp': 101}])
    row = news.execute('SELECT title, original_title, title_source, title_updated_at FROM articles').fetchone()
    assert tuple(row[:3]) == ('Latest source headline', 'Latest source headline', None)
    assert row['title_updated_at'] is not None
    for _ in range(2):
        with news:
            migrate_results(news)
        assert news.execute('SELECT title FROM articles').fetchone()[0] == 'Latest source headline'
    assert json.loads(refresh_server.api_news_detail(1))['title'] == 'Latest source headline'
    assert json.loads(refresh_server.api_news_list({}))['items'][0]['title'] == 'Latest source headline'
    assert json.loads(refresh_server.api_news_list({'q': ['Latest source headline']}))['total'] == 1
    assert json.loads(refresh_server.api_news_list({'q': ['First source title']}))['total'] == 0


@pytest.mark.parametrize('source', [None, '', 'feed'])
def test_unmarked_snapshot_does_not_override_title_in_upgrade_or_reads(news, source):
    seed_legacy_ai_title(news)
    news.execute('UPDATE articles SET title=?, title_source=?', ('Current source headline', source))
    news.commit()
    item = dict(news.execute('SELECT * FROM articles').fetchone())
    assert refresh_server._clean_article_display_fields(dict(item))['title'] == item['title']
    assert web_server._original_article_meta(dict(item))['title'] == item['title']
    assert json.loads(refresh_server.api_news_list({'q': ['Current source headline']}))['total'] == 1
    assert json.loads(refresh_server.api_news_list({'q': ['First source title']}))['total'] == 0
    with news:
        migrate_results(news)
    assert news.execute('SELECT title FROM articles').fetchone()[0] == item['title']


@pytest.mark.parametrize('source', ['translation', 'title_summary', 'ai'])
@pytest.mark.parametrize('original', ['First source title', '', None])
def test_actual_legacy_ai_title_still_restores_or_hides_original(news, source, original):
    seed_legacy_ai_title(news)
    news.execute('UPDATE articles SET title_source=?, original_title=?', (source, original))
    news.commit()
    expected = original or '无标题文章'
    item = dict(news.execute('SELECT * FROM articles').fetchone())
    assert refresh_server._clean_article_display_fields(dict(item))['title'] == expected
    assert web_server._original_article_meta(dict(item))['title'] == expected
    with news:
        migrate_results(news)
    assert news.execute('SELECT title, title_source FROM articles').fetchone()[:] == (expected, None)


def test_upgrade_with_snapshot_but_no_source_column_preserves_current_title():
    with sqlite3.connect(':memory:') as conn:
        conn.execute('CREATE TABLE ai_results(article_id INTEGER PRIMARY KEY)')
        conn.execute('CREATE TABLE articles(title TEXT, original_title TEXT)')
        conn.execute("INSERT INTO articles VALUES('Current source title', 'Old snapshot')")
        migrate_results(conn)
        assert conn.execute('SELECT title FROM articles').fetchone()[0] == 'Current source title'


def test_background_title_processing_uses_current_unmarked_title(monkeypatch):
    seen = []

    def pending(title, cached, config):
        seen.append(title)
        return dict.fromkeys(('title_translation', 'title_summary', 'translated_title_summary'), False)

    monkeypatch.setattr(web_server, '_get_ai_result', lambda _: {})
    monkeypatch.setattr(web_server, '_pending_title_tasks', pending)
    assert not web_server._process_article_title(
        {'id': 1, 'title': 'Current source title', 'original_title': 'Old snapshot'}, {})
    assert seen == ['Current source title'] * 3


@pytest.mark.parametrize('original', ['Old snapshot', '', None])
def test_frontend_uses_public_source_title_instead_of_legacy_snapshot(original):
    item = refresh_server._clean_article_display_fields({
        'id': 1, 'title': 'Current source headline',
        'original_title': original, 'title_source': None,
    })
    source = ('const serverTitleResults = new Map();\n'
              + source_between('function displayTitle(', '// Convert contiguous list lines'))
    run_node(source, f'''
const item = {json.dumps(item)};
assert.equal(context.displayTitle(item), 'Current source headline');
context.userAutoSettings = {{view_title_translation:true,view_title_shortening:true}};
assert.equal(context.displayTitle(item), 'Current source headline');
vm.runInContext("serverTitleResults.set(1, {{title_translation:'Full translation',title_summary:'Short source',translated_title_summary:'Short translation'}})", context);
assert.equal(context.displayTitle(item), 'Short translation');
context.userAutoSettings = {{view_title_translation:true}};
assert.equal(context.displayTitle(item), 'Full translation');
context.userAutoSettings = {{view_title_shortening:true}};
assert.equal(context.displayTitle(item), 'Short source');
context.userAutoSettings = null;
assert.equal(context.displayTitle(item), 'Current source headline');
''', 'context.userAutoSettings = null;')
