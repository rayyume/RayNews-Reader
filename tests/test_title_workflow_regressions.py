"""Independent title variants and retries must not gate one another."""
import json
from datetime import datetime

import pytest

import web_server
from test_ai_access_v6 import app

SHORT_ORIGINAL = 'OpenAI news'
LONG_ORIGINAL = ('An original English headline describing the server article title processing '
                 'workflow and its independent permission controls in detail')
LONG_TRANSLATION = '这是一篇详细介绍服务端文章标题处理方式以及翻译与原语言标题简写独立权限和任务处理规则的完整中文标题'
ORIGINAL_SHORT = 'Original English headline shortened'
TRANSLATED_SHORT = '服务端标题独立处理规则'
CONFIG = {'auto_translate_title': True, 'auto_title_summary_enabled': True}


def article(title):
    with web_server._news_db_conn() as db:
        columns = {row[1] for row in db.execute('PRAGMA table_info(articles)')}
        for column, declaration in (('date','TEXT'),('timestamp','INTEGER')):
            if column not in columns:
                db.execute(f'ALTER TABLE articles ADD COLUMN {column} {declaration}')
        db.execute('UPDATE articles SET title=?,date=?,timestamp=? WHERE id=1',
                   (title, datetime.now().strftime('%Y-%m-%d'), int(datetime.now().timestamp())))
        db.commit()
    return {'id':1, 'title':title}


class Service:
    def __init__(self):
        self.calls = []

    def translate_title(self, *args, **kwargs):
        self.calls.append('translation')
        return LONG_TRANSLATION

    def chat(self, *args, **kwargs):
        self.calls.append('original-short')
        return json.dumps({'title':ORIGINAL_SHORT})

    def summarize_title(self, *args, **kwargs):
        self.calls.append('translated-short')
        return json.dumps({'title':TRANSLATED_SHORT})


@pytest.mark.parametrize('cached_translation', [False, True])
def test_short_original_long_translation_is_selected_and_shortened(app, monkeypatch, cached_translation):
    item = article(SHORT_ORIGINAL)
    assert not web_server._needs_title_summary(SHORT_ORIGINAL)
    assert web_server._needs_title_summary(LONG_TRANSLATION)
    service = Service()
    monkeypatch.setattr(web_server, '_title_service', lambda config:service)
    if cached_translation:
        assert web_server._save_article_title_update(1, LONG_TRANSLATION, 'translation')
    assert [a['id'] for a in web_server._fetch_title_process_articles(CONFIG)] == [1]
    assert web_server._process_article_title(item, CONFIG)
    result = web_server._get_ai_result(1)
    assert result['translated_title_summary'] == TRANSLATED_SHORT
    assert not result['title_summary']
    assert service.calls == (['translated-short'] if cached_translation else ['translation','translated-short'])
    assert web_server._fetch_title_process_articles(CONFIG) == []


@pytest.mark.parametrize('failure', ['exception', 'validation'])
def test_translation_failure_still_generates_original_short_title(app, monkeypatch, failure):
    item = article(LONG_ORIGINAL)
    service = Service()
    monkeypatch.setattr(web_server, '_title_service', lambda config:service)
    def fail(*args):
        if failure == 'exception':
            raise TimeoutError('translation timed out')
        return {'valid':False, 'title':'', 'reason':'invalid translation'}
    monkeypatch.setattr(web_server, '_translate_title_with_retry', fail)
    assert web_server._process_article_title(item, CONFIG)
    result = web_server._get_ai_result(1)
    assert result['title_summary'] == ORIGINAL_SHORT
    assert result['title_translation_error']
    assert result['title_translation_error_at']
    assert not result['title_summary_error']
    assert service.calls == ['original-short']
    assert web_server._fetch_title_process_articles(CONFIG) == []


def test_translation_backoff_does_not_hide_original_shortening_candidate(app, monkeypatch):
    item = article(LONG_ORIGINAL)
    web_server._save_ai_result(1, title_translation_error='translation failed')
    service = Service()
    monkeypatch.setattr(web_server, '_title_service', lambda config:service)
    assert [a['id'] for a in web_server._fetch_title_process_articles(CONFIG)] == [1]
    assert web_server._process_article_title(item, CONFIG)
    assert service.calls == ['original-short']
    assert web_server._get_ai_result(1)['title_translation_error'] == 'translation failed'


def test_original_shortening_failure_does_not_block_translated_shortening(app, monkeypatch):
    item = article(LONG_ORIGINAL)
    web_server._save_article_title_update(1, LONG_TRANSLATION, 'translation')
    service = Service()
    def failed_original(*args, **kwargs):
        service.calls.append('original-short')
        raise TimeoutError('original shortening failed')
    service.chat = failed_original
    monkeypatch.setattr(web_server, '_title_service', lambda config:service)
    assert web_server._process_article_title(item, CONFIG)
    result = web_server._get_ai_result(1)
    assert result['translated_title_summary'] == TRANSLATED_SHORT
    assert result['title_summary_error']
    assert not result['translated_title_summary_error']
    assert service.calls == ['original-short','translated-short']
    assert web_server._fetch_title_process_articles(CONFIG) == []


def test_short_translation_does_not_need_an_extra_shortening_call(app, monkeypatch):
    item = article(LONG_ORIGINAL)
    web_server._save_article_title_update(1, TRANSLATED_SHORT, 'translation')
    service = Service()
    monkeypatch.setattr(web_server, '_title_service', lambda config:service)
    assert web_server._process_article_title(item, CONFIG)
    assert service.calls == ['original-short']
    assert not web_server._get_ai_result(1)['translated_title_summary']


@pytest.mark.parametrize('blocked,expected', [
    ('title_translation',['original-short']),
    ('title_summary',['translated-short']),
    ('translated_title_summary',['original-short']),
])
def test_each_variant_backoff_only_blocks_that_variant(app, monkeypatch, blocked, expected):
    item = article(LONG_ORIGINAL)
    if blocked != 'title_translation':
        web_server._save_article_title_update(1, LONG_TRANSLATION, 'translation')
    web_server._save_title_result(1, blocked, error='retry later')
    service = Service()
    monkeypatch.setattr(web_server, '_title_service', lambda config:service)
    assert [a['id'] for a in web_server._fetch_title_process_articles(CONFIG)] == [1]
    assert web_server._process_article_title(item, CONFIG)
    assert service.calls == expected
    assert web_server._get_ai_result(1)[blocked + '_error'] == 'retry later'
    assert web_server._fetch_title_process_articles(CONFIG) == []
    assert not web_server._process_article_title(item, CONFIG)
    assert service.calls == expected


@pytest.mark.parametrize('kind', ['title_translation','title_summary','translated_title_summary'])
def test_success_clears_only_its_expired_error_and_publishes_update(app, monkeypatch, kind):
    item = article(LONG_ORIGINAL if kind == 'title_summary' else SHORT_ORIGINAL)
    if kind != 'title_translation':
        web_server._save_article_title_update(1, LONG_TRANSLATION, 'translation')
    web_server._save_title_result(1, kind, error='old failure')
    with web_server._news_db_conn() as db:
        db.execute(f"UPDATE ai_results SET {kind}_error_at='2000-01-01 00:00:00' WHERE article_id=1")
        db.commit()
    previous = web_server._get_ai_result(1, strict=True)['version']
    monkeypatch.setattr(web_server, '_title_service', lambda config:Service())
    assert web_server._process_article_title(item, CONFIG)
    result = web_server._get_ai_result(1, strict=True)
    assert result[kind]
    assert result[kind + '_error'] is None
    assert result[kind + '_error_at'] is None
    assert result['version'] > previous


def test_v6_retry_upgrade_preserves_results_and_resets_ambiguous_backoff_once(tmp_path):
    import sqlite3
    from server_ai_store import migrate_results
    db = sqlite3.connect(tmp_path/'existing-v6.db')
    db.executescript("""
        CREATE TABLE ai_results(article_id INTEGER PRIMARY KEY,summary TEXT,translation TEXT,
            title_summary TEXT,title_translation TEXT,title_summary_error TEXT,title_summary_error_at TEXT);
        CREATE TABLE ai_schema_migrations(name TEXT PRIMARY KEY);
        INSERT INTO ai_schema_migrations VALUES('server_only_v6');
        INSERT INTO ai_results VALUES(1,'keep summary',NULL,NULL,NULL,'ambiguous old failure','2000-01-01');
        INSERT INTO ai_results VALUES(2,NULL,NULL,NULL,'existing translation','independent shortening error','2000-01-01');
    """)
    db.execute('BEGIN IMMEDIATE')
    assert migrate_results(db) == {'reset_ambiguous_title_errors':1}
    db.commit()
    assert db.execute('SELECT summary,title_summary_error,title_summary_error_at FROM ai_results WHERE article_id=1').fetchone() == ('keep summary',None,None)
    assert db.execute('SELECT title_translation,title_summary_error FROM ai_results WHERE article_id=2').fetchone() == ('existing translation','independent shortening error')
    db.execute("UPDATE ai_results SET title_summary_error='new failure' WHERE article_id=1")
    db.commit()
    db.execute('BEGIN IMMEDIATE')
    assert migrate_results(db) is None
    db.commit()
    assert db.execute('SELECT title_summary_error FROM ai_results WHERE article_id=1').fetchone()[0] == 'new failure'
    db.close()
