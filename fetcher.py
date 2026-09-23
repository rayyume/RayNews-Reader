#!/usr/bin/env python3
"""
RayNews Fetcher
Fetches messages from Telegram channel (like BroadcastChannel), then
optionally fetches Telegraph full articles for messages that contain
telegra.ph links. Outputs news.json
"""

import json
import os
import re
import sqlite3
import logging
import time
import html as html_mod
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlsplit
from concurrent.futures import ThreadPoolExecutor, as_completed, wait
from email.utils import parsedate_to_datetime
from datetime import timezone as dt_timezone, timedelta
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from network_safety import safe_get
from news_schema import enable_wal_mode, ensure_article_schema
from source_categories import (
    ensure_article_sources, init_source_categories,
    ensure_article_source_columns,
    extract_domain_from_url, extract_domains_from_html, lookup_source_by_domain,
    publisher_source_for_domain,
)

# ─── Config (overridable via environment variables) ──────
def _resolve_telegram_urls() -> tuple[str, str, str]:
    """Derive (channel, list_url, post_url) from TELEGRAM_CHANNEL_URL if set,
    otherwise fall back to the legacy TELEGRAM_CHANNEL + hardcoded t.me domain.

    Using a full URL avoids hardcoding the t.me domain in code, so a domain
    change or mirror can be handled purely via env var.
    """
    channel_url = os.environ.get("TELEGRAM_CHANNEL_URL", "").strip()
    if channel_url:
        parsed = urlsplit(channel_url)
        base = f"{parsed.scheme}://{parsed.netloc}"
        path_parts = [p for p in parsed.path.split("/") if p]
        channel = path_parts[-1] if path_parts else "your_channel"
        return channel, channel_url, f"{base}/{channel}/{{id}}?embed=1&mode=tme"

    channel = os.environ.get("TELEGRAM_CHANNEL", "your_channel")
    return channel, f"https://t.me/s/{channel}", f"https://t.me/{channel}/{{id}}?embed=1&mode=tme"


TELEGRAM_CHANNEL, TELEGRAM_LIST_URL, TELEGRAM_POST_URL = _resolve_telegram_urls()
OUTPUT_DIR = Path(os.environ.get("DATA_DIR", "/app/data"))
OUTPUT_FILE = OUTPUT_DIR / "news.json"
STATE_FILE = OUTPUT_DIR / "fetcher_state.json"
DB_FILE = OUTPUT_DIR / "news.db"
PROGRESS_FILE = OUTPUT_DIR / "fetch_progress.json"
# Set by refresh_server.py via subprocess env when it launches this fetch cycle, so the
# progress file we write can be matched to a job by exact ID rather than a
# second-granularity timestamp heuristic. Empty when run standalone (e.g. manually,
# or by tests) — write_fetch_progress() then just omits job_id from the payload.
FETCH_JOB_ID = os.environ.get("FETCH_JOB_ID", "")
MAX_WORKERS = 15
REQUEST_TIMEOUT = 20
# Telegraph full-text fetches get a shorter timeout than the Telegram list-page fetch
# (REQUEST_TIMEOUT) — a slow outbound article page shouldn't stretch a manual refresh
# out that long. fetch_telegraph() catches its own exceptions and returns None on
# failure, so a timeout just falls back to the plain Telegram excerpt for this cycle
# (see process_message()) without failing the message or blocking last_seen_id. That
# fallback is no longer permanent: backfill_missing_fulltext() re-fetches recent
# Telegraph articles still stuck on the excerpt on later cycles (it has the stored
# telegraph_url to retry), so keeping this short trades a little first-paint latency
# for speed without costing article quality.
FULLTEXT_TIMEOUT = 10
FULLTEXT_BODY_LIMIT = 2 * 1024 * 1024
# WeChat keeps the longer timeout: unlike Telegraph, a failed WeChat full-text has no
# backfill safety net (we don't persist the source WeChat URL, and
# backfill_missing_fulltext() only retries telegraph_url), so a short timeout here would
# permanently downgrade otherwise-fetchable articles to the excerpt. Restore the original
# 20s until WeChat backfill exists.
WECHAT_FULLTEXT_TIMEOUT = 20
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
HEADERS = {"User-Agent": USER_AGENT}
# Max historical pages to fetch on initial full sync
MAX_HISTORY_PAGES = 200
# Stream articles into SQLite in small batches during processing (instead of one big
# upsert at the end) so they become visible to readers as soon as they're ready, rather
# than waiting for the whole fetch cycle (all pages + all full-text fetches) to finish.
STREAM_BATCH_SIZE = 5
STREAM_BATCH_SECONDS = 2.0
STREAM_EXECUTOR_SHUTDOWN_SECONDS = 3.0
CST = dt_timezone(timedelta(hours=8))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("fetcher")


# ─── SQLite ──────────────────────────────────────────────
def init_db() -> sqlite3.Connection:
    """Initialize SQLite DB and return connection."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_FILE), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS articles (
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
            original_body_html TEXT DEFAULT '',
            summary TEXT DEFAULT ''
        )
    """)
    conn.commit()
    ensure_article_schema(conn)
    enable_wal_mode(conn)
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_timestamp ON articles(timestamp DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_source ON articles(source)")
    init_source_categories(conn)
    conn.commit()
    return conn


def upsert_articles(
    conn: sqlite3.Connection,
    entries: list[dict],
    sync_sources: bool = True,
) -> list[int]:
    """Batch insert or update articles into SQLite and return persisted positive IDs.

    ensure_article_sources() does a full-table alias UPDATE plus a DISTINCT scan over
    every article, so it's expensive relative to a handful of row upserts. The
    streaming-ingest loop in run() calls this once per small batch (every few seconds)
    — pass sync_sources=False there and let the caller run it once after the whole
    cycle finishes, instead of repeating a full-table pass per batch.
    """
    sql = """INSERT INTO articles
        (id, title, source, feed_source, origin_source, group_source, publisher_domain,
         source_detection_version, ingested_at, time, date, timestamp, thumb,
         has_full_content, telegraph_url, body_html, original_body_html, summary,
         original_title, title_updated_at, title_source)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            title = excluded.title,
            source = CASE WHEN EXISTS (SELECT 1 FROM source_categories sc
                WHERE sc.source = COALESCE(NULLIF(articles.group_source, ''), articles.source)
                  AND sc.status IN ('manual', 'classified'))
                THEN articles.source ELSE excluded.source END,
            feed_source = excluded.feed_source,
            origin_source = CASE WHEN EXISTS (SELECT 1 FROM source_categories sc
                WHERE sc.source = COALESCE(NULLIF(articles.group_source, ''), articles.source)
                  AND sc.status IN ('manual', 'classified'))
                THEN articles.origin_source ELSE excluded.origin_source END,
            group_source = CASE WHEN EXISTS (SELECT 1 FROM source_categories sc
                WHERE sc.source = COALESCE(NULLIF(articles.group_source, ''), articles.source)
                  AND sc.status IN ('manual', 'classified'))
                THEN articles.group_source ELSE excluded.group_source END,
            publisher_domain = CASE WHEN EXISTS (SELECT 1 FROM source_categories sc
                WHERE sc.source = COALESCE(NULLIF(articles.group_source, ''), articles.source)
                  AND sc.status IN ('manual', 'classified'))
                THEN articles.publisher_domain ELSE excluded.publisher_domain END,
            source_detection_version = CASE WHEN EXISTS (SELECT 1 FROM source_categories sc
                WHERE sc.source = COALESCE(NULLIF(articles.group_source, ''), articles.source)
                  AND sc.status IN ('manual', 'classified'))
                THEN articles.source_detection_version ELSE excluded.source_detection_version END,
            time = excluded.time,
            date = excluded.date,
            timestamp = excluded.timestamp,
            thumb = excluded.thumb,
            has_full_content = CASE
                WHEN articles.has_full_content = 1 OR excluded.has_full_content = 1 THEN 1 ELSE 0 END,
            telegraph_url = CASE WHEN excluded.telegraph_url != ''
                THEN excluded.telegraph_url ELSE articles.telegraph_url END,
            body_html = CASE WHEN excluded.body_html != ''
                THEN excluded.body_html ELSE articles.body_html END,
            original_body_html = CASE
                WHEN articles.original_body_html IS NULL OR articles.original_body_html = ''
                THEN excluded.original_body_html ELSE articles.original_body_html END,
            summary = CASE WHEN excluded.summary != ''
                THEN excluded.summary ELSE articles.summary END,
            original_title = CASE WHEN excluded.original_title IS NOT NULL
                THEN excluded.original_title ELSE articles.original_title END,
            title_updated_at = CASE WHEN excluded.title_updated_at IS NOT NULL
                THEN excluded.title_updated_at ELSE articles.title_updated_at END,
            title_source = CASE WHEN excluded.title_source IS NOT NULL
                THEN excluded.title_source ELSE articles.title_source END"""
    rows = []
    deleted_ids = {
        int(row[0])
        for row in conn.execute("SELECT article_id FROM deleted_articles").fetchall()
    }
    # Reuse mappings within this batch; the next batch sees admin edits.
    publisher_sources = {}
    history = classified_source_history(conn)
    for e in entries:
        article_id = int(e.get("id", 0) or 0)
        if article_id in deleted_ids:
            continue
        domain = e.get("publisher_domain", "") or ""
        group_source = e.get("group_source") or e.get("source") or e.get("feed_source", "")
        source_html = e.get("source_html") or e.get("body_html") or ""
        learned = source_from_classified_history(source_html, history)
        if learned:
            group_source, domain = learned
        if domain:
            if domain not in publisher_sources:
                publisher_sources[domain] = publisher_source_for_domain(conn, domain)
            if not learned:
                group_source = publisher_sources[domain] or group_source
        rows.append((
            article_id,
            e.get("title", ""),
            group_source,
            e.get("feed_source", e.get("source", "")),
            group_source if learned else (e.get("origin_source") or group_source),
            group_source,
            domain,
            int(e.get("source_detection_version", 0) or 0),
            int(e.get("ingested_at") or time.time()),
            e.get("time", ""),
            e.get("date", ""),
            e.get("timestamp", 0),
            e.get("thumb", ""),
            1 if e.get("has_full_content") else 0,
            e.get("telegraph_url", ""),
            e.get("body_html", ""),
            e.get("original_body_html", e.get("body_html", "")),
            e.get("summary", ""),
            e.get("original_title"),
            e.get("title_updated_at"),
            e.get("title_source"),
        ))
    conn.executemany(sql, rows)
    if sync_sources:
        ensure_article_sources(conn)
    persisted_ids = sorted({int(row[0]) for row in rows if int(row[0]) > 0})
    total_count = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
    log_message = f"SQLite: upserted {len(rows)} articles (total: {total_count})"
    conn.commit()
    # The commit is the final fallible batch operation. Logging is observational:
    # a broken sink must not make a durable batch look uncommitted and retry it.
    try:
        log.info(log_message)
    except Exception:
        pass
    return persisted_ids


# A Telegraph full-text fetch that fails on the cycle an article first arrives (e.g.
# FULLTEXT_TIMEOUT tripped on a briefly-slow article page, or a transient 5xx) leaves the
# article stored with has_full_content=0 and only the Telegram excerpt. The main loop
# never revisits it — last_seen_id advances past it — so without this pass that downgrade
# is permanent. Re-fetch a bounded batch of recent such articles at the end of each cycle
# so a transient blip costs full text for at most a cycle or two, not forever. The window
# is bounded by recency rather than a per-article retry counter: a genuinely dead
# Telegraph URL simply ages out of BACKFILL_MAX_AGE_DAYS instead of being retried forever
# (and no schema migration is needed to track attempts).
BACKFILL_MAX_AGE_DAYS = 3
BACKFILL_LIMIT = 40


def _snapshot_translation_updated_at(conn, article_id: int):
    try:
        row = conn.execute(
            "SELECT translation_updated_at FROM ai_results WHERE article_id = ?",
            (article_id,),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    return row[0] if row is not None else None


def _invalidate_stale_translation(
    conn: sqlite3.Connection,
    article_id: int,
    before_translation_updated_at: str | None = None,
) -> bool:
    """Clear a cached excerpt translation after the original body is upgraded."""
    table_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'ai_results'"
    ).fetchone()
    if table_exists is None:
        return False

    columns = {
        row[1] for row in conn.execute("PRAGMA table_info(ai_results)").fetchall()
    }
    if "translation" not in columns:
        raise sqlite3.OperationalError(
            "ai_results table is missing required translation column"
        )

    assignments = ["translation = NULL"]
    if "translation_updated_at" in columns:
        assignments.append("translation_updated_at = NULL")
    stale_guard = ""
    params: list = [article_id]
    if (
        "translation_updated_at" in columns
        and before_translation_updated_at is not None
    ):
        stale_guard = " AND (translation_updated_at IS NULL OR translation_updated_at <= ?)"
        params.append(before_translation_updated_at)
    cursor = conn.execute(
        f"UPDATE ai_results SET {', '.join(assignments)} "
        "WHERE article_id = ? AND translation IS NOT NULL"
        f"{stale_guard}",
        params,
    )
    return cursor.rowcount > 0


def backfill_missing_fulltext(conn: sqlite3.Connection) -> int:
    """Retry Telegraph full-text for recent articles still stuck on the excerpt fallback.

    Returns the number of articles upgraded to full content this cycle.
    """
    cutoff = int(time.time()) - BACKFILL_MAX_AGE_DAYS * 86400
    rows = conn.execute(
        """SELECT id, telegraph_url, thumb, origin_source
             FROM articles
            WHERE has_full_content = 0
              AND telegraph_url != ''
              AND timestamp >= ?
            ORDER BY timestamp DESC
            LIMIT ?""",
        (cutoff, BACKFILL_LIMIT),
    ).fetchall()
    if not rows:
        return 0
    updated = 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(fetch_telegraph, row["telegraph_url"]): row for row in rows
        }
        for future in as_completed(futures):
            row = futures.pop(future)
            try:
                result = future.result()
            except Exception as e:
                log.warning(f"  Backfill fetch failed (id={row['id']}): {e}")
                continue
            if not result:
                continue
            # Mirror process_message()'s full-text application: keep an existing thumb,
            # otherwise adopt the article's first image; prefer a Telegraph-detected
            # source when one was found, else keep what we already had.
            new_thumb = row["thumb"] or (result["images"][0] if result["images"] else "")
            origin = result.get("detected_source", "") or row["origin_source"]
            try:
                prior_translation_updated_at = _snapshot_translation_updated_at(
                    conn, row["id"]
                )
                conn.execute(
                    """UPDATE articles
                          SET has_full_content = 1,
                              body_html = ?,
                              original_body_html = ?,
                              thumb = ?,
                              origin_source = ?,
                              summary = ''
                        WHERE id = ?""",
                    (
                        result["body_html"],
                        result["body_html"],
                        new_thumb,
                        origin,
                        row["id"],
                    ),
                )
                _invalidate_stale_translation(
                    conn, row["id"], prior_translation_updated_at
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            updated += 1
    if updated:
        log.info(f"Backfilled full text for {updated}/{len(rows)} article(s)")
    return updated


def write_fetch_progress(
    inserted: int,
    total: int,
    inserted_ids: list[int] | None = None,
) -> None:
    """Record streaming-ingest progress for the current fetch cycle so
    /refresh/status can report how many articles have already landed in SQLite
    while the cycle is still running. Written atomically (temp + rename), same
    pattern as news.json, so a concurrent reader never sees a half-written file."""
    normalized_ids = []
    for value in inserted_ids or []:
        try:
            article_id = int(value)
        except (TypeError, ValueError):
            continue
        if article_id > 0:
            normalized_ids.append(article_id)
    payload = {
        "pid": os.getpid(),
        "job_id": FETCH_JOB_ID,
        "inserted": inserted,
        "inserted_ids": sorted(set(normalized_ids)),
        "total_messages": total,
        "updated_at": int(time.time()),
    }
    try:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        tmp = PROGRESS_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(PROGRESS_FILE)
    except Exception as e:
        log.warning(f"Could not write fetch progress: {e}")


def _commit_stream_batch(
    conn: sqlite3.Connection,
    batch: list[dict],
    inserted_ids: dict[int, None],
    total_messages: int,
) -> int:
    """Commit one completed batch and immediately release its article payloads."""
    batch_size = len(batch)
    committed_ids = upsert_articles(conn, batch, sync_sources=False)
    batch.clear()
    inserted_ids.update(dict.fromkeys(committed_ids))
    del committed_ids
    write_fetch_progress(len(inserted_ids), total_messages, list(inserted_ids))
    return batch_size


def _rollback_best_effort(conn: sqlite3.Connection) -> None:
    """Rollback a failed stream write without allowing cleanup to be bypassed."""
    try:
        conn.rollback()
    except Exception as exc:
        try:
            log.error(f"Streaming SQLite rollback failed: {exc}")
        except Exception:
            pass


def _shutdown_stream_executor(executor, futures: dict) -> None:
    """Cancel pending work, bound the wait for running work, then release results."""
    try:
        for owned_future in tuple(futures):
            if not owned_future.done():
                owned_future.cancel()
        if executor is not None:
            pending = tuple(futures)
            if pending:
                _done, not_done = wait(
                    pending, timeout=STREAM_EXECUTOR_SHUTDOWN_SECONDS
                )
                if not_done:
                    log.warning(
                        f"{len(not_done)} stream worker(s) still running after "
                        f"{STREAM_EXECUTOR_SHUTDOWN_SECONDS}s shutdown cap"
                    )
            executor.shutdown(wait=False, cancel_futures=True)
    finally:
        futures.clear()


def _clear_stream_payloads(
    stream_batch: list[dict], failed_batches: list[list[dict]]
) -> None:
    """Release every uncommitted entry owned by the streaming lifecycle."""
    stream_batch.clear()
    for failed_batch in failed_batches:
        failed_batch.clear()
    failed_batches.clear()


def migrate_news_json(conn: sqlite3.Connection):
    """Import existing news.json into SQLite if DB is empty."""
    count = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
    if count > 0:
        return  # already migrated
    if not OUTPUT_FILE.exists():
        return
    try:
        data = json.loads(OUTPUT_FILE.read_text(encoding="utf-8"))
        items = data.get("items", [])
        if items:
            upsert_articles(conn, [
                {**item, "ingested_at": item.get("ingested_at") or item.get("timestamp")}
                for item in items
            ])
            log.info(f"Migrated {len(items)} articles from news.json to SQLite")
    except Exception as e:
        log.warning(f"Migration from news.json failed: {e}")


# ─── Helpers ──────────────────────────────────────────────
def clean_html(text: str) -> str:
    """Strip HTML tags and decode entities."""
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_mod.unescape(text)
    return " ".join(text.split()).strip()


def extract_title(text: str) -> str:
    """Extract first meaningful line as title."""
    # Try first line with content
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    for line in lines:
        # Skip pure emoji or very short lines
        cleaned = re.sub(r'[\U0001F000-\U0010FFFF]', '', line).strip()
        if len(cleaned) >= 6:
            return line[:120]
    # Fallback: first 80 chars
    return text[:80]


# ─── Telegram Fetching ───────────────────────────────────
def fetch_telegram_page(before: str = "") -> str | None:
    """Fetch a Telegram channel page. Returns HTML or None on failure."""
    url = TELEGRAM_LIST_URL
    params = {"before": before} if before else {}
    try:
        log.info(f"Fetching Telegram: {url} before={before or '(latest)'}")
        resp = requests.get(url, headers=HEADERS, params=params, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return resp.text
    except Exception as e:
        log.error(f"Telegram fetch failed: {e}")
        return None


def parse_messages(html: str) -> list[dict]:
    """Parse all messages from a Telegram channel page HTML."""
    soup = BeautifulSoup(html, "html.parser")
    messages = []

    for wrap in soup.select(".tgme_widget_message_wrap"):
        msg = wrap.select_one(".tgme_widget_message")
        if not msg:
            continue

        # Message ID: data-post="your_channel/277214"
        data_post = msg.get("data-post", "")
        post_parts = data_post.rsplit("/", 1)
        feed_channel = post_parts[0] if len(post_parts) == 2 else TELEGRAM_CHANNEL
        msg_id = post_parts[-1].strip()
        if not msg_id or not msg_id.isdigit():
            continue

        # Skip service messages (join/leave/etc)
        classes = msg.get("class", "")
        if isinstance(classes, str) and "service_message" in classes:
            continue
        if isinstance(classes, (list, set, frozenset)) and "service_message" in classes:
            continue

        # Date/time
        time_el = msg.select_one(".tgme_widget_message_date time")
        datetime_str = time_el.get("datetime", "") if time_el else ""

        # Content HTML
        # Try direct text first, fallback to with reply
        has_reply = bool(msg.select_one(".js-message_reply_text"))
        text_selector = ".tgme_widget_message_text.js-message_text" if has_reply else ".tgme_widget_message_text"
        text_el = msg.select_one(text_selector)

        # Extract plain text
        content_text = ""
        content_html = ""
        if text_el:
            content_text = text_el.get_text("\n", strip=True)
            # Get inner HTML only, strip the outer wrapper element
            content_html = text_el.decode_contents()

        # Images
        images = []
        for photo in msg.select(".tgme_widget_message_photo_wrap"):
            style = photo.get("style", "")
            m = re.search(r'url\(["\']?([^"\'()]+)["\']?\)', style)
            if m:
                img_url = m.group(1)
                if img_url.startswith("//"):
                    img_url = "https:" + img_url
                images.append(img_url)

        # Videos
        videos = []
        for vwrap in msg.select(".tgme_widget_message_video_wrap"):
            video_html = str(vwrap)
            video_html = video_html.replace("preload muted autoplay loop playsinline", "controls preload metadata playsinline webkit-playsinline")
            videos.append(video_html)

        # Link preview
        link_preview = msg.select_one(".tgme_widget_message_link_preview")
        link_preview_url = ""
        link_preview_title = ""
        if link_preview:
            link_a = link_preview.get("href", "")
            if link_a:
                link_preview_url = link_a
            lp_title = link_preview.select_one(".link_preview_title")
            if lp_title:
                link_preview_title = lp_title.get_text(strip=True)

        if not content_text and not images:
            continue

        messages.append({
            "id": int(msg_id),
            "feed_source": f"@{feed_channel.lstrip('@')}",
            "datetime": datetime_str,
            "text": content_text,
            "html": content_html,
            "images": images,
            "videos": videos,
            "link_preview_url": link_preview_url,
            "link_preview_title": link_preview_title,
        })

    # Return in chronological order (oldest first)
    messages.sort(key=lambda x: x["id"])
    return messages


def get_anchor_id(messages: list[dict]) -> int | None:
    """Get the newest (largest) message ID from a page, used for Telegram's before= parameter."""
    if not messages:
        return None
    return messages[-1]["id"]


# ─── State Management ────────────────────────────────────
def load_state() -> dict:
    """Load fetcher state (last_seen_id, etc.)."""
    try:
        if STATE_FILE.exists():
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning(f"State load failed: {e}")
    return {"last_seen_id": 0}


def save_state(state: dict):
    """Save fetcher state atomically (write to temp, then rename)."""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    tmp.replace(STATE_FILE)


# ─── Source Detection ─────────────────────────────────────
def _extract_bottom_html(html: str, ratio: float = 0.15) -> str:
    """Return the bottom *ratio* portion of HTML content.

    Splits on block-level boundaries (<br>, </p>, </div>, <hr>, \n\n)
    and keeps only the last N chunks.  Source attribution links are
    almost always at the very end of an article; links in the top/middle
    are content references, not source indicators.
    """
    if not html:
        return ""
    # Split on common block boundaries
    chunks = re.split(r'(?:<br\s*/?>\s*)+|</p>|</div>|<hr[^>]*>|\n\s*\n', html, flags=re.IGNORECASE)
    chunks = [c.strip() for c in chunks if c.strip()]
    if not chunks:
        return html
    keep = max(1, int(len(chunks) * ratio))
    return "\n".join(chunks[-keep:])


def detect_feed_source(channel: str = "") -> str:
    """Return the Telegram/RSS feed identity, never an article publisher."""
    value = (channel or TELEGRAM_CHANNEL or "").strip().lstrip("@")
    return f"@{value}" if value else "Unknown Feed"


def detect_group_source(content: str, preview_url: str = "", channel: str = "") -> tuple[str, str]:
    """Identify an explicit footer attribution or the article preview publisher."""
    links = bottom_source_links(content)
    # An explicit attribution outweighs ordinary footer reference links.
    for item in reversed(links):
        if item["via"]:
            return item["group"], item["domain"]
    attribution = _extract_bottom_via_sources(content or "")
    if attribution:
        return attribution[0], ""
    # Without attribution, prefer a recognized publisher among footer links.
    for item in reversed(links):
        if item["domain"]:
            match = lookup_source_by_domain([item["domain"]])
            if match:
                return match[0], item["domain"]
    # A single footer website is usable evidence. Several unknown websites
    # are ambiguous, so leave the existing source alone during re-detection.
    websites = [item for item in links if item["domain"]]
    if len(websites) == 1:
        return websites[0]["group"], websites[0]["domain"]
    domain = extract_domain_from_url(preview_url)
    if domain:
        match = lookup_source_by_domain([domain])
        return (match[0] if match else domain), domain
    return detect_feed_source(channel=channel), ""


def classified_source_history(
    conn: sqlite3.Connection, before_timestamp: int | None = None,
) -> tuple[dict[str, str], dict[str, str]]:
    """Learn only unambiguous identities from already classified sources/articles."""
    if before_timestamp is None:
        rows = conn.execute(
            "SELECT source, label FROM source_categories WHERE status IN ('manual', 'classified')"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT sc.source, sc.label FROM source_categories sc "
            "WHERE sc.status IN ('manual', 'classified') AND EXISTS "
            "(SELECT 1 FROM articles a WHERE a.group_source = sc.source "
            "AND a.timestamp < ?)", (before_timestamp,)
        ).fetchall()
    names = {source.casefold(): source for source, _label in rows}
    label_targets = {}
    for source, label in rows:
        if label:
            label_targets.setdefault(label.casefold(), set()).add(source)
    for label, targets in label_targets.items():
        if len(targets) == 1:
            names.setdefault(label, next(iter(targets)))
    classified_keys = {source.casefold() for source, _label in rows}
    domains = {}
    conflicts = set()
    article_sql = (
        "SELECT publisher_domain, group_source FROM articles "
        "WHERE publisher_domain != '' AND group_source != ''"
    )
    if before_timestamp is not None:
        article_sql += " AND timestamp < ?"
    for domain, source in conn.execute(
        article_sql, () if before_timestamp is None else (before_timestamp,)
    ):
        if source.casefold() not in classified_keys:
            continue
        if domain in domains and domains[domain] != source:
            conflicts.add(domain)
        else:
            domains[domain] = source
    for domain, source in conn.execute(
        "SELECT domain, group_source FROM publisher_domains WHERE enabled = 1"
    ):
        if source.casefold() not in classified_keys:
            continue
        if domain in domains and domains[domain] != source:
            conflicts.add(domain)
        else:
            domains[domain] = source
    for domain in conflicts:
        domains.pop(domain, None)
    return names, domains


def source_from_classified_history(
    content: str, history: tuple[dict[str, str], dict[str, str]],
) -> tuple[str, str] | None:
    """Match footer evidence to existing classifications without changing history."""
    names, domains = history
    links = bottom_source_links(content)
    via_links = [item for item in links if item["via"]]
    telegram_links = [item for item in links if item["telegram"] and not item["via"]]

    def classified_telegram_source(via_label: str = "") -> str | None:
        matches = {
            names[item["label"].casefold()]: item["label"]
            for item in telegram_links if item["label"].casefold() in names
        }
        if via_label:
            related = {
                source for source, label in matches.items()
                if label.casefold() in via_label.casefold()
            }
            return next(iter(related)) if len(related) == 1 else None
        return next(iter(matches)) if len(matches) == 1 else None

    if via_links:
        # An unknown explicit via is still stronger than a classified reference.
        for item in reversed(via_links):
            if item["label"] and item["label"].casefold() in names:
                return names[item["label"].casefold()], ""
            if item["domain"] in domains:
                return domains[item["domain"]], item["domain"]
            if item["telegram"]:
                source = classified_telegram_source(item["label"])
                if source:
                    return source, ""
        return None
    via_names = _extract_bottom_via_sources(content)
    if via_names:
        for name in via_names:
            if name.casefold() in names:
                return names[name.casefold()], ""
            source = classified_telegram_source(name)
            if source:
                return source, ""
        return None
    source = classified_telegram_source()
    if source:
        return source, ""
    if any(item["label"].casefold() in names for item in telegram_links):
        # Several classified channel links are ambiguous; an unrelated source
        # URL in the footer cannot break the tie.
        return None
    for item in reversed(links):
        if item["telegram"]:
            continue
        if item["label"] and item["label"].casefold() in names:
            return names[item["label"].casefold()], ""
        if item["domain"] in domains:
            return domains[item["domain"]], item["domain"]
    return None


def _safe_http_host(href: str) -> str:
    """Reject malformed or non-web footer links without aborting the article."""
    try:
        parsed = urlsplit(href or "")
        return (parsed.hostname or "").lower() if parsed.scheme.lower() in {"http", "https"} else ""
    except (TypeError, ValueError):
        return ""


def bottom_source_links(content: str) -> list[dict]:
    """Footer links with explicit via links marked; preserve document order."""
    chunks = re.split(r'(?:<br\s*/?>\s*)+|</p>|</div>|<hr[^>]*>|\n\s*\n', content or "", flags=re.I)
    chunks = [chunk.strip() for chunk in chunks if chunk.strip()]
    if not chunks:
        return []
    end = len(chunks) - 1
    skipped = 0
    while end >= 0 and "<a" not in chunks[end].lower() and skipped < 2:
        if len(clean_html(chunks[end])) > 80:
            return []
        end -= 1
        skipped += 1
    if end < 0 or "<a" not in chunks[end].lower():
        return []
    start = end
    while start > 0:
        previous = chunks[start - 1]
        if "<a" not in previous.lower() or len(clean_html(previous)) > 100:
            break
        start -= 1
    tail = chunks[start:end + 1]
    items = []
    for chunk in tail:
        text = clean_html(chunk).strip()
        via = bool(re.match(r"(?i)^via\b\s*[:：-]?", text))
        if via:
            match = re.search(r"(?i)\bvia\b", chunk)
            fragment = chunk[match.end():] if match else chunk
        else:
            fragment = chunk
        plain_via = _plain_via_prefix(fragment) if via else ""
        soup = BeautifulSoup(fragment, "html.parser")
        for link in soup.find_all("a", href=True):
            # An image wrapped in an anchor is a thumbnail, not a source link.
            if link.find("img") and not link.get_text(" ", strip=True):
                continue
            href = link.get("href", "")
            host = _safe_http_host(href)
            if not host:
                continue
            telegram = host in {"t.me", "telegram.me"}
            if host == "telegra.ph":
                continue
            domain = "" if telegram else (extract_domain_from_url(href) or "")
            label = _clean_source_name(clean_html(link.get_text(" ", strip=True)))
            if not _valid_attribution_name(label):
                label = ""
            if not domain and not (host.endswith("weixin.qq.com") or telegram):
                continue
            if not domain and not label:
                continue
            match = lookup_source_by_domain([domain]) if domain else None
            items.append({"group": match[0] if match else (domain or label),
                          "label": label, "domain": domain, "via": via and not plain_via,
                          "telegram": telegram})
            # Only the first valid link after "via" is an attribution.
            via = False
    return items


def _plain_via_prefix(fragment: str) -> str:
    """Return a standalone source name written before the first footer link."""
    before_link = re.split(r"<a\b", fragment, maxsplit=1, flags=re.I)[0]
    value = _clean_source_name(clean_html(before_link)).strip(" :：-—|\t\n")
    return value if _valid_attribution_name(value) and re.search(r"\w", value) else ""


def _valid_attribution_name(name: str) -> bool:
    value = (name or "").strip()
    words = value.lower().split()
    return bool(value and len(value) <= 30 and value != "常务副主席"
                and len(words) <= 4 and (not words or words[0] not in {
                    "and", "as", "via", "or", "by", "from", "source"
                }) and value.lower() != "read more")


def detect_source_from_attribution(content: str) -> str | None:
    """Extract source only from explicit bottom via attribution."""
    via_candidates = _extract_bottom_via_sources(content)
    if via_candidates:
        return via_candidates[0]

    return None


def _extract_body_attribution(content: str) -> str | None:
    """Extract source from body-inline attribution like '—— SourceName' at article end."""
    plain = clean_html(content)
    if not plain:
        return None

    # Only look at the last 300 chars — in-body attributions are always at the end
    tail = plain[-300:] if len(plain) > 300 else plain
    # Match patterns: —— SourceName, ▲ SourceName, — SourceName, | Source
    m = re.search(r"(?:——|▲|—\s|\|\s)([\w一-鿿㐀-䶿·]+(?:[一-鿿\w]|\b))(?:\s|$)", tail)
    if not m:
        return None
    raw = _clean_source_name(m.group(1).strip())
    if raw and 1 < len(raw) <= 20:
        return raw
    return None


def _extract_bottom_via_sources(content: str) -> list[str]:
    """Return valid via sources from bottom to top, preferring link text.

    Only examines links that appear AFTER the last "via" keyword in each chunk,
    so that reference/body links elsewhere in the article cannot interfere.

    Two critical guards against misidentifying body text as source names:
    1. The "via" keyword must be in the bottom 500 chars of the content.
    2. The cleaned source name must be ≤ 40 chars (real source names are short).
    """
    # Quick guard: the attribution "via" is always near the end.
    # If the last "via" in the whole content is far from the bottom, it is body text.
    plain_all = clean_html(content)
    all_via = list(re.finditer(r"(?i)\bvia\b", plain_all))
    if not all_via or (len(plain_all) - all_via[-1].start()) > 500:
        return []

    chunks = re.split(r'(?:<br\s*/?>\s*)+|</p>|</div>|<hr[^>]*>|\n\s*\n', content, flags=re.I)
    candidates = []
    for chunk in reversed(chunks):
        line = clean_html(chunk)
        if not re.match(r"(?i)^\s*via\b", line):
            continue

        # Find the last "via" in the raw HTML — attribution lines are at the end
        via_matches = list(re.finditer(r"(?i)\bvia\b", chunk))
        if via_matches:
            # Only examine HTML after the last "via" (at most 150 chars)
            after_via_html = chunk[via_matches[-1].end():][:150]
            plain_prefix = _plain_via_prefix(after_via_html)
            if plain_prefix:
                candidates.append(plain_prefix)
                continue
            after_via_soup = BeautifulSoup(after_via_html, "html.parser")
            via_links = []
            for link in after_via_soup.find_all("a"):
                text = clean_html(link.get_text(" ", strip=True))
                href = link.get("href", "")
                if not text:
                    continue
                host = _safe_http_host(href)
                if not host:
                    continue
                if host in {"telegra.ph", "t.me", "telegram.me"}:
                    continue
                via_links.append(text)
            if via_links:
                raw = _clean_source_name(via_links[0])
                if _valid_attribution_name(raw):
                    candidates.append(raw)
                continue
            if any(link.get_text(" ", strip=True) for link in after_via_soup.find_all("a")):
                continue

        # No via-adjacent link found — fall back to plain text after "via".
        # Extract from RAW HTML (before clean_html, which collapses <br> → space).
        # Split by <br> to isolate the via line from any article text that follows.
        via_match = re.search(r"(?i)\bvia\b", chunk)
        if via_match:
            after_via_html = chunk[via_match.end():]
            # Take only the first <br>-delimited segment after "via"
            first_segment = re.split(r"<br\s*/?>", after_via_html, maxsplit=1, flags=re.IGNORECASE)[0]
            raw = clean_html(first_segment).strip()
        else:
            raw = _text_after_last_via(line)
        if raw and len(raw) > 80:
            raw = raw[:80].rsplit(" ", 1)[0]
        raw = _clean_source_name(raw)
        if _valid_attribution_name(raw) and len(raw) <= 30:
            candidates.append(raw)
    return candidates


def _text_after_last_via(line: str) -> str:
    matches = list(re.finditer(r"(?i)(^|\s)via\b", line))
    if not matches:
        return ""
    return line[matches[-1].end():].strip(" :：-—")


def _clean_source_name(name: str) -> str:
    """Strip verbose suffixes and extract meaningful source name."""
    name = name.strip("《》【】").strip()
    # Strip emoji (U+1F000–U+1FFFF)
    name = re.sub(r"[\U0001F000-\U0010FFFF]", "", name).strip()

    known_keywords = [
        "格隆汇", "联合早报", "cnBeta", "界面新闻", "少数派",
        "爱范儿", "金十数据", "TechCrunch", "36氪", "阮一峰",
        "MacRumors", "华尔街日报", "投资界", "包邮区",
        "XP Digital Lab", "凤凰网财经", "凤凰网科技",
        "WBusiness商业",
    ]
    for kw in known_keywords:
        if kw in name:
            return kw

    # Special cases for commonly merged channel display names
    if "科技圈" in name and "在花" in name:
        return "在花科技圈"

    suffixes = [
        " - Telegram Channel", " - Channel",
        " | Telegram Channel",
        " (Telegram Channel",
        " - 日排行", " - 即时", " - 国际", " - 要闻",
        " (author:", "全文版",
        " - Telegram", "Telegram",
        "频道",
    ]
    for s in suffixes:
        if s in name:
            name = name.split(s)[0]

    name = name.strip().rstrip(".:：")
    name = re.sub(r"\s*[-–]\s*(Channel|Group|Bot|频道|群)$", "", name, flags=re.IGNORECASE)
    name = re.sub(r"^【.*?】\s*", "", name)
    name = name.strip().rstrip(".:：")

    if " - " in name:
        segments = [s.strip() for s in name.split(" - ") if s.strip()]
        if segments:
            name = segments[0]

    return name.strip()


# ─── Telegraph Extraction ────────────────────────────────
def extract_telegraph_url(content: str) -> str | None:
    match = re.search(r'https?://telegra\.ph/[^"\'<>\s]+', content)
    if match:
        return match.group(0).rstrip('"').rstrip("'")
    return None


def extract_wechat_url(content: str) -> str | None:
    """Extract mp.weixin.qq.com URL from article content."""
    match = re.search(r'https?://mp\.weixin\.qq\.com/[^"\'<>\s]+', content)
    if match:
        return match.group(0).rstrip('"').rstrip("'")
    return None


def extract_thumb(content: str) -> str:
    match = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', content)
    if match:
        return match.group(1)
    return ""


def parse_datetime(dt_str: str) -> dict:
    """Parse ISO datetime string (from Telegram)."""
    try:
        if dt_str:
            dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
            dt_cst = dt.astimezone(CST)
            return {
                "iso": dt_cst.isoformat(),
                "time": dt_cst.strftime("%H:%M"),
                "date": dt_cst.strftime("%Y-%m-%d"),
                "timestamp": int(dt.timestamp()),
            }
    except Exception as exc:
        log.warning(f"Could not parse message datetime {dt_str!r}; using current Beijing time: {exc}")
    dt_cst = datetime.now(CST)
    return {
        "iso": dt_cst.isoformat(),
        "time": dt_cst.strftime("%H:%M"),
        "date": dt_cst.strftime("%Y-%m-%d"),
        "timestamp": int(dt_cst.timestamp()),
    }


# ─── Telegraph Fetching ──────────────────────────────────
def _read_body_with_limit(resp, limit: int) -> str:
    chunks: list[bytes] = []
    total = 0
    for chunk in resp.iter_content(chunk_size=65536):
        if not chunk:
            continue
        total += len(chunk)
        if total > limit:
            raise ValueError("fulltext body too large")
        chunks.append(chunk)
    encoding = requests.utils.get_encoding_from_headers(resp.headers) or "utf-8"
    return b"".join(chunks).decode(encoding, errors="replace")


def fetch_telegraph(url: str) -> dict | None:
    resp = None
    try:
        log.info(f"  Fetching Telegraph: {unquote(url)[:80]}...")
        resp = safe_get(
            url,
            headers=HEADERS,
            timeout=FULLTEXT_TIMEOUT,
            stream=True,
        )
        resp.raise_for_status()
        body = _read_body_with_limit(resp, FULLTEXT_BODY_LIMIT)

        soup = BeautifulSoup(body, "html.parser")
        article = soup.find("article")
        if not article:
            return None

        # ── Extract source from Telegraph metadata BEFORE cleanup ──
        telegraph_source = ""
        telegraph_domain = ""

        # 1) <address> tag below title — often the original author/source name
        address = article.find("address")
        if address:
            addr_text = address.get_text(" ", strip=True)
            if addr_text and len(addr_text) < 60:
                # Try domain lookup first on any links in address
                addr_html = str(address)
                addr_domains = extract_domains_from_html(addr_html)
                domain_match = lookup_source_by_domain(addr_domains)
                telegraph_domain = addr_domains[0] if addr_domains else ""
                if domain_match:
                    telegraph_source = domain_match[0]
                else:
                    telegraph_source = _clean_source_name(addr_text)
                log.info(f"  Telegraph source from <address>: {telegraph_source}")

        # 2) Attribution <a> links at the VERY BOTTOM of the article
        #    Only scan the last 5 <p> tags — source attributions are always at the end,
        #    links in the top/middle of articles are content references.
        if not telegraph_source:
            all_ps = article.find_all("p")
            bottom_ps = all_ps[-5:] if len(all_ps) > 5 else all_ps
            for p in bottom_ps:
                a_tag = p.find("a", href=True)
                if not a_tag:
                    continue
                href = a_tag.get("href", "")
                link_text = a_tag.get_text(strip=True)
                # Check if this looks like a source attribution link
                domains = extract_domains_from_html(f'<a href="{href}">link</a>')
                domain_match = lookup_source_by_domain(domains)
                if domains:
                    telegraph_domain = domains[0]
                if domain_match:
                    telegraph_source = domain_match[0]
                    log.info(f"  Telegraph source from bottom attribution link ({href[:60]}): {telegraph_source}")
                    break
                # Also try the link text itself
                if link_text and len(link_text) < 40:
                    cleaned = _clean_source_name(link_text)
                    if cleaned and len(cleaned) >= 2:
                        telegraph_source = cleaned
                        log.info(f"  Telegraph source from link text: {telegraph_source}")
                        break

        # ── Clean up non-content elements ──
        for p in article.find_all("p"):
            text = p.get_text(strip=True)
            if text.startswith("Generated by"):
                p.decompose()
            elif p.find("a", href=True):
                href = p.a["href"]
                if ("gelonghui.com" in href or "zaobao.com" in href) and len(text) < 30:
                    p.decompose()

        h1 = article.find("h1")
        if h1:
            h1.decompose()
        address = article.find("address")
        if address:
            address.decompose()

        # Fix relative URLs in iframe, img, a, video, source tags
        # Telegraph uses relative /embed/ paths that break on external sites
        base_tg = "https://telegra.ph"
        for tag in article.find_all(["iframe", "img", "a", "video", "source"]):
            for attr in ["src", "href"]:
                val = tag.get(attr, "")
                if val and val.startswith("/"):
                    tag[attr] = urljoin(base_tg, val)
        body_html = str(article)
        images = [img.get("src", "") for img in article.find_all("img") if img.get("src")]

        result = {
            "body_html": body_html,
            "images": images,
            "char_count": len(article.get_text()),
        }
        if telegraph_source:
            result["detected_source"] = telegraph_source
        if telegraph_domain:
            result["detected_domain"] = telegraph_domain
        return result
    except Exception as e:
        log.error(f"  Telegraph fetch failed: {e}")
        return None
    finally:
        if resp is not None:
            resp.close()


# ─── WeChat Article Fetching ──────────────────────────────
def fetch_wechat_article(url: str) -> dict | None:
    """Fetch full content from a WeChat article (mp.weixin.qq.com)."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
        "Referer": "https://mp.weixin.qq.com/",
    }
    resp = None
    try:
        log.info(f"  Fetching WeChat: {url[:80]}...")
        resp = safe_get(
            url,
            headers=headers,
            timeout=WECHAT_FULLTEXT_TIMEOUT,
            stream=True,
        )
        resp.raise_for_status()
        body = _read_body_with_limit(resp, FULLTEXT_BODY_LIMIT)

        soup = BeautifulSoup(body, "html.parser")

        # Main content container in WeChat articles
        content_div = (
            soup.select_one("#js_content")
            or soup.select_one("#rich_media_content")
        )
        if not content_div:
            log.warning(f"  WeChat: no content div found")
            return None

        # Fix lazy-loaded images: data-src → src, make absolute
        for img in content_div.find_all("img"):
            data_src = img.get("data-src", "")
            src = img.get("src", "")
            if data_src:
                final_src = data_src
                if final_src.startswith("//"):
                    final_src = "https:" + final_src
                img["src"] = final_src
            elif src:
                if src.startswith("//"):
                    img["src"] = "https:" + src
                elif src.startswith("/"):
                    img["src"] = "https://mp.weixin.qq.com" + src

        # Clean up WeChat-specific styling that hides content
        if content_div.get("style"):
            style = content_div["style"]
            style = re.sub(r'visibility\s*:\s*hidden\s*;?\s*', '', style, flags=re.IGNORECASE)
            style = re.sub(r'opacity\s*:\s*0\s*;?\s*', '', style, flags=re.IGNORECASE)
            style = style.strip().rstrip(";")
            if style:
                content_div["style"] = style
            else:
                del content_div["style"]

        # Strip inline styles from all child elements so page CSS handles formatting
        for tag in content_div.find_all(True):
            if tag.name == "img":
                # Keep only width/height/display styles for images
                style = tag.get("style", "")
                if style:
                    keep = {}
                    for kv in style.split(";"):
                        kv = kv.strip()
                        if ":" in kv:
                            k, v = kv.split(":", 1)
                            k = k.strip().lower()
                            if k in ("width", "height", "display", "max-width"):
                                keep[k] = v.strip()
                    if keep:
                        tag["style"] = "; ".join(f"{k}: {v}" for k, v in keep.items())
                    else:
                        del tag["style"]
            else:
                tag.attrs.pop("style", None)

        body_html = str(content_div)
        images = [
            img.get("src", "") for img in content_div.find_all("img") if img.get("src")
        ]
        text_content = content_div.get_text(strip=True)

        log.info(f"  WeChat: {len(text_content)} chars, {len(images)} images")
        return {
            "body_html": body_html,
            "images": images,
            "char_count": len(text_content),
        }
    except Exception as e:
        log.error(f"  WeChat fetch failed: {e}")
        return None
    finally:
        if resp is not None:
            resp.close()


# ─── Main Pipeline ────────────────────────────────────────
def process_message(msg: dict, orig_msg_id: int) -> dict:
    """Process a single Telegram message into a news entry."""
    content = msg["html"]
    text = msg["text"]
    title = extract_title(text)
    telegraph_url = extract_telegraph_url(content)
    feed_source = detect_feed_source(channel=msg.get("feed_source", ""))
    link_preview_url = msg.get("link_preview_url", "") or ""
    group_source, publisher_domain = detect_group_source(content, link_preview_url, feed_source)
    origin_source = group_source
    thumb = msg["images"][0] if msg["images"] else ""
    time_info = parse_datetime(msg["datetime"])

    entry = {
        "id": orig_msg_id,  # Use stable Telegram message ID
        "title": title,
        "source": group_source,
        "feed_source": feed_source,
        "group_source": group_source,
        "publisher_domain": publisher_domain,
        "source_detection_version": 1,
        "origin_source": origin_source,
        "time": time_info.get("time", ""),
        "date": time_info.get("date", ""),
        "timestamp": time_info.get("timestamp", 0),
        "thumb": thumb,
        "has_full_content": False,
        "telegraph_url": telegraph_url or "",
        "body_html": "",
        # Keep the Telegram footer available when full text replaces the body.
        "source_html": content,
        "summary": "",
    }

    if telegraph_url:
        result = fetch_telegraph(telegraph_url)
        if result:
            entry["has_full_content"] = True
            entry["body_html"] = result["body_html"]
            entry["thumb"] = result["images"][0] if result["images"] and not thumb else thumb
            # Telegraph articles have no "via" line; use the source detected from
            # Telegraph metadata (<address>, attribution links) when the initial
            # detection is weak (plain "未分类" or just an @channel name).
            ts = result.get("detected_source", "")
            if ts:
                domain = result.get("detected_domain", "")
                if domain:
                    publisher_domain = domain
                    match = lookup_source_by_domain([domain])
                    group_source = match[0] if match else domain
                else:
                    group_source = ts
                origin_source = group_source
                entry["source"] = group_source
                entry["group_source"] = group_source
                entry["publisher_domain"] = publisher_domain
                entry["origin_source"] = origin_source
                log.info(f"  ✓ {title[:40]}... ({result['char_count']} chars, from Telegraph, origin → {origin_source})")
            else:
                log.info(f"  ✓ {title[:40]}... ({result['char_count']} chars, from Telegraph)")
        else:
            # Fallback to Telegram message content
            videos_html = "".join(msg.get("videos", []))
            entry["body_html"] = videos_html + content
            log.info(f"  ~ {title[:40]}... (Telegraph failed, using Telegram fallback)")
    else:
        videos_html = "".join(msg.get("videos", []))
        entry["body_html"] = videos_html + content
        plain = re.sub(r"<[^>]+>", " ", text).strip()
        entry["summary"] = plain[:250]
        log.info(f"  - {title[:40]}... (from Telegram)")

    # If body is too short (<64 chars) and original message has a WeChat URL,
    # try fetching full article content from mp.weixin.qq.com
    plain_body = re.sub(r"<[^>]+>", " ", entry["body_html"]).strip()
    if len(plain_body) < 64:
        wechat_url = extract_wechat_url(content)
        if wechat_url:
            log.info(f"  Short content ({len(plain_body)} chars), fetching WeChat: {wechat_url[:60]}...")
            wechat_result = fetch_wechat_article(wechat_url)
            if wechat_result:
                # Only append the "via <source>" link from the original content,
                # not the full original message (which repeats the title)
                via_matches = list(re.finditer(r'via\s*<a[^>]*>.*?</a>', content, re.DOTALL | re.IGNORECASE))
                via_suffix = via_matches[-1].group(0) if via_matches else ""
                entry["has_full_content"] = True
                entry["body_html"] = wechat_result["body_html"] + "\n" + via_suffix
                entry["thumb"] = wechat_result["images"][0] if wechat_result["images"] and not thumb else thumb
                log.info(f"  ✓ {title[:40]}... ({wechat_result['char_count']} chars, from WeChat)")
            else:
                log.warning(f"  ✗ WeChat fetch failed, keeping original content")

    return entry


def is_from_today(datetime_str: str) -> bool:
    """Check if a message datetime is from today (Beijing time)."""
    if not datetime_str:
        return False
    try:
        dt = datetime.fromisoformat(datetime_str.replace("Z", "+00:00"))
        dt_cst = dt.astimezone(CST)
        now_cst = datetime.now(CST)
        return dt_cst.date() == now_cst.date()
    except Exception:
        return True  # If we can't parse, be inclusive


def fetch_all_new_messages(state: dict) -> tuple[list[dict], int]:
    """Fetch new messages since last seen ID; keep first-run history bounded.

    Routine incremental runs retain late arrivals from previous dates for the
    next digest. A first run without a cursor only imports today's history.

    Returns (messages, highest_observed_id). The latter includes every ID newer
    than the cursor, including history skipped during a first-run bootstrap.
    """
    last_id = state.get("last_seen_id", 0)
    all_msgs: list[dict] = []
    highest_observed_id = last_id
    before = ""
    pages_fetched = 0

    log.info(f"Last seen message ID: {last_id}")

    while pages_fetched < MAX_HISTORY_PAGES:
        html = fetch_telegram_page(before)
        if not html:
            break

        msgs = parse_messages(html)
        if not msgs:
            log.info("  No messages on this page")
            break

        pages_fetched += 1

        new_ids = [m["id"] for m in msgs if m["id"] > last_id]
        if new_ids:
            highest_observed_id = max(highest_observed_id, max(new_ids))

        # First bootstrap stays today-only; incremental runs retain all new IDs.
        seen_ids = {m["id"] for m in all_msgs}
        new_messages = [
            m for m in msgs
            if m["id"] > last_id and m["id"] not in seen_ids
            and (last_id > 0 or is_from_today(m["datetime"]))
        ]
        if new_messages:
            log.info(f"  Page {pages_fetched}: {len(new_messages)} new msgs (IDs {new_messages[0]['id']}~{new_messages[-1]['id']})")
            all_msgs.extend(new_messages)
        else:
            log.info(f"  Page {pages_fetched}: no eligible new messages")

        # Stop condition 1: every message on this page is already at/behind the
        # incremental cursor — we've caught up, no need to page further back.
        if all(m["id"] <= last_id for m in msgs):
            log.info("  Caught up — stopping")
            break

        # On first bootstrap, stop when the newest message predates today.
        if last_id == 0 and not is_from_today(msgs[-1]["datetime"]):
            log.info("  Newest message on page is from before today — stopping")
            break

        # Paginate for next page — always use OLDEST message ID
        # Telegram before=X returns messages with ID < X, so using the oldest
        # message on the current page avoids overlap with the next page
        anchor = msgs[0]["id"]  # oldest on this page
        log.info(f"  Next page before={anchor} (oldest on current page)")
        if anchor is None or anchor <= 1:
            break
        before = str(anchor)

        if len(msgs) < 3:
            log.info("  Page has < 3 messages, likely end of channel")
            break

    log.info(f"Fetched {pages_fetched} pages, {len(all_msgs)} messages total")
    return all_msgs, highest_observed_id


def run():
    log.info("=" * 50)
    log.info("Starting fetch cycle")
    cycle_started_at = time.monotonic()

    state = load_state()
    try:
        conn = init_db()
        migrate_news_json(conn)
        article_count = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
        conn.close()
        if article_count == 0 and state.get("last_seen_id", 0) > 0:
            log.warning(
                "SQLite articles table is empty but fetcher_state has "
                f"last_seen_id={state.get('last_seen_id')}; forcing bootstrap fetch"
            )
            state["last_seen_id"] = 0
    except Exception as e:
        log.error(f"SQLite bootstrap check failed: {e}")

    telegram_fetch_started_at = time.monotonic()
    messages, highest_observed_id = fetch_all_new_messages(state)
    log.info(f"[timing] Telegram fetch: {time.monotonic() - telegram_fetch_started_at:.2f}s")

    if not messages:
        if highest_observed_id > state.get("last_seen_id", 0):
            # First-run bootstrap may have skipped older messages. Advance the
            # cursor so this history is not scanned again on the next run.
            state["last_seen_id"] = highest_observed_id
            save_state(state)
            log.info(
                f"Only out-of-scope (pre-today) backlog found — advancing "
                f"last_seen_id to {highest_observed_id} without processing any articles"
            )
        else:
            log.info("No new messages — keeping existing news.json")
        # Still ensure SQLite is initialized from existing data — and run full-text
        # backfill here too. A cycle with no new messages is exactly when a previously
        # failed Telegraph fetch would otherwise never get retried (the main path below
        # is skipped), letting it age past the backfill window and stay downgraded.
        try:
            conn = init_db()
            migrate_news_json(conn)
            try:
                backfill_missing_fulltext(conn)
            except Exception as e:
                log.error(f"Full-text backfill failed: {e}")
            conn.close()
        except Exception as e:
            log.error(f"SQLite init failed: {e}")
        log.info(f"[timing] Fetch cycle total: {time.monotonic() - cycle_started_at:.2f}s (no new messages)")
        return

    # Reset progress for this cycle before streaming starts, so a stale value from a
    # previous run/crash is never mistaken for current progress (see
    # write_fetch_progress() / refresh_server.py's started_at guard).
    write_fetch_progress(0, len(messages), [])

    # Process new messages with thread pool, streaming completed articles into SQLite
    # in small batches as they finish (rather than one big upsert at the end) so
    # readers can see new articles well before the whole cycle — including every
    # full-text fetch — has finished.
    failed_count = 0
    stream_conn = None
    stream_batch = []
    failed_batches: list[list[dict]] = []
    executor = None
    futures: dict = {}
    inserted_total = 0
    inserted_ids: dict[int, None] = {}
    last_commit_at = time.monotonic()
    fulltext_started_at = time.monotonic()
    try:
        stream_conn = init_db()
        stream_conn.execute("PRAGMA busy_timeout=30000")
        executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)
        futures = {
            executor.submit(process_message, msg, msg["id"]): int(msg["id"])
            for msg in messages
        }
        # Provider work is owned by the futures; reduce to id-only so the raw
        # html/text/images blobs are released before the full-text/backfill window.
        messages = [m["id"] for m in messages]
        for future in as_completed(futures):
            msg_id = futures.pop(future)
            try:
                entry = future.result()
                stream_batch.append(entry)
            except Exception as e:
                failed_count += 1
                log.error(f"Message processing failed (ID={msg_id}): {e}")
            finally:
                # A completed Future owns its result. Removing both the mapping
                # entry and loop-local Future lets a committed batch's full body
                # become unreachable as soon as the batch list is cleared.
                entry = None
                future = None
                msg_id = None
            if stream_batch and (
                len(stream_batch) >= STREAM_BATCH_SIZE
                or time.monotonic() - last_commit_at >= STREAM_BATCH_SECONDS
            ):
                try:
                    inserted_total += _commit_stream_batch(
                        stream_conn,
                        stream_batch,
                        inserted_ids,
                        len(messages),
                    )
                except Exception as e:
                    _rollback_best_effort(stream_conn)
                    failed_batches.append(stream_batch)
                    stream_batch = []
                    log.error(f"Streaming SQLite batch ingest failed: {e}")
                last_commit_at = time.monotonic()
        if stream_batch:
            try:
                inserted_total += _commit_stream_batch(
                    stream_conn,
                    stream_batch,
                    inserted_ids,
                    len(messages),
                )
            except Exception as e:
                _rollback_best_effort(stream_conn)
                failed_batches.append(stream_batch)
                stream_batch = []
                log.error(f"Streaming SQLite trailing batch ingest failed: {e}")

        # Retry only batches that did not commit. Provider work is complete and is
        # never re-run; already committed batches were cleared and are absent here.
        for failed_batch in failed_batches:
            try:
                inserted_total += _commit_stream_batch(
                    stream_conn,
                    failed_batch,
                    inserted_ids,
                    len(messages),
                )
            except Exception as e:
                _rollback_best_effort(stream_conn)
                failed_count += len(failed_batch)
                log.error(f"Streaming SQLite batch retry failed: {e}")
                failed_batch.clear()
            finally:
                del failed_batch
        failed_batches.clear()
    except Exception as e:
        failed_count = max(failed_count, len(messages) - inserted_total)
        log.error(f"Streaming SQLite ingest failed: {e}")
    finally:
        try:
            _shutdown_stream_executor(executor, futures)
        finally:
            _clear_stream_payloads(stream_batch, failed_batches)
            entry = None
            future = None
            msg_id = None
            if stream_conn:
                stream_conn.close()
    log.info(
        f"[timing] Full-text fetch + streaming ingest: "
        f"{time.monotonic() - fulltext_started_at:.2f}s ({len(messages)} messages, "
        f"{inserted_total} inserted, {failed_count} failed)"
    )

    # Update state with latest message ID
    # Only advance last_seen_id if ALL messages processed successfully,
    # otherwise failed messages would be permanently skipped on next run.
    if failed_count:
        log.warning(
            f"{failed_count} message(s) failed — keeping last_seen_id={state.get('last_seen_id')} "
            "so they can be retried on next fetch"
        )
    else:
        # highest_observed_id already covers every id seen this cycle (including
        # bootstrap messages outside today); max() with kept messages is defensive in case
        # a future change ever decouples the two. messages is id-only here (reduced
        # after the futures were submitted).
        max_id = max(highest_observed_id, max(messages))
        state["last_seen_id"] = max(state.get("last_seen_id", 0), max_id)
        save_state(state)
        log.info(f"Updated state: last_seen_id = {state['last_seen_id']}")

    # ── SQLite sync ── the load-bearing step: source-category bookkeeping that
    # GET /sources and the drawer rely on. Runs before the news.json mirror below
    # so a slow/failed news.json write can never delay or block it.
    sqlite_sync_started_at = time.monotonic()
    mirror_entries = None
    try:
        conn = init_db()
        migrate_news_json(conn)
        # Streaming batches always skip the expensive full-table source sync. Run
        # that bookkeeping exactly once after both initial commits and batch retries.
        ensure_article_sources(conn)
        conn.commit()
        # Retry any recent articles whose Telegraph full text failed on arrival, so a
        # transient timeout/5xx doesn't leave them permanently downgraded to the excerpt
        # (see backfill_missing_fulltext()). Isolated in its own try so a backfill hiccup
        # can never undo the sync above.
        try:
            backfill_missing_fulltext(conn)
        except Exception as e:
            log.error(f"Full-text backfill failed: {e}")
        mirror_entries = _load_recent_articles_for_mirror(
            conn,
            limit=NEWS_JSON_MIRROR_LIMIT,
        )
        conn.close()
    except Exception as e:
        log.error(f"SQLite write failed: {e}")
    log.info(f"[timing] SQLite sync: {time.monotonic() - sqlite_sync_started_at:.2f}s")

    # ── news.json mirror ── best-effort only: legacy bootstrap source for
    # migrate_news_json() when SQLite is empty, not read by anything else. A
    # failure here is logged, not raised, so it can never fail the fetch cycle —
    # articles are already durably in SQLite by this point regardless.
    news_json_started_at = time.monotonic()
    try:
        if mirror_entries is not None:
            write_news_json_mirror(mirror_entries)
    except Exception as e:
        log.error(f"news.json write failed: {e}")
    finally:
        if mirror_entries is not None:
            mirror_entries.clear()
    log.info(f"[timing] news.json mirror write: {time.monotonic() - news_json_started_at:.2f}s")

    log.info(f"[timing] Fetch cycle total: {time.monotonic() - cycle_started_at:.2f}s")
    log.info("Fetch cycle complete")


# Most recent items kept in news.json — it's only ever read back by
# migrate_news_json() to bootstrap SQLite when the DB is empty, so it doesn't need
# the full unbounded history it used to accumulate.
NEWS_JSON_MIRROR_LIMIT = 2000


def _load_recent_articles_for_mirror(
    conn: sqlite3.Connection,
    limit: int = NEWS_JSON_MIRROR_LIMIT,
) -> list[dict]:
    """Load a stable, bounded recent-article snapshot for the legacy JSON mirror."""
    rows = conn.execute(
        """SELECT id, title, source, feed_source, origin_source, time, date,
                  timestamp, ingested_at, thumb, has_full_content, telegraph_url, body_html,
                  original_body_html, summary, original_title, title_updated_at,
                  title_source
             FROM articles
            ORDER BY timestamp DESC, id DESC
            LIMIT ?""",
        (max(0, int(limit)),),
    ).fetchall()
    entries = [dict(row) for row in rows]
    for entry in entries:
        entry["has_full_content"] = bool(entry["has_full_content"])
    return entries


def write_news_json_mirror(recent_entries: list[dict]) -> None:
    """Best-effort mirror of the most recent articles to news.json.

    Not on the critical path: only read back by migrate_news_json() to bootstrap
    SQLite when the DB is empty. Raises on failure (caller logs and moves on) —
    articles are already durably in SQLite by the time this runs regardless.
    """
    # The caller loads this bounded snapshot from SQLite after all writes/backfill.
    # Do not merge the previous JSON contents: SQLite is authoritative and merging
    # would retain stale or deleted articles outside its recent window.
    all_items = list(recent_entries[:NEWS_JSON_MIRROR_LIMIT])

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    output = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "count": len(all_items),
        "items": all_items,
    }
    # Atomic write: write to temp file, then rename. No indent — this file is
    # machine-read (migrate_news_json), never hand-inspected.
    tmp = OUTPUT_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(output, ensure_ascii=False), encoding="utf-8")
    tmp.replace(OUTPUT_FILE)
    log.info(f"Wrote {len(all_items)} SQLite entries to {OUTPUT_FILE}")


if __name__ == "__main__":
    run()
