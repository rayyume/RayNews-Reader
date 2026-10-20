"""Atomic migration of mixed legacy results into a server-only result store."""
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


def migrate_results(conn):
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
    quarantined = conn.execute("SELECT COUNT(*) FROM ai_results").fetchone()[0]
    missing_titles = 0
    conn.execute("CREATE TABLE IF NOT EXISTS ai_results_legacy_v5 AS SELECT * FROM ai_results")
    conn.execute("DELETE FROM ai_results")
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "articles" in tables:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(articles)")}
        if "title_source" in cols and "title" in cols:
            original_missing = "(original_title IS NULL OR original_title='') AND " if "original_title" in cols else ""
            missing_titles = conn.execute("SELECT COUNT(*) FROM articles WHERE " + original_missing + "COALESCE(title_source,'') IN ('translation','title_summary','ai')").fetchone()[0]
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

    return {"quarantined_article_results": quarantined, "unrecoverable_titles": missing_titles}
