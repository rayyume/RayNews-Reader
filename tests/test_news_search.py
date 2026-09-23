import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import refresh_server


def _setup_news_db(tmp_path):
    db_path = tmp_path / "news.db"
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE articles (
            id INTEGER PRIMARY KEY,
            title TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL DEFAULT '',
            feed_source TEXT NOT NULL DEFAULT '',
            origin_source TEXT NOT NULL DEFAULT '',
            time TEXT DEFAULT '',
            date TEXT DEFAULT '',
            timestamp INTEGER NOT NULL DEFAULT 0,
            thumb TEXT DEFAULT '',
            has_full_content INTEGER DEFAULT 0,
            telegraph_url TEXT DEFAULT '',
            body_html TEXT DEFAULT '',
            summary TEXT DEFAULT ''
        )
    """)
    rows = [
        (1, "OpenAI releases model", "Tech Feed", "Tech Feed", "OpenAI", "10:00", "2026-06-07", 300, "", 1, "", "", "New reasoning model details"),
        (2, "Markets rally", "Biz Feed", "Biz Feed", "Reuters", "09:00", "2026-06-07", 200, "", 0, "", "", "Stocks rise after earnings"),
        (3, "Local weather", "City Feed", "City Feed", "", "08:00", "2026-06-06", 100, "", 0, "", "", "Sunny day"),
    ]
    conn.executemany(
        "INSERT INTO articles "
        "(id, title, source, feed_source, origin_source, time, date, timestamp, thumb, "
        "has_full_content, telegraph_url, body_html, summary) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.execute("""
        CREATE TABLE source_categories (
            source TEXT PRIMARY KEY,
            category TEXT NOT NULL DEFAULT 'Info',
            label TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending',
            confidence REAL,
            reason TEXT DEFAULT '',
            sample_titles TEXT,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.executemany(
        "INSERT INTO source_categories (source, category, label, status) VALUES (?, ?, ?, 'manual')",
        [
            ("Tech Feed", "Tech", "Tech Feed"),
            ("Biz Feed", "Biz", "Biz Feed"),
            ("City Feed", "Info", "City Feed"),
        ],
    )
    conn.commit()
    conn.close()

    refresh_server.DB_FILE = db_path
    refresh_server._schema_ready = False
    return db_path


def _api_news(params):
    return json.loads(refresh_server.api_news_list(params).decode("utf-8"))


def test_api_news_search_matches_title_source_origin_and_summary(tmp_path):
    _setup_news_db(tmp_path)

    assert [item["id"] for item in _api_news({"q": ["openai"]})["items"]] == [1]
    assert [item["id"] for item in _api_news({"q": ["biz feed"]})["items"]] == [2]
    assert [item["id"] for item in _api_news({"q": ["reuters"]})["items"]] == [2]
    assert [item["id"] for item in _api_news({"q": ["sunny"]})["items"]] == [3]


def test_api_news_search_keeps_empty_query_and_pagination_compatible(tmp_path):
    _setup_news_db(tmp_path)

    empty = _api_news({"q": ["   "], "page": ["1"], "size": ["2"]})
    assert empty["total"] == 3
    assert [item["id"] for item in empty["items"]] == [1, 2]

    paged = _api_news({"q": ["feed"], "page": ["2"], "size": ["1"]})
    assert paged["total"] == 3
    assert paged["page"] == 2
    assert paged["size"] == 1
    assert [item["id"] for item in paged["items"]] == [2]


def test_api_news_search_handles_special_characters(tmp_path):
    _setup_news_db(tmp_path)

    result = _api_news({"q": ["%'_"]})
    assert result["total"] == 0
    assert result["items"] == []


def test_uncategorized_filter_includes_sources_not_yet_registered(tmp_path):
    db_path = _setup_news_db(tmp_path)
    _api_news({})  # Complete schema initialization before simulating a new source.
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE source_categories SET category = 'Uncategorized' WHERE source = 'Biz Feed'")
        conn.execute("DELETE FROM source_categories WHERE source = 'City Feed'")
        # Exercise the legacy source fallback inside the correlated subquery.
        conn.execute("UPDATE articles SET group_source = '', feed_source = '' WHERE id = 3")
    pending = _api_news({"category": ["Uncategorized"], "size": ["1"]})
    assert pending["total"] == 2
    assert [item["id"] for item in pending["items"]] == [2]
    page2 = _api_news({"category": ["Uncategorized"], "size": ["1"], "page": ["2"]})
    assert [item["id"] for item in page2["items"]] == [3]
    assert _api_news({"category": ["Info"]})["items"] == []
    assert [item["id"] for item in _api_news({"category": ["Tech"]})["items"]] == [1]
    recent = _api_news({"category": ["Uncategorized"], "since": ["150"]})
    assert [item["id"] for item in recent["items"]] == [2]


def test_api_news_filters_category_and_source_before_paginating(tmp_path):
    _setup_news_db(tmp_path)

    tech = _api_news({"category": ["Tech"], "page": ["1"], "size": ["1"]})
    assert tech["total"] == 1
    assert [item["id"] for item in tech["items"]] == [1]

    selected = _api_news({
        "source": ["Tech Feed", "City Feed"],
        "page": ["1"],
        "size": ["1"],
    })
    assert selected["total"] == 2
    assert [item["id"] for item in selected["items"]] == [1]

    selected_page_2 = _api_news({
        "source": ["Tech Feed", "City Feed"],
        "page": ["2"],
        "size": ["1"],
    })
    assert [item["id"] for item in selected_page_2["items"]] == [3]


def test_api_news_filters_by_date_for_lightweight_daily_count(tmp_path):
    _setup_news_db(tmp_path)
    result = _api_news({"date": ["2026-06-07"], "page": ["1"], "size": ["1"]})
    assert result["total"] == 2
    assert len(result["items"]) == 1
