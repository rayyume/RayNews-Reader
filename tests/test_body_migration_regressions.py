"""Upgrade must not treat first-ingest snapshots as the latest source body."""
import sqlite3
import json

import pytest

import fetcher
import refresh_server
from server_ai_store import migrate_results


@pytest.fixture
def public_detail(tmp_path, monkeypatch):
    monkeypatch.setattr(refresh_server, 'DB_FILE', tmp_path / 'news.db')
    monkeypatch.setattr(refresh_server, '_schema_ready', False)
    refresh_server._schema_ready_event.clear()
    refresh_server.clear_article_cache()
    yield lambda: json.loads(refresh_server.api_news_detail(1))
    refresh_server.clear_article_cache()
    refresh_server._schema_ready_event.clear()


@pytest.mark.parametrize('has_full_content', [False, True])
def test_upgrade_preserves_reingested_body(tmp_path, monkeypatch, has_full_content, public_detail):
    monkeypatch.setattr(fetcher, 'OUTPUT_DIR', tmp_path)
    monkeypatch.setattr(fetcher, 'DB_FILE', tmp_path / 'news.db')
    conn = fetcher.init_db()
    try:
        article = {'id': 1, 'title': 'Original title', 'body_html': '<p>Old excerpt</p>'}
        fetcher.upsert_articles(conn, [article])
        latest = '<p>Latest source body with refreshed content</p>'
        fetcher.upsert_articles(conn, [{**article, 'body_html': latest,
                                       'has_full_content': has_full_content}])
        row = conn.execute('SELECT body_html, original_body_html FROM articles').fetchone()
        assert tuple(row) == (latest, article['body_html'])
        conn.execute('CREATE TABLE ai_results(article_id INTEGER PRIMARY KEY, translation TEXT)')
        conn.execute("INSERT INTO ai_results VALUES(1, '<p>Legacy translation</p>')")
        conn.commit()

        for _ in range(2):
            with conn:
                migrate_results(conn)
            row = conn.execute('SELECT body_html, original_body_html, has_full_content FROM articles').fetchone()
            assert tuple(row) == (latest, article['body_html'], int(has_full_content))
        assert conn.execute('SELECT COUNT(*) FROM ai_results').fetchone()[0] == 0
        assert conn.execute('SELECT translation FROM ai_results_legacy_v5').fetchone()[0] == '<p>Legacy translation</p>'
        # Check the public, cached endpoint as well as the migration's DB rows.
        detail = public_detail()
        assert detail['body_html'] == latest
        assert 'original_body_html' not in detail
        assert public_detail()['body_html'] == latest
    finally:
        conn.close()


@pytest.mark.parametrize(('current', 'snapshot'), [
    ('Short updated source', 'An older source snapshot that happens to be longer'),
    ('Current source', ''),
    ('Current source', None),
    ('', 'Old snapshot'),
    (None, 'Old snapshot'),
])
def test_upgrade_does_not_guess_body_freshness(current, snapshot):
    with sqlite3.connect(':memory:') as conn:
        conn.execute('CREATE TABLE ai_results(article_id INTEGER PRIMARY KEY)')
        conn.execute('CREATE TABLE articles(id INTEGER PRIMARY KEY, body_html TEXT, original_body_html TEXT)')
        conn.execute('INSERT INTO articles VALUES(1, ?, ?)', (current, snapshot))
        migrate_results(conn)
        assert conn.execute('SELECT body_html, original_body_html FROM articles').fetchone() == (current, snapshot)


@pytest.mark.parametrize('current',['<p>Short current body</p>','',None])
def test_public_detail_never_guesses_from_older_snapshot(tmp_path,monkeypatch,public_detail,current):
    monkeypatch.setattr(fetcher,'OUTPUT_DIR',tmp_path)
    monkeypatch.setattr(fetcher,'DB_FILE',tmp_path/'news.db')
    conn=fetcher.init_db()
    try:
        fetcher.upsert_articles(conn,[{'id':1,'title':'Original title','body_html':'<p>An old source snapshot that happens to be longer</p>'}])
        conn.execute('UPDATE articles SET body_html=? WHERE id=1',(current,))
        conn.commit()
        assert public_detail()['body_html']==(current or '')
    finally:
        conn.close()
