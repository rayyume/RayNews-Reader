"""Tests for the read-only source_rows() / write-heavy maintain_source_categories()
split introduced to keep GET /sources fast during a fetch cycle (cold-start fix)."""

from pathlib import Path
import sqlite3

import pytest

import source_categories as sc

ROOT = Path(__file__).resolve().parents[1]


def _make_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE articles (
            id INTEGER PRIMARY KEY,
            title TEXT,
            source TEXT,
            feed_source TEXT,
            origin_source TEXT,
            timestamp INTEGER
        )
        """
    )
    return conn


def _add_article(conn, article_id, feed_source, ts=1):
    conn.execute(
        "INSERT INTO articles (id, title, source, feed_source, origin_source, timestamp) "
        "VALUES (?, ?, ?, ?, '', ?)",
        (article_id, f"t{article_id}", feed_source, feed_source, ts),
    )
    conn.commit()


def test_source_rows_does_not_write(monkeypatch):
    """source_rows() must stay read-only: no discovery, no cleanup on the read path."""
    conn = _make_conn()
    sc.init_source_categories(conn)
    _add_article(conn, 1, "全新来源")

    calls = []
    monkeypatch.setattr(sc, "ensure_article_sources",
                        lambda c: calls.append("ensure") or 0)
    monkeypatch.setattr(sc, "cleanup_stale_source_categories",
                        lambda c: calls.append("cleanup") or 0)

    rows = sc.source_rows(conn)

    assert calls == []  # neither bookkeeping pass ran
    # A brand-new source not yet in source_categories still surfaces via the unlinked query
    assert any(r["source"] == "全新来源" for r in rows)


def test_source_rows_work_does_not_multiply_by_number_of_sources():
    """The read endpoint must aggregate articles once, not rescan them for every source.

    The homepage aborts its first metadata request after eight seconds.  A LEFT JOIN
    on COALESCE(feed_source, source) makes SQLite scan the whole articles table once
    per source because that expression cannot use either source index.
    """
    conn = _make_conn()
    conn.execute("CREATE INDEX idx_source ON articles(source)")
    conn.execute("CREATE INDEX idx_feed_source ON articles(feed_source)")
    conn.executemany(
        "INSERT INTO articles (id, title, source, feed_source, origin_source, timestamp) "
        "VALUES (?, ?, ?, ?, '', ?)",
        (
            (
                article_id,
                f"t{article_id}",
                f"来源{article_id % 100}",
                "" if article_id % 10 == 0 else f"来源{article_id % 100}",
                article_id,
            )
            for article_id in range(1, 5001)
        ),
    )
    conn.commit()
    sc.init_source_categories(conn)
    sc.ensure_article_sources(conn)

    progress_calls = 0

    def count_progress():
        nonlocal progress_calls
        progress_calls += 1
        return 0

    conn.set_progress_handler(count_progress, 1000)
    rows = sc.source_rows(conn)
    conn.set_progress_handler(None, 0)

    by_source = {row["source"]: row for row in rows}
    assert by_source["来源1"]["article_count"] == 50
    # Leave ample SQLite-version headroom while still catching the old
    # source-count × article-count nested scan (more than 5 million VM steps).
    assert progress_calls * 1000 < 500_000


def test_source_rows_bootstraps_tables_on_fresh_db():
    """On a fresh deployment the tables may not exist yet; source_rows must not crash."""
    conn = _make_conn()
    # Note: no init_source_categories() call here.
    rows = sc.source_rows(conn)
    assert isinstance(rows, list)
    # The table should now exist (cheap bootstrap ran).
    exists = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='source_categories'"
    ).fetchone()
    assert exists is not None


def test_known_source_identity_does_not_seed_personal_category():
    conn = _make_conn()

    sc.init_source_categories(conn)
    assert conn.execute(
        "SELECT 1 FROM source_categories WHERE source = '少数派'"
    ).fetchone() is None

    _add_article(conn, 101, "少数派")
    sc.init_source_categories(conn)
    row = conn.execute(
        "SELECT category, label, status, reason "
        "FROM source_categories WHERE source = '少数派'"
    ).fetchone()

    assert row is None  # source category discovery is explicit maintenance, no personal presets


def test_custom_categories_and_domain_overrides_are_instance_configurable():
    conn = _make_conn()
    sc.init_source_categories(conn)
    custom = sc.save_category_definition(conn, "Research", "研究", 10)
    assert custom["category"] == "Research"
    assert sc.valid_category(conn, "Research")

    conn.execute(
        "INSERT INTO source_categories (source, category, label, status) "
        "VALUES ('南华早报', 'Research', '南早', 'manual')"
    )
    conn.commit()
    sc.save_category_definition(conn, "Research", "研究", 10, enabled=False,
                                migration_target="News")
    assert not sc.valid_category(conn, "Research")
    assert any(
        item["category"] == "Research" and not item["enabled"]
        for item in sc.category_definitions(conn, include_disabled=True)
    )
    assert conn.execute(
        "SELECT category FROM source_categories WHERE source = '南华早报'"
    ).fetchone()[0] == "News"

    sc.save_publisher_domain(conn, "edition.example.co.uk", "Example Press")
    assert sc.publisher_source_for_domain(conn, "www.example.co.uk") == "Example Press"


def test_deleted_default_category_stays_deleted_after_reinitialization():
    conn = _make_conn()
    sc.init_source_categories(conn)
    sc.delete_category_definition(conn, "News", "Info")
    sc.init_source_categories(conn)
    assert "News" not in {row["category"] for row in sc.category_definitions(conn, True)}


def test_category_read_succeeds_while_another_wal_connection_writes(tmp_path):
    path = tmp_path / "news.db"
    setup = sqlite3.connect(path)
    setup.row_factory = sqlite3.Row
    setup.execute("PRAGMA journal_mode=WAL")
    setup.execute("CREATE TABLE articles (id INTEGER PRIMARY KEY, source TEXT)")
    sc.init_source_categories(setup)
    setup.close()
    writer = sqlite3.connect(path, timeout=0.01)
    reader = sqlite3.connect(path, timeout=0.01)
    reader.row_factory = sqlite3.Row
    try:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("INSERT INTO articles (id, source) VALUES (1, '甲报')")
        assert "News" in {row["category"] for row in sc.category_definitions(reader)}
    finally:
        writer.rollback()
        writer.close()
        reader.close()


def test_delete_source_metadata_removes_group_and_all_connected_aliases():
    conn = _make_conn()
    sc.init_source_categories(conn)
    conn.executemany(
        "INSERT INTO source_categories (source, category, label, status) "
        "VALUES (?, 'Tech', 'Group', 'manual')",
        [("Primary",), ("Variant",), ("Unrelated",)],
    )
    conn.executemany(
        "INSERT INTO user_source_categories "
        "(user_id, source, category, label, status) VALUES (7, ?, 'Biz', 'Custom', 'manual')",
        [("Primary",), ("Variant",), ("Unrelated",)],
    )
    conn.executemany(
        "INSERT INTO source_aliases (alias_source, target_source) VALUES (?, ?)",
        [
            ("Legacy", "Primary"),
            ("Variant", "External"),
            ("Unrelated Alias", "Unrelated"),
        ],
    )
    conn.executemany(
        "INSERT INTO user_source_aliases (user_id, alias_source, target_source) VALUES (7, ?, ?)",
        [
            ("User Legacy", "Primary"),
            ("Variant", "User External"),
            ("User Unrelated", "Unrelated"),
        ],
    )
    conn.commit()

    deleted = sc.delete_source_metadata(conn, ["Primary", "Variant"])

    assert deleted == 8
    assert {
        row[0] for row in conn.execute("SELECT source FROM source_categories")
    } == {"Unrelated"}
    assert {
        row[0] for row in conn.execute("SELECT source FROM user_source_categories")
    } == {"Unrelated"}
    assert {
        row[0] for row in conn.execute("SELECT alias_source FROM source_aliases")
    } == {"Unrelated Alias"}
    assert {
        row[0] for row in conn.execute("SELECT alias_source FROM user_source_aliases")
    } == {"User Unrelated"}


def test_delete_source_metadata_requires_bootstrap_before_caller_transaction():
    """A missing-table bootstrap cannot commit a transaction owned by the caller."""
    conn = _make_conn()
    conn.execute(
        "INSERT INTO articles (id, title, source, feed_source, origin_source, timestamp) "
        "VALUES (303, 'pending', 'Pending Feed', 'Pending Feed', '', 1)"
    )

    with pytest.raises(RuntimeError, match="bootstrap source metadata tables"):
        sc.delete_source_metadata(conn, ["Pending Feed"])

    assert conn.in_transaction
    assert conn.execute("SELECT 1 FROM articles WHERE id = 303").fetchone() is not None
    conn.rollback()
    assert conn.execute("SELECT 1 FROM articles WHERE id = 303").fetchone() is None


def test_deleted_unknown_source_is_rediscovered_without_old_settings():
    conn = _make_conn()
    sc.init_source_categories(conn)
    conn.execute(
        "INSERT INTO source_categories (source, category, label, status) "
        "VALUES ('Fresh Feed', 'Biz', 'Old Custom Label', 'manual')"
    )
    conn.commit()

    sc.delete_source_metadata(conn, ["Fresh Feed"])
    _add_article(conn, 202, "Fresh Feed")
    sc.ensure_article_sources(conn)

    row = conn.execute(
        "SELECT category, label, status, reason "
        "FROM source_categories WHERE source = 'Fresh Feed'"
    ).fetchone()
    assert tuple(row) == ("Uncategorized", "Fresh Feed", "pending", "discovered")


def test_maintain_discovers_and_cleans(monkeypatch):
    conn = _make_conn()
    sc.init_source_categories(conn)
    _add_article(conn, 1, "来源A")

    # Force bypasses the throttle so the pass runs immediately.
    result = sc.maintain_source_categories(conn, force=True)
    assert result["ran"] is True
    assert result["discovered"] >= 1
    row = conn.execute(
        "SELECT source FROM source_categories WHERE source = '来源A'"
    ).fetchone()
    assert row is not None


def test_maintain_is_throttled(monkeypatch):
    conn = _make_conn()
    sc.init_source_categories(conn)

    # Reset throttle clock so the first non-forced call is allowed.
    monkeypatch.setattr(sc, "_maintenance_last_run", 0.0)

    ensure_calls = []
    monkeypatch.setattr(sc, "ensure_article_sources",
                        lambda c: ensure_calls.append(1) or 0)
    monkeypatch.setattr(sc, "cleanup_stale_source_categories", lambda c: 0)

    first = sc.maintain_source_categories(conn)
    second = sc.maintain_source_categories(conn)

    assert first["ran"] is True
    assert second["ran"] is False  # throttled within the window
    assert len(ensure_calls) == 1


def test_cleanup_only_removes_stale_automatic_metadata():
    """Cleanup must not discard explicit metadata merely because its source is quiet.

    This catches a cleanup regression that deletes manual/classified source settings or
    aliases whenever they have no current articles. Only stale pending, failed, and
    review category records plus aliases whose category target is gone may be removed.
    """
    conn = _make_conn()
    sc.init_source_categories(conn)
    _add_article(conn, 1, "global-live-pending")
    _add_article(conn, 2, "user-live-pending")

    conn.executemany(
        "INSERT INTO source_categories (source, category, label, status) VALUES (?, 'Info', '', ?)",
        [
            ("global-manual", "manual"),
            ("global-classified", "classified"),
            ("global-pending-stale", "pending"),
            ("global-failed-stale", "failed"),
            ("global-review-stale", "review"),
            ("global-live-pending", "pending"),
            ("user-manual", "manual"),
            ("user-classified", "classified"),
            ("user-pending-stale", "pending"),
            ("user-failed-stale", "failed"),
            ("user-live-pending", "pending"),
        ],
    )
    conn.executemany(
        "INSERT INTO user_source_categories (user_id, source, category, label, status) "
        "VALUES (7, ?, 'Info', '', ?)",
        [
            ("user-manual", "manual"),
            ("user-classified", "classified"),
            ("user-pending-stale", "pending"),
            ("user-failed-stale", "failed"),
            ("user-live-pending", "pending"),
        ],
    )
    conn.executemany(
        "INSERT INTO source_aliases (alias_source, target_source) VALUES (?, ?)",
        [
            ("global-manual-alias", "global-manual"),
            ("global-classified-alias", "global-classified"),
            ("global-dangling-alias", "missing-global-target"),
        ],
    )
    conn.executemany(
        "INSERT INTO user_source_aliases (user_id, alias_source, target_source) VALUES (7, ?, ?)",
        [
            ("user-manual-alias", "user-manual"),
            ("user-classified-alias", "user-classified"),
            ("user-dangling-alias", "missing-user-target"),
        ],
    )
    conn.commit()

    sc.cleanup_stale_source_categories(conn)

    global_categories = {
        row[0] for row in conn.execute(
            "SELECT source FROM source_categories WHERE source LIKE 'global-%'"
        )
    }
    user_categories = {
        row[0] for row in conn.execute(
            "SELECT source FROM user_source_categories WHERE user_id = 7"
        )
    }
    global_aliases = {
        row[0] for row in conn.execute("SELECT alias_source FROM source_aliases")
    }
    user_aliases = {
        row[0] for row in conn.execute(
            "SELECT alias_source FROM user_source_aliases WHERE user_id = 7"
        )
    }

    assert global_categories == {
        "global-manual", "global-classified", "global-live-pending"
    }
    assert user_categories == {
        "user-manual", "user-classified", "user-live-pending"
    }
    assert {"global-manual-alias", "global-classified-alias"} <= global_aliases
    assert "global-dangling-alias" not in global_aliases
    assert {"user-manual-alias", "user-classified-alias"} <= user_aliases
    assert "user-dangling-alias" not in user_aliases


def test_cleanup_preserves_user_alias_only_for_same_user_private_target():
    conn = _make_conn()
    sc.init_source_categories(conn)
    conn.executemany(
        "INSERT INTO user_source_categories "
        "(user_id, source, category, label, status) VALUES (?, ?, 'Info', '', 'manual')",
        [
            (7, "same-user-private"),
            (8, "other-user-private"),
        ],
    )
    conn.executemany(
        "INSERT INTO user_source_aliases (user_id, alias_source, target_source) "
        "VALUES (7, ?, ?)",
        [
            ("same-user-alias", "same-user-private"),
            ("other-user-alias", "other-user-private"),
            ("missing-alias", "nowhere"),
        ],
    )
    conn.commit()

    sc.cleanup_stale_source_categories(conn)

    aliases = {
        row[0]
        for row in conn.execute(
            "SELECT alias_source FROM user_source_aliases WHERE user_id = 7"
        )
    }
    assert aliases == {"same-user-alias"}


def test_refresh_server_lets_post_fetch_maintenance_be_throttled():
    # A manual/periodic refresh's post-fetch maintenance call no longer forces past
    # the throttle — back-to-back refreshes skip the two full-table scans
    # (source discovery, stale cleanup) when the last run was recent. New/stale
    # sources are still caught by whichever refresh lands after the throttle
    # window (60s) expires.
    source = (ROOT / "refresh_server.py").read_text(encoding="utf-8")
    assert "maintain_source_categories(conn, force=False)" in source
    assert "maintain_source_categories(conn, force=True)" not in source
