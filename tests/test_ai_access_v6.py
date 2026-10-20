"""The server AI contract, including trusted history and revocation boundaries."""
import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

import ai_access
import models
import web_server


@pytest.fixture
def app(tmp_path, monkeypatch):
    models.close_db()
    monkeypatch.setattr(models, 'DB_FILE', tmp_path / 'accounts.db')
    monkeypatch.setattr(web_server, 'NEWS_DB', str(tmp_path / 'news.db'))
    db = models.get_db()
    admin = models.create_user('admin@example.test', 'pw', 'admin', 'admin')['id']
    user = models.create_user('user@example.test', 'pw', 'reader')['id']
    with sqlite3.connect(web_server.NEWS_DB) as news:
        news.execute('CREATE TABLE articles(id INTEGER PRIMARY KEY,title TEXT,original_title TEXT,body_html TEXT,original_body_html TEXT,title_source TEXT,title_updated_at TEXT)')
        news.execute("INSERT INTO articles(id,title,body_html) VALUES(1,'Original headline','<p>Original body</p>')")
    assert web_server._init_ai_results_table()
    client = web_server.app.test_client()
    headers = lambda uid: {'Authorization': 'Bearer ' + web_server.create_token(uid, 'admin' if uid == admin else 'user')}
    yield client, headers(admin), headers(user), user, admin
    models.close_db()


def authorize(user, **grants):
    ai_access.set_permissions(user, grants)


def test_permission_helper_rejects_unknown_or_non_boolean_keys(app):
    _, _, _, user, _ = app
    for updates in ({'summary; DROP TABLE users': True}, {'summary': 1}, {}):
        with pytest.raises(ValueError):
            ai_access.set_permissions(user, updates)
    assert not any(ai_access.permissions(user).values())


def test_completed_account_migration_does_not_take_write_lock(app):
    db = models.get_db()
    statements = []
    db.set_trace_callback(statements.append)
    try:
        ai_access.migrate_accounts(db, models.SCHEMA_SQL)
    finally:
        db.set_trace_callback(None)
    assert not any('BEGIN IMMEDIATE' in statement for statement in statements)


def test_new_users_closed_admins_open(app):
    client, admin_headers, headers, user, admin = app
    assert not any(ai_access.permissions(user).values())
    assert all(ai_access.permissions(admin).values())
    data = client.get('/settings', headers=headers).get_json()
    assert not any(data[key] for key in ai_access.PREFERENCES)
    assert not data['access']['services']['tts']
    assert not any(key.startswith('share_') for key in data)
    assert not models.get_db().execute("SELECT 1 FROM sqlite_master WHERE name='ai_configs'").fetchone()


def test_atomic_unauthorized_enable_cannot_save_theme(app):
    client, _, headers, user, _ = app
    response = client.put('/settings', headers=headers, json={'view_summary': True, 'theme_preference': 'dark'})
    assert response.status_code == 403
    assert response.get_json() == {'code': 'AI_PERMISSION_DENIED', 'error': '无权限，请联系管理员'}
    assert (models.get_user_settings(user) or {}).get('theme_preference', 'system') == 'system'
    assert client.put('/settings', headers=headers, json={'permissions': {'summary': True}}).status_code == 400


def test_admin_grants_and_revocation_do_not_reenable(app):
    client, admin_headers, headers, user, admin = app
    path = f'/auth/users/{user}/ai-permissions'
    assert client.put(path, headers=headers, json={'summary': True}).status_code == 403
    assert client.put(path, headers=admin_headers, json={'summary': 1}).status_code == 400
    assert client.put(path, headers=admin_headers, json={'summary': True}).status_code == 200
    assert not ai_access.access(user)['view_summary']
    assert client.put('/settings', headers=headers, json={'view_summary': True}).status_code == 200
    assert ai_access.access(user)['view_summary']
    assert client.put(path, headers=admin_headers, json={'summary': False}).status_code == 200
    assert not ai_access.access(user)['view_summary']
    client.put(path, headers=admin_headers, json={'summary': True})
    assert not ai_access.access(user)['view_summary']
    assert client.put(f'/auth/users/{admin}/ai-permissions', headers=admin_headers, json={'summary': False}).status_code == 400


@pytest.mark.parametrize('path,method', [('/ai/config','get'),('/ai/config','put'),('/ai/config/client','get'),('/ai/chat','post'),('/ai/test-connection','post'),('/ai/result/1','post')])
def test_retired_endpoints_never_call_provider(app, monkeypatch, path, method):
    client, _, headers, _, _ = app
    monkeypatch.setattr(web_server, 'AIService', lambda **kw: pytest.fail('personal provider called'))
    response = getattr(client, method)(path, headers=headers, json={'summary':'forged','api_key':'sk-personal'})
    assert response.status_code == 410
    assert response.get_json()['code'] == 'PERSONAL_AI_REMOVED'
    assert not web_server._get_ai_result(1)


def test_tts_permission_and_service_availability_are_distinct(app):
    client, _, headers, user, _ = app
    assert client.put('/settings', headers=headers, json={'view_tts': True}).status_code == 403
    authorize(user, tts=True)
    response = client.put('/settings', headers=headers, json={'view_tts': True})
    assert response.status_code == 409
    assert response.get_json()['code'] == 'AI_SERVICE_UNAVAILABLE'


def test_field_level_access_and_private_result_headers(app):
    client, _, headers, user, _ = app
    assert web_server._save_ai_result(1, summary='Server summary', translation=json.dumps({'title':'Secret translated title','html':'<p>Translation</p>'}), title_summary='Short original')
    web_server._save_article_title_update(1, 'Full translated headline', 'translation')
    with web_server._news_db_conn() as db:
        db.execute("UPDATE ai_results SET translated_title_summary='Short translation' WHERE article_id=1")
        db.commit()
    assert client.get('/ai/result/1', headers=headers).status_code == 403
    authorize(user, summary=True, title_shortening=True)
    models.set_user_settings(user, view_summary=1, view_title_shortening=1)
    response = client.get('/ai/result/1', headers=headers)
    data = response.get_json()
    assert data['summary'] == 'Server summary'
    assert data['title_summary'] == 'Short original'
    assert 'translation' not in data and 'title_translation' not in data and 'translated_title_summary' not in data
    assert 'summary_model' not in data and 'summary_source' not in data
    assert response.headers['Cache-Control'] == 'private, no-store'
    authorize(user, translation=True)
    models.set_user_settings(user, view_translation=1)
    data = client.get('/ai/result/1', headers=headers).get_json()
    assert json.loads(data['translation']) == {'html':'<p>Translation</p>'}
    assert 'title_translation' not in data
    models.set_user_settings(user, view_title_translation=1)
    data = client.get('/ai/results?ids=1', headers=headers).get_json()['items']['1']
    assert data['title_translation'] == 'Full translated headline'
    assert data['translated_title_summary'] == 'Short translation'
    with sqlite3.connect(web_server.NEWS_DB) as db:
        assert db.execute('SELECT title FROM articles WHERE id=1').fetchone()[0] == 'Original headline'


def test_trusted_legacy_result_respects_current_grants_and_sanitizes_html(app):
    client, _, headers, user, _ = app
    with web_server._news_db_conn() as db:
        db.execute(
            "INSERT INTO ai_results(article_id,summary,summary_source,translation,translation_source) "
            "VALUES(1,'Legacy summary','legacy',?,'legacy')",
            (json.dumps({'html': '<p>Old translation</p><img src="x" onerror="alert(1)"><script>alert(2)</script>'}),),
        )
        db.commit()
    assert client.get('/ai/result/1', headers=headers).status_code == 403
    authorize(user, summary=True, translation=True)
    models.set_user_settings(user, view_summary=1, view_translation=1)
    data = client.get('/ai/result/1', headers=headers).get_json()
    assert data['summary'] == 'Legacy summary'
    translated = json.loads(data['translation'])['html']
    assert '<p>Old translation</p>' in translated
    assert '<script' not in translated and 'onerror' not in translated
    authorize(user, summary=False, translation=False)
    assert client.get('/ai/result/1', headers=headers).status_code == 403


def test_daily_master_filters_old_notifications_and_recipients(app, monkeypatch):
    client, _, headers, user, _ = app
    authorize(user, daily_digest=True)
    models.set_user_settings(user, view_daily_digest=1, daily_summary_inapp_enabled=1)
    models.add_notification(user, 'daily_summary', 'Daily', 'Protected digest')
    models.add_notification(user, 'general', 'Public notice', 'Notice')
    assert len(client.get('/notifications', headers=headers).get_json()['items']) == 2
    assert user in models.get_daily_summary_inapp_user_ids()
    models.set_user_settings(user, view_daily_digest=0)
    data=client.get('/notifications', headers=headers).get_json()
    assert [item['type'] for item in data['items']] == ['general']
    assert data['unread'] == 1
    assert user not in models.get_daily_summary_inapp_user_ids()
    assert client.get('/ai/daily-summary/today', headers=headers).status_code == 403
    assert models.get_user_settings(user)['daily_summary_inapp_enabled'] == 1


def test_account_upgrade_backfills_once_including_no_settings(tmp_path):
    # Use the released schema to model a real upgrade, not a copy of the new implementation.
    schema = (Path(__file__).parent/'fixtures/legacy_accounts_v5.sql').read_text()
    db=sqlite3.connect(tmp_path/'legacy.db'); db.row_factory=sqlite3.Row
    db.executescript(schema)
    db.execute("INSERT INTO users(id,email,password,nickname,role) VALUES(1,'a@x','hash','admin','admin'),(2,'b@x','hash','reader','user'),(3,'c@x','hash','unset','user')")
    db.execute('INSERT INTO user_settings(user_id,share_view_summary,share_view_title,auto_summary_enabled) VALUES(1,1,1,1)')
    db.execute("INSERT INTO ai_configs(user_id,api_key) VALUES(2,'sk-legacy-secret')");db.commit()
    ai_access.migrate_accounts(db, models.SCHEMA_SQL)
    assert db.execute('SELECT COUNT(*) FROM ai_permissions WHERE summary=1 AND tts=1').fetchone()[0] == 3
    assert db.execute('SELECT view_summary,view_title_translation,view_daily_digest FROM user_settings WHERE user_id=1').fetchone()[:] == (1,1,1)
    assert db.execute('SELECT view_title_shortening FROM user_settings WHERE user_id=1').fetchone()[0] == 1
    assert db.execute('SELECT view_title_shortening FROM user_settings WHERE user_id=3').fetchone()[0] == 0
    assert db.execute('SELECT view_daily_digest FROM user_settings WHERE user_id=3').fetchone()[0] == 1
    assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='ai_configs'").fetchone()
    assert not any(row[1].startswith('share_') for row in db.execute('PRAGMA table_info(user_settings)'))
    db.execute('UPDATE ai_permissions SET summary=0 WHERE user_id=2')
    db.execute("INSERT INTO users(id,email,password,nickname) VALUES(4,'new@x','hash','new')");db.commit()
    ai_access.migrate_accounts(db, models.SCHEMA_SQL)
    assert db.execute('SELECT summary FROM ai_permissions WHERE user_id=2').fetchone()[0] == 0
    assert db.execute('SELECT summary FROM ai_permissions WHERE user_id=4').fetchone()[0] == 0
    db.close()


def test_result_upgrade_preserves_legacy_content_and_restores_public_original_title(tmp_path):
    from server_ai_store import migrate_results
    db=sqlite3.connect(tmp_path/'legacy-news.db')
    db.executescript("CREATE TABLE ai_results(article_id INTEGER PRIMARY KEY,summary TEXT,translation TEXT); INSERT INTO ai_results VALUES(1,'browser content','legacy translation'); CREATE TABLE articles(id INTEGER PRIMARY KEY,title TEXT,original_title TEXT,title_source TEXT,title_updated_at TEXT,body_html TEXT,original_body_html TEXT); INSERT INTO articles VALUES(1,'AI title','Original','translation',NULL,'Current body','Earlier body');")
    db.execute('BEGIN IMMEDIATE');migrate_results(db);db.commit()
    assert db.execute('SELECT summary,translation,title_translation FROM ai_results').fetchone() == ('browser content','legacy translation','AI title')
    assert db.execute('SELECT summary FROM ai_results_legacy_v5').fetchone()[0] == 'browser content'
    assert db.execute('SELECT title,body_html FROM articles').fetchone() == ('Original','Current body')
    db.execute("UPDATE ai_results SET summary='New server summary',summary_source='server' WHERE article_id=1");db.commit()
    db.execute('BEGIN IMMEDIATE');migrate_results(db);db.commit()
    assert db.execute('SELECT summary FROM ai_results').fetchone()[0] == 'New server summary'
    assert db.execute('SELECT summary FROM ai_results_legacy_v5').fetchone()[0] == 'browser content'
    db.close()


def test_global_generation_is_not_a_user_preference(app, monkeypatch):
    client, admin_headers, headers, user, _ = app
    assert client.put('/admin/ai-generation', headers=headers, json={'auto_summary_enabled': True}).status_code == 403
    assert client.put('/admin/ai-generation', headers=admin_headers, json={'auto_summary_enabled': True}).status_code == 200
    assert ai_access.generation_settings()['auto_summary_enabled']
    assert client.put('/settings', headers=admin_headers, json={'auto_summary_enabled': False}).status_code == 400
    config={'enabled':1,'api_key':'system-only','endpoint':'https://example.test/v1','model':'test'}
    monkeypatch.setattr(web_server,'get_system_ai_config',lambda:config)
    assert web_server._system_auto_config('auto_summary_enabled')['api_key'] == 'system-only'
    assert 'api_key' not in client.get('/settings',headers=headers).get_data(as_text=True)


def test_title_jobs_keep_language_variants_separate_and_reuse_results(app, monkeypatch):
    calls=[]
    class TitleService:
        def translate_title(self, *args, **kwargs):
            calls.append('translate')
            return '这是一篇介绍服务端文章标题处理方式以及翻译和标题简写独立权限与缓存规则的完整中文标题'
        def chat(self, *args, **kwargs):
            calls.append('original-short')
            return json.dumps({'title':'Original English headline shortened'})
        def summarize_title(self, *args, **kwargs):
            calls.append('translated-short')
            return json.dumps({'title':'服务端标题处理方式'})
    monkeypatch.setattr(web_server,'_title_service',lambda config:TitleService())
    original='An original English headline describing the server article title processing workflow and its independent permission controls in detail'
    with web_server._news_db_conn() as db:
        db.execute('UPDATE articles SET title=? WHERE id=1',(original,))
        db.commit()
    config={'auto_translate_title':True,'auto_title_summary_enabled':True}
    assert web_server._process_article_title({'id':1,'title':original},config)
    result=web_server._get_ai_result(1)
    assert result['title_translation'].startswith('这是一篇')
    assert result['title_summary'] == 'Original English headline shortened'
    assert result['translated_title_summary'] == '服务端标题处理方式'
    assert result['title_translation_source'] == result['title_summary_source'] == result['translated_title_summary_source'] == 'server'
    assert not web_server._process_article_title({'id':1,'title':original},config)
    assert calls == ['translate','original-short','translated-short']


def test_original_metadata_does_not_expose_derived_titles():
    assert web_server._original_article_meta({'title':'AI title','original_title':'Raw title','title_source':'translation'})['title']=='Raw title'
    assert web_server._original_article_meta({'title':'AI title','title_source':'title_summary'})['title']=='无标题文章'


def test_disabling_digest_preserves_channel_preferences_without_grant(app):
    client,_,headers,user,_=app
    response=client.put('/settings',headers=headers,json={'daily_summary_inapp_enabled':True})
    assert response.status_code==200
    assert not ai_access.daily_enabled(user)
