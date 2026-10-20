"""A changed source title invalidates only title results and fences old workers."""
from datetime import datetime
import json

import pytest

import fetcher
import refresh_server
import web_server
from test_title_snapshot_regressions import news

OLD = 'An extensive report on the development of autonomous cars and transportation infrastructure in the busiest European cities'
NEW = 'A completely different report on the launch of a satellite and new scientific discoveries during its deep space exploration mission'
ZH = '全新卫星发射与深空探索任务中的科学发现以及相关工程进展和关键技术挑战的详细新闻报道及完整分析'
KINDS = ('title_translation', 'title_summary', 'translated_title_summary')
CONFIG = {'auto_translate_title': True, 'auto_title_summary_enabled': True}


@pytest.fixture
def titles(news, monkeypatch):
    monkeypatch.setattr(web_server, 'NEWS_DB', str(fetcher.DB_FILE))
    today = datetime.now()
    fetcher.upsert_articles(news, [{'id':1,'title':OLD,'date':today.strftime('%Y-%m-%d'),
                                  'timestamp':int(today.timestamp()),'body_html':'<p>Original body</p>'}])
    assert web_server._init_ai_results_table()
    assert web_server._save_ai_result(1,summary='Keep summary',translation=json.dumps({'title':'Old translated title','html':'<p>Keep translated body</p>'}))
    for kind in KINDS:
        assert web_server._save_title_result(1, kind, title='Old '+kind)
        assert web_server._save_title_result(1, kind, error='Old failure')
    return news


def reingest(conn,title):
    now=datetime.now()
    fetcher.upsert_articles(conn,[{'id':1,'title':title,'date':now.strftime('%Y-%m-%d'),'timestamp':int(now.timestamp())}])


def test_source_correction_reaches_title_poll_once_per_change(titles):
    cursor=json.loads(refresh_server.api_title_updates({}))['cursor']
    reingest(titles,NEW)
    first=json.loads(refresh_server.api_title_updates({'since':[cursor]}))
    assert [(item['id'],item['title']) for item in first['items']]==[(1,NEW)]
    assert first['cursor']>cursor

    reingest(titles,NEW)
    assert json.loads(refresh_server.api_title_updates({'since':[first['cursor']]}))['items']==[]

    corrected=NEW+' (corrected)'
    reingest(titles,corrected)
    second=json.loads(refresh_server.api_title_updates({'since':[first['cursor']]}))
    assert [(item['id'],item['title']) for item in second['items']]==[(1,corrected)]
    assert second['cursor']>first['cursor']


def test_reingest_of_legacy_ai_title_without_source_change_does_not_emit_event(titles):
    titles.execute("UPDATE articles SET original_title=title, title='Legacy AI display', title_source='translation'")
    titles.commit()
    cursor=json.loads(refresh_server.api_title_updates({}))['cursor']
    reingest(titles,OLD)
    assert json.loads(refresh_server.api_title_updates({'since':[cursor]}))['items']==[]


def test_two_corrections_advance_past_an_existing_later_cursor(titles):
    future='2099-01-01 00:00:00.000'
    titles.execute('UPDATE articles SET title_updated_at=? WHERE id=1',(future,))
    titles.commit()
    reingest(titles,NEW)
    first=titles.execute('SELECT title_updated_at FROM articles WHERE id=1').fetchone()[0]
    assert first>future
    reingest(titles,NEW+' again')
    second=titles.execute('SELECT title_updated_at FROM articles WHERE id=1').fetchone()[0]
    assert second>first
    update=json.loads(refresh_server.api_title_updates({'since':[first+'|1']}))
    assert [(item['id'],item['title']) for item in update['items']]==[(1,NEW+' again')]


def test_changed_source_invalidates_three_variants_and_backoffs_and_regenerates(titles,monkeypatch):
    before=web_server._get_ai_result(1,strict=True)
    reingest(titles,NEW)
    after=web_server._get_ai_result(1,strict=True)
    for kind in KINDS:
        assert after[kind] is None
        assert after[kind+'_error'] is None
        assert after[kind+'_error_at'] is None
        assert after[kind+'_source'] is None
    assert after['summary']==before['summary'] and after['translation']==before['translation']
    assert after['version']>before['version']
    assert [a['id'] for a in web_server._fetch_title_process_articles(CONFIG)]==[1]
    calls=[]
    class Service:
        def chat(self,*args,**kwargs):
            calls.append('title_summary')
            return json.dumps({'title':'New satellite science report'})
    monkeypatch.setattr(web_server,'_title_service',lambda config:Service())
    monkeypatch.setattr(web_server,'_translate_title_with_retry',lambda svc,title:(calls.append('title_translation') or {'valid':True,'title':ZH}))
    monkeypatch.setattr(web_server,'_summarize_title_with_retry',lambda svc,title:(calls.append('translated_title_summary') or {'valid':True,'title':'卫星发射与科学发现'}))
    assert web_server._process_article_title(dict(titles.execute('SELECT * FROM articles').fetchone()),CONFIG)
    assert calls==list(KINDS)
    assert all(web_server._get_ai_result(1)[kind] for kind in KINDS)
    assert web_server._fetch_title_process_articles(CONFIG)==[]


@pytest.mark.parametrize('legacy_ai', [False,True])
def test_identical_canonical_source_keeps_results_and_versions(titles,legacy_ai):
    if legacy_ai:
        titles.execute("UPDATE articles SET original_title=title,title='Legacy AI display',title_source='translation' WHERE id=1")
        titles.commit()
    before=web_server._get_ai_result(1,strict=True)
    reingest(titles,OLD)
    after=web_server._get_ai_result(1,strict=True)
    assert after==before


def test_title_invalidation_rolls_back_with_article_write(titles):
    before=web_server._get_ai_result(1,strict=True)
    titles.execute('BEGIN IMMEDIATE')
    titles.execute('UPDATE articles SET title=? WHERE id=1',(NEW,))
    assert titles.execute('SELECT title_translation FROM ai_results WHERE article_id=1').fetchone()[0] is None
    titles.rollback()
    assert titles.execute('SELECT title FROM articles WHERE id=1').fetchone()[0]==OLD
    assert web_server._get_ai_result(1,strict=True)==before


@pytest.mark.parametrize('outcome',['success','failure'])
def test_inflight_old_title_cannot_restore_results_or_backoff(titles,monkeypatch,outcome):
    titles.execute('UPDATE ai_results SET title_translation=NULL,title_translation_error=NULL,title_translation_error_at=NULL WHERE article_id=1')
    titles.commit()
    monkeypatch.setattr(web_server,'_title_service',lambda config:object())
    def translate(svc,title):
        assert title==OLD
        reingest(titles,NEW)
        if outcome=='failure':
            raise TimeoutError('Late failure for old title')
        return {'valid':True,'title':'旧标题的迟到译文'}
    monkeypatch.setattr(web_server,'_translate_title_with_retry',translate)
    assert not web_server._process_article_title({'id':1,'title':OLD},{'auto_translate_title':True})
    result=web_server._get_ai_result(1)
    assert not result['title_translation'] and not result['title_translation_error']
    assert not result['title_translation_error_at']


def test_body_translation_title_is_not_reused_for_updated_source(titles,monkeypatch):
    reingest(titles,NEW)
    calls=[]
    class Service:
        def __init__(self,*args,**kwargs):pass
        def translate_title(self,title,*args):
            calls.append(title)
            return '新标题的正确译文'
    monkeypatch.setattr(web_server,'_SystemAIService',Service)
    monkeypatch.setattr(web_server,'_publish_translation_update',lambda article_id:None)
    item={'id':1,'title':NEW,'translation':json.dumps({'title':'旧正文缓存中的旧译名','html':'<p>Keep translated body</p>'}),
          'translate_content_needed':True,'translate_title_needed':True}
    assert web_server._translate_article_background(item,{'api_key':'mock','endpoint':'https://ai.test','model':'mock'})
    assert calls==[NEW]
    assert web_server._get_ai_result(1)['title_translation']=='新标题的正确译文'
