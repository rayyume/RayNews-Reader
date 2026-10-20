"""Atomic migration of mixed legacy results into a server-only result store."""
import time

TITLE_FIELDS = ("title_translation", "translated_title_summary")
TITLE_ERROR_FIELDS = ("translated_title_summary_error", "translated_title_summary_error_at")
SOURCE_FIELDS = ("summary_source", "translation_source", "title_summary_source", "title_translation_source", "translated_title_summary_source")


def ensure_title_result_invalidation(conn):
    """Invalidate derived titles atomically with any canonical source edit.

    The fetcher can start before the web process and can encounter a partial
    legacy result schema. Install only fields that exist; the web migration
    upgrades the trigger after completing that schema. Unrelated results stay.
    """
    article_cols = {row[1] for row in conn.execute("PRAGMA table_info(articles)")}
    result_cols = {row[1] for row in conn.execute("PRAGMA table_info(ai_results)")}
    if not {"id", "title"} <= article_cols or "article_id" not in result_cols:
        return
    kinds = ("title_translation", "title_summary", "translated_title_summary")
    suffixes = ("", "_error", "_error_at", "_source", "_provider", "_model", "_by_user_id")
    fields = [kind + suffix for kind in kinds for suffix in suffixes if kind + suffix in result_cols]
    if not fields:
        return
    def canonical(alias):
        if {"original_title", "title_source"} <= article_cols:
            return (f"CASE WHEN COALESCE({alias}.title_source,'') IN ('translation','title_summary','ai') "
                    f"THEN COALESCE(NULLIF({alias}.original_title,''),'无标题文章') ELSE {alias}.title END")
        return alias + ".title"
    updates = ", ".join(field + "=NULL" for field in fields)
    if "updated_at" in result_cols:
        updates += ", updated_at=datetime('now')"
    watched = ",".join(c for c in ("title", "original_title", "title_source") if c in article_cols)
    sql = f"""CREATE TRIGGER article_title_results_invalidate
        AFTER UPDATE OF {watched} ON articles
        WHEN ({canonical('OLD')}) IS NOT ({canonical('NEW')})
        BEGIN
            UPDATE ai_results SET {updates} WHERE article_id=NEW.id
                AND ({' OR '.join(field + ' IS NOT NULL' for field in fields)});
        END"""
    existing = conn.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name='article_title_results_invalidate'").fetchone()
    if existing and existing[0] == sql:
        return
    conn.execute("DROP TRIGGER IF EXISTS article_title_results_invalidate")
    conn.execute(sql)


def migration_preview(conn, *, include_archived=True):
    """Count v5 content affected by the migration without changing the database."""
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    migrated = ("ai_schema_migrations" in tables and conn.execute(
        "SELECT 1 FROM ai_schema_migrations WHERE name='server_only_v6'"
    ).fetchone() is not None)
    result = {
        "migration_pending": not migrated,
        "legacy_result_rows": 0,
        "legacy_summary_rows": 0,
        "legacy_translation_rows": 0,
        "historical_summary_rows": 0,
        "historical_translation_rows": 0,
        "ai_derived_titles": 0,
        "unrecoverable_titles": 0,
        "archive_present": bool({"ai_results_legacy_v5", "ai_titles_missing_source_v5"} & tables),
    }
    if migrated and not include_archived:
        return result
    article_cols = ({row[1] for row in conn.execute("PRAGMA table_info(articles)")}
                    if "articles" in tables else set())
    result_table = "ai_results_legacy_v5" if migrated else "ai_results"
    if result_table in tables:
        result_cols = {row[1] for row in conn.execute(f"PRAGMA table_info({result_table})")}
        result["legacy_result_rows"] = conn.execute(f"SELECT COUNT(*) FROM {result_table}").fetchone()[0]
        for field in ("summary", "translation"):
            if field not in result_cols:
                continue
            present = f"TRIM(COALESCE(r.{field}, '')) != ''"
            result[f"legacy_{field}_rows"] = conn.execute(
                f"SELECT COUNT(*) FROM {result_table} r WHERE {present}"
            ).fetchone()[0]
            if {"id", "ingested_at"} <= article_cols:
                result[f"historical_{field}_rows"] = conn.execute(
                    f"SELECT COUNT(*) FROM {result_table} r LEFT JOIN articles a ON a.id=r.article_id "
                    f"WHERE {present} AND (a.id IS NULL OR COALESCE(a.ingested_at,0) < ?)",
                    (int(time.time()) - 86400,),
                ).fetchone()[0]
            else:
                result[f"historical_{field}_rows"] = result[f"legacy_{field}_rows"]
    if migrated:
        # The migration resets title_source, so the original total cannot be
        # reconstructed from the live articles after the migration.
        result["ai_derived_titles"] = None
        if "ai_titles_missing_source_v5" in tables:
            result["unrecoverable_titles"] = conn.execute(
                "SELECT COUNT(*) FROM ai_titles_missing_source_v5"
            ).fetchone()[0]
        else:
            result["unrecoverable_titles"] = None
        return result
    if {"title", "title_source"} <= article_cols:
        source_filter = "COALESCE(title_source,'') IN ('translation','title_summary','ai')"
        result["ai_derived_titles"] = conn.execute(
            "SELECT COUNT(*) FROM articles WHERE " + source_filter
        ).fetchone()[0]
        missing = "(original_title IS NULL OR original_title='') AND " if "original_title" in article_cols else ""
        result["unrecoverable_titles"] = conn.execute(
            "SELECT COUNT(*) FROM articles WHERE " + missing
            + source_filter
        ).fetchone()[0]
    return result


def require_migration_acceptance(conn, *, accept_legacy_loss=False):
    preview = migration_preview(conn, include_archived=False)
    if preview["migration_pending"] and preview["archive_present"]:
        raise RuntimeError("Legacy AI archive exists without a completed migration; inspect the database before retrying")
    if preview["migration_pending"] and (preview["legacy_result_rows"] or preview["ai_derived_titles"]) and not accept_legacy_loss:
        raise RuntimeError(
            "AI v6 migration needs explicit acceptance: "
            f"{preview['legacy_result_rows']} legacy AI rows will stop displaying "
            f"and {preview['ai_derived_titles']} AI-derived titles will be reset "
            f"({preview['unrecoverable_titles']} lack an original snapshot). "
            "Run the read-only migration preview, then set AI_V6_ACCEPT_LEGACY_LOSS=1 to proceed."
        )
    return preview


def migrate_results(conn, *, accept_legacy_loss=False):
    preview = require_migration_acceptance(conn, accept_legacy_loss=accept_legacy_loss)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(ai_results)")}
    needs_retry_reset = "translated_title_summary_error" not in columns
    for field in (*TITLE_FIELDS, *TITLE_ERROR_FIELDS, *SOURCE_FIELDS):
        if field not in columns:
            conn.execute("ALTER TABLE ai_results ADD COLUMN " + field + " TEXT")
    conn.execute("CREATE TABLE IF NOT EXISTS ai_schema_migrations(name TEXT PRIMARY KEY)")
    ensure_title_result_invalidation(conn)
    if conn.execute("SELECT 1 FROM ai_schema_migrations WHERE name='server_only_v6'").fetchone():
        if needs_retry_reset:
            # Before retry isolation, translation failures could be stored as
            # shortening errors. Release ambiguous old backoffs once, without
            # dropping results or changing clearly independent cached failures.
            count = conn.execute(
                "UPDATE ai_results SET title_summary_error=NULL, title_summary_error_at=NULL "
                "WHERE title_summary IS NULL AND title_translation IS NULL AND title_summary_error IS NOT NULL"
            ).rowcount
            return {"reset_ambiguous_title_errors": count}
        return
    # Legacy provenance cannot distinguish browser writes from background calls
    # performed under an administrator ID. Keep an inspectable offline archive.
    quarantined = preview["legacy_result_rows"]
    missing_titles = preview["unrecoverable_titles"]
    conn.execute("CREATE TABLE IF NOT EXISTS ai_results_legacy_v5 AS SELECT * FROM ai_results")
    conn.execute("DELETE FROM ai_results")
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "articles" in tables:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(articles)")}
        if missing_titles and {"id", "title", "title_source"} <= cols:
            missing = "(original_title IS NULL OR original_title='') AND " if "original_title" in cols else ""
            conn.execute("CREATE TABLE ai_titles_missing_source_v5 (article_id INTEGER PRIMARY KEY, legacy_title TEXT, title_source TEXT)")
            conn.execute(
                "INSERT INTO ai_titles_missing_source_v5(article_id,legacy_title,title_source) "
                "SELECT id,title,title_source FROM articles WHERE " + missing
                + "COALESCE(title_source,'') IN ('translation','title_summary','ai')"
            )
        if {"original_title", "title", "title_source"} <= cols:
            conn.execute("UPDATE articles SET title=COALESCE(NULLIF(original_title,''),'无标题文章') WHERE COALESCE(title_source,'') IN ('translation','title_summary','ai')")
        elif {"title", "title_source"} <= cols:
            conn.execute("UPDATE articles SET title='无标题文章' WHERE COALESCE(title_source,'') IN ('translation','title_summary','ai')")
        # Keep the canonical body untouched. The fetcher's upsert refreshes
        # body_html but retains original_body_html from the first ingest, so
        # that snapshot may still be an excerpt or an older source revision.
        # Unlike title_source, there is no body provenance marker that can
        # justify restoring this snapshot (nor does length establish freshness).
        if "title_source" in cols:
            conn.execute("UPDATE articles SET title_source=NULL" + (", title_updated_at=NULL" if "title_updated_at" in cols else ""))
    conn.execute("INSERT INTO ai_schema_migrations(name) VALUES('server_only_v6')")
    if "ai_result_versions" in tables:
        conn.execute("DELETE FROM ai_result_versions")
    if "ai_result_version_state" in tables:
        conn.execute("UPDATE ai_result_version_state SET epoch=lower(hex(randomblob(16))),sequence=0")
    for event in ("INSERT", "UPDATE", "DELETE"):
        conn.execute("DROP TRIGGER IF EXISTS ai_result_version_" + event)

    return {"quarantined_article_results": quarantined, "unrecoverable_titles": missing_titles,
            "historical_summary_rows": preview["historical_summary_rows"],
            "historical_translation_rows": preview["historical_translation_rows"]}


if __name__ == "__main__":
    import argparse
    import json
    import sqlite3
    from pathlib import Path
    from urllib.parse import quote

    parser = argparse.ArgumentParser(description="Read-only AI v6 migration preview")
    parser.add_argument("--db", default="/app/data/news.db", help="path to news.db")
    args = parser.parse_args()
    path = Path(args.db).expanduser().resolve(strict=True)
    with sqlite3.connect("file:" + quote(str(path)) + "?mode=ro", uri=True) as connection:
        print(json.dumps(migration_preview(connection), ensure_ascii=False, indent=2))
