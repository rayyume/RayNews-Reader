"""Administrator source names are shared across anonymous and signed-in reads."""
import json
import sqlite3

import pytest

import fetcher
import models
import refresh_server
import web_server
from source_categories import (apply_article_source_labels, init_source_categories,
                               merge_source, save_publisher_domain, update_source_category)
from test_title_snapshot_regressions import news


@pytest.fixture
def curated_news(news, monkeypatch):
    monkeypatch.setattr(web_server, 'NEWS_DB', str(fetcher.DB_FILE))
    fetcher.upsert_articles(news, [{'id':1, 'title':'新闻标题', 'source':'旧AI来源',
        'group_source':'旧AI来源', 'feed_source':'旧订阅源', 'origin_source':'旧出处',
        'body_html':'<p>文章正文。</p>', 'timestamp':100}], sync_sources=False)
    init_source_categories(news)
    update_source_category(news, '旧AI来源', 'Tech', '管理员确认来源', status='manual')
    return news


@pytest.mark.parametrize('params', [{}, {'since':['0']}, {'q':['新闻标题']}])
def test_public_news_and_article_details_include_curated_source_without_metadata_request(curated_news, params):
    listing = json.loads(refresh_server.api_news_list(params))
    detail = json.loads(refresh_server.api_news_detail(1))
    for item in (listing['items'][0], detail):
        assert item['display_source'] == '管理员确认来源'
        assert item['display_source_key'] == '旧AI来源'
        assert item['source_curated'] is True
        assert isinstance(item['source_labels_read_at'], int) and item['source_labels_read_at'] > 0


def test_source_edits_reach_cached_article_bodies_immediately(curated_news, monkeypatch):
    builds = []
    build = refresh_server._build_news_detail_response
    def counted(article_id):
        builds.append(article_id)
        return build(article_id)
    monkeypatch.setattr(refresh_server, '_build_news_detail_response', counted)
    first = json.loads(refresh_server.api_news_detail(1))
    update_source_category(curated_news, '旧AI来源', 'Tech', '再次整理后的来源', status='manual')
    second = json.loads(refresh_server.api_news_detail(1))
    assert first['display_source'] == '管理员确认来源'
    assert second['display_source'] == '再次整理后的来源'
    assert second['body_html'] == first['body_html']
    assert second['source_labels_read_at'] >= first['source_labels_read_at']
    assert builds == [1]


def test_shared_alias_chain_resolves_even_for_previously_cached_articles(curated_news):
    refresh_server.api_news_detail(1)
    update_source_category(curated_news, '中间来源', 'Tech', '中间名称', status='manual')
    update_source_category(curated_news, '最终来源', 'Tech', '最终显示名称', status='manual')
    merge_source(curated_news, '旧AI来源', '中间来源')
    merge_source(curated_news, '中间来源', '最终来源')
    item = json.loads(refresh_server.api_news_detail(1))
    assert item['display_source'] == '最终显示名称'
    assert item['display_source_key'] == '最终来源'
    assert item['source_curated']


def test_custom_domain_mapping_has_priority_over_old_ai_identity(curated_news):
    update_source_category(curated_news, '正确出版方', 'Tech', '管理员核实出版方', status='manual')
    curated_news.execute("UPDATE articles SET publisher_domain='example.com' WHERE id=1")
    curated_news.commit()
    save_publisher_domain(curated_news, 'example.com', '正确出版方')
    item = json.loads(refresh_server.api_news_list({}))['items'][0]
    assert item['display_source'] == '管理员核实出版方'
    assert item['display_source_key'] == '正确出版方'


def test_favorites_and_source_articles_share_the_public_display_name(curated_news, tmp_path, monkeypatch):
    models.close_db()
    monkeypatch.setattr(models, 'DB_FILE', tmp_path/'accounts.db')
    user = models.create_user('reader@source.test', 'pw', 'Reader')['id']
    headers = {'Authorization':'Bearer '+web_server.create_token(user, 'user')}
    try:
        client = web_server.app.test_client()
        assert client.post('/favorites', headers=headers, json={'article_id':1}).status_code == 201
        for response in (client.get('/favorites', headers=headers),
                         client.get('/sources/articles?source=旧AI来源', headers=headers)):
            assert response.status_code == 200
            assert response.json['items'][0]['display_source'] == '管理员确认来源'
            assert response.json['items'][0]['source_labels_read_at'] > 0
        assert web_server._get_article_meta(1)['display_source'] == '管理员确认来源'
    finally:
        models.close_db()


def test_public_source_snapshots_share_the_article_freshness_clock(curated_news, monkeypatch):
    import source_categories
    monkeypatch.setattr(source_categories.time, 'time_ns', lambda: 100_000_000)
    item = json.loads(refresh_server.api_news_list({}))['items'][0]
    assert item['source_labels_read_at'] == 100_000
    monkeypatch.setattr(source_categories.time, 'time_ns', lambda: 200_000_000)
    public = json.loads(refresh_server.api_sources())
    signed_in = web_server.app.test_client().get('/sources').json
    assert public['source_labels_read_at'] == signed_in['source_labels_read_at'] == 200_000


def test_label_resolution_is_read_only_and_handles_uninitialized_metadata():
    with sqlite3.connect(':memory:') as conn:
        conn.execute('CREATE TABLE sentinel (value TEXT)')
        before = conn.total_changes
        item = apply_article_source_labels(conn, [{'source':'原始来源'}])[0]
        assert item['display_source'] == '原始来源' and not item['source_curated']
        assert conn.total_changes == before
        assert conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == [('sentinel',)]
