import sqlite3
import threading
import time

import pytest

import fetcher


def _patch_paths(monkeypatch, tmp_path):
    monkeypatch.setattr(fetcher, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(fetcher, "DB_FILE", tmp_path / "news.db")


def _insert(conn, **kw):
    cols = {
        "id": kw["id"],
        "title": kw.get("title", "t"),
        "timestamp": kw["timestamp"],
        "has_full_content": kw.get("has_full_content", 0),
        "telegraph_url": kw.get("telegraph_url", ""),
        "body_html": kw.get("body_html", "excerpt"),
        "origin_source": kw.get("origin_source", "未分类"),
        "thumb": kw.get("thumb", ""),
        "summary": kw.get("summary", "short"),
    }
    conn.execute(
        "INSERT INTO articles (id, title, timestamp, has_full_content, telegraph_url, "
        "body_html, origin_source, thumb, summary) VALUES "
        "(:id, :title, :timestamp, :has_full_content, :telegraph_url, :body_html, "
        ":origin_source, :thumb, :summary)",
        cols,
    )
    conn.commit()


class _CommitObserver:
    """Delegate to a real SQLite connection and inspect only durable commits."""

    def __init__(self, connection, observer):
        self._connection = connection
        self._observer = observer

    def __getattr__(self, name):
        return getattr(self._connection, name)

    def commit(self):
        self._connection.commit()
        self._observer()


def _fulltext_result(url):
    return {
        "body_html": f"<article>full body for {url}</article>",
        "images": [],
        "char_count": 20,
        "detected_source": "",
    }


def test_backfill_publishes_each_article_before_the_next_update(tmp_path, monkeypatch):
    _patch_paths(monkeypatch, tmp_path)
    writer = fetcher.init_db()
    now = int(time.time())
    _insert(writer, id=31, timestamp=now, telegraph_url="https://telegra.ph/31")
    _insert(writer, id=32, timestamp=now - 1, telegraph_url="https://telegra.ph/32")
    reader = sqlite3.connect(fetcher.DB_FILE)
    snapshots = []

    monkeypatch.setattr(fetcher, "fetch_telegraph", _fulltext_result)

    def observe_commit():
        snapshots.append(
            tuple(
                row[0]
                for row in reader.execute(
                    "SELECT has_full_content FROM articles WHERE id IN (31, 32) ORDER BY id"
                ).fetchall()
            )
        )

    try:
        assert fetcher.backfill_missing_fulltext(
            _CommitObserver(writer, observe_commit)
        ) == 2
    finally:
        reader.close()
        writer.close()

    assert len(snapshots) == 2
    assert sum(snapshots[0]) == 1
    assert sum(snapshots[1]) == 2


def test_backfill_holds_no_write_transaction_while_waiting_for_network_future(
    tmp_path, monkeypatch
):
    _patch_paths(monkeypatch, tmp_path)
    setup = fetcher.init_db()
    now = int(time.time())
    _insert(setup, id=41, timestamp=now, telegraph_url="https://telegra.ph/fast")
    _insert(setup, id=42, timestamp=now - 1, telegraph_url="https://telegra.ph/slow")
    setup.close()

    writer = sqlite3.connect(fetcher.DB_FILE, check_same_thread=False)
    writer.row_factory = sqlite3.Row
    reader = sqlite3.connect(fetcher.DB_FILE, timeout=0.1)
    reader.execute("PRAGMA busy_timeout=100")
    slow_started = threading.Event()
    release_slow = threading.Event()
    first_update_executed = threading.Event()
    first_commit_finished = threading.Event()
    errors = []

    def fetch(url):
        if url.endswith("/slow"):
            slow_started.set()
            assert release_slow.wait(5), "test did not release the slow network future"
        return _fulltext_result(url)

    class SignallingConnection(_CommitObserver):
        def execute(self, sql, *args):
            cursor = self._connection.execute(sql, *args)
            if "UPDATE articles" in sql:
                first_update_executed.set()
            return cursor

    monkeypatch.setattr(fetcher, "fetch_telegraph", fetch)
    observed = SignallingConnection(writer, first_commit_finished.set)

    def run_backfill():
        try:
            fetcher.backfill_missing_fulltext(observed)
        except Exception as exc:  # surfaced below after the worker is joined
            errors.append(exc)

    worker = threading.Thread(target=run_backfill)
    worker.start()
    try:
        assert slow_started.wait(2)
        assert first_update_executed.wait(2)
        committed_before_slow_future = first_commit_finished.wait(0.5)
        try:
            reader.execute("UPDATE articles SET title = 'reader write' WHERE id = 42")
            reader.commit()
            second_writer_succeeded = True
        except sqlite3.OperationalError:
            reader.rollback()
            second_writer_succeeded = False

        assert committed_before_slow_future
        assert second_writer_succeeded
        assert reader.execute(
            "SELECT has_full_content FROM articles WHERE id = 41"
        ).fetchone()[0] == 1
        assert reader.execute(
            "SELECT has_full_content FROM articles WHERE id = 42"
        ).fetchone()[0] == 0
    finally:
        release_slow.set()
        worker.join(5)
        reader.close()
        writer.close()

    assert not worker.is_alive()
    assert errors == []


def test_backfill_rolls_back_a_failed_article_update(tmp_path, monkeypatch):
    _patch_paths(monkeypatch, tmp_path)
    writer = fetcher.init_db()
    now = int(time.time())
    _insert(writer, id=51, timestamp=now, telegraph_url="https://telegra.ph/fail")
    writer.execute(
        """CREATE TRIGGER reject_fulltext
           BEFORE UPDATE OF has_full_content ON articles WHEN NEW.id = 51
           BEGIN SELECT RAISE(ABORT, 'forced backfill failure'); END"""
    )
    writer.commit()
    monkeypatch.setattr(fetcher, "fetch_telegraph", _fulltext_result)

    with pytest.raises(sqlite3.IntegrityError, match="forced backfill failure"):
        fetcher.backfill_missing_fulltext(writer)

    reader = sqlite3.connect(fetcher.DB_FILE, timeout=0.1)
    try:
        assert not writer.in_transaction
        reader.execute("BEGIN IMMEDIATE")
        reader.rollback()
    finally:
        reader.close()
        writer.close()


def test_backfill_invalidates_excerpt_translation(tmp_path, monkeypatch):
    _patch_paths(monkeypatch, tmp_path)
    conn = fetcher.init_db()
    _insert(
        conn,
        id=61,
        timestamp=int(time.time()),
        telegraph_url="https://telegra.ph/translated-excerpt",
        has_full_content=0,
        body_html="excerpt",
    )
    conn.execute(
        "CREATE TABLE ai_results ("
        "article_id INTEGER PRIMARY KEY, translation TEXT, translation_updated_at TEXT)"
    )
    conn.execute(
        "INSERT INTO ai_results VALUES (?, ?, ?)",
        (61, "<p>short translation</p>", "2026-08-01 12:00:00"),
    )
    conn.commit()
    monkeypatch.setattr(fetcher, "fetch_telegraph", _fulltext_result)

    assert fetcher.backfill_missing_fulltext(conn) == 1

    article = conn.execute(
        "SELECT has_full_content, body_html FROM articles WHERE id = 61"
    ).fetchone()
    cached = conn.execute(
        "SELECT translation, translation_updated_at FROM ai_results WHERE article_id = 61"
    ).fetchone()
    assert tuple(article) == (
        1,
        "<article>full body for https://telegra.ph/translated-excerpt</article>",
    )
    assert tuple(cached) == (None, None)
    conn.close()


def test_translation_invalidation_is_noop_when_table_is_absent(tmp_path):
    conn = sqlite3.connect(tmp_path / "news.db")
    try:
        assert fetcher._invalidate_stale_translation(conn, 1) is False
    finally:
        conn.close()


def test_translation_invalidation_supports_table_with_only_translation_column(tmp_path):
    conn = sqlite3.connect(tmp_path / "news.db")
    conn.execute(
        "CREATE TABLE ai_results (article_id INTEGER PRIMARY KEY, translation TEXT)"
    )
    conn.execute("INSERT INTO ai_results VALUES (1, '<p>stale</p>')")
    conn.commit()
    try:
        assert fetcher._invalidate_stale_translation(conn, 1) is True
        assert conn.execute(
            "SELECT translation FROM ai_results WHERE article_id = 1"
        ).fetchone()[0] is None
    finally:
        conn.close()


def test_translation_invalidation_surfaces_malformed_ai_results_schema(tmp_path):
    conn = sqlite3.connect(tmp_path / "news.db")
    conn.execute("CREATE TABLE ai_results (article_id INTEGER PRIMARY KEY)")
    conn.execute("INSERT INTO ai_results VALUES (1)")
    conn.commit()
    try:
        with pytest.raises(sqlite3.OperationalError, match="translation"):
            fetcher._invalidate_stale_translation(conn, 1)
    finally:
        conn.close()


def test_backfill_preserves_concurrent_web_translation_save(tmp_path, monkeypatch):
    """A web-side translation saved with a newer translation_updated_at between the
    backfill snapshot and the invalidation must NOT be clobbered."""
    _patch_paths(monkeypatch, tmp_path)
    conn = fetcher.init_db()
    _insert(
        conn,
        id=71,
        timestamp=int(time.time()),
        telegraph_url="https://telegra.ph/concurrent-translation",
        has_full_content=0,
        body_html="excerpt",
    )
    conn.execute(
        "CREATE TABLE ai_results ("
        "article_id INTEGER PRIMARY KEY, translation TEXT, translation_updated_at TEXT)"
    )
    conn.execute(
        "INSERT INTO ai_results VALUES (?, ?, ?)",
        (71, "<p>stale excerpt translation</p>", "2026-08-01 12:00:00"),
    )
    conn.commit()
    monkeypatch.setattr(fetcher, "fetch_telegraph", _fulltext_result)

    concurrent = sqlite3.connect(fetcher.DB_FILE, timeout=5)

    class ConcurrentSaveConnection(_CommitObserver):
        def __init__(self, connection, observer):
            super().__init__(connection, observer)
            self._web_saved = False

        def execute(self, sql, *args):
            if "UPDATE articles" in sql and not self._web_saved:
                self._web_saved = True
                concurrent.execute(
                    "UPDATE ai_results SET translation = ?, translation_updated_at = ? "
                    "WHERE article_id = ?",
                    (
                        "<p>user translation saved mid-backfill</p>",
                        "2026-08-10 09:00:00",
                        71,
                    ),
                )
                concurrent.commit()
            return self._connection.execute(sql, *args)

    observed = ConcurrentSaveConnection(conn, lambda: None)

    try:
        assert fetcher.backfill_missing_fulltext(observed) == 1
        article = conn.execute(
            "SELECT has_full_content, body_html FROM articles WHERE id = 71"
        ).fetchone()
        cached = conn.execute(
            "SELECT translation, translation_updated_at FROM ai_results WHERE article_id = 71"
        ).fetchone()
        assert tuple(article) == (
            1,
            "<article>full body for https://telegra.ph/concurrent-translation</article>",
        )
        assert tuple(cached) == (
            "<p>user translation saved mid-backfill</p>",
            "2026-08-10 09:00:00",
        )
    finally:
        concurrent.close()
        conn.close()


def test_backfill_rolls_back_article_upgrade_when_translation_invalidation_fails(
    tmp_path, monkeypatch
):
    _patch_paths(monkeypatch, tmp_path)
    conn = fetcher.init_db()
    _insert(
        conn,
        id=62,
        timestamp=int(time.time()),
        telegraph_url="https://telegra.ph/invalidation-fails",
        has_full_content=0,
        body_html="excerpt",
    )
    conn.execute(
        "CREATE TABLE ai_results ("
        "article_id INTEGER PRIMARY KEY, translation TEXT, translation_updated_at TEXT)"
    )
    conn.execute(
        "INSERT INTO ai_results VALUES (62, '<p>stale</p>', '2026-08-01 12:00:00')"
    )
    conn.execute(
        """CREATE TRIGGER reject_translation_invalidation
           BEFORE UPDATE OF translation ON ai_results WHEN OLD.article_id = 62
           BEGIN SELECT RAISE(ABORT, 'forced invalidation failure'); END"""
    )
    conn.commit()
    monkeypatch.setattr(fetcher, "fetch_telegraph", _fulltext_result)

    with pytest.raises(sqlite3.IntegrityError, match="forced invalidation failure"):
        fetcher.backfill_missing_fulltext(conn)

    assert not conn.in_transaction
    article = conn.execute(
        "SELECT has_full_content, body_html FROM articles WHERE id = 62"
    ).fetchone()
    cached = conn.execute(
        "SELECT translation, translation_updated_at FROM ai_results WHERE article_id = 62"
    ).fetchone()
    assert tuple(article) == (0, "excerpt")
    assert tuple(cached) == ("<p>stale</p>", "2026-08-01 12:00:00")
    conn.close()


def test_backfill_upgrades_recent_downgraded_telegraph_article(tmp_path, monkeypatch):
    _patch_paths(monkeypatch, tmp_path)
    conn = fetcher.init_db()
    now = int(time.time())
    _insert(conn, id=1, timestamp=now, telegraph_url="https://telegra.ph/x",
            has_full_content=0, body_html="short excerpt", summary="short",
            origin_source="未分类", thumb="")

    monkeypatch.setattr(fetcher, "fetch_telegraph", lambda url: {
        "body_html": "<article>full body</article>",
        "images": ["https://img/first.jpg"],
        "char_count": 9,
        "detected_source": "华尔街见闻",
    })

    upgraded = fetcher.backfill_missing_fulltext(conn)
    assert upgraded == 1

    row = conn.execute(
        "SELECT has_full_content, body_html, thumb, origin_source, summary "
        "FROM articles WHERE id = 1"
    ).fetchone()
    assert row[0] == 1
    assert row[1] == "<article>full body</article>"
    assert row[2] == "https://img/first.jpg"   # adopted first image (had no thumb)
    assert row[3] == "华尔街见闻"                # Telegraph-detected source applied
    assert row[4] == ""                         # excerpt summary cleared
    conn.close()


def test_backfill_skips_articles_beyond_the_recency_window(tmp_path, monkeypatch):
    _patch_paths(monkeypatch, tmp_path)
    conn = fetcher.init_db()
    old = int(time.time()) - (fetcher.BACKFILL_MAX_AGE_DAYS + 1) * 86400
    _insert(conn, id=2, timestamp=old, telegraph_url="https://telegra.ph/old",
            has_full_content=0)

    called = []
    monkeypatch.setattr(fetcher, "fetch_telegraph",
                        lambda url: called.append(url) or None)

    assert fetcher.backfill_missing_fulltext(conn) == 0
    assert called == []   # a genuinely dead/old URL ages out — never retried
    conn.close()


def test_backfill_ignores_articles_without_telegraph_url_or_already_full(tmp_path, monkeypatch):
    _patch_paths(monkeypatch, tmp_path)
    conn = fetcher.init_db()
    now = int(time.time())
    _insert(conn, id=3, timestamp=now, telegraph_url="", has_full_content=0)   # no url
    _insert(conn, id=4, timestamp=now, telegraph_url="https://telegra.ph/y",
            has_full_content=1)                                                # already full

    monkeypatch.setattr(fetcher, "fetch_telegraph",
                        lambda url: {"body_html": "x", "images": [], "char_count": 1})

    assert fetcher.backfill_missing_fulltext(conn) == 0
    conn.close()


def test_backfill_leaves_row_downgraded_when_fetch_still_fails(tmp_path, monkeypatch):
    _patch_paths(monkeypatch, tmp_path)
    conn = fetcher.init_db()
    now = int(time.time())
    _insert(conn, id=5, timestamp=now, telegraph_url="https://telegra.ph/z",
            has_full_content=0, body_html="excerpt")

    monkeypatch.setattr(fetcher, "fetch_telegraph", lambda url: None)  # still failing

    assert fetcher.backfill_missing_fulltext(conn) == 0
    row = conn.execute(
        "SELECT has_full_content, body_html FROM articles WHERE id = 5"
    ).fetchone()
    assert row[0] == 0
    assert row[1] == "excerpt"   # untouched, will be retried again next cycle
    conn.close()


def test_upsert_keeps_existing_fulltext_but_refreshes_title_state_on_empty_reingest(
    tmp_path, monkeypatch
):
    _patch_paths(monkeypatch, tmp_path)
    conn = fetcher.init_db()
    original_body = "<article>original full body</article>"
    fetcher.upsert_articles(conn, [{
        "id": 13,
        "title": "Original incoming title",
        "source": "Old feed",
        "feed_source": "Old feed",
        "origin_source": "Old origin",
        "time": "08:00",
        "date": "2026-08-01",
        "timestamp": 100,
        "thumb": "https://img/old.jpg",
        "has_full_content": True,
        "telegraph_url": "https://telegra.ph/original",
        "body_html": original_body,
        "summary": "Existing summary",
    }])
    conn.execute(
        "UPDATE articles SET original_title = ?, title_updated_at = ?, title_source = ? "
        "WHERE id = 13",
        ("Original incoming title", "2026-08-01 08:01:00", "title_summary"),
    )
    conn.commit()

    fetcher.upsert_articles(conn, [{
        "id": 13,
        "title": "Fresh incoming title",
        "source": "New feed",
        "feed_source": "New feed",
        "origin_source": "New origin",
        "time": "09:00",
        "date": "2026-08-02",
        "timestamp": 200,
        "thumb": "https://img/new.jpg",
        "has_full_content": False,
        "telegraph_url": "",
        "body_html": "",
        "summary": "",
    }])

    row = conn.execute(
        "SELECT title, source, feed_source, origin_source, time, date, timestamp, thumb, "
        "has_full_content, telegraph_url, body_html, original_body_html, summary, "
        "original_title, title_updated_at, title_source FROM articles WHERE id = 13"
    ).fetchone()
    assert tuple(row[:14]) == (
        "Fresh incoming title", "New feed", "New feed", "New origin", "09:00",
        "2026-08-02", 200, "https://img/new.jpg", 1,
        "https://telegra.ph/original", original_body, original_body, "Existing summary",
        "Fresh incoming title",
    )
    assert row[14] is not None and row[15] is None

    fetcher.upsert_articles(conn, [{
        "id": 13,
        "title": "Latest incoming title",
        "body_html": "<article>explicit replacement</article>",
    }])
    row = conn.execute(
        "SELECT body_html, original_body_html FROM articles WHERE id = 13"
    ).fetchone()
    assert tuple(row) == ("<article>explicit replacement</article>", original_body)
    conn.close()


def test_run_still_backfills_when_there_are_no_new_messages(tmp_path, monkeypatch):
    # A no-new-messages cycle is exactly when a previously failed Telegraph fetch would
    # otherwise never get retried — backfill must still run so it doesn't age out of the
    # window and stay permanently downgraded.
    monkeypatch.setattr(fetcher, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(fetcher, "OUTPUT_FILE", tmp_path / "news.json")
    monkeypatch.setattr(fetcher, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(fetcher, "DB_FILE", tmp_path / "news.db")
    monkeypatch.setattr(fetcher, "PROGRESS_FILE", tmp_path / "progress.json")
    monkeypatch.setattr(fetcher, "fetch_all_new_messages", lambda state: ([], 0))

    calls = []
    monkeypatch.setattr(fetcher, "backfill_missing_fulltext", lambda conn: calls.append(True))

    fetcher.run()

    assert calls == [True]   # backfill ran on the empty cycle


def test_wechat_fetch_uses_the_longer_dedicated_timeout(monkeypatch):
    # WeChat has no backfill safety net, so its full-text fetch must keep the longer
    # timeout rather than the short Telegraph one.
    assert fetcher.WECHAT_FULLTEXT_TIMEOUT > fetcher.FULLTEXT_TIMEOUT

    seen = {}
    monkeypatch.setattr(fetcher, "safe_get",
                        lambda url, **kw: seen.update(timeout=kw.get("timeout")) or _raise())
    fetcher.fetch_wechat_article("https://mp.weixin.qq.com/s/abc")
    assert seen["timeout"] == fetcher.WECHAT_FULLTEXT_TIMEOUT


def _raise():
    raise RuntimeError("stop after capturing timeout")
