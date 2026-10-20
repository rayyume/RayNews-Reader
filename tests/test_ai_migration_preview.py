"""The v5-to-v6 migration keeps shared historical AI content available."""
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from server_ai_store import migrate_results, migration_preview, validate_migration_state


def test_web_initialization_preserves_existing_content_without_loss_flag(tmp_path, monkeypatch):
    import web_server

    path = tmp_path / 'news.db'
    with sqlite3.connect(path) as conn:
        conn.executescript("""
            CREATE TABLE articles(id INTEGER PRIMARY KEY, title TEXT, original_title TEXT,
                                  title_source TEXT, title_updated_at TEXT);
            INSERT INTO articles VALUES(1,'AI headline',NULL,'translation',NULL);
            CREATE TABLE ai_results(article_id INTEGER PRIMARY KEY,summary TEXT);
            INSERT INTO ai_results VALUES(1,'Old summary');
        """)
    monkeypatch.setattr(web_server, 'NEWS_DB', str(path))
    monkeypatch.delenv('AI_V6_ACCEPT_LEGACY_LOSS', raising=False)
    assert web_server._init_ai_results_table()
    with sqlite3.connect(path) as conn:
        assert conn.execute('SELECT summary,title_translation FROM ai_results').fetchone() == ('Old summary', 'AI headline')
        assert conn.execute('SELECT title FROM articles').fetchone()[0] == '无标题文章'
        assert conn.execute('SELECT summary FROM ai_results_legacy_v5').fetchone()[0] == 'Old summary'
        assert conn.execute('SELECT legacy_title FROM ai_titles_legacy_v5').fetchone()[0] == 'AI headline'


def test_account_upgrade_can_proceed_with_legacy_news_results(tmp_path, monkeypatch):
    import models

    models.close_db()
    accounts_path = tmp_path / 'raynews.db'
    schema = (Path(__file__).parent / 'fixtures' / 'legacy_accounts_v5.sql').read_text()
    with sqlite3.connect(accounts_path) as accounts:
        accounts.executescript(schema)
        accounts.execute("INSERT INTO users(id,email,password,nickname,role) VALUES(1,'admin@example.test','hash','Admin','admin')")
        accounts.execute("INSERT INTO ai_configs(user_id,api_key) VALUES(1,'legacy-key')")
    with sqlite3.connect(tmp_path / 'news.db') as news:
        news.executescript("""
            CREATE TABLE ai_results(article_id INTEGER PRIMARY KEY,summary TEXT);
            INSERT INTO ai_results VALUES(1,'Old summary');
        """)
    monkeypatch.setattr(models, 'DB_FILE', accounts_path)
    monkeypatch.delenv('AI_V6_ACCEPT_LEGACY_LOSS', raising=False)
    assert models.get_db().execute("SELECT 1 FROM app_state WHERE key='ai_access_v6'").fetchone()
    models.close_db()
    with sqlite3.connect(tmp_path / 'news.db') as news:
        assert news.execute('SELECT summary FROM ai_results').fetchone()[0] == 'Old summary'


def test_preview_is_read_only_and_migration_preserves_old_results_and_titles(tmp_path):
    path = tmp_path / 'news.db'
    with sqlite3.connect(path) as conn:
        conn.executescript("""
            CREATE TABLE articles(id INTEGER PRIMARY KEY, title TEXT, original_title TEXT,
                                  title_source TEXT, title_updated_at TEXT, ingested_at INTEGER);
            INSERT INTO articles VALUES(1,'AI headline',NULL,'translation',NULL,1);
            CREATE TABLE ai_results(article_id INTEGER PRIMARY KEY, summary TEXT, translation TEXT);
            INSERT INTO ai_results VALUES(1,'Old summary','<p>Old translation</p>');
        """)
        before_schema = conn.execute('PRAGMA schema_version').fetchone()[0]
        preview = migration_preview(conn)
        assert preview['legacy_result_rows'] == 1
        assert preview['historical_summary_rows'] == 1
        assert preview['historical_translation_rows'] == 1
        assert preview['ai_derived_titles'] == 1
        assert preview['unrecoverable_titles'] == 1
        output = subprocess.check_output(
            [sys.executable, str(Path(__file__).resolve().parents[1] / 'server_ai_store.py'),
             '--db', str(path)], text=True)
        assert json.loads(output) == preview
        assert conn.execute('PRAGMA schema_version').fetchone()[0] == before_schema

        with conn:
            report = migrate_results(conn)
        assert report['preserved_legacy_results'] == 1
        assert report['preserved_ai_titles'] == 1
        assert conn.execute('SELECT summary,translation,title_translation FROM ai_results').fetchone() == (
            'Old summary', '<p>Old translation</p>', 'AI headline')
        assert conn.execute('SELECT summary_source,translation_source,title_translation_source FROM ai_results').fetchone() == (
            'legacy', 'legacy', 'legacy')
        assert conn.execute('SELECT summary FROM ai_results_legacy_v5').fetchone()[0] == 'Old summary'
        assert conn.execute('SELECT title FROM articles').fetchone()[0] == '无标题文章'
        assert conn.execute('SELECT legacy_title FROM ai_titles_legacy_v5').fetchone()[0] == 'AI headline'
        after = migration_preview(conn)
        assert not after['migration_pending']
        assert after['legacy_result_rows'] == 1
        assert after['ai_derived_titles'] == 1
        assert after['unrecoverable_titles'] == 1
        with conn:
            assert migrate_results(conn) is None
        assert conn.execute('SELECT summary FROM ai_results').fetchone()[0] == 'Old summary'


def test_clean_install_needs_no_acknowledgement():
    with sqlite3.connect(':memory:') as conn:
        conn.execute('CREATE TABLE ai_results(article_id INTEGER PRIMARY KEY,summary TEXT)')
        assert migration_preview(conn)['legacy_result_rows'] == 0
        migrate_results(conn)
        assert not migration_preview(conn)['migration_pending']


@pytest.mark.parametrize('source,field', [('translation', 'title_translation'),
                                          ('title_summary', 'title_summary'), ('ai', 'title_summary')])
def test_ai_title_without_result_row_is_retained_privately(source, field):
    with sqlite3.connect(':memory:') as conn:
        conn.executescript("""
            CREATE TABLE articles(id INTEGER PRIMARY KEY, title TEXT, original_title TEXT,
                                  title_source TEXT, title_updated_at TEXT);
            CREATE TABLE ai_results(article_id INTEGER PRIMARY KEY,summary TEXT);
        """)
        conn.execute("INSERT INTO articles VALUES(1,'AI headline','Source headline',?,NULL)", (source,))
        with conn:
            migrate_results(conn)
        assert conn.execute('SELECT title FROM articles').fetchone()[0] == 'Source headline'
        assert conn.execute(f'SELECT {field} FROM ai_results').fetchone()[0] == 'AI headline'
        assert conn.execute('SELECT legacy_title FROM ai_titles_legacy_v5').fetchone()[0] == 'AI headline'


def test_translation_json_title_is_retained_when_public_title_was_unchanged():
    with sqlite3.connect(':memory:') as conn:
        conn.executescript("""
            CREATE TABLE articles(id INTEGER PRIMARY KEY,title TEXT,original_title TEXT,title_source TEXT);
            INSERT INTO articles VALUES(1,'Source headline','Source headline',NULL);
            CREATE TABLE ai_results(article_id INTEGER PRIMARY KEY,translation TEXT);
            INSERT INTO ai_results VALUES(1,'{"title":"Translated headline","html":"<p>Text</p>"}');
            INSERT INTO ai_results VALUES(2,'{malformed');
        """)
        with conn:
            migrate_results(conn)
        assert conn.execute('SELECT title_translation,title_translation_source FROM ai_results WHERE article_id=1').fetchone() == (
            'Translated headline', 'legacy')
        assert conn.execute('SELECT title FROM articles').fetchone()[0] == 'Source headline'


def test_interrupted_archive_is_refused_without_touching_articles():
    with sqlite3.connect(':memory:') as conn:
        conn.executescript("""
            CREATE TABLE articles(id INTEGER PRIMARY KEY,title TEXT);
            INSERT INTO articles VALUES(1,'Original');
            CREATE TABLE ai_results(article_id INTEGER PRIMARY KEY,summary TEXT);
            CREATE TABLE ai_results_legacy_v5(article_id INTEGER PRIMARY KEY,summary TEXT);
        """)
        with pytest.raises(RuntimeError, match='Legacy AI archive exists'):
            validate_migration_state(conn)
        assert conn.execute('SELECT title FROM articles').fetchone()[0] == 'Original'
