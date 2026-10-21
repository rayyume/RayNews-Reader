"""TTS provider contract, listening text, authorization and disposable spool."""
import base64
import json
import sqlite3
import threading
import time

import pytest
import requests

import ai_access
import models
import tts_cache
import tts_routes
import tts_service
import web_server
from tts_service import TTSError, listening_text

MP3 = b"ID3" + b"\x00" * 64
ENGLISH_TITLE = 'A detailed original English headline about changing transportation in modern cities'
ENGLISH_BODY = 'The original English article describes new transportation technology.'
FULL_CHINESE_TITLE = '一篇详细介绍现代城市交通技术及其影响的完整中文译名'
SHORT_CHINESE_TITLE = '城市交通新技术'
CHINESE_BODY = '已经翻译完成的中文正文，保留具体数字 1.3%。'


@pytest.fixture
def app(tmp_path, monkeypatch):
    models.close_db()
    monkeypatch.setattr(models, 'DB_FILE', tmp_path / 'accounts.db')
    monkeypatch.setattr(web_server, 'NEWS_DB', str(tmp_path / 'news.db'))
    monkeypatch.setattr(tts_service, 'assert_ai_endpoint_url', lambda url: url)
    models.get_db()
    admin = models.create_user('admin@tts.test', 'pw', 'Admin', 'admin')['id']
    user = models.create_user('user@tts.test', 'pw', 'Reader')['id']
    with sqlite3.connect(web_server.NEWS_DB) as db:
        db.execute('CREATE TABLE articles(id INTEGER PRIMARY KEY,title TEXT,original_title TEXT,title_source TEXT,body_html TEXT,source TEXT,thumb TEXT,date TEXT,time TEXT,has_full_content INTEGER)')
        db.execute("INSERT INTO articles VALUES(1,'原始标题','旧快照',NULL,'<h1>原始标题</h1><p>正文包含 1.3% 的增长。</p>','科技观察','','2026-10-20','09:30',1)")
        db.execute("INSERT INTO articles VALUES(2,'第二篇',NULL,NULL,'<p>第二篇正文。</p>','科技观察','','2026-10-20','09:31',0)")
    assert web_server._init_ai_results_table()
    cache = tts_cache.TTSCache(tmp_path / 'spool')
    monkeypatch.setattr(cache, '_clean_loop', lambda: None)
    monkeypatch.setattr(tts_routes, 'cache', cache)
    monkeypatch.setattr(tts_cache, 'cache', cache)
    monkeypatch.setattr(tts_cache, 'synthesize', lambda config, text, target, **kw: target.write_bytes(MP3))
    headers = lambda uid: {'Authorization': 'Bearer ' + web_server.create_token(uid, 'admin' if uid == admin else 'user')}
    client = web_server.app.test_client()
    yield client, headers(admin), headers(user), user, cache
    cache.pool.shutdown(wait=True)
    models.close_db()


def enable(client, admin, user):
    response = client.put('/admin/tts-config', headers=admin, json={
        'enabled':True,'endpoint':'https://tts.example.test/v1','username':'reader','password':'secret'})
    assert response.status_code == 200
    ai_access.set_permissions(user, {'tts':True})
    models.set_user_settings(user, view_tts=1)


def ready(cache, job_id):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        job = cache.get(job_id)
        if job['status'] not in {'queued','generating'}:
            return job
        time.sleep(.005)
    pytest.fail('TTS worker did not finish')


def test_new_tts_config_requires_own_endpoint_and_username(app):
    client, admin, _, _, _ = app
    assert tts_service.DEFAULT_CONFIG['endpoint'] == ''
    assert tts_service.DEFAULT_CONFIG['username'] == ''
    assert client.put('/admin/tts-config', headers=admin,
                      json={'enabled':True,'password':'secret'}).status_code == 400
    assert client.put('/admin/tts-config', headers=admin,
                      json={'enabled':False,'voice':'zf_094'}).status_code == 200


def test_tts_capacity_and_workers_can_be_configured(tmp_path, monkeypatch):
    monkeypatch.setenv('TTS_CACHE_MAX_MB', '128')
    monkeypatch.setenv('TTS_WORKERS', '4')
    cache = tts_cache.TTSCache(tmp_path / 'spool')
    assert cache.max_bytes == 128 * 1024 * 1024
    assert cache.pool._max_workers == 4
    cache.pool.shutdown()


def test_listening_text_preserves_facts_and_removes_reading_noise():
    text, summary = listening_text('新闻标题', '''
      <h1>新闻标题</h1><script>evil()</script><style>red</style>
      <p>据<a href="https://example.test/story">路透社</a>报道，营收增长 <strong>1.3%</strong>，型号为 GPT-4.1。</p>
      <figure><img src="https://image.test/photo.jpg" alt="不要读图片"><figcaption>图源：摄影师</figcaption></figure>
      <p><a href="https://example.test">点击查看原文</a> https://example.test/path</p>
      <ul><li>第一项事实</li><li>第二项事实</li></ul><pre>secret code()</pre>
      <p>引述：“这不是增长。”<sup><a href="#note">1</a></sup></p>
      ''', '**摘要**：核心事实[1]。')
    assert summary
    assert text.count('新闻标题') == 1
    assert text.index('内容摘要') < text.index('核心事实') < text.index('以下是正文') < text.index('据路透社报道')
    for noise in ['https', 'evil', 'red', '不要读图片', '摄影师', '点击查看', '[1]', '**', 'secret code']:
        assert noise not in text
    for fact in ['1.3%', 'GPT-4.1', '这不是增长', '第一项事实。', '第二项事实。', '此处包含代码']:
        assert fact in text


def test_table_markdown_and_empty_content():
    text, _ = listening_text('标题', '<table><tr><td>营收</td><td>10 亿元</td></tr></table>')
    assert '营收，10 亿元。' in text
    text, _ = listening_text('标题', '<table><tr><td>A</td><td>B</td><td>C</td></tr></table>\n![图片](x.jpg)\n[机构](https://x.test)')
    assert '详细数据见原文表格' in text and '机构' in text and '图片' not in text
    with pytest.raises(TTSError, match='暂无可供'):
        listening_text('标题', '<img src="x">', '已有摘要也不能冒充全文')


def test_config_is_admin_only_secrets_preserved_and_auth_modes_exclusive(app):
    client, admin, headers, user, _ = app
    assert client.get('/admin/tts-config', headers=headers).status_code == 403
    response = client.put('/admin/tts-config', headers=admin, json={
        'enabled':True,'endpoint':'https://tts.example.test/v1','username':'reader','password':'private-password'})
    assert response.status_code == 200
    assert response.json['has_password'] and 'password' not in response.json and 'authorization' not in response.json
    assert response.headers['Cache-Control'] == 'private, no-store'
    client.put('/admin/tts-config', headers=admin, json={'password':'','speed':1.1})
    assert tts_service.get_config()['password'] == 'private-password'
    response = client.put('/admin/tts-config', headers=admin, json={'auth_type':'authorization','authorization':'Basic custom'})
    assert response.json['has_authorization']
    response = client.put('/admin/tts-config', headers=admin, json={'enabled':False,'clear_password':True,'clear_authorization':True})
    assert not response.json['has_password'] and not response.json['has_authorization']


@pytest.mark.parametrize('payload', [
    {'speed':0}, {'speed':4.1}, {'speed':True}, {'enabled':1}, {'stream':'true'},
    {'auth_type':'bearer'}, {'password':'x\r\nHeader: secret'},
    {'endpoint':'https://tts.test/v1?key=secret'}, {'username':'name:password'},
    *({field:audio_format} for field in ('response_format','download_format')
      for audio_format in ('aac','opus','flac','wav','pcm')),
    {'volume_multiplier':-1}, {'volume_multiplier':True},
    {'normalization_options':{'normalize':True}}, {'api_key':'unsupported'},
])
def test_invalid_config_is_atomic(app, payload):
    client, admin, _, _, _ = app
    before = tts_service.get_config()
    assert client.put('/admin/tts-config', headers=admin, json=payload).status_code == 400
    assert tts_service.get_config() == before


def test_permissions_generation_dedup_audio_and_revocation(app, monkeypatch):
    client, admin, headers, user, cache = app
    assert client.post('/ai/tts/1').status_code == 401
    assert client.post('/ai/tts/1', headers=headers).status_code == 403
    enable(client, admin, user)
    ai_access.set_permissions(user, {'summary':True})
    models.set_user_settings(user, view_summary=1)
    web_server._save_ai_result(1, summary='重要摘要')
    calls = []
    def generate(config, text, target, **kw):
        calls.append(text)
        target.write_bytes(MP3)
    monkeypatch.setattr(tts_cache, 'synthesize', generate)
    result = client.post('/ai/tts/1', headers=headers, json={'input':'伪造内容','endpoint':'https://evil.test'}).json
    assert result['has_summary']
    assert result['article']['title'] == '原始标题'
    assert ready(cache, result['id'])['status'] == 'ready'
    again = client.post('/ai/tts/1', headers=headers).json
    assert again['id'] == result['id'] and len(calls) == 1
    assert '伪造内容' not in calls[0] and '重要摘要' in calls[0]
    path = '/ai/tts/audio/' + result['id']
    response = client.get(path, headers=headers)
    assert response.status_code == 200 and response.data == MP3
    assert response.headers['Cache-Control'] == 'private, no-store'
    response.close()
    response = client.get(path, headers={**headers, 'Range':'bytes=0-9'})
    assert response.status_code == 206 and response.data == MP3[:10]
    response.close()
    ai_access.set_permissions(user, {'summary':False})
    assert client.get(path, headers=headers).status_code == 403
    assert client.get('/ai/tts/jobs/' + result['id'], headers=headers).status_code == 403
    plain = client.post('/ai/tts/1', headers=headers).json
    assert plain['id'] != result['id'] and not plain['has_summary']
    ready(cache, plain['id'])
    ai_access.set_permissions(user, {'tts':False})
    assert client.get('/ai/tts/audio/' + plain['id'], headers=headers).status_code == 403


def test_service_availability_and_preference(app):
    client, admin, headers, user, _ = app
    ai_access.set_permissions(user, {'tts':True})
    assert client.put('/settings', headers=headers, json={'view_tts':True}).status_code == 409
    client.put('/admin/tts-config', headers=admin, json={
        'enabled':True,'endpoint':'https://tts.example.test/v1','username':'reader','password':'secret'})
    assert client.put('/settings', headers=headers, json={'view_tts':True}).status_code == 200
    assert client.get('/settings', headers=headers).json['access']['view_tts']
    client.put('/settings', headers=headers, json={'view_tts':False})
    assert client.post('/ai/tts/1', headers=headers).status_code == 403


def test_changed_article_and_configuration_expire_old_audio(app):
    client, admin, headers, user, cache = app
    enable(client, admin, user)
    result=client.post('/ai/tts/1', headers=headers).json
    ready(cache,result['id'])
    with sqlite3.connect(web_server.NEWS_DB) as db:
        db.execute("UPDATE articles SET body_html='<p>更新后的内容</p>' WHERE id=1")
    assert client.get('/ai/tts/audio/'+result['id'],headers=headers).status_code == 410
    second=client.post('/ai/tts/1',headers=headers).json
    ready(cache,second['id'])
    client.put('/admin/tts-config',headers=admin,json={'speed':1.2})
    assert client.get('/ai/tts/audio/'+second['id'],headers=headers).status_code == 410


def test_tts_article_metadata_uses_the_administrator_source_label(app):
    from source_categories import init_source_categories, update_source_category
    client, admin, headers, user, cache = app
    enable(client, admin, user)
    with sqlite3.connect(web_server.NEWS_DB) as conn:
        conn.row_factory = sqlite3.Row
        init_source_categories(conn)
        update_source_category(conn, '科技观察', 'Tech', '管理员核实来源', status='manual')
    result = client.post('/ai/tts/1', headers=headers).json
    assert result['article']['display_source'] == '管理员核实来源'
    assert result['article']['source_curated'] is True
    assert result['article']['source_labels_read_at'] > 0
    assert ready(cache, result['id'])['status'] == 'ready'


def _translated_article(app, monkeypatch, *, with_translation=True, translation_format='json'):
    client, admin, headers, user, cache = app
    enable(client, admin, user)
    with sqlite3.connect(web_server.NEWS_DB) as db:
        db.execute('UPDATE articles SET title=?, original_title=NULL, body_html=? WHERE id=2',
                   (ENGLISH_TITLE, '<h1>'+ENGLISH_TITLE+'</h1><p>'+ENGLISH_BODY+'</p>'))
    ai_access.set_permissions(user, {'translation':True, 'title_shortening':True})
    models.set_user_settings(user, view_translation=1, view_title_translation=1, view_title_shortening=1)
    html = '<h1>'+FULL_CHINESE_TITLE+'</h1><p>'+CHINESE_BODY+'</p>'
    translation = json.dumps({'title':FULL_CHINESE_TITLE,'html':html}) if translation_format=='json' else html
    assert web_server._save_ai_result(2, title_summary='Short English headline',
                                     translation=translation if with_translation else None)
    assert web_server._save_article_title_update(2, FULL_CHINESE_TITLE, 'translation')
    with web_server._news_db_conn() as db:
        db.execute('UPDATE ai_results SET translated_title_summary=? WHERE article_id=2', (SHORT_CHINESE_TITLE,))
        db.commit()
    spoken = []
    def synthesize(config, text, target, **kw):
        spoken.append(text)
        target.write_bytes(MP3)
    monkeypatch.setattr(tts_cache, 'synthesize', synthesize)
    return spoken, translation


@pytest.mark.parametrize('title_translation,shortening,body_translation,title', [
    (True,True,True,SHORT_CHINESE_TITLE), (True,False,True,FULL_CHINESE_TITLE),
    (False,True,True,'Short English headline'), (False,False,True,ENGLISH_TITLE),
    (True,True,False,SHORT_CHINESE_TITLE), (False,True,False,'Short English headline'),
    (False,False,False,ENGLISH_TITLE),
])
def test_tts_matches_display_title_and_uses_available_translation(app, monkeypatch, title_translation, shortening, body_translation, title):
    client, _, headers, user, cache = app
    spoken, _ = _translated_article(app, monkeypatch)
    models.set_user_settings(user, view_title_translation=title_translation,
                             view_title_shortening=shortening, view_translation=body_translation)
    response = client.post('/ai/tts/2', headers=headers)
    assert response.status_code in {200,202}
    data = response.json
    assert ready(cache, data['id'])['status'] == 'ready'
    assert data['article']['title'] == title
    assert spoken[0].startswith(title)
    assert data['has_translation'] is body_translation
    assert (CHINESE_BODY if body_translation else ENGLISH_BODY) in spoken[0]
    assert (ENGLISH_BODY if body_translation else CHINESE_BODY) not in spoken[0]
    if shortening:
        assert ENGLISH_TITLE not in spoken[0]
    if title_translation and shortening and body_translation:
        assert FULL_CHINESE_TITLE not in spoken[0]
        assert spoken[0].count(SHORT_CHINESE_TITLE) == 1
    # Client-supplied content cannot replace the authorized server result.
    again = client.post('/ai/tts/2', headers=headers, json={'title':'伪造标题','translation':'伪造译文'})
    assert again.json['id'] == data['id'] and len(spoken) == 1


@pytest.mark.parametrize('translation_format',['json','html'])
def test_translation_completion_and_updates_expire_original_audio(app, monkeypatch, translation_format):
    client, _, headers, _, cache = app
    spoken, translation = _translated_article(app, monkeypatch, with_translation=False, translation_format=translation_format)
    first = client.post('/ai/tts/2', headers=headers).json
    ready(cache, first['id'])
    assert first['has_translation'] is False and ENGLISH_BODY in spoken[0]
    assert web_server._save_ai_result(2, translation=translation)
    for path in ('/ai/tts/jobs/','/ai/tts/audio/','/ai/tts/stream/'):
        assert client.get(path+first['id'], headers=headers).status_code == 410
    second = client.post('/ai/tts/2', headers=headers).json
    ready(cache, second['id'])
    assert first['id'] != second['id'] and second['has_translation']
    assert CHINESE_BODY in spoken[-1] and ENGLISH_BODY not in spoken[-1]
    assert web_server._save_ai_result(2, translation='<p>更新完成的另一版中文正文。</p>')
    assert client.get('/ai/tts/audio/'+second['id'], headers=headers).status_code == 410


@pytest.mark.parametrize('preference',['view_translation','view_title_translation','view_title_shortening'])
def test_tts_rechecks_permissions_of_selected_title_and_body(app, monkeypatch, preference):
    client, _, headers, user, cache = app
    _translated_article(app, monkeypatch)
    result = client.post('/ai/tts/2', headers=headers).json
    ready(cache, result['id'])
    assert preference in result['required_preferences']
    models.set_user_settings(user, **{preference:False})
    for path in ('/ai/tts/jobs/','/ai/tts/audio/','/ai/tts/stream/'):
        assert client.get(path+result['id'], headers=headers).status_code == 403
    replacement = client.post('/ai/tts/2', headers=headers).json
    ready(cache, replacement['id'])
    assert replacement['id'] != result['id']
    assert preference not in replacement['required_preferences']
    ai_access.set_permissions(user, {'translation':False})
    original = client.post('/ai/tts/2', headers=headers).json
    ready(cache, original['id'])
    assert not original['has_translation']
    assert original['article']['title'] in {ENGLISH_TITLE,'Short English headline'}


def test_identical_original_and_translated_text_have_separate_audio_grants(app):
    client, admin, headers, user, cache = app
    enable(client, admin, user)
    ai_access.set_permissions(user, {'translation':True})
    models.set_user_settings(user, view_translation=1)
    admin_id = models.get_user_by_email('admin@tts.test')['id']
    models.set_user_settings(admin_id, view_tts=1)
    with sqlite3.connect(web_server.NEWS_DB) as db:
        db.execute("UPDATE articles SET title='同一标题',body_html='<p>同一正文。</p>' WHERE id=2")
    assert web_server._save_ai_result(2, translation='<p>同一正文。</p>')
    translated = client.post('/ai/tts/2', headers=headers).json
    ready(cache, translated['id'])
    original = client.post('/ai/tts/2', headers=admin).json
    ready(cache, original['id'])
    assert translated['has_translation'] and not original['has_translation']
    assert translated['id'] != original['id']
    assert cache.get(translated['id'])['text_hash'] == cache.get(original['id'])['text_hash']
    ai_access.set_permissions(user, {'translation':False})
    assert client.get('/ai/tts/audio/'+translated['id'], headers=headers).status_code == 403
    response = client.get('/ai/tts/audio/'+original['id'], headers=admin)
    assert response.data == MP3
    response.close()


def test_short_title_does_not_repeat_embedded_full_article_headlines():
    text, _ = listening_text(SHORT_CHINESE_TITLE,
        '<h1>'+ENGLISH_TITLE+'</h1><h1>'+FULL_CHINESE_TITLE+'</h1><p>'+CHINESE_BODY+'</p>',
        title_aliases=[ENGLISH_TITLE,FULL_CHINESE_TITLE])
    assert text.startswith(SHORT_CHINESE_TITLE)
    assert ENGLISH_TITLE not in text and FULL_CHINESE_TITLE not in text
    assert CHINESE_BODY in text


def test_inflight_deletion_never_republishes_audio(app, monkeypatch):
    client, admin, headers, user, cache = app
    enable(client, admin, user)
    started, finish = threading.Event(), threading.Event()
    def generate(config,text,target,**kw):
        started.set()
        assert finish.wait(3)
        target.write_bytes(MP3)
    monkeypatch.setattr(tts_cache,'synthesize',generate)
    result=client.post('/ai/tts/1',headers=headers).json
    assert started.wait(2)
    cache.invalidate_articles([1])
    finish.set()
    cache.pool.shutdown(wait=True)
    assert not list(cache.directory.glob('*.mp3')) and not list(cache.directory.glob('*.part'))
    assert client.get('/ai/tts/audio/'+result['id'],headers=headers).status_code == 410


def test_ttl_cleanup_protects_active_transfers_and_startup_removes_orphans(app):
    client, admin, headers, user, cache=app
    enable(client,admin,user)
    result=client.post('/ai/tts/1',headers=headers).json
    ready(cache,result['id'])
    path=cache.acquire(result['id'])
    cache.jobs[result['id']]['created_at']-=3601
    cache.cleanup()
    assert path.exists()
    cache.release(result['id'])
    assert not path.exists()
    orphans=[cache.directory/('a'*32+'.'+suffix) for suffix in ('part','mp3','opus','flac','wav')]
    for orphan in orphans:
        orphan.write_bytes(b'orphaned')
    other_file=cache.directory/'notes.flac';other_file.write_bytes(b'keep')
    fresh=tts_cache.TTSCache(cache.directory)
    fresh._clean_loop=lambda:None
    fresh.start()
    assert all(not orphan.exists() for orphan in orphans)
    assert other_file.read_bytes()==b'keep'
    fresh.pool.shutdown()


def test_capacity_reserves_downloads_and_never_evicts_a_leased_file(tmp_path,monkeypatch):
    cache=tts_cache.TTSCache(tmp_path/'bounded',max_bytes=100)
    monkeypatch.setattr(cache,'_clean_loop',lambda:None)
    monkeypatch.setattr(tts_cache,'synthesize',lambda config,text,target,**kw:target.write_bytes(MP3))
    config={**tts_service.DEFAULT_CONFIG,'enabled':True,'password':'secret'}
    first=cache.request(1,1,config,'first',False)
    assert ready(cache,first['id'])['status']=='ready'
    path=cache.acquire(first['id'])
    second=cache.request(2,2,config,'second',False)
    assert ready(cache,second['id'])['code']=='TTS_STORAGE_FULL'
    assert path.exists() and cache.reserved_bytes==0
    cache.release(first['id'])
    third=cache.request(3,3,config,'third',False)
    assert ready(cache,third['id'])['status']=='ready'
    assert not path.exists() and sum(p.stat().st_size for p in cache.directory.iterdir())<=100
    cache.pool.shutdown()


def test_queued_deleted_article_does_not_call_provider(tmp_path,monkeypatch):
    cache=tts_cache.TTSCache(tmp_path/'queued',workers=1)
    monkeypatch.setattr(cache,'_clean_loop',lambda:None)
    started,finish=threading.Event(),threading.Event()
    calls=[]
    def generate(config,text,target,**kw):
        calls.append(text);started.set()
        assert finish.wait(3)
        target.write_bytes(MP3)
    monkeypatch.setattr(tts_cache,'synthesize',generate)
    config={**tts_service.DEFAULT_CONFIG,'enabled':True,'password':'secret'}
    cache.request(1,1,config,'first',False)
    assert started.wait(2)
    queued=cache.request(2,2,config,'deleted',False)
    cache.invalidate_articles([2])
    finish.set()
    cache.pool.shutdown(wait=True)
    assert calls==['first'] and cache.reserved_bytes==0
    assert not (cache.directory/(queued['id']+'.mp3')).exists()


class ProviderResponse:
    status_code=200
    def __enter__(self):return self
    def __exit__(self,*args):pass
    def iter_content(self,size):yield MP3


def test_kokoro_request_contract_and_custom_auth(tmp_path, monkeypatch):
    calls=[]
    def post(url,**kwargs):calls.append((url,kwargs));return ProviderResponse()
    monkeypatch.setattr(tts_service,'safe_post',post)
    config={**tts_service.DEFAULT_CONFIG,'enabled':True,'endpoint':'https://tts.example.test/v1',
            'username':'reader','password':'密码'}
    target=tmp_path/'audio.mp3'
    tts_service.synthesize(config,'标题。正文。',target)
    url,kw=calls[0]
    assert url=='https://tts.example.test/v1/audio/speech'
    assert kw['json']=={'model':'kokoro','input':'标题。正文。','voice':'zf_001(0.5)+zf_022(0.5)',
                        'speed':1.3,'lang_code':'z','response_format':'mp3','stream':True,
                        'return_download_link':False}
    assert kw['headers']['Authorization']=='Basic '+base64.b64encode('reader:密码'.encode()).decode()
    assert kw['stream'] is True and kw['max_redirects']==0 and target.read_bytes()==MP3
    config.update(auth_type='authorization',authorization='Basic custom-header')
    tts_service.synthesize(config,'正文。',target)
    assert calls[-1][1]['headers']['Authorization']=='Basic custom-header'
    config['stream'] = False
    tts_service.synthesize(config,'关闭流式。',target)
    assert calls[-1][1]['json']['stream'] is False


def test_provider_publishes_flushed_bytes_before_completion(tmp_path, monkeypatch):
    target = tmp_path/'progress.mp3'
    published = []
    class StreamingResponse(ProviderResponse):
        def iter_content(self, size):
            assert size <= 4096
            yield MP3[:2]
            assert published == []
            yield MP3[2:]
            assert published == [len(MP3)]
            assert target.read_bytes() == MP3
            yield b'next audio bytes'
    monkeypatch.setattr(tts_service, 'safe_post', lambda *args, **kw: StreamingResponse())
    config = {**tts_service.DEFAULT_CONFIG, 'enabled':True,
              'endpoint':'https://tts.example.test/v1', 'username':'reader', 'password':'secret'}
    tts_service.synthesize(config, '正文。', target, on_progress=published.append)
    assert published == [len(MP3), len(target.read_bytes())]


@pytest.mark.parametrize('outcome', ['complete', 'failure', 'deleted', 'disconnect'])
def test_live_audio_arrives_before_generation_finishes(app, monkeypatch, outcome):
    client, admin, headers, user, cache = app
    enable(client, admin, user)
    published, finish = threading.Event(), threading.Event()
    tail = b'remaining MP3 frames'
    def generate(config, text, target, on_progress, **kw):
        with target.open('wb') as output:
            output.write(MP3); output.flush(); on_progress(len(MP3)); published.set()
            assert finish.wait(5)
            if outcome == 'failure':
                raise TTSError('TTS_TIMEOUT', '语音生成超时，请重试', 504)
            output.write(tail)
    monkeypatch.setattr(tts_cache, 'synthesize', generate)
    response = None
    try:
        result = client.post('/ai/tts/1', headers=headers).json
        assert result['stream_url'] == '/ai/tts/stream/' + result['id']
        assert published.wait(2)
        assert client.get(result['stream_url']).status_code == 401
        response = client.get(result['stream_url'], headers=headers, buffered=False)
        assert response.status_code == 200
        assert response.headers['X-Accel-Buffering'] == 'no'
        assert response.headers['Cache-Control'] == 'private, no-store'
        chunks = iter(response.response)
        assert next(chunks) == MP3
        assert cache.get(result['id'])['status'] == 'generating'
        assert cache.jobs[result['id']]['leases'] == 1
        if outcome == 'disconnect':
            response.close()
            assert cache.jobs[result['id']]['leases'] == 0
        if outcome == 'deleted':
            cache.invalidate_articles([1])
        finish.set()
        cache.pool.shutdown(wait=True)
        if outcome == 'complete':
            assert b''.join(chunks) == tail
            assert cache.get(result['id'])['status'] == 'ready'
            assert (cache.directory/(result['id']+'.mp3')).read_bytes() == MP3 + tail
        elif outcome in {'failure', 'deleted'}:
            with pytest.raises(TTSError) as error:
                next(chunks)
            assert error.value.code == ('TTS_TIMEOUT' if outcome == 'failure' else 'TTS_EXPIRED')
        response.close()
        if outcome == 'deleted':
            assert result['id'] not in cache.jobs
        else:
            assert cache.jobs[result['id']]['leases'] == 0
        assert cache.reserved_bytes == 0
    finally:
        finish.set()
        if response:
            response.close()


def test_stream_checks_permissions_expiry_and_early_provider_failure(app, monkeypatch):
    client, admin, headers, user, cache = app
    enable(client, admin, user)
    result = client.post('/ai/tts/1', headers=headers).json
    ready(cache, result['id'])
    response = client.get(result['stream_url'], headers=headers)
    assert response.data == MP3
    response.close()
    ai_access.set_permissions(user, {'tts':False})
    assert client.get(result['stream_url'], headers=headers).status_code == 403
    ai_access.set_permissions(user, {'tts':True})
    models.set_user_settings(user, view_tts=1)
    client.put('/admin/tts-config', headers=admin, json={'speed':1.1})
    assert client.get(result['stream_url'], headers=headers).status_code == 410
    def fail(*args, **kw):
        raise TTSError('TTS_UPSTREAM_ERROR', '语音服务暂时不可用', 502, '上游返回 HTTP 401')
    monkeypatch.setattr(tts_cache, 'synthesize', fail)
    failed = client.post('/ai/tts/1', headers=headers).json
    ready(cache, failed['id'])
    path = '/ai/tts/stream/' + failed['id']
    response = client.get(path, headers=headers)
    assert response.status_code == 502 and response.json['code'] == 'TTS_UPSTREAM_ERROR'
    assert 'detail' not in response.json
    admin_id = models.get_db().execute("SELECT id FROM users WHERE role='admin'").fetchone()[0]
    models.set_user_settings(admin_id, view_tts=1)
    assert client.get(path, headers=admin).json['detail'] == '上游返回 HTTP 401'
    assert cache.jobs[failed['id']]['leases'] == 0


def test_admin_can_configure_optional_kokoro_parameters(app, tmp_path, monkeypatch):
    client, admin, _, _, _ = app
    options = {**tts_service.NORMALIZATION_DEFAULTS, 'unit_normalization':True,
               'url_normalization':False}
    response = client.put('/admin/tts-config', headers=admin, json={
        'enabled':True, 'endpoint':'https://tts.example.test/v1', 'username':'reader',
        'password':'secret', 'model':'tts-1-hd', 'voice':'zf_001(0.5)+zf_022(0.5)',
        'lang_code':None, 'speed':3.5, 'response_format':'mp3', 'stream':True,
        'volume_multiplier':0.8, 'return_download_link':True,
        'download_format':'mp3', 'normalization_options':options,
    })
    assert response.status_code == 200
    assert response.json['normalization_options'] == options
    assert response.json['lang_code'] is None
    calls = []
    def post(url, **kwargs):
        calls.append(kwargs)
        return ProviderResponse()
    monkeypatch.setattr(tts_service, 'safe_post', post)
    target = tmp_path/'speech.mp3'
    tts_service.synthesize(tts_service.get_config(), '测试文本', target)
    payload = calls[0]['json']
    assert payload == {
        'model':'tts-1-hd', 'input':'测试文本', 'voice':'zf_001(0.5)+zf_022(0.5)',
        'speed':3.5, 'response_format':'mp3', 'stream':True,
        'return_download_link':True, 'volume_multiplier':0.8,
        'download_format':'mp3', 'normalization_options':options,
    }
    assert calls[0]['headers']['Accept'] == 'audio/mpeg'
    assert target.read_bytes() == MP3


@pytest.mark.parametrize('legacy_format', ['opus','flac','wav','pcm'])
def test_legacy_audio_formats_fall_back_to_mp3(app, monkeypatch, legacy_format):
    client, admin, user_headers, user, cache = app
    enable(client, admin, user)
    legacy_config = {**tts_service.get_config(), 'response_format':legacy_format,
                     'download_format':legacy_format, 'return_download_link':True}
    models.set_app_state(tts_service.CONFIG_KEY, json.dumps(legacy_config))
    config = client.get('/admin/tts-config', headers=admin).json
    assert config['response_format'] == config['download_format'] == 'mp3'
    assert client.put('/admin/tts-config', headers=admin, json={'speed':1.2}).status_code == 200
    calls = []
    def post(url, **kwargs):
        calls.append(kwargs)
        return ProviderResponse()
    monkeypatch.setattr(tts_service, 'safe_post', post)
    monkeypatch.setattr(tts_cache, 'synthesize', tts_service.synthesize)
    test_audio = client.post('/admin/tts-test', headers=admin)
    assert test_audio.status_code == 200 and test_audio.mimetype == 'audio/mpeg'
    assert test_audio.data == MP3
    result = client.post('/ai/tts/1', headers=user_headers).json
    assert ready(cache, result['id'])['status'] == 'ready'
    assert len(calls) == 2
    for call in calls:
        assert call['json']['response_format'] == call['json']['download_format'] == 'mp3'
        assert call['headers']['Accept'] == 'audio/mpeg'
    assert (cache.directory / (result['id'] + '.mp3')).exists()
    audio = client.get('/ai/tts/audio/' + result['id'], headers=user_headers)
    assert audio.status_code == 200 and audio.mimetype == 'audio/mpeg'
    assert audio.headers['Content-Disposition'].endswith('news.mp3')
    assert audio.data == MP3
    audio.close()


@pytest.mark.parametrize('kind', ['timeout','auth','html'])
def test_provider_failures_are_safe_and_actionable(tmp_path,monkeypatch,kind):
    def post(*args,**kwargs):
        if kind=='timeout':raise requests.ReadTimeout('password=do-not-leak')
        response=ProviderResponse()
        if kind=='auth':response.status_code=401
        if kind=='html':response.iter_content=lambda size:iter([b'<html>password=do-not-leak</html>'])
        return response
    monkeypatch.setattr(tts_service,'safe_post',post)
    with pytest.raises(TTSError) as error:
        tts_service.synthesize({**tts_service.DEFAULT_CONFIG,'enabled':True,
                                'endpoint':'https://tts.example.test/v1','username':'reader',
                                'password':'secret'},'正文',tmp_path/'a.mp3')
    assert error.value.code=={'timeout':'TTS_TIMEOUT','auth':'TTS_UPSTREAM_ERROR','html':'TTS_INVALID_AUDIO'}[kind]
    assert 'do-not-leak' not in str(error.value)
