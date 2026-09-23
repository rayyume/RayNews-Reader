"""Shared SQLite schema helpers for RayNews news data.

Every process which reads ``news.db`` can be the first one to open a database
created by an older release.  Keep those migrations here, rather than behind a
web-process-only lock, so fetcher, refresh server and maintenance workers use
the same cross-process-safe protocol.
"""

from __future__ import annotations

import sqlite3
import time


_TITLE_COLUMNS = {
    "original_title": "TEXT",
    "title_updated_at": "TEXT",
    "title_source": "TEXT",
}

_SOURCE_COLUMNS = {
    "group_source": "TEXT NOT NULL DEFAULT ''",
    "publisher_domain": "TEXT NOT NULL DEFAULT ''",
    "source_detection_version": "INTEGER NOT NULL DEFAULT 0",
    "source_detection_last_attempt_at": "INTEGER NOT NULL DEFAULT 0",
    "ingested_at": "INTEGER NOT NULL DEFAULT 0",
}


def enable_wal_mode(conn: sqlite3.Connection, *, attempts: int = 8, delay: float = 0.05) -> None:
    """Enable WAL after schema migration without masking unrelated failures.

    Switching journal mode needs an exclusive SQLite lock, so it may briefly
    collide with another process's ``BEGIN IMMEDIATE`` migration.  Retry only
    SQLite's documented busy/locked condition; any other operational error is
    surfaced to the caller unchanged.
    """
    for attempt in range(attempts):
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            return
        except sqlite3.OperationalError as exc:
            message = str(exc).lower()
            retryable = "database is locked" in message or "database is busy" in message
            if not retryable or attempt + 1 == attempts:
                raise
            time.sleep(delay)


def _article_columns(conn: sqlite3.Connection) -> set[str]:
    return {row[1] for row in conn.execute("PRAGMA table_info(articles)").fetchall()}


def _schema_already_current(
    conn: sqlite3.Connection,
    *,
    include_source_columns: bool,
    include_title_columns: bool,
) -> bool:
    """Return whether the requested schema is ready without taking a write lock."""
    deleted = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'deleted_articles'"
    ).fetchone()
    if not deleted:
        return False

    articles = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'articles'"
    ).fetchone()
    if not articles:
        return True

    columns = _article_columns(conn)
    if "body_html" in columns and "original_body_html" not in columns:
        return False
    if include_source_columns:
        if not {"feed_source", "origin_source", *_SOURCE_COLUMNS}.issubset(columns):
            return False
        for index in ("idx_feed_source", "idx_group_source", "idx_publisher_domain", "idx_ingested_at"):
            if not conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = ?", (index,)
            ).fetchone():
                return False
    if include_title_columns and not set(_TITLE_COLUMNS).issubset(columns):
        return False
    return True


def _add_column_if_missing(
    conn: sqlite3.Connection, columns: set[str], name: str, definition: str
) -> None:
    """Add one column without hiding unrelated SQLite operational failures."""
    if name in columns:
        return
    try:
        conn.execute(f"ALTER TABLE articles ADD COLUMN {name} {definition}")
    except sqlite3.OperationalError as exc:
        # Callers already inside a transaction cannot be upgraded to BEGIN
        # IMMEDIATE here.  In that narrow case another process may finish the
        # same ALTER first; accept only that exact, rechecked outcome.
        refreshed = _article_columns(conn)
        if "duplicate column name" in str(exc).lower() and name in refreshed:
            columns.update(refreshed)
            return
        raise
    columns.add(name)


def ensure_article_schema(
    conn: sqlite3.Connection,
    *,
    include_source_columns: bool = True,
    include_title_columns: bool = True,
) -> None:
    """Upgrade ``articles`` and deletion metadata with a cross-process lock.

    On a normal standalone connection the immediate transaction obtains
    SQLite's write lock *before* reading ``PRAGMA table_info``.  A second
    process therefore waits, then rechecks the columns after the first process
    commits.  Existing caller-owned transactions retain their boundary; the
    exact duplicate-column/recheck guard above is their safe fallback.
    """
    owns_transaction = not conn.in_transaction
    if owns_transaction and _schema_already_current(
        conn,
        include_source_columns=include_source_columns,
        include_title_columns=include_title_columns,
    ):
        return
    if owns_transaction:
        conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS deleted_articles (
                article_id   INTEGER PRIMARY KEY,
                title        TEXT NOT NULL DEFAULT '',
                source       TEXT NOT NULL DEFAULT '',
                deleted_by   INTEGER,
                deleted_at   TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        has_articles = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'articles'"
        ).fetchone()
        if has_articles:
            columns = _article_columns(conn)
            if "body_html" in columns:
                _add_column_if_missing(conn, columns, "original_body_html", "TEXT")
                # Existing databases predate the original-body column. Preserve
                # their current canonical body before any future translation
                # cache can be generated; never replace a previously captured
                # original.
                conn.execute(
                    "UPDATE articles SET original_body_html = body_html "
                    "WHERE (original_body_html IS NULL OR original_body_html = '') "
                    "AND body_html IS NOT NULL AND body_html != ''"
                )
            if include_source_columns and "feed_source" not in columns:
                _add_column_if_missing(
                    conn, columns, "feed_source", "TEXT NOT NULL DEFAULT ''"
                )
                conn.execute(
                    "UPDATE articles SET feed_source = source WHERE TRIM(feed_source) = ''"
                )
            if include_source_columns:
                _add_column_if_missing(
                    conn, columns, "origin_source", "TEXT NOT NULL DEFAULT ''"
                )
                for name, definition in _SOURCE_COLUMNS.items():
                    _add_column_if_missing(conn, columns, name, definition)
                if "timestamp" in columns:
                    conn.execute(
                        "UPDATE articles SET ingested_at = timestamp "
                        "WHERE ingested_at = 0 AND timestamp > 0"
                    )
                # Preserve historical source labels while making the new grouping
                # key available immediately. A later background pass can refine it.
                if "source" in columns:
                    conn.execute(
                        "UPDATE articles SET group_source = "
                        "COALESCE(NULLIF(feed_source, ''), NULLIF(source, ''), '') "
                        "WHERE TRIM(group_source) = ''"
                    )
                    conn.execute(
                        "UPDATE articles SET source = group_source "
                        "WHERE TRIM(group_source) != '' AND source != group_source"
                    )
            if include_title_columns:
                for name, definition in _TITLE_COLUMNS.items():
                    _add_column_if_missing(conn, columns, name, definition)
            if include_source_columns:
                conn.execute("CREATE INDEX IF NOT EXISTS idx_feed_source ON articles(feed_source)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_group_source ON articles(group_source)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_publisher_domain ON articles(publisher_domain)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_ingested_at ON articles(ingested_at)")
        if owns_transaction:
            conn.commit()
    except Exception:
        if owns_transaction and conn.in_transaction:
            conn.rollback()
        raise


def ensure_article_source_columns(conn: sqlite3.Connection) -> None:
    """Compatibility entry point for source-only callers."""
    ensure_article_schema(conn, include_title_columns=False)


def ensure_article_title_columns(conn: sqlite3.Connection) -> None:
    """Compatibility entry point for title-maintenance callers."""
    ensure_article_schema(conn, include_source_columns=False, include_title_columns=True)


def ensure_deleted_articles_table(conn: sqlite3.Connection) -> None:
    """Compatibility entry point for callers needing deletion tombstones."""
    ensure_article_schema(conn, include_source_columns=False, include_title_columns=False)
