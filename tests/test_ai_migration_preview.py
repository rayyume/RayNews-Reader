"""The v6 news migration requires an explicit, inspectable loss decision."""
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from server_ai_store import migrate_results, migration_preview


def test_web_initialization_rolls_back_when_acceptance_is_missing(tmp_path, monkeypatch):
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
    assert not web_server._init_ai_results_table()
    with sqlite3.connect(path) as conn:
        assert conn.execute('SELECT summary FROM ai_results').fetchone()[0] == 'Old summary'
        assert conn.execute('SELECT title FROM articles').fetchone()[0] == 'AI headline'
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='ai_results_legacy_v5'").fetchone()
    monkeypatch.setenv('AI_V6_ACCEPT_LEGACY_LOSS', '1')
    assert web_server._init_ai_results_table()
    with sqlite3.connect(path) as conn:
        assert conn.execute('SELECT summary FROM ai_results_legacy_v5').fetchone()[0] == 'Old summary'


def test_refusal_happens_before_account_credentials_are_removed(tmp_path, monkeypatch):
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
    with pytest.raises(RuntimeError, match='AI_V6_ACCEPT_LEGACY_LOSS=1'):
        models.get_db()
    with sqlite3.connect(accounts_path) as accounts:
        assert accounts.execute('SELECT api_key FROM ai_configs').fetchone()[0] == 'legacy-key'
        assert not accounts.execute("SELECT 1 FROM sqlite_master WHERE name='ai_permissions'").fetchone()
    monkeypatch.setenv('AI_V6_ACCEPT_LEGACY_LOSS', '1')
    assert models.get_db().execute("SELECT 1 FROM app_state WHERE key='ai_access_v6'").fetchone()
    models.close_db()


def test_legacy_result_and_missing_title_block_migration_until_accepted(tmp_path):
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

        with pytest.raises(RuntimeError, match='AI_V6_ACCEPT_LEGACY_LOSS=1'):
            migrate_results(conn)
        assert conn.execute('PRAGMA schema_version').fetchone()[0] == before_schema
        assert conn.execute('SELECT summary FROM ai_results').fetchone()[0] == 'Old summary'
        assert conn.execute('SELECT title FROM articles').fetchone()[0] == 'AI headline'
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='ai_results_legacy_v5'").fetchone()

        with conn:
            report = migrate_results(conn, accept_legacy_loss=True)
        assert report['historical_summary_rows'] == 1
        assert conn.execute('SELECT COUNT(*) FROM ai_results').fetchone()[0] == 0
        assert conn.execute('SELECT summary FROM ai_results_legacy_v5').fetchone()[0] == 'Old summary'
        assert conn.execute('SELECT title FROM articles').fetchone()[0] == '无标题文章'
        assert conn.execute('SELECT legacy_title FROM ai_titles_missing_source_v5').fetchone()[0] == 'AI headline'
        after = migration_preview(conn)
        assert not after['migration_pending']
        assert after['legacy_result_rows'] == 1
        assert after['unrecoverable_titles'] == 1


def test_clean_install_needs_no_acknowledgement():
    with sqlite3.connect(':memory:') as conn:
        conn.execute('CREATE TABLE ai_results(article_id INTEGER PRIMARY KEY,summary TEXT)')
        assert migration_preview(conn)['legacy_result_rows'] == 0
        migrate_results(conn)
        assert not migration_preview(conn)['migration_pending']


def test_ai_title_without_result_row_still_requires_acceptance():
    with sqlite3.connect(':memory:') as conn:
        conn.executescript("""
            CREATE TABLE articles(id INTEGER PRIMARY KEY, title TEXT, original_title TEXT,
                                  title_source TEXT, title_updated_at TEXT);
            INSERT INTO articles VALUES(1,'AI headline','Source headline','translation',NULL);
            CREATE TABLE ai_results(article_id INTEGER PRIMARY KEY,summary TEXT);
        """)
        preview = migration_preview(conn)
        assert preview['legacy_result_rows'] == 0
        assert preview['ai_derived_titles'] == 1
        assert preview['unrecoverable_titles'] == 0
        with pytest.raises(RuntimeError, match='AI_V6_ACCEPT_LEGACY_LOSS=1'):
            migrate_results(conn)
        with conn:
            migrate_results(conn, accept_legacy_loss=True)
        assert conn.execute('SELECT title FROM articles').fetchone()[0] == 'Source headline'
