import json
import logging
import sqlite3
import time
import weakref
from collections import Counter

import fetcher
import refresh_server


# ─── fetcher.py: streaming ingest + progress file ──────────────────────────

def _patch_fetcher_paths(monkeypatch, tmp_path):
    monkeypatch.setattr(fetcher, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(fetcher, "OUTPUT_FILE", tmp_path / "news.json")
    monkeypatch.setattr(fetcher, "STATE_FILE", tmp_path / "fetcher_state.json")
    monkeypatch.setattr(fetcher, "DB_FILE", tmp_path / "news.db")
    monkeypatch.setattr(fetcher, "PROGRESS_FILE", tmp_path / "fetch_progress.json")


def test_write_fetch_progress_writes_atomic_json(tmp_path, monkeypatch):
    _patch_fetcher_paths(monkeypatch, tmp_path)
    monkeypatch.setattr(fetcher, "FETCH_JOB_ID", "job-42")

    fetcher.write_fetch_progress(3, 10, [103, 101, 103, 102])

    payload = json.loads(fetcher.PROGRESS_FILE.read_text(encoding="utf-8"))
    assert payload["inserted"] == 3
    assert payload["inserted_ids"] == [101, 102, 103]
    assert payload["total_messages"] == 10
    assert payload["job_id"] == "job-42"
    assert payload["updated_at"] > 0
    assert not fetcher.PROGRESS_FILE.with_suffix(".json.tmp").exists()


def test_write_fetch_progress_drops_invalid_ids(tmp_path, monkeypatch):
    _patch_fetcher_paths(monkeypatch, tmp_path)
    monkeypatch.setattr(fetcher, "FETCH_JOB_ID", "job-42")

    fetcher.write_fetch_progress(2, 2, [7, "8", 0, -1, None, "bad"])

    payload = json.loads(fetcher.PROGRESS_FILE.read_text(encoding="utf-8"))
    assert payload["inserted_ids"] == [7, 8]


def test_run_streams_articles_into_sqlite_in_batches_before_cycle_completes(tmp_path, monkeypatch):
    _patch_fetcher_paths(monkeypatch, tmp_path)
    monkeypatch.setattr(fetcher, "STREAM_BATCH_SIZE", 2)
    monkeypatch.setattr(fetcher, "STREAM_BATCH_SECONDS", 100.0)  # size-triggered only

    messages = [{"id": i} for i in range(1, 7)]
    monkeypatch.setattr(fetcher, "fetch_all_new_messages", lambda state: (messages, max((m["id"] for m in messages), default=0)))
    monkeypatch.setattr(
        fetcher, "process_message",
        lambda msg, orig_id: {
            "id": orig_id, "title": f"t{orig_id}", "source": "s",
            "feed_source": "s", "timestamp": orig_id,
        },
    )

    batch_sizes = []
    sync_sources_flags = []
    original_upsert = fetcher.upsert_articles

    def tracking_upsert(conn, entries, sync_sources=True):
        batch_sizes.append(len(entries))
        sync_sources_flags.append(sync_sources)
        return original_upsert(conn, entries, sync_sources=sync_sources)

    monkeypatch.setattr(fetcher, "upsert_articles", tracking_upsert)

    ensure_sources_calls = []
    original_ensure_sources = fetcher.maintain_source_categories

    def tracking_ensure_sources(conn, **kwargs):
        ensure_sources_calls.append(True)
        return original_ensure_sources(conn, **kwargs)

    monkeypatch.setattr(fetcher, "maintain_source_categories", tracking_ensure_sources)

    fetcher.run()

    # More than one upsert call proves articles were committed incrementally during
    # processing, not just once at the very end.
    assert len(batch_sizes) >= 2
    assert sum(batch_sizes) == 6
    # Every streamed batch skips the expensive full-table source sync — since every
    # entry made it in via streaming, run() doesn't fall back to a trailing
    # full-pass upsert_articles(..., sync_sources=True); it runs the one
    # full-table source sync directly (ensure_article_sources) instead.
    assert sync_sources_flags == [False] * len(sync_sources_flags)
    assert ensure_sources_calls == [True]

    conn = sqlite3.connect(fetcher.DB_FILE)
    count = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
    conn.close()
    assert count == 6

    progress = json.loads(fetcher.PROGRESS_FILE.read_text(encoding="utf-8"))
    assert progress["inserted"] == 6
    assert progress["inserted_ids"] == [1, 2, 3, 4, 5, 6]
    assert progress["inserted"] == len(progress["inserted_ids"])
    assert progress["total_messages"] == 6


def test_run_releases_large_completed_payloads_and_builds_bounded_mirror_from_sqlite(
    tmp_path, monkeypatch
):
    _patch_fetcher_paths(monkeypatch, tmp_path)
    monkeypatch.setattr(fetcher, "STREAM_BATCH_SIZE", 5)
    monkeypatch.setattr(fetcher, "STREAM_BATCH_SECONDS", 100.0)
    monkeypatch.setattr(fetcher, "NEWS_JSON_MIRROR_LIMIT", 3)

    # This durable row is not part of the provider response. Seeing it in the mirror
    # proves the mirror is rebuilt from SQLite rather than the just-processed payloads.
    seed = fetcher.init_db()
    fetcher.upsert_articles(
        seed,
        [
            {
                "id": 1000,
                "title": "durable history",
                "source": "s",
                "feed_source": "s",
                "timestamp": 1000,
                "has_full_content": True,
                "body_html": "historical body",
            }
        ],
    )
    seed.close()

    messages = [{"id": article_id} for article_id in range(1, 101)]
    monkeypatch.setattr(
        fetcher,
        "fetch_all_new_messages",
        lambda state: (messages, 100),
    )

    released_ids = []

    class TrackedEntry(dict):
        def __del__(self):
            released_ids.append(self["id"])

    def process_message(msg, orig_id):
        return TrackedEntry(
            id=orig_id,
            title=f"t{orig_id}",
            source="s",
            feed_source="s",
            timestamp=orig_id,
            has_full_content=True,
            body_html=(f"body-{orig_id}-" + "x" * (128 * 1024)),
        )

    monkeypatch.setattr(fetcher, "process_message", process_message)

    observed_future_values = []
    observed_future_mapping = {}
    real_as_completed = fetcher.as_completed

    def observing_as_completed(futures):
        observed_future_values.extend(futures.values())
        observed_future_mapping["mapping"] = futures
        # Iterate over a snapshot so production may remove completed futures from
        # its own explicit mapping without invalidating this test iterator.
        yield from real_as_completed(tuple(futures))

    monkeypatch.setattr(fetcher, "as_completed", observing_as_completed)

    committed_batch_refs = []
    original_upsert = fetcher.upsert_articles

    def tracking_upsert(conn, entries, sync_sources=True):
        if not sync_sources:
            committed_batch_refs.append(entries)
        return original_upsert(conn, entries, sync_sources=sync_sources)

    monkeypatch.setattr(fetcher, "upsert_articles", tracking_upsert)

    mirror_snapshots = []
    released_at_mirror = []

    def capture_mirror(entries):
        mirror_snapshots.append([dict(entry) for entry in entries])
        released_at_mirror.extend(released_ids)

    monkeypatch.setattr(fetcher, "write_news_json_mirror", capture_mirror)

    fetcher.run()

    assert observed_future_values == list(range(1, 101))
    assert observed_future_mapping["mapping"] == {}
    assert committed_batch_refs
    assert all(batch == [] for batch in committed_batch_refs)
    # Every provider-produced entry should already be unreachable at mirror time.
    # The mirror rows are fresh bounded dicts loaded from SQLite.
    assert sorted(released_at_mirror) == list(range(1, 101))
    assert [[entry["id"] for entry in snapshot] for snapshot in mirror_snapshots] == [
        [1000, 100, 99]
    ]


def test_run_retries_only_failed_stream_batch_once_without_refetching_provider(
    tmp_path, monkeypatch
):
    _patch_fetcher_paths(monkeypatch, tmp_path)
    monkeypatch.setattr(fetcher, "STREAM_BATCH_SIZE", 2)
    monkeypatch.setattr(fetcher, "STREAM_BATCH_SECONDS", 100.0)

    messages = [{"id": article_id} for article_id in range(1, 7)]
    monkeypatch.setattr(
        fetcher,
        "fetch_all_new_messages",
        lambda state: (messages, 6),
    )
    provider_calls = []

    def process_message(msg, orig_id):
        provider_calls.append(orig_id)
        return {
            "id": orig_id,
            "title": f"t{orig_id}",
            "source": "s",
            "feed_source": "s",
            "timestamp": orig_id,
            "has_full_content": True,
            "body_html": f"body {orig_id}",
        }

    monkeypatch.setattr(fetcher, "process_message", process_message)

    upsert_calls = []
    batch_refs = []
    failed_once = False
    original_upsert = fetcher.upsert_articles

    def flaky_upsert(conn, entries, sync_sources=True):
        nonlocal failed_once
        ids = [entry["id"] for entry in entries]
        if not sync_sources:
            upsert_calls.append(ids)
            batch_refs.append(entries)
            if len(upsert_calls) == 2 and not failed_once:
                failed_once = True
                raise sqlite3.OperationalError("transient batch failure")
        return original_upsert(conn, entries, sync_sources=sync_sources)

    monkeypatch.setattr(fetcher, "upsert_articles", flaky_upsert)

    ensure_sources_calls = []
    original_ensure_sources = fetcher.maintain_source_categories

    def tracking_ensure_sources(conn, **kwargs):
        ensure_sources_calls.append(True)
        return original_ensure_sources(conn, **kwargs)

    monkeypatch.setattr(fetcher, "maintain_source_categories", tracking_ensure_sources)
    backfill_calls = []
    monkeypatch.setattr(
        fetcher,
        "backfill_missing_fulltext",
        lambda conn: backfill_calls.append(True) or 0,
    )

    fetcher.run()

    assert Counter(provider_calls) == Counter({article_id: 1 for article_id in range(1, 7)})
    assert len(upsert_calls) == 4
    assert sorted(article_id for batch in upsert_calls[:3] for article_id in batch) == list(
        range(1, 7)
    )
    assert upsert_calls[3] == upsert_calls[1]
    assert all(batch == [] for batch in batch_refs)
    assert ensure_sources_calls == [True]
    assert backfill_calls == [True]

    conn = sqlite3.connect(fetcher.DB_FILE)
    ids = [row[0] for row in conn.execute("SELECT id FROM articles ORDER BY id")]
    conn.close()
    assert ids == [1, 2, 3, 4, 5, 6]

    progress = json.loads(fetcher.PROGRESS_FILE.read_text(encoding="utf-8"))
    assert progress["inserted"] == 6
    assert progress["inserted_ids"] == [1, 2, 3, 4, 5, 6]


def test_terminal_batch_failure_keeps_cursor_for_next_cycle_and_reports_only_commits(
    tmp_path, monkeypatch, caplog
):
    """Missing persistence must fail the cycle cursor, not lose provider messages."""
    _patch_fetcher_paths(monkeypatch, tmp_path)
    monkeypatch.setattr(fetcher, "STREAM_BATCH_SIZE", 2)
    monkeypatch.setattr(fetcher, "STREAM_BATCH_SECONDS", 100.0)
    fetcher.save_state({"last_seen_id": 0})

    all_messages = [{"id": article_id} for article_id in range(1, 5)]
    fetch_states = []

    def fetch_messages(state):
        fetch_states.append(state["last_seen_id"])
        return (
            [message for message in all_messages if message["id"] > state["last_seen_id"]],
            4,
        )

    monkeypatch.setattr(fetcher, "fetch_all_new_messages", fetch_messages)
    provider_calls = []

    def process_message(msg, orig_id):
        provider_calls.append(orig_id)
        return {
            "id": orig_id,
            "title": f"t{orig_id}",
            "source": "s",
            "feed_source": "s",
            "timestamp": orig_id,
            "has_full_content": True,
            "body_html": f"body {orig_id}",
        }

    monkeypatch.setattr(fetcher, "process_message", process_message)

    original_upsert = fetcher.upsert_articles
    failed_ids = None
    failed_attempts = 0
    first_cycle_calls = []
    first_cycle = True

    def persistently_flaky_upsert(conn, entries, sync_sources=True):
        nonlocal failed_ids, failed_attempts
        ids = tuple(sorted(entry["id"] for entry in entries))
        if not sync_sources and first_cycle:
            first_cycle_calls.append(ids)
            if failed_ids is None:
                failed_ids = ids
            if ids == failed_ids:
                failed_attempts += 1
                raise sqlite3.OperationalError("persistent batch failure")
        return original_upsert(conn, entries, sync_sources=sync_sources)

    monkeypatch.setattr(fetcher, "upsert_articles", persistently_flaky_upsert)

    fetcher.run()

    assert failed_ids is not None
    assert failed_attempts == 2
    assert first_cycle_calls.count(failed_ids) == 2
    assert fetcher.load_state()["last_seen_id"] == 0
    assert f"{len(failed_ids)} message(s) failed" in caplog.text

    successful_ids = sorted(set(range(1, 5)) - set(failed_ids))
    conn = sqlite3.connect(fetcher.DB_FILE)
    first_cycle_db_ids = [
        row[0] for row in conn.execute("SELECT id FROM articles ORDER BY id")
    ]
    conn.close()
    assert first_cycle_db_ids == successful_ids
    first_progress = json.loads(fetcher.PROGRESS_FILE.read_text(encoding="utf-8"))
    assert first_progress["inserted"] == len(successful_ids)
    assert first_progress["inserted_ids"] == successful_ids

    # The provider honors last_seen_id. A preserved cursor makes the missing IDs
    # eligible next cycle; committed IDs may be idempotently observed again.
    first_cycle = False
    fetcher.run()

    assert fetch_states == [0, 0]
    assert Counter(provider_calls) == Counter(
        {article_id: 2 for article_id in range(1, 5)}
    )
    conn = sqlite3.connect(fetcher.DB_FILE)
    final_ids = [row[0] for row in conn.execute("SELECT id FROM articles ORDER BY id")]
    conn.close()
    assert final_ids == [1, 2, 3, 4]
    assert fetcher.load_state()["last_seen_id"] == 4


def test_rollback_exception_releases_all_future_results_before_backfill(
    tmp_path, monkeypatch
):
    """A rollback failure must not retain completed Future payloads for the cycle."""
    _patch_fetcher_paths(monkeypatch, tmp_path)
    monkeypatch.setattr(fetcher, "STREAM_BATCH_SIZE", 1)
    monkeypatch.setattr(fetcher, "STREAM_BATCH_SECONDS", 100.0)
    fetcher.save_state({"last_seen_id": 0})

    messages = [{"id": article_id} for article_id in range(1, 21)]
    monkeypatch.setattr(fetcher, "fetch_all_new_messages", lambda state: (messages, 20))
    entry_refs = []

    class WeakEntry(dict):
        pass

    def process_message(msg, orig_id):
        entry = WeakEntry(
            id=orig_id,
            title=f"t{orig_id}",
            source="s",
            feed_source="s",
            timestamp=orig_id,
            has_full_content=True,
            body_html=f"body-{orig_id}-" + "x" * (256 * 1024),
        )
        entry_refs.append(weakref.ref(entry))
        return entry

    monkeypatch.setattr(fetcher, "process_message", process_message)

    real_init_db = fetcher.init_db
    init_calls = 0
    rollback_calls = 0
    stream_connection = None

    class RollbackRaisingConnection:
        def __init__(self, connection):
            self.connection = connection

        def __getattr__(self, name):
            return getattr(self.connection, name)

        def rollback(self):
            nonlocal rollback_calls
            rollback_calls += 1
            raise sqlite3.OperationalError("rollback also failed")

    def init_db():
        nonlocal init_calls, stream_connection
        init_calls += 1
        connection = real_init_db()
        if init_calls == 2:
            stream_connection = RollbackRaisingConnection(connection)
            return stream_connection
        return connection

    monkeypatch.setattr(fetcher, "init_db", init_db)
    real_upsert = fetcher.upsert_articles
    failed_once = False

    def fail_first_stream_write(conn, entries, sync_sources=True):
        nonlocal failed_once
        if conn is stream_connection and not sync_sources and not failed_once:
            failed_once = True
            raise sqlite3.OperationalError("stream write failed")
        return real_upsert(conn, entries, sync_sources=sync_sources)

    monkeypatch.setattr(fetcher, "upsert_articles", fail_first_stream_write)
    released_before_backfill = []

    def observe_backfill(conn):
        released_before_backfill.append(sum(ref() is None for ref in entry_refs))
        return 0

    monkeypatch.setattr(fetcher, "backfill_missing_fulltext", observe_backfill)

    fetcher.run()

    assert rollback_calls == 1
    assert entry_refs
    assert released_before_backfill == [len(entry_refs)]


def test_outer_completion_loop_exception_cancels_and_releases_future_results(
    tmp_path, monkeypatch
):
    """Unexpected as_completed failure must clean every owned result before sync."""
    _patch_fetcher_paths(monkeypatch, tmp_path)
    monkeypatch.setattr(fetcher, "STREAM_BATCH_SIZE", 100)
    monkeypatch.setattr(fetcher, "STREAM_BATCH_SECONDS", 100.0)
    fetcher.save_state({"last_seen_id": 0})

    messages = [{"id": article_id} for article_id in range(1, 21)]
    monkeypatch.setattr(fetcher, "fetch_all_new_messages", lambda state: (messages, 20))
    entry_refs = []

    class WeakEntry(dict):
        pass

    def process_message(msg, orig_id):
        entry = WeakEntry(
            id=orig_id,
            title=f"t{orig_id}",
            source="s",
            feed_source="s",
            timestamp=orig_id,
            has_full_content=True,
            body_html=f"body-{orig_id}-" + "x" * (256 * 1024),
        )
        entry_refs.append(weakref.ref(entry))
        return entry

    monkeypatch.setattr(fetcher, "process_message", process_message)
    real_as_completed = fetcher.as_completed

    def raising_as_completed(futures):
        iterator = real_as_completed(tuple(futures))
        yield next(iterator)
        raise RuntimeError("completion iterator failed")

    monkeypatch.setattr(fetcher, "as_completed", raising_as_completed)
    released_before_backfill = []

    def observe_backfill(conn):
        released_before_backfill.append(sum(ref() is None for ref in entry_refs))
        return 0

    monkeypatch.setattr(fetcher, "backfill_missing_fulltext", observe_backfill)

    fetcher.run()

    # Futures that had not started may be cancelled and therefore create no entry.
    # Every large result that was created must be unreachable before backfill.
    assert entry_refs
    assert released_before_backfill == [len(entry_refs)]
    assert fetcher.load_state()["last_seen_id"] == 0


def test_post_commit_logging_failure_does_not_retry_committed_batch(
    tmp_path, monkeypatch
):
    """A sink failure after commit cannot reclassify a durable batch as fallback."""
    _patch_fetcher_paths(monkeypatch, tmp_path)
    monkeypatch.setattr(fetcher, "STREAM_BATCH_SIZE", 2)
    monkeypatch.setattr(fetcher, "STREAM_BATCH_SECONDS", 100.0)
    messages = [{"id": article_id} for article_id in range(1, 5)]
    monkeypatch.setattr(fetcher, "fetch_all_new_messages", lambda state: (messages, 4))
    monkeypatch.setattr(
        fetcher,
        "process_message",
        lambda msg, orig_id: {
            "id": orig_id,
            "title": f"t{orig_id}",
            "source": "s",
            "feed_source": "s",
            "timestamp": orig_id,
        },
    )

    original_upsert = fetcher.upsert_articles
    streamed_batches = []

    def tracking_upsert(conn, entries, sync_sources=True):
        if not sync_sources:
            streamed_batches.append(tuple(sorted(entry["id"] for entry in entries)))
        return original_upsert(conn, entries, sync_sources=sync_sources)

    monkeypatch.setattr(fetcher, "upsert_articles", tracking_upsert)

    class RaiseFirstSqliteInfo(logging.Handler):
        def __init__(self):
            super().__init__()
            self.raised = False

        def emit(self, record):
            if not self.raised and record.getMessage().startswith("SQLite: upserted"):
                self.raised = True
                raise RuntimeError("post-commit log sink failed")

    handler = RaiseFirstSqliteInfo()
    old_level = fetcher.log.level
    fetcher.log.setLevel(logging.INFO)
    fetcher.log.addHandler(handler)
    try:
        fetcher.run()
    finally:
        fetcher.log.removeHandler(handler)
        fetcher.log.setLevel(old_level)

    assert handler.raised is True
    assert len(streamed_batches) == 2
    assert sorted(article_id for batch in streamed_batches for article_id in batch) == [
        1,
        2,
        3,
        4,
    ]


def test_upsert_has_no_fallible_query_after_successful_commit(tmp_path, monkeypatch):
    """The durable commit is the final fallible database operation for a batch."""
    _patch_fetcher_paths(monkeypatch, tmp_path)
    raw_connection = fetcher.init_db()

    class RejectPostCommitQuery:
        def __init__(self, connection):
            self.connection = connection
            self.committed = False
            self.commit_calls = 0

        def __getattr__(self, name):
            return getattr(self.connection, name)

        def execute(self, sql, *args):
            if self.committed:
                raise RuntimeError("query attempted after commit")
            return self.connection.execute(sql, *args)

        def commit(self):
            self.connection.commit()
            self.commit_calls += 1
            self.committed = True

    connection = RejectPostCommitQuery(raw_connection)
    try:
        persisted_ids = fetcher.upsert_articles(
            connection,
            [
                {
                    "id": 7,
                    "title": "t7",
                    "source": "s",
                    "feed_source": "s",
                    "timestamp": 7,
                }
            ],
            sync_sources=False,
        )
    finally:
        raw_connection.close()

    assert persisted_ids == [7]
    assert connection.commit_calls == 1


def test_load_recent_articles_for_mirror_has_stable_order_and_limit(
    tmp_path, monkeypatch
):
    _patch_fetcher_paths(monkeypatch, tmp_path)
    conn = fetcher.init_db()
    fetcher.upsert_articles(
        conn,
        [
            {
                "id": article_id,
                "title": f"t{article_id}",
                "source": "s",
                "feed_source": "s",
                "timestamp": 100 if article_id <= 3 else 99,
                "body_html": f"body {article_id}",
            }
            for article_id in range(1, 6)
        ],
    )

    try:
        rows = fetcher._load_recent_articles_for_mirror(conn, limit=4)
    finally:
        conn.close()

    assert [row["id"] for row in rows] == [3, 2, 1, 5]
    assert rows[0]["body_html"] == "body 3"


def test_news_json_mirror_round_trip_preserves_wire_types_and_title_metadata(
    tmp_path, monkeypatch
):
    """SQLite snapshot JSON must remain a lossless legacy recovery payload."""
    _patch_fetcher_paths(monkeypatch, tmp_path)
    conn = fetcher.init_db()
    fetcher.upsert_articles(
        conn,
        [
            {
                "id": 77,
                "title": "Shared title",
                "source": "Feed",
                "feed_source": "Feed",
                "origin_source": "Origin",
                "time": "09:30",
                "date": "2026-08-10",
                "timestamp": 177,
                "thumb": "https://img/77.jpg",
                "has_full_content": True,
                "telegraph_url": "https://telegra.ph/77",
                "body_html": "<article>original body</article>",
                "summary": "summary",
            },
            {
                "id": 76,
                "title": "Default fields",
                "timestamp": 176,
                "has_full_content": False,
            },
        ],
    )
    conn.execute(
        "UPDATE articles SET body_html = ?, original_title = ?, "
        "title_updated_at = ?, title_source = ? WHERE id = ?",
        (
            "<article>current translated body</article>",
            "Original title",
            "2026-08-10 09:31:00",
            "title_summary",
            77,
        ),
    )
    conn.commit()

    snapshot = fetcher._load_recent_articles_for_mirror(conn, limit=10)
    by_id = {entry["id"]: entry for entry in snapshot}
    assert by_id[77]["has_full_content"] is True
    assert by_id[76]["has_full_content"] is False
    assert by_id[77]["original_title"] == "Original title"
    assert by_id[77]["title_updated_at"] == "2026-08-10 09:31:00"
    assert by_id[77]["title_source"] == "title_summary"
    assert by_id[76]["original_title"] is None
    assert by_id[76]["title_updated_at"] is None
    assert by_id[76]["title_source"] is None
    assert by_id[76]["source"] == ""
    assert by_id[76]["body_html"] == ""
    assert by_id[76]["original_body_html"] == ""

    fetcher.write_news_json_mirror(snapshot)
    wire_items = json.loads(fetcher.OUTPUT_FILE.read_text(encoding="utf-8"))["items"]
    wire_by_id = {entry["id"]: entry for entry in wire_items}
    assert wire_by_id[77]["has_full_content"] is True
    assert wire_by_id[76]["has_full_content"] is False
    assert wire_by_id[77]["original_title"] == "Original title"
    assert wire_by_id[76]["original_title"] is None

    # Recreate articles solely through the existing legacy migration entry point.
    conn.execute("DELETE FROM articles")
    conn.commit()
    fetcher.migrate_news_json(conn)
    restored = conn.execute(
        "SELECT title, has_full_content, body_html, original_body_html, "
        "original_title, title_updated_at, title_source "
        "FROM articles WHERE id = 77"
    ).fetchone()
    defaults = conn.execute(
        "SELECT has_full_content, source, body_html, original_body_html, "
        "original_title, title_updated_at, title_source "
        "FROM articles WHERE id = 76"
    ).fetchone()
    conn.close()

    assert tuple(restored) == (
        "Shared title",
        1,
        "<article>current translated body</article>",
        "<article>original body</article>",
        "Original title",
        "2026-08-10 09:31:00",
        "title_summary",
    )
    assert tuple(defaults) == (0, "", "", "", None, None, None)


def test_news_json_mirror_is_truncated_and_unindented(tmp_path, monkeypatch):
    _patch_fetcher_paths(monkeypatch, tmp_path)
    monkeypatch.setattr(fetcher, "NEWS_JSON_MIRROR_LIMIT", 3)

    messages = [{"id": i} for i in range(1, 6)]
    monkeypatch.setattr(fetcher, "fetch_all_new_messages", lambda state: (messages, max(m["id"] for m in messages)))
    monkeypatch.setattr(
        fetcher, "process_message",
        lambda msg, orig_id: {
            "id": orig_id, "title": f"t{orig_id}", "source": "s",
            "feed_source": "s", "timestamp": orig_id,
        },
    )

    fetcher.run()

    raw = fetcher.OUTPUT_FILE.read_text(encoding="utf-8")
    assert "\n" not in raw  # no pretty-printing indent — this file is machine-read only
    data = json.loads(raw)
    assert len(data["items"]) == 3
    # Keeps the most recent (highest timestamp) entries, not an arbitrary slice.
    assert sorted(item["id"] for item in data["items"]) == [3, 4, 5]


def test_news_json_mirror_failure_does_not_fail_the_cycle(tmp_path, monkeypatch):
    _patch_fetcher_paths(monkeypatch, tmp_path)

    messages = [{"id": 1}]
    monkeypatch.setattr(fetcher, "fetch_all_new_messages", lambda state: (messages, 1))
    monkeypatch.setattr(
        fetcher, "process_message",
        lambda msg, orig_id: {
            "id": orig_id, "title": "t1", "source": "s",
            "feed_source": "s", "timestamp": orig_id,
        },
    )

    def boom(entries):
        raise RuntimeError("disk full")

    monkeypatch.setattr(fetcher, "write_news_json_mirror", boom)

    fetcher.run()  # must not raise despite the news.json mirror blowing up

    conn = sqlite3.connect(fetcher.DB_FILE)
    count = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
    conn.close()
    assert count == 1  # SQLite already has the article regardless of the mirror


def test_upsert_articles_skips_source_sync_when_disabled(tmp_path, monkeypatch):
    _patch_fetcher_paths(monkeypatch, tmp_path)
    conn = fetcher.init_db()

    sync_calls = []
    monkeypatch.setattr(fetcher, "ensure_article_sources", lambda c: sync_calls.append(c) or 0)

    fetcher.upsert_articles(conn, [
        {"id": 1, "title": "t1", "source": "s", "feed_source": "s", "timestamp": 1},
    ], sync_sources=False)
    assert sync_calls == []

    fetcher.upsert_articles(conn, [
        {"id": 2, "title": "t2", "source": "s", "feed_source": "s", "timestamp": 2},
    ])  # default stays True
    assert sync_calls == [conn]

    count = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
    conn.close()
    assert count == 2


def test_streaming_ingest_still_respects_deleted_articles(tmp_path, monkeypatch):
    _patch_fetcher_paths(monkeypatch, tmp_path)
    monkeypatch.setattr(fetcher, "STREAM_BATCH_SIZE", 2)
    monkeypatch.setattr(fetcher, "STREAM_BATCH_SECONDS", 100.0)

    # Pre-seed the DB (and its deleted_articles table) before the cycle runs.
    conn = fetcher.init_db()
    conn.execute("INSERT INTO deleted_articles (article_id) VALUES (?)", (3,))
    conn.commit()
    conn.close()

    # Five rows with batch size two exercises both the size-triggered commits and
    # the trailing partial-batch branch.
    messages = [{"id": i} for i in range(1, 6)]
    monkeypatch.setattr(fetcher, "fetch_all_new_messages", lambda state: (messages, max((m["id"] for m in messages), default=0)))
    monkeypatch.setattr(
        fetcher, "process_message",
        lambda msg, orig_id: {
            "id": orig_id, "title": f"t{orig_id}", "source": "s",
            "feed_source": "s", "timestamp": orig_id,
        },
    )

    fetcher.run()

    conn = sqlite3.connect(fetcher.DB_FILE)
    ids = {row[0] for row in conn.execute("SELECT id FROM articles").fetchall()}
    conn.close()
    assert ids == {1, 2, 4, 5}

    # Running progress is a discovery contract, not an attempted-upsert log.
    # A tombstoned row must therefore never enter the browser's authoritative Set.
    progress = json.loads(fetcher.PROGRESS_FILE.read_text(encoding="utf-8"))
    assert progress["inserted"] == 4
    assert progress["inserted_ids"] == [1, 2, 4, 5]


def test_run_keeps_last_seen_id_unchanged_when_a_message_fails(tmp_path, monkeypatch):
    _patch_fetcher_paths(monkeypatch, tmp_path)
    monkeypatch.setattr(fetcher, "STREAM_BATCH_SIZE", 2)
    monkeypatch.setattr(fetcher, "STREAM_BATCH_SECONDS", 100.0)
    fetcher.save_state({"last_seen_id": 0})

    messages = [{"id": i} for i in range(1, 4)]
    monkeypatch.setattr(fetcher, "fetch_all_new_messages", lambda state: (messages, max((m["id"] for m in messages), default=0)))

    def flaky_process_message(msg, orig_id):
        if orig_id == 2:
            raise ValueError("boom")
        return {
            "id": orig_id, "title": f"t{orig_id}", "source": "s",
            "feed_source": "s", "timestamp": orig_id,
        }

    monkeypatch.setattr(fetcher, "process_message", flaky_process_message)

    fetcher.run()

    state = fetcher.load_state()
    assert state["last_seen_id"] == 0

    conn = sqlite3.connect(fetcher.DB_FILE)
    ids = {row[0] for row in conn.execute("SELECT id FROM articles").fetchall()}
    conn.close()
    assert ids == {1, 3}


# ─── refresh_server.py: new_count_so_far exposure ──────────────────────────

def _reset_running_job(monkeypatch, started_at):
    monkeypatch.setattr(refresh_server, "REFRESH_JOB", {
        "job_id": "job-1", "status": "running", "trigger": "manual",
        "started_at": started_at, "finished_at": None,
        "new_count": 0, "new_ids": [], "error": "",
    })


def test_status_reports_new_count_so_far_while_running(tmp_path, monkeypatch):
    started_at = int(time.time())
    _reset_running_job(monkeypatch, started_at)
    monkeypatch.setattr(refresh_server, "PROGRESS_FILE", tmp_path / "fetch_progress.json")
    (tmp_path / "fetch_progress.json").write_text(
        json.dumps({
            "job_id": "job-1",
            "inserted": 3,
            "inserted_ids": [9, 7, 8, 8],
            "total_messages": 12,
            "updated_at": started_at + 1,
        }),
        encoding="utf-8",
    )

    payload = json.loads(refresh_server.get_refresh_job_status())

    assert payload["new_count_so_far"] == 3
    assert payload["new_ids_so_far"] == [7, 8, 9]


def test_status_sanitizes_progress_ids(tmp_path, monkeypatch):
    started_at = int(time.time())
    _reset_running_job(monkeypatch, started_at)
    monkeypatch.setattr(refresh_server, "PROGRESS_FILE", tmp_path / "fetch_progress.json")
    (tmp_path / "fetch_progress.json").write_text(
        json.dumps({
            "job_id": "job-1",
            "inserted": 2,
            "inserted_ids": [3, "4", 0, None, "bad"],
        }),
        encoding="utf-8",
    )

    payload = json.loads(refresh_server.get_refresh_job_status())

    assert payload["new_ids_so_far"] == [3, 4]


def test_running_status_excludes_existing_ids_retried_after_failed_cycle(tmp_path, monkeypatch):
    """A failed cycle keeps its cursor, so its successful rows are retried next job."""
    started_at = int(time.time())
    _reset_running_job(monkeypatch, started_at)
    monkeypatch.setattr(refresh_server, "PROGRESS_FILE", tmp_path / "fetch_progress.json")

    # IDs 1 and 3 were committed by the previous failed cycle; only 4 is new in
    # this job. Replaying 1 and 3 through INSERT OR REPLACE must not advertise
    # them as new running discoveries.
    monkeypatch.setattr(refresh_server, "article_id_snapshot", lambda: {1, 3})
    running_payload = {}

    def fake_run_fetcher(existing_article_ids):
        assert existing_article_ids == {1, 3}
        refresh_server.PROGRESS_FILE.write_text(
            json.dumps({
                "job_id": "job-1",
                "inserted": 3,
                "inserted_ids": [1, 3, 4],
                "total_messages": 3,
                "updated_at": started_at + 1,
            }),
            encoding="utf-8",
        )
        running_payload.update(json.loads(refresh_server.get_refresh_job_status()))
        return json.dumps({"status": "ok", "new_ids": [4]}).encode(), 200

    monkeypatch.setattr(refresh_server, "run_fetcher", fake_run_fetcher)

    refresh_server._run_refresh_job("job-1")

    assert running_payload["new_count_so_far"] == 1
    assert running_payload["new_ids_so_far"] == [4]
    terminal = json.loads(refresh_server.get_refresh_job_status())
    assert terminal["new_count"] == 1
    assert terminal["new_ids"] == [4]


def test_status_ignores_progress_file_from_a_different_job_id(tmp_path, monkeypatch):
    started_at = int(time.time())
    _reset_running_job(monkeypatch, started_at)
    monkeypatch.setattr(refresh_server, "PROGRESS_FILE", tmp_path / "fetch_progress.json")
    # A previous job's progress file, left over from a job that finished — or started —
    # within the same wall-clock second as this one. A timestamp-only comparison
    # (updated_at >= started_at) would have wrongly accepted this; only the job_id
    # mismatch should reject it.
    (tmp_path / "fetch_progress.json").write_text(
        json.dumps({"job_id": "job-0", "inserted": 99, "total_messages": 99, "updated_at": started_at}),
        encoding="utf-8",
    )

    payload = json.loads(refresh_server.get_refresh_job_status())

    assert "new_count_so_far" not in payload


def test_status_ignores_progress_file_with_no_job_id(tmp_path, monkeypatch):
    started_at = int(time.time())
    _reset_running_job(monkeypatch, started_at)
    monkeypatch.setattr(refresh_server, "PROGRESS_FILE", tmp_path / "fetch_progress.json")
    # A progress file written by a fetcher.py invocation that never received
    # FETCH_JOB_ID (e.g. run standalone) must not be attributed to any job.
    (tmp_path / "fetch_progress.json").write_text(
        json.dumps({"job_id": "", "inserted": 5, "total_messages": 5, "updated_at": started_at}),
        encoding="utf-8",
    )

    payload = json.loads(refresh_server.get_refresh_job_status())

    assert "new_count_so_far" not in payload


def test_status_omits_new_count_so_far_when_job_not_running(tmp_path, monkeypatch):
    monkeypatch.setattr(refresh_server, "REFRESH_JOB", {
        "job_id": "job-1", "status": "completed", "trigger": "manual",
        "started_at": int(time.time()) - 10, "finished_at": int(time.time()),
        "new_count": 3, "error": "",
    })
    monkeypatch.setattr(refresh_server, "PROGRESS_FILE", tmp_path / "fetch_progress.json")
    (tmp_path / "fetch_progress.json").write_text(
        json.dumps({"job_id": "job-1", "inserted": 3, "total_messages": 3, "updated_at": int(time.time())}),
        encoding="utf-8",
    )

    payload = json.loads(refresh_server.get_refresh_job_status())

    assert "new_count_so_far" not in payload


def test_status_handles_missing_progress_file(monkeypatch, tmp_path):
    started_at = int(time.time())
    _reset_running_job(monkeypatch, started_at)
    monkeypatch.setattr(refresh_server, "PROGRESS_FILE", tmp_path / "fetch_progress.json")

    payload = json.loads(refresh_server.get_refresh_job_status())

    assert "new_count_so_far" not in payload
    assert payload["status"] == "running"


def test_run_fetcher_passes_current_job_id_to_fetcher_subprocess_env(monkeypatch):
    monkeypatch.setattr(refresh_server, "CURRENT_FETCH_JOB_ID", "job-xyz")
    monkeypatch.setattr(refresh_server, "acquire_lock", lambda: True)
    monkeypatch.setattr(refresh_server, "release_lock", lambda: None)
    monkeypatch.setattr(refresh_server, "article_id_snapshot", lambda: set())
    monkeypatch.setattr(refresh_server, "clear_article_cache", lambda: None)

    captured_env = {}

    def fake_run_fetcher_process(env, timeout):
        captured_env.update(env)
        captured_env["timeout"] = timeout
        return {
            "returncode": 0,
            "stdout_tail": "",
            "stderr_tail": "",
            "timed_out": False,
        }

    monkeypatch.setattr(refresh_server, "_run_fetcher_process", fake_run_fetcher_process)
    monkeypatch.setattr(refresh_server.threading, "Thread", lambda **kwargs: type(
        "T", (), {"start": lambda self: None},
    )())

    refresh_server.run_fetcher()

    assert captured_env.get("FETCH_JOB_ID") == "job-xyz"
    assert captured_env["timeout"] == 120


def test_run_fetcher_maps_streaming_timeout_to_existing_error_contract(
    caplog, monkeypatch
):
    monkeypatch.setattr(refresh_server, "acquire_lock", lambda: True)
    monkeypatch.setattr(refresh_server, "release_lock", lambda: None)
    monkeypatch.setattr(refresh_server, "article_id_snapshot", lambda: set())
    monkeypatch.setattr(
        refresh_server,
        "LAST_FETCH_STATUS",
        {
            "status": "never",
            "returncode": None,
            "stdout": "",
            "stderr": "",
            "updated_at": None,
        },
    )
    monkeypatch.setattr(
        refresh_server,
        "_run_fetcher_process",
        lambda env, timeout: {
            "returncode": None,
            "stdout_tail": "before timeout",
            "stderr_tail": "",
            "timed_out": True,
        },
    )

    body, status = refresh_server.run_fetcher(set())

    assert status == 500
    assert json.loads(body) == {"status": "error", "error": "timeout"}
    assert refresh_server.LAST_FETCH_STATUS["status"] == "error"
    assert refresh_server.LAST_FETCH_STATUS["returncode"] is None
    assert refresh_server.LAST_FETCH_STATUS["stdout"] == ""
    assert refresh_server.LAST_FETCH_STATUS["stderr"] == "timeout"
    assert "Fetcher timed out after 120 seconds" in caplog.text


def test_run_fetcher_reuses_baseline_and_returns_new_ids_to_warmup(monkeypatch):
    monkeypatch.setattr(refresh_server, "acquire_lock", lambda: True)
    monkeypatch.setattr(refresh_server, "release_lock", lambda: None)
    monkeypatch.setattr(refresh_server, "clear_article_cache", lambda: None)
    snapshot_calls = []

    def after_snapshot():
        snapshot_calls.append(True)
        return {2, 3, 4}

    monkeypatch.setattr(refresh_server, "article_id_snapshot", after_snapshot)

    monkeypatch.setattr(
        refresh_server,
        "_run_fetcher_process",
        lambda env, timeout: {
            "returncode": 0,
            "stdout_tail": "fetch output",
            "stderr_tail": "fetch warning",
            "timed_out": False,
        },
    )

    class MaintenanceDb:
        def commit(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(refresh_server.sqlite3, "connect", lambda *args, **kwargs: MaintenanceDb())
    monkeypatch.setattr(refresh_server, "ensure_article_source_columns", lambda conn: None)
    monkeypatch.setattr(refresh_server, "maintain_source_categories", lambda conn, force=False: {})

    thread_calls = []

    class ImmediateThread:
        def __init__(self, **kwargs):
            thread_calls.append(kwargs)

        def start(self):
            thread_calls[-1]["target"](*thread_calls[-1]["args"])

    warmed_ids = []
    monkeypatch.setattr(refresh_server.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(
        refresh_server,
        "enqueue_new_article_images",
        lambda new_ids: warmed_ids.append(new_ids),
    )

    body, status = refresh_server.run_fetcher({2})

    assert status == 200
    assert json.loads(body) == {
        "status": "ok",
        "returncode": 0,
        "stdout": "fetch output",
        "stderr": "fetch warning",
        "new_ids": [3, 4],
    }
    assert snapshot_calls == [True]
    assert warmed_ids == [[3, 4]]


def test_image_warmup_queries_only_parameterized_new_id_batches(monkeypatch):
    queries = []

    class QueryResult:
        def __init__(self, rows):
            self.rows = rows

        def fetchall(self):
            return self.rows

    class RecordingDb:
        row_factory = None

        def execute(self, sql, params=()):
            queries.append((sql, params))
            return QueryResult([
                {"id": article_id, "thumb": f"thumb-{article_id}", "body_html": ""}
                for article_id in params
            ])

        def close(self):
            pass

    monkeypatch.setattr(refresh_server.sqlite3, "connect", lambda *args, **kwargs: RecordingDb())
    queued = []
    monkeypatch.setattr(
        refresh_server,
        "enqueue_article_image_prefetch",
        lambda article_id, body_html, thumb: queued.append(article_id) or 1,
    )
    requested_ids = list(range(1003, 0, -1)) + [2, 2, 0, -4, "bad", None]

    refresh_server.enqueue_new_article_images(requested_ids)

    assert [len(params) for _, params in queries] == [500, 500, 3]
    assert [article_id for _, params in queries for article_id in params] == list(range(1, 1004))
    assert all("WHERE id IN (" in sql for sql, _ in queries)
    assert all(sql.count("?") == len(params) for sql, params in queries)
    assert queued == list(range(1, 1004))


def test_run_refresh_job_sets_current_fetch_job_id_before_running(monkeypatch):
    monkeypatch.setattr(refresh_server, "REFRESH_JOB", {
        "job_id": "job-abc", "status": "running", "trigger": "manual",
        "started_at": int(time.time()), "finished_at": None,
        "new_count": 0, "error": "",
    })
    monkeypatch.setattr(refresh_server, "CURRENT_FETCH_JOB_ID", "")
    seen_job_id = {}

    def fake_run_fetcher(existing_article_ids):
        seen_job_id["value"] = refresh_server.CURRENT_FETCH_JOB_ID
        return json.dumps({"status": "ok", "new_ids": []}).encode(), 200

    monkeypatch.setattr(refresh_server, "run_fetcher", fake_run_fetcher)
    monkeypatch.setattr(refresh_server, "article_id_snapshot", lambda: set())

    refresh_server._run_refresh_job("job-abc")

    assert seen_job_id["value"] == "job-abc"
    assert refresh_server.CURRENT_FETCH_JOB_ID == "job-abc"
