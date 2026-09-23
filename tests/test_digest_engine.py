import datetime as dt
import sqlite3
import json
import pytest

from ai_service import AIService
import digest_engine as engine
import fetcher
import web_server


BEIJING = dt.timezone(dt.timedelta(hours=8))


def article(article_id, source, event, impact=3, *, ingested_at=1000, entity="A"):
    return {
        "id": article_id, "title": event, "source": source, "category": "News",
        "summary": event, "url": f"https://example.com/{article_id}",
        "ingested_at": ingested_at,
        "digest_signals": engine.normalize_signals({"title": event}, {
            "event": event, "entities": [entity], "action": "发布", "impact": impact,
            "novelty": 2, "evidence": 2,
        }),
    }


def test_independent_coverage_and_major_single_source_both_qualify():
    articles = [
        article(1, "甲报", "A发布重大决定", 3),
        article(2, "乙报", "A发布重大决定", 3),
        article(3, "丙报", "B发生重大变化", 5, entity="B"),
    ]
    groups = engine.group_events(articles)
    selected = engine.rank_events(groups, [], cutoff=2000)
    assert len(groups) == len(selected) == 2
    assert any(g["publisher_count"] == 2 for g in selected)
    assert any(g["publisher_count"] == 1 and g["signal"]["impact"] == 5 for g in selected)


def test_generic_topic_does_not_merge_distinct_entities_and_prior_event_is_skipped():
    left = article(1, "甲报", "A发布新模型", entity="A")
    right = article(2, "乙报", "B发布新模型", impact=4, entity="B")
    groups = engine.group_events([left, right])
    assert len(groups) == 2
    selected = engine.rank_events(groups, [left["digest_signals"]], cutoff=2000)
    assert [g["representative"]["id"] for g in selected] == [2]
    assert groups[0]["reason"] == "already covered"


def test_identical_generic_ai_event_does_not_merge_unrelated_articles():
    left = article(1, "甲报", "A公司公布财报", entity="A公司")
    right = article(2, "乙报", "B公司公布财报", entity="B公司")
    left["digest_signals"]["event"] = right["digest_signals"]["event"] = "公司公布财报"
    assert len(engine.group_events([left, right])) == 2
    left["digest_signals"]["entities"] = []
    right["digest_signals"]["entities"] = []
    left["digest_signals"]["event"] = right["digest_signals"]["event"] = "新闻"
    assert len(engine.group_events([left, right])) == 2


def test_relative_ranking_fills_requested_count_even_with_low_scores():
    reliable = article(1, "甲报", "机构A发布消息", impact=2, entity="机构A")
    fallback = article(2, "乙报", "机构B发布消息", impact=2, entity="机构B")
    for item in (reliable, fallback):
        item["digest_signals"].update({"novelty": 1, "evidence": 1})
    fallback["digest_signal_fallback"] = True
    selected = engine.rank_events(
        engine.group_events([reliable, fallback]), [], cutoff=1000, min_events=2,
    )
    assert [group["representative"]["id"] for group in selected] == [1, 2]
    assert all(group["selection_basis"] == "relative ranking" for group in selected)


def test_relative_ranking_still_excludes_unchanged_previous_events():
    articles = [article(i, f"来源{i}", f"机构{i}发布消息", impact=0, entity=f"机构{i}")
                for i in range(1, 4)]
    selected = engine.rank_events(
        engine.group_events(articles), [articles[0]["digest_signals"]],
        cutoff=1000, min_events=60,
    )
    assert [group["representative"]["id"] for group in selected] == [2, 3]


def test_low_signal_coverage_does_not_cache_a_two_item_daily_digest(tmp_path, monkeypatch):
    monkeypatch.setattr(web_server, "NEWS_DB", str(tmp_path / "absent.db"))
    rows = []
    for article_id in range(1, 21):
        row = article(article_id, f"来源{article_id}", f"机构{article_id}发布消息", 5)
        row["digest_signals"] = row["digest_signals"] if article_id <= 2 else None
        rows.append(row)

    class EmptyService:
        def __init__(self, **_kwargs):
            pass

        def batch_digest_signals(self, _articles):
            return {}

    monkeypatch.setattr(web_server, "AIService", EmptyService)
    monkeypatch.setattr(web_server, "_note_system_ai_success", lambda: None)
    monkeypatch.setattr(web_server, "_fetch_articles_by_date", lambda *_args, **_kwargs: rows)
    monkeypatch.setattr(web_server, "get_system_ai_config", lambda: {
        "enabled": True, "api_key": "key", "endpoint": "https://example.com", "model": "test",
    })
    assert web_server._generate_daily_summary_global("2026-09-23") is None
    assert "signal coverage too low" in web_server._daily_summary_last_error


def test_one_failed_signal_batch_does_not_block_cached_daily_digest(tmp_path, monkeypatch):
    monkeypatch.setattr(web_server, "NEWS_DB", str(tmp_path / "absent.db"))
    rows = [article(i, f"来源{i}", f"机构{i}发布消息", impact=2, entity=f"机构{i}")
            for i in range(1, 101)]
    rows[-1]["digest_signals"] = None

    class PartialService:
        def __init__(self, **_kwargs):
            pass

        def batch_digest_signals(self, _articles):
            raise RuntimeError("temporary provider error")

        def write_digest_events(self, _events):
            return {}

    monkeypatch.setattr(web_server, "AIService", PartialService)
    monkeypatch.setattr(web_server, "_note_system_ai_success", lambda: None)
    monkeypatch.setattr(web_server, "_fetch_articles_by_date", lambda *_args, **_kwargs: rows)
    monkeypatch.setattr(web_server, "_digest_category_definitions", lambda: [
        {"category": "News", "label": "政经新闻"},
    ])
    monkeypatch.setattr(web_server, "_previous_digest_signals", lambda _date: [])
    monkeypatch.setattr(web_server, "_save_daily_summary_global_cache", lambda *_args: True)
    monkeypatch.setattr(web_server, "get_system_ai_config", lambda: {
        "enabled": True, "api_key": "key", "endpoint": "https://example.com", "model": "test",
    })
    result = web_server._generate_daily_summary_global("2026-09-23")
    assert result is not None
    assert result["stats"]["signal_fallbacks"] == 1
    assert result["stats"]["digest_item_count"] == 60


def test_high_volume_digest_uses_relative_ranking_after_valid_signals(tmp_path, monkeypatch):
    monkeypatch.setattr(web_server, "NEWS_DB", str(tmp_path / "absent.db"))
    rows = [article(i, f"来源{i}", f"机构{i}发布消息", impact=2, entity=f"机构{i}")
            for i in range(1, 101)]
    _start, cutoff = web_server._digest_window("2026-09-23")
    for row in rows:
        row["ingested_at"] = cutoff - 60
        row["digest_signals"].update({"novelty": 1, "evidence": 1})

    class Writer:
        def __init__(self, **_kwargs):
            pass

        def write_digest_events(self, _events):
            return {}

    monkeypatch.setattr(web_server, "AIService", Writer)
    monkeypatch.setattr(web_server, "_note_system_ai_success", lambda: None)
    monkeypatch.setattr(web_server, "_fetch_articles_by_date", lambda *_args, **_kwargs: rows)
    monkeypatch.setattr(web_server, "_digest_category_definitions", lambda: [
        {"category": "News", "label": "政经新闻"},
    ])
    monkeypatch.setattr(web_server, "_previous_digest_signals", lambda _date: [])
    monkeypatch.setattr(web_server, "_save_daily_summary_global_cache", lambda *_args: True)
    monkeypatch.setattr(web_server, "get_system_ai_config", lambda: {
        "enabled": True, "api_key": "key", "endpoint": "https://example.com", "model": "test",
    })
    result = web_server._generate_daily_summary_global("2026-09-23")
    assert result["stats"]["articles_after_dedup"] == 100
    assert result["stats"]["digest_item_count"] == 60
    assert result["stats"]["relative_ranked_events"] == 60


def test_dynamic_sections_number_from_one_and_limit_to_sixty():
    articles = [article(i, f"来源{i}", f"机构{i}发布决定", 5, entity=f"机构{i}")
                for i in range(1, 65)]
    groups = engine.group_events(articles)
    selected = engine.rank_events(groups, [], cutoff=2000)
    assert len(selected) == 60
    assert sum(g["reason"] == "daily limit" for g in groups) == 4
    articles[1]["category"] = "Tech"
    text = engine.render_digest(selected[:2], [
        {"category": "News", "label": "新闻"},
        {"category": "Tech", "label": "技术"},
    ], {})
    assert "## 新闻\n1." in text
    assert "## 技术\n1." in text


def test_selected_unclassified_event_is_rendered():
    item = article(1, "新来源", "机构发布重要决定", 5)
    item["category"] = "Uncategorized"
    selected = engine.rank_events(engine.group_events([item]), [], cutoff=2000)
    text = engine.render_digest(selected, [{"category": "News", "label": "政经新闻"}], {})
    assert "## 待分类\n1." in text


def test_beijing_cutoff_and_shared_category_mapping(tmp_path, monkeypatch):
    db_path = tmp_path / "news.db"
    monkeypatch.setattr(fetcher, "DB_FILE", db_path)
    monkeypatch.setattr(web_server, "NEWS_DB", str(db_path))
    conn = fetcher.init_db()
    conn.execute("INSERT INTO source_categories (source, category, status) VALUES ('甲报', 'Tech', 'manual')")
    start, cutoff = web_server._digest_window("2026-09-23")
    assert cutoff == int(dt.datetime(2026, 9, 23, 21, tzinfo=BEIJING).timestamp())
    for article_id, seen in [(1, start), (2, cutoff - 1), (3, cutoff)]:
        conn.execute("INSERT INTO articles "
                     "(id, title, source, group_source, date, timestamp, ingested_at) "
                     "VALUES (?, ?, '甲报', '甲报', '2026-09-22', ?, ?)",
                     (article_id, f"新闻{article_id}", seen, seen))
    conn.commit()
    conn.close()
    rows = web_server._fetch_articles_by_date("2026-09-23")
    assert [r["id"] for r in rows] == [1, 2]
    assert all(r["category"] == "Tech" for r in rows)
    assert [r["id"] for r in web_server._fetch_articles_by_date("2026-09-24")] == [3]


def test_generation_persists_selected_and_rejected_events(tmp_path, monkeypatch):
    db_path = tmp_path / "news.db"
    monkeypatch.setattr(fetcher, "DB_FILE", db_path)
    monkeypatch.setattr(web_server, "NEWS_DB", str(db_path))
    conn = fetcher.init_db()
    start, _ = web_server._digest_window("2026-09-23")
    for article_id, source, title in [
        (1, "甲报", "A公司公布重大决定"),
        (2, "乙报", "A公司公布重大决定"),
        (3, "丙报", "普通消息"),
    ]:
        conn.execute("INSERT INTO articles "
                     "(id, title, source, group_source, date, timestamp, ingested_at, summary) "
                     "VALUES (?, ?, ?, ?, '2026-09-23', ?, ?, ?)",
                     (article_id, title, source, source, start + 100, start + 100, title))
    conn.commit()
    conn.close()

    class FakeService:
        def __init__(self, **kwargs):
            pass

        def batch_digest_signals(self, articles):
            return {item["id"]: {
                "event": item["title"], "entities": ["A公司"] if item["id"] < 3 else [],
                "action": "公布", "impact": 4 if item["id"] < 3 else 1,
                "novelty": 2 if item["id"] < 3 else 0,
                "evidence": 2 if item["id"] < 3 else 1,
            } for item in articles}

        def write_digest_events(self, events):
            return {g["representative"]["id"]: {
                "headline": "重大决定", "sentence": "A公司公布重大决定。"
            } for g in events}

    monkeypatch.setattr(web_server, "AIService", FakeService)
    monkeypatch.setattr(web_server, "get_system_ai_config", lambda: {
        "enabled": True, "api_key": "key", "endpoint": "https://example.com",
        "model": "test", "provider_type": "openai",
    })
    result = web_server._generate_daily_summary_global("2026-09-23")
    assert result["stats"]["total_articles"] == 3
    assert result["stats"]["events"] == 2
    assert result["stats"]["selected_events"] == 2
    assert "重大决定" in result["summary"]
    with sqlite3.connect(db_path) as check:
        audit = check.execute("SELECT selected, reason FROM daily_digest_events ORDER BY selected DESC").fetchall()
        assert audit == [(1, ""), (1, "")]
        assert check.execute("SELECT COUNT(*) FROM ai_results WHERE digest_signals_json IS NOT NULL").fetchone()[0] == 3
    assert web_server._generate_daily_summary_global("2026-09-23")["summary"] == result["summary"]


def test_upsert_preserves_first_ingestion_time(tmp_path, monkeypatch):
    monkeypatch.setattr(fetcher, "DB_FILE", tmp_path / "news.db")
    conn = fetcher.init_db()
    fetcher.upsert_articles(conn, [{"id": 31, "title": "初稿", "source": "甲报",
                                   "timestamp": 100, "ingested_at": 500}], sync_sources=False)
    fetcher.upsert_articles(conn, [{"id": 31, "title": "更新", "source": "甲报",
                                   "timestamp": 100, "ingested_at": 900}], sync_sources=False)
    assert conn.execute("SELECT title, ingested_at FROM articles WHERE id = 31").fetchone()[:] == ("更新", 500)
    conn.close()


def test_article_summary_produces_signals_in_one_ai_call(monkeypatch):
    service = AIService("key", "https://example.com", "test")
    calls = []

    def fake_chat(messages, **kwargs):
        calls.append(messages)
        return json.dumps({"summary": "摘要", "signals": {
            "event": "A发布决定", "impact": 4, "entities": ["A"]
        }})

    monkeypatch.setattr(service, "chat", fake_chat)
    summary, signals = service.summarize_with_signals("正文", "标题")
    assert summary == "摘要"
    assert signals["impact"] == 4
    assert len(calls) == 1


def test_article_summary_falls_back_when_model_ignores_json(monkeypatch):
    service = AIService("key", "https://example.com", "test")
    calls = []

    def fake_chat(messages, **kwargs):
        calls.append(kwargs["max_tokens"] if "max_tokens" in kwargs else None)
        return "普通文本" if len(calls) == 1 else "可用的文章摘要"

    monkeypatch.setattr(service, "chat", fake_chat)
    summary, signals = service.summarize_with_signals("文章正文", "标题")
    assert (summary, signals) == ("可用的文章摘要", {})
    assert len(calls) == 2
    assert calls[0] >= 2000
    assert calls[1] is None


def test_failed_article_summary_remains_retryable_after_first_day(tmp_path, monkeypatch):
    db_path = tmp_path / "news.db"
    monkeypatch.setattr(fetcher, "DB_FILE", db_path)
    monkeypatch.setattr(web_server, "NEWS_DB", str(db_path))
    old_ingestion = int(dt.datetime.now(dt.timezone.utc).timestamp()) - 2 * 86400
    conn = fetcher.init_db()
    for article_id in (1, 2):
        conn.execute(
            "INSERT INTO articles (id, title, source, group_source, ingested_at, body_html) "
            "VALUES (?, '标题', '来源', '来源', ?, '<p>正文</p>')",
            (article_id, old_ingestion),
        )
    conn.commit()
    conn.close()
    assert web_server._init_ai_results_table()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO ai_results (article_id, summary_error, summary_error_at) "
            "VALUES (1, 'temporary failure', datetime('now', '-7 hours'))"
        )
    assert [row["id"] for row in web_server._fetch_unsummarized_articles()] == [1]
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO articles (id, title, source, group_source, ingested_at, body_html) "
            "VALUES (3, '新文章', '来源', '来源', ?, '<p>正文</p>')",
            (int(dt.datetime.now(dt.timezone.utc).timestamp()),),
        )
    assert [row["id"] for row in web_server._fetch_unsummarized_articles()] == [3, 1]


def test_empty_batch_signal_response_is_an_ai_failure(monkeypatch):
    service = AIService("key", "https://example.com", "test")
    monkeypatch.setattr(service, "chat", lambda *_args, **_kwargs: '{"items":[]}')
    with pytest.raises(ValueError, match="no usable digest signals"):
        service.batch_digest_signals([{"id": 1, "title": "消息"}])
