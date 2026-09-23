import datetime as dt
import sqlite3
import json

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
    assert result["stats"]["selected_events"] == 1
    assert "重大决定" in result["summary"]
    with sqlite3.connect(db_path) as check:
        audit = check.execute("SELECT selected, reason FROM daily_digest_events ORDER BY selected DESC").fetchall()
        assert audit == [(1, ""), (0, "below importance threshold")]
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
