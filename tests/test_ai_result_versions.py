import sqlite3
import pytest
from ai_result_updates import ensure_result_versions, read_updates, result_state, track_generation


@pytest.fixture
def store(tmp_path):
    conn = sqlite3.connect(tmp_path / 'results.db')
    conn.execute('CREATE TABLE ai_results(article_id INTEGER PRIMARY KEY, summary TEXT, translation TEXT, '
                 'summary_error TEXT, summary_error_at TEXT, title_summary TEXT, title_summary_error TEXT, translation_updated_at TEXT, title_translation TEXT, translated_title_summary TEXT)')
    ensure_result_versions(conn)
    conn.commit()
    yield conn
    conn.close()


def updates(conn, cursor=''):
    try:
        return read_updates(conn, cursor)
    finally:
        conn.rollback()


def test_summary_and_translation_advance_independently_and_replay(store):
    cursor = updates(store)['cursor']
    store.execute("INSERT INTO ai_results(article_id,summary) VALUES(1,'summary')")
    store.commit()
    first = updates(store, cursor)
    assert first['items'] == [{'id': 1, 'version': 1}]
    store.execute("UPDATE ai_results SET translation='translation' WHERE article_id=1")
    store.commit()
    assert updates(store, first['cursor'])['items'] == [{'id': 1, 'version': 2}]
    assert updates(store, cursor)['items'] == [{'id': 1, 'version': 2}]
    assert updates(store, cursor) == updates(store, cursor)


def test_rollback_never_publishes_and_unchanged_write_has_no_event(store):
    cursor = updates(store)['cursor']
    store.execute("INSERT INTO ai_results(article_id,summary) VALUES(1,'not committed')")
    store.rollback()
    assert updates(store, cursor)['items'] == []
    store.execute("INSERT INTO ai_results(article_id,summary) VALUES(1,'committed')")
    store.commit()
    cursor = updates(store)['cursor']
    store.execute('UPDATE ai_results SET summary=summary')
    store.commit()
    assert updates(store, cursor)['items'] == []


def test_pagination_catches_newer_write_during_catchup(store):
    cursor = updates(store)['cursor']
    store.executemany("INSERT INTO ai_results(article_id,summary) VALUES(?, 'same instant')", [(i,) for i in range(1, 1002)])
    store.commit()
    first = updates(store, cursor)
    assert len(first['items']) == 500 and first['has_more']
    store.execute("UPDATE ai_results SET summary='updated while paging' WHERE article_id=1")
    store.commit()
    second = updates(store, first['cursor'])
    third = updates(store, second['cursor'])
    assert second['has_more'] and not third['has_more']
    assert third['items'] == [{'id':1001,'version':1001},{'id':1,'version':1002}]
    assert len({i['id'] for page in (first,second,third) for i in page['items']}) == 1001


def test_delete_and_fulltext_invalidation_publish(store):
    store.execute("INSERT INTO ai_results(article_id,translation) VALUES(1,'cached')")
    store.commit()
    cursor = updates(store)['cursor']
    store.execute('UPDATE ai_results SET translation=NULL WHERE article_id=1')
    store.commit()
    invalidated = updates(store,cursor)
    assert invalidated['items'] == [{'id':1,'version':2}]
    store.execute('DELETE FROM ai_results WHERE article_id=1')
    store.commit()
    assert updates(store,invalidated['cursor'])['items'] == [{'id':1,'version':3}]


def test_restart_and_schema_reentry_keep_cursor(store):
    cursor = updates(store)['cursor']
    ensure_result_versions(store)
    store.commit()
    assert updates(store)['cursor'] == cursor
    assert updates(store,'other-database|1')['reset']
    assert updates(store,cursor.split('|')[0]+'|999')['reset']
    with pytest.raises(ValueError):
        updates(store,'invalid')


def test_generation_state_is_real_and_cleared_after_error():
    @track_generation('summary')
    def generate(article_id):
        assert result_state(article_id,'summary',{}) == 'generating'
        raise TimeoutError()
    assert result_state(7,'summary',{}) == 'absent'
    with pytest.raises(TimeoutError):
        generate(7)
    assert result_state(7,'summary',{}) == 'absent'
    assert result_state(7,'summary',{'summary_error':'failed'}) == 'failed'
    assert result_state(7,'summary',{'summary':'available'}) == 'available'


def test_real_api_keeps_authentication_and_permission_gate(tmp_path,monkeypatch):
    import models
    import web_server
    path = tmp_path/'api.db'
    with sqlite3.connect(path) as conn:
        conn.execute('CREATE TABLE articles(id INTEGER PRIMARY KEY)')
        conn.execute('INSERT INTO articles VALUES(1)')
    monkeypatch.setattr(web_server,'NEWS_DB',str(path))
    monkeypatch.setattr(models,'get_user',lambda user_id:{'id':user_id,'role':'user'})
    monkeypatch.setattr(models,'record_access',lambda user_id:None)
    settings={'share_ai_results':1,'share_last_check_ok':1,'share_last_check_revision':2,
              'share_current_config_revision':2,'share_view_summary':1,'share_view_translation':0,'share_view_title':0}
    monkeypatch.setattr(web_server,'ai_access',lambda user_id: {'permissions':{'summary':True,'translation':False,'title_shortening':False},'view_summary':not settings.get('share_suspended'),'view_translation':False,'view_title_translation':False,'view_title_shortening':False})
    client=web_server.app.test_client()
    headers={'Authorization':'Bearer '+web_server.create_token(7,'user')}
    assert client.get('/ai/result-updates').status_code == 401
    initial=client.get('/ai/result-updates',headers=headers)
    assert initial.status_code == 200
    cursor=initial.get_json()['cursor']
    assert web_server._save_ai_result(1,summary='shared',translation='<p>hidden</p>',title_summary='restricted title')
    page=client.get('/ai/result-updates',headers=headers,query_string={'since':cursor}).get_json()
    assert page['items'] and set(page['items'][0]) == {'id','version'}
    assert 'shared' not in str(page)
    result=client.get('/ai/result/1',headers=headers)
    assert result.get_json()['summary']=='shared' and 'translation' not in result.get_json()
    assert result.headers['Cache-Control']=='private, no-store'
    assert 'title_summary' not in result.get_json()
    assert 'title_summary_by_user_id' not in result.get_json()
    settings['share_suspended']=1
    result=client.get('/ai/result/1',headers=headers).get_json()
    assert 'summary' not in result and result['access']['view_summary'] is False
    assert client.get('/ai/result/999',headers=headers).status_code == 404
    replacement=tmp_path/'replacement.db'
    sqlite3.connect(replacement).close()
    replacement.replace(path)
    reset=client.get('/ai/result-updates',headers=headers,query_string={'since':page['cursor']})
    assert reset.status_code==200 and reset.get_json()['reset'] is True


def test_strict_read_failure_is_an_http_error(tmp_path,monkeypatch):
    import models
    import web_server
    path=tmp_path/'api.db'
    sqlite3.connect(path).close()
    monkeypatch.setattr(web_server,'NEWS_DB',str(path))
    monkeypatch.setattr(models,'get_user',lambda user_id:{'id':user_id,'role':'user'})
    monkeypatch.setattr(models,'record_access',lambda user_id:None)
    monkeypatch.setattr(web_server,'ai_access',lambda uid:{'permissions':{'summary':True,'translation':False,'title_shortening':False}})
    assert web_server._init_ai_results_table()
    with sqlite3.connect(path) as conn:
        conn.execute('DROP TABLE ai_results')
    headers={'Authorization':'Bearer '+web_server.create_token(7,'user')}
    assert web_server.app.test_client().get('/ai/result/1',headers=headers).status_code == 500


def test_existing_update_trigger_upgrades_without_resetting_cursor(store):
    store.execute("INSERT INTO ai_results(article_id,summary) VALUES(1,'preserved')")
    store.commit()
    cursor = updates(store)['cursor']
    store.execute('ALTER TABLE ai_results ADD COLUMN translated_title_summary_error TEXT')
    store.execute('ALTER TABLE ai_results ADD COLUMN translated_title_summary_error_at TEXT')
    ensure_result_versions(store)
    store.commit()
    assert updates(store)['cursor'] == cursor
    store.execute("UPDATE ai_results SET translated_title_summary_error='failed', translated_title_summary_error_at='2000-01-01 00:00:00' WHERE article_id=1")
    store.commit()
    failure = updates(store,cursor)
    assert failure['items'] == [{'id':1,'version':2}]
    store.execute("UPDATE ai_results SET translated_title_summary_error_at='2000-01-02 00:00:00' WHERE article_id=1")
    store.commit()
    assert updates(store,failure['cursor'])['items'] == [{'id':1,'version':3}]
