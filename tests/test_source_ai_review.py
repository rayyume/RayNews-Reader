import sqlite3

import web_server


def test_low_confidence_suggestion_waits_for_review_without_repeating_ai(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE source_categories (source TEXT PRIMARY KEY, category TEXT, label TEXT, "
        "status TEXT, confidence REAL, reason TEXT, sample_titles TEXT, suggested_category TEXT, "
        "suggested_label TEXT, suggested_confidence REAL)"
    )
    conn.execute(
        "INSERT INTO source_categories (source, category, label, status) "
        "VALUES ('Example Press', 'Uncategorized', 'Example', 'pending')"
    )

    def rows(_conn):
        return [dict(row) for row in _conn.execute("SELECT * FROM source_categories")]

    calls = []

    class FakeAI:
        def __init__(self, *_args, **_kwargs):
            pass

        def classify_source(self, *_args, **_kwargs):
            calls.append(1)
            return {"category": "News", "label": "Example", "confidence": 0.7,
                    "reason": "not certain"}

    monkeypatch.setattr(web_server, "_get_news_db", lambda: conn)
    monkeypatch.setattr(web_server, "ensure_article_sources", lambda _conn: 0)
    monkeypatch.setattr(web_server, "source_rows", rows)
    monkeypatch.setattr(web_server, "category_definitions", lambda _conn: [
        {"category": "News", "is_system": 0},
    ])
    monkeypatch.setattr(web_server, "recent_titles_for_source", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(web_server, "_extract_domains_for_source", lambda *_args: [])
    monkeypatch.setattr(web_server, "_SystemAIService", FakeAI)

    config = {"api_key": "test", "endpoint": "https://example.com", "model": "test"}
    first = web_server._classify_source_batch(config)
    second = web_server._classify_source_batch(config)

    row = conn.execute("SELECT * FROM source_categories").fetchone()
    assert row["status"] == "review"
    assert row["suggested_category"] == "News"
    assert first["remaining"] == 0
    assert second["processed"] == []
    assert calls == [1]
