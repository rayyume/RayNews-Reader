"""Automatic translation must publish only after its gated cache commits."""

import datetime as dt
import json
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import pytest

import web_server


_ARTICLES_SCHEMA = """
    CREATE TABLE articles (
        id INTEGER PRIMARY KEY,
        title TEXT NOT NULL DEFAULT '',
        source TEXT NOT NULL DEFAULT '',
        feed_source TEXT NOT NULL DEFAULT '',
        origin_source TEXT NOT NULL DEFAULT '',
        summary TEXT NOT NULL DEFAULT '',
        body_html TEXT NOT NULL DEFAULT '',
        date TEXT NOT NULL DEFAULT '',
        timestamp INTEGER NOT NULL DEFAULT 0,
        has_full_content INTEGER,
        telegraph_url TEXT
    )
"""


def _create_news_db(db_path):
    conn = sqlite3.connect(db_path)
    conn.execute(_ARTICLES_SCHEMA)
    conn.commit()
    conn.close()


@pytest.fixture
def news_db(tmp_path, monkeypatch):
    db_path = tmp_path / "news.db"
    _create_news_db(db_path)
    monkeypatch.setattr(web_server, "NEWS_DB", str(db_path))
    monkeypatch.setattr(web_server, "STALE_TRANSLATION_SCAN_HORIZON_DAYS", 36500)
    return db_path


def _insert_article(
    news_db,
    *,
    id,
    date,
    telegraph_url="",
    has_full_content=0,
    title="English title",
    body_html="<p>English body</p>",
    timestamp=None,
):
    conn = sqlite3.connect(news_db)
    conn.execute(
        """
        INSERT INTO articles
            (id, title, source, feed_source, origin_source, summary, body_html,
             date, timestamp, has_full_content, telegraph_url)
        VALUES (?, ?, 'Source', 'Source', '', '', ?, ?, ?, ?, ?)
        """,
        (
            id,
            title,
            body_html,
            date,
            id if timestamp is None else timestamp,
            has_full_content,
            telegraph_url,
        ),
    )
    conn.commit()
    conn.close()


def _insert_translations(news_db, translations):
    assert web_server._init_ai_results_table()
    conn = sqlite3.connect(news_db)
    conn.executemany(
        "INSERT INTO ai_results (article_id, translation) VALUES (?, ?)",
        translations,
    )
    conn.commit()
    conn.close()


@pytest.mark.parametrize(("article", "expected"), [
    ({"telegraph_url": "https://telegra.ph/x", "has_full_content": 0}, True),
    ({"telegraph_url": "https://telegra.ph/x", "has_full_content": False}, True),
    ({"telegraph_url": "https://telegra.ph/x", "has_full_content": None}, True),
    ({"telegraph_url": "https://telegra.ph/x", "has_full_content": 1}, False),
    ({"telegraph_url": "", "has_full_content": 0}, False),
    ({"has_full_content": None}, False),
])
def test_translation_pending_fulltext(article, expected):
    assert web_server._translation_pending_fulltext(article) is expected


def test_fetch_untranslated_skips_pending_telegraph_content_but_keeps_title(news_db):
    today = dt.datetime.now().strftime("%Y-%m-%d")
    _insert_article(
        news_db, id=1, date=today, telegraph_url="https://telegra.ph/pending",
        has_full_content=0,
    )
    _insert_article(
        news_db, id=2, date=today, telegraph_url="https://telegra.ph/complete",
        has_full_content=1,
    )
    _insert_article(news_db, id=3, date=today, has_full_content=0)

    content_only = web_server._fetch_untranslated_articles(
        {"auto_translate_title": False, "auto_translate_content": True}, limit=10
    )
    assert [row["id"] for row in content_only] == [3, 2]

    candidates = web_server._fetch_untranslated_articles(
        {"auto_translate_title": True, "auto_translate_content": True}, limit=10
    )
    pending = next(row for row in candidates if row["id"] == 1)
    assert pending["translate_title_needed"] is True
    assert pending["translate_content_needed"] is False


def test_translation_looks_truncated_is_conservative():
    source = "<p>" + ("English source sentence. " * 30) + "</p>"

    assert web_server._translation_looks_truncated(
        {"has_full_content": 1}, "<p>短译文</p>", source
    ) is True
    assert web_server._translation_looks_truncated(
        {"has_full_content": 0}, "<p>短译文</p>", source
    ) is False
    assert web_server._translation_looks_truncated(
        {"has_full_content": 1}, "<p>" + ("完整译文" * 80) + "</p>", source
    ) is False
    assert web_server._translation_looks_truncated(
        {"has_full_content": 1}, "<p>短</p>", "<p>short source</p>"
    ) is False


def test_stale_translation_scan_reaches_old_articles_across_pages(news_db):
    source = "<p>" + ("Historical English source sentence. " * 30) + "</p>"
    complete_translation = "<p>" + ("完整译文" * 100) + "</p>"
    for article_id in range(1, 62):
        _insert_article(
            news_db,
            id=article_id,
            date="2024-01-01",
            has_full_content=1,
            body_html=source,
        )
    _insert_translations(
        news_db,
        [(1, "<p>短译文</p>")]
        + [(article_id, complete_translation) for article_id in range(2, 62)],
    )

    pages = [web_server._fetch_stale_translation_articles(limit=25) for _ in range(3)]

    assert [[row["id"] for row in page] for page in pages] == [[], [], [1]]
    candidate = pages[-1][0]
    assert candidate["translation_stale"] is True
    assert candidate["translate_content_needed"] is True
    assert candidate["translate_title_needed"] is True
    assert web_server._translation_repair_cursor is None


def test_repair_cursor_resets_after_end_of_history_and_finds_new_old_candidate(news_db):
    source = "<p>" + ("Complete English source sentence. " * 30) + "</p>"
    _insert_article(
        news_db,
        id=10,
        date="2023-12-31",
        timestamp=10,
        has_full_content=1,
        body_html=source,
    )
    _insert_translations(news_db, [(10, "<p>" + ("完整译文" * 100) + "</p>")])

    assert web_server._fetch_stale_translation_articles(limit=1) == []
    assert web_server._translation_repair_cursor == (10, 10)
    assert web_server._fetch_stale_translation_articles(limit=1) == []
    assert web_server._translation_repair_cursor is None

    _insert_article(
        news_db,
        id=1,
        date="2020-01-01",
        timestamp=1,
        has_full_content=1,
        title="中文标题",
        body_html=source,
    )
    _insert_translations(news_db, [(1, "<p>短</p>")])

    candidates = web_server._fetch_stale_translation_articles(limit=2)
    assert [row["id"] for row in candidates] == [1]
    assert candidates[0]["translate_title_needed"] is False


def test_repair_cursor_uses_id_as_tiebreaker_for_equal_timestamps(news_db):
    source = "<p>" + ("English source sentence. " * 30) + "</p>"
    for article_id in range(1, 6):
        _insert_article(
            news_db,
            id=article_id,
            date="2022-01-01",
            timestamp=100,
            has_full_content=1,
            body_html=source,
        )
    _insert_translations(
        news_db,
        [(1, "<p>短</p>")]
        + [
            (article_id, "<p>" + ("完整译文" * 100) + "</p>")
            for article_id in range(2, 6)
        ],
    )

    pages = [web_server._fetch_stale_translation_articles(limit=2) for _ in range(3)]

    assert [[row["id"] for row in page] for page in pages] == [[], [], [1]]
    assert web_server._translation_repair_cursor is None


def test_stale_scan_page_contains_only_full_articles_with_nonempty_translations(news_db):
    source = "<p>" + ("English source sentence. " * 30) + "</p>"
    for article_id, has_full_content in ((1, 1), (2, 1), (3, 1), (4, 0)):
        _insert_article(
            news_db,
            id=article_id,
            date="2020-01-01",
            timestamp=article_id,
            has_full_content=has_full_content,
            body_html=source,
        )
    _insert_translations(
        news_db,
        [(1, "<p>短</p>"), (3, "   "), (4, "<p>短</p>")],
    )

    candidates = web_server._fetch_stale_translation_articles(limit=1)

    assert [row["id"] for row in candidates] == [1]


def test_repair_cursor_is_isolated_when_news_database_changes(
    news_db, tmp_path, monkeypatch
):
    source = "<p>" + ("English source sentence. " * 30) + "</p>"
    _insert_article(
        news_db,
        id=1,
        date="2024-01-01",
        timestamp=50,
        has_full_content=1,
        body_html=source,
    )
    _insert_translations(news_db, [(1, "<p>" + ("完整译文" * 100) + "</p>")])
    assert web_server._fetch_stale_translation_articles(limit=1) == []
    assert web_server._translation_repair_cursor == (50, 1)

    second_db = tmp_path / "second-news.db"
    _create_news_db(second_db)
    monkeypatch.setattr(web_server, "NEWS_DB", str(second_db))
    _insert_article(
        second_db,
        id=9,
        date="2021-01-01",
        timestamp=100,
        has_full_content=1,
        body_html=source,
    )
    _insert_translations(second_db, [(9, "<p>短</p>")])

    assert [
        row["id"] for row in web_server._fetch_stale_translation_articles(limit=1)
    ] == [9]


def test_concurrent_stale_scans_claim_distinct_keyset_pages(news_db, monkeypatch):
    source = "<p>" + ("English source sentence. " * 30) + "</p>"
    for article_id in (1, 2):
        _insert_article(
            news_db,
            id=article_id,
            date="2022-01-01",
            timestamp=article_id,
            has_full_content=1,
            body_html=source,
        )
    _insert_translations(news_db, [(1, "<p>短</p>"), (2, "<p>短</p>")])

    original_news_db_conn = web_server._news_db_conn
    first_connection_started = threading.Event()
    connection_count_lock = threading.Lock()
    connection_count = 0

    @contextmanager
    def delayed_first_connection():
        nonlocal connection_count
        with connection_count_lock:
            connection_count += 1
            is_first = connection_count == 1
        if is_first:
            first_connection_started.set()
            time.sleep(0.1)
        with original_news_db_conn() as conn:
            yield conn

    monkeypatch.setattr(web_server, "_news_db_conn", delayed_first_connection)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(web_server._fetch_stale_translation_articles, 1)
        assert first_connection_started.wait(timeout=1)
        second = pool.submit(web_server._fetch_stale_translation_articles, 1)
        claimed = first.result(timeout=2) + second.result(timeout=2)

    assert sorted(row["id"] for row in claimed) == [1, 2]


def test_auto_translation_publishes_marker_after_cache_without_body_writeback(monkeypatch):
    calls = []

    class TranslationService:
        def __init__(self, **kwargs):
            pass

        def translate_full(self, *args, **kwargs):
            return {"title": "中文标题", "html": "<p>中文正文</p>"}

    monkeypatch.setattr(web_server, "AIService", TranslationService)
    monkeypatch.setattr(
        web_server,
        "_save_article_translation",
        lambda article_id, title=None, body_html=None, **kwargs: calls.append(
            ("article", article_id, title, body_html)
        ) or True,
    )
    monkeypatch.setattr(
        web_server,
        "_save_ai_result",
        lambda article_id, **kwargs: calls.append(("cache", article_id, kwargs["translation"])),
    )
    monkeypatch.setattr(
        web_server,
        "_publish_translation_update",
        lambda article_id: calls.append(("marker", article_id)),
    )

    assert web_server._translate_article_background(
        {
            "id": 42,
            "title": "English title",
            "body_html": "<p>English body</p>",
            "translate_content_needed": True,
            "translate_title_needed": True,
        },
        {"api_key": "key", "endpoint": "https://example.test", "model": "model"},
    )

    assert [call[0] for call in calls] == ["article", "cache", "marker"]
    assert calls[0][3] is None


def test_stale_translation_bypasses_content_cache_and_reuses_cached_title(monkeypatch):
    translated = []
    saved = []

    class TranslationService:
        def __init__(self, *_args, **_kwargs):
            pass

        def translate_full(self, html, *_args, **_kwargs):
            translated.append(html)
            return {"html": "<p>完整译文</p>", "title": ""}

        def translate_title(self, *_args, **_kwargs):
            raise AssertionError("cached title must be reused")

    monkeypatch.setattr(web_server, "_SystemAIService", TranslationService)
    monkeypatch.setattr(web_server, "_save_article_translation", lambda *_a, **_k: False)
    monkeypatch.setattr(
        web_server,
        "_save_ai_result",
        lambda article_id, **kwargs: saved.append(kwargs["translation"]) or True,
    )
    monkeypatch.setattr(web_server, "_publish_translation_update", lambda _id: None)

    assert web_server._translate_article_background(
        {
            "id": 9,
            "title": "中文标题",
            "body_html": "<p>long English body</p>",
            "translation": json.dumps({"title": "旧标题", "html": "<p>短译文</p>"}),
            "translation_stale": True,
            "translate_content_needed": True,
            "translate_title_needed": False,
        },
        {"api_key": "key", "endpoint": "https://example.test", "model": "model"},
    )

    assert translated == ["<p>long English body</p>"]
    assert json.loads(saved[0]) == {"title": "旧标题", "html": "<p>完整译文</p>"}


def test_auto_translation_uses_remaining_batch_capacity_for_history(monkeypatch):
    scanned = []
    translated = []
    config = {"user_id": 7, "auto_translate_content": True}

    monkeypatch.setattr(web_server, "AUTO_TRANSLATION_BATCH_LIMIT", 3)
    monkeypatch.setattr(web_server, "_get_auto_translation_users", lambda: [config])
    monkeypatch.setattr(
        web_server,
        "_fetch_untranslated_articles",
        lambda received, limit: [{"id": 1, "title": "today"}],
    )

    def fetch_stale(limit):
        scanned.append(limit)
        return [{"id": 2, "title": "old-2"}, {"id": 3, "title": "old-3"}]

    monkeypatch.setattr(web_server, "_fetch_stale_translation_articles", fetch_stale)
    monkeypatch.setattr(
        web_server,
        "_translate_article_background",
        lambda article, received: translated.append(article["id"]) or True,
    )

    web_server._run_auto_translation_once()

    assert scanned == [2]
    assert translated == [1, 2, 3]


def test_content_only_historical_repair_does_not_translate_or_save_title(
    news_db, monkeypatch
):
    today = dt.datetime.now().strftime("%Y-%m-%d")
    source = "<p>" + ("Long English article body. " * 30) + "</p>"
    _insert_article(
        news_db,
        id=11,
        date=today,
        timestamp=110,
        title="English title",
        body_html=source,
        has_full_content=1,
    )
    _insert_translations(
        news_db,
        [(11, json.dumps({"title": "缓存标题", "html": "<p>短译文</p>"}))],
    )

    full_calls = []
    saved_titles = []
    saved_translations = []

    class TranslationService:
        def __init__(self, *_args, **_kwargs):
            pass

        def translate_full(self, html, *_args, title="", **_kwargs):
            full_calls.append((html, title))
            return {"html": "<p>完整译文</p>", "title": ""}

        def translate_title(self, *_args, **_kwargs):
            raise AssertionError("content-only repair must not translate the title")

    config = {
        "user_id": 7,
        "api_key": "key",
        "endpoint": "https://example.test",
        "model": "model",
        "auto_translate_title": False,
        "auto_translate_content": True,
    }
    monkeypatch.setattr(web_server, "AUTO_TRANSLATION_BATCH_LIMIT", 1)
    monkeypatch.setattr(web_server, "_get_auto_translation_users", lambda: [config])
    monkeypatch.setattr(web_server, "_SystemAIService", TranslationService)
    monkeypatch.setattr(
        web_server,
        "_save_article_translation",
        lambda article_id, title=None, body_html=None, **kwargs: saved_titles.append(title) or False,
    )
    monkeypatch.setattr(
        web_server,
        "_save_ai_result",
        lambda article_id, **kwargs: saved_translations.append(kwargs["translation"]) or True,
    )
    monkeypatch.setattr(web_server, "_publish_translation_update", lambda _id: None)

    web_server._run_auto_translation_once()

    assert full_calls == [(source, "")]
    assert saved_titles == [None]
    assert json.loads(saved_translations[0]) == {
        "title": "缓存标题",
        "html": "<p>完整译文</p>",
    }


def test_today_title_candidate_merges_duplicate_historical_content_repair(
    news_db, monkeypatch
):
    today = dt.datetime.now().strftime("%Y-%m-%d")
    source = "<p>" + ("Complete English article body. " * 30) + "</p>"
    _insert_article(
        news_db,
        id=12,
        date=today,
        timestamp=120,
        title="English title",
        body_html=source,
        has_full_content=1,
    )
    _insert_translations(
        news_db,
        [(12, json.dumps({"title": "缓存标题", "html": "<p>短译文</p>"}))],
    )

    candidates = []
    full_calls = []
    title_calls = []

    class TranslationService:
        def __init__(self, *_args, **_kwargs):
            pass

        def translate_full(self, html, *_args, title="", **_kwargs):
            full_calls.append((html, title))
            return {"html": "<p>完整译文</p>", "title": "新标题"}

        def translate_title(self, title, *_args, **_kwargs):
            title_calls.append(title)
            return "仅标题译文"

    config = {
        "user_id": 7,
        "api_key": "key",
        "endpoint": "https://example.test",
        "model": "model",
        "auto_translate_title": True,
        "auto_translate_content": True,
    }
    fetch_today = web_server._fetch_untranslated_articles
    translate_background = web_server._translate_article_background

    def marked_today(received, limit):
        rows = fetch_today(received, limit)
        assert [(row["id"], row["translate_title_needed"], row["translate_content_needed"])
                for row in rows] == [(12, True, False)]
        rows[0]["today_marker"] = "preserved"
        return rows

    def capture_background(article, received):
        candidates.append(dict(article))
        return translate_background(article, received)

    monkeypatch.setattr(web_server, "AUTO_TRANSLATION_BATCH_LIMIT", 2)
    monkeypatch.setattr(web_server, "_get_auto_translation_users", lambda: [config])
    monkeypatch.setattr(web_server, "_fetch_untranslated_articles", marked_today)
    monkeypatch.setattr(web_server, "_translate_article_background", capture_background)
    monkeypatch.setattr(web_server, "_SystemAIService", TranslationService)
    monkeypatch.setattr(web_server, "_save_article_translation", lambda *_a, **_k: True)
    monkeypatch.setattr(web_server, "_save_ai_result", lambda *_a, **_k: True)
    monkeypatch.setattr(web_server, "_publish_translation_update", lambda _id: None)

    web_server._run_auto_translation_once()

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate["id"] == 12
    assert candidate["today_marker"] == "preserved"
    assert candidate["translation_stale"] is True
    assert candidate["translate_content_needed"] is True
    assert candidate["translate_title_needed"] is True
    assert web_server._translation_repair_cursor == (120, 12)
    assert full_calls == [(source, "English title")]
    assert title_calls == []


def test_auto_translation_does_not_scan_history_when_today_fills_batch(monkeypatch):
    scanned = []
    translated = []
    config = {"user_id": 7, "auto_translate_content": True}

    monkeypatch.setattr(web_server, "AUTO_TRANSLATION_BATCH_LIMIT", 3)
    monkeypatch.setattr(web_server, "_get_auto_translation_users", lambda: [config])
    monkeypatch.setattr(
        web_server,
        "_fetch_untranslated_articles",
        lambda received, limit: [
            {"id": 1, "title": "today-1"},
            {"id": 2, "title": "today-2"},
            {"id": 3, "title": "today-3"},
        ],
    )
    monkeypatch.setattr(
        web_server,
        "_fetch_stale_translation_articles",
        lambda limit: scanned.append(limit) or [],
    )
    monkeypatch.setattr(
        web_server,
        "_translate_article_background",
        lambda article, received: translated.append(article["id"]) or True,
    )

    web_server._run_auto_translation_once()

    assert scanned == []
    assert translated == [1, 2, 3]


def test_auto_translation_deduplicates_ids_and_never_exceeds_batch(monkeypatch):
    scanned = []
    translated = []
    config = {"user_id": 7, "auto_translate_content": True}

    monkeypatch.setattr(web_server, "AUTO_TRANSLATION_BATCH_LIMIT", 3)
    monkeypatch.setattr(web_server, "_get_auto_translation_users", lambda: [config])
    monkeypatch.setattr(
        web_server,
        "_fetch_untranslated_articles",
        lambda received, limit: [
            {"id": 1, "title": "today"},
            {"id": 1, "title": "today duplicate"},
        ],
    )

    def fetch_stale(limit):
        scanned.append(limit)
        return [
            {"id": 1, "title": "history duplicate"},
            {"id": 2, "title": "old-2"},
            {"id": 3, "title": "old-3"},
            {"id": 4, "title": "old-4"},
        ]

    monkeypatch.setattr(web_server, "_fetch_stale_translation_articles", fetch_stale)
    monkeypatch.setattr(
        web_server,
        "_translate_article_background",
        lambda article, received: translated.append(article["id"]) or True,
    )

    web_server._run_auto_translation_once()

    assert scanned == [2]
    assert translated == [1, 2, 3]


def test_stale_scan_failure_does_not_skip_today_translations(monkeypatch):
    translated = []
    config = {"user_id": 7, "auto_translate_content": True}

    monkeypatch.setattr(web_server, "AUTO_TRANSLATION_BATCH_LIMIT", 3)
    monkeypatch.setattr(web_server, "_get_auto_translation_users", lambda: [config])
    monkeypatch.setattr(
        web_server,
        "_fetch_untranslated_articles",
        lambda received, limit: [{"id": 1, "title": "today"}],
    )

    def fail_stale_scan(limit):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(web_server, "_fetch_stale_translation_articles", fail_stale_scan)
    monkeypatch.setattr(
        web_server,
        "_translate_article_background",
        lambda article, received: translated.append(article["id"]) or True,
    )

    web_server._run_auto_translation_once()

    assert translated == [1]
