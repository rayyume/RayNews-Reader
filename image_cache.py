"""Persistent image cache for RayNews article images."""

from __future__ import annotations

import hashlib
import fcntl
import tempfile
from contextlib import contextmanager
from functools import wraps
import html
import logging
import os
import queue
import re
import sqlite3
import threading
import time
import urllib.parse
from pathlib import Path

from image_validation import detect_image_content_type
from network_safety import UnsafeUrlError, safe_get


log = logging.getLogger("image_cache")
DATA_DIR = Path(os.environ.get("DATA_DIR", "/app/data"))
CACHE_DIR = DATA_DIR / "image_cache"
DB_FILE = CACHE_DIR / "cache.db"

IMAGE_CACHE_ENABLED = os.environ.get("IMAGE_CACHE_ENABLED", "true").lower() not in {
    "0", "false", "no", "off"
}
IMAGE_CACHE_MAX_MB = int(os.environ.get("IMAGE_CACHE_MAX_MB", "5120"))
IMAGE_CACHE_MAX_FILE_MB = int(os.environ.get("IMAGE_CACHE_MAX_FILE_MB", "10"))
IMAGE_CACHE_PREFETCH_BODY_LIMIT = int(os.environ.get("IMAGE_CACHE_PREFETCH_BODY_LIMIT", "3"))
IMAGE_CACHE_PREFETCH_WORKERS = max(1, int(os.environ.get("IMAGE_CACHE_PREFETCH_WORKERS", "2")))
IMAGE_CACHE_PREFETCH_QUEUE_SIZE = max(100, int(os.environ.get("IMAGE_CACHE_PREFETCH_QUEUE_SIZE", "3000")))

MAX_CACHE_BYTES = max(1, IMAGE_CACHE_MAX_MB) * 1024 * 1024
MAX_FILE_BYTES = max(1, IMAGE_CACHE_MAX_FILE_MB) * 1024 * 1024

ALLOWED_IMAGE_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}

_prefetch_queue: queue.Queue[tuple[int, str, bool, bool]] = queue.Queue(maxsize=IMAGE_CACHE_PREFETCH_QUEUE_SIZE)
_prefetch_pending: set[str] = set()
_prefetch_lock = threading.Lock()
_prefetch_started = False
_cache_init_lock = threading.Lock()
_initialized_cache_paths: set[str] = set()


_cache_metadata_lock = threading.RLock()
_cache_lock_local = threading.local()


@contextmanager
def _disk_lock(name, *, metadata=False):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if metadata:
        with _cache_metadata_lock:
            if getattr(_cache_lock_local, 'depth', 0):
                _cache_lock_local.depth += 1
                try:
                    yield
                finally:
                    _cache_lock_local.depth -= 1
                return
            with (CACHE_DIR / '.metadata.lock').open('a+b') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                _cache_lock_local.depth = 1
                try:
                    yield
                finally:
                    _cache_lock_local.depth = 0
    else:
        with (CACHE_DIR / name).open('a+b') as lock:
            deadline = time.monotonic() + 30
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError('image download coordination timeout')
                    time.sleep(0.02)
            yield


def _metadata_mutation(function):
    @wraps(function)
    def guarded(*args, **kwargs):
        if not IMAGE_CACHE_ENABLED:
            return function(*args, **kwargs)
        with _disk_lock('metadata', metadata=True):
            return function(*args, **kwargs)
    return guarded


def open_cached_image(url):
    """Return an owned binary handle; unlink after open cannot break a reader."""
    with _disk_lock('metadata', metadata=True):
        cached = get_cached_image(url)
        if not cached:
            return None
        path, content_type = cached
        try:
            return path.open('rb'), content_type
        except FileNotFoundError:
            # An external cache cleaner may not participate in our lock.
            return None


def init_cache() -> None:
    with _cache_init_lock:
        cache_key = str(DB_FILE.resolve())
        if cache_key in _initialized_cache_paths and DB_FILE.exists():
            return

        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(DB_FILE, timeout=30)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS image_cache_entries (
                    url_hash     TEXT PRIMARY KEY,
                    url          TEXT NOT NULL UNIQUE,
                    content_type TEXT NOT NULL,
                    size_bytes   INTEGER NOT NULL DEFAULT 0,
                    path         TEXT NOT NULL,
                    pinned       INTEGER NOT NULL DEFAULT 0,
                    is_cover     INTEGER NOT NULL DEFAULT 0,
                    created_at   INTEGER NOT NULL,
                    accessed_at  INTEGER NOT NULL,
                    hit_count    INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_image_cache_cleanup
                    ON image_cache_entries(pinned, is_cover, accessed_at);
                CREATE TABLE IF NOT EXISTS image_cache_article_images (
                    article_id INTEGER NOT NULL,
                    url_hash   TEXT NOT NULL,
                    PRIMARY KEY(article_id, url_hash)
                );
                """
            )
            conn.commit()
        except Exception:
            raise
        else:
            _initialized_cache_paths.add(cache_key)
        finally:
            conn.close()


def _connect() -> sqlite3.Connection:
    init_cache()
    conn = sqlite3.connect(DB_FILE, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def open_cache_connection() -> sqlite3.Connection:
    """Return a cache connection for a caller performing a batch operation."""
    return _connect()


def normalize_image_url(url: str) -> str:
    url = html.unescape((url or "").strip())
    if url.startswith("//"):
        url = "https:" + url
    return url


def _url_hash(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def _cache_path(url_hash: str, content_type: str) -> Path:
    ext = ALLOWED_IMAGE_TYPES.get(content_type.split(";")[0].strip().lower(), ".img")
    return CACHE_DIR / url_hash[:2] / f"{url_hash}{ext}"


def _valid_remote_url(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def _remote_image_candidates(url: str) -> list[str]:
    parsed = urllib.parse.urlparse(url)
    candidates = [url]
    hostname = (parsed.hostname or "").lower()
    if hostname == "wsrv.nl":
        inner_values = urllib.parse.parse_qs(parsed.query).get("url") or []
        inner_url = normalize_image_url(inner_values[0]) if inner_values else ""
        if _valid_remote_url(inner_url):
            inner_parsed = urllib.parse.urlparse(inner_url)
            if (inner_parsed.hostname or "").lower() == "cdnfile.sspai.com":
                rss_parsed = inner_parsed._replace(netloc="rssfile.sspai.com")
                candidates.append(urllib.parse.urlunparse(rss_parsed))
            candidates.append(inner_url)
    elif hostname:
        candidates.append("https://wsrv.nl/?url=" + urllib.parse.quote(url, safe=""))
    return list(dict.fromkeys(candidates))


def fetch_remote_image(url: str) -> tuple[bytes, str]:
    url = normalize_image_url(url)
    if not _valid_remote_url(url):
        raise ValueError("invalid URL")
    last_error: Exception | None = None
    for candidate in _remote_image_candidates(url):
        resp = None
        parsed = urllib.parse.urlparse(candidate)
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": f"{parsed.scheme}://{parsed.netloc}/",
        }
        try:
            resp = safe_get(candidate, headers=headers, timeout=15, stream=True)
            resp.raise_for_status()

            chunks: list[bytes] = []
            total = 0
            for chunk in resp.iter_content(chunk_size=65536):
                if not chunk:
                    continue
                total += len(chunk)
                if total > MAX_FILE_BYTES:
                    raise ValueError("image too large")
                chunks.append(chunk)
            body = b"".join(chunks)
            if not body:
                raise ValueError("empty image")
            content_type = detect_image_content_type(body)
            if content_type is None:
                raise ValueError("unsupported image content")
            return body, content_type
        except UnsafeUrlError:
            raise
        except Exception as exc:
            last_error = exc
            continue
        finally:
            if resp is not None:
                resp.close()
    raise last_error or ValueError("image fetch failed")


def collect_image_urls(body_html: str | None, thumb: str | None = "", body_limit: int | None = None) -> list[tuple[str, bool]]:
    """Return unique (url, is_cover) pairs from thumb and body HTML."""
    result: list[tuple[str, bool]] = []
    seen: set[str] = set()

    def add(url: str, is_cover: bool) -> None:
        normalized = normalize_image_url(url)
        if not normalized or normalized in seen or not _valid_remote_url(normalized):
            return
        seen.add(normalized)
        result.append((normalized, is_cover))

    add(thumb or "", True)
    matches = re.findall(r"<img\b[^>]*\bsrc\s*=\s*['\"]([^'\"]+)['\"]", body_html or "", flags=re.I)
    if body_limit is not None:
        matches = matches[:max(0, body_limit)]
    for url in matches:
        add(url, False)
    return result


def get_cached_image(url: str) -> tuple[Path, str] | None:
    if not IMAGE_CACHE_ENABLED:
        return None
    url = normalize_image_url(url)
    if not _valid_remote_url(url):
        return None
    url_hash = _url_hash(url)
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT path, content_type FROM image_cache_entries WHERE url_hash = ?",
            (url_hash,),
        ).fetchone()
        if not row:
            return None
        path = CACHE_DIR / row["path"]
        if not path.exists():
            return None
        conn.execute(
            "UPDATE image_cache_entries SET accessed_at = ?, hit_count = hit_count + 1 WHERE url_hash = ?",
            (int(time.time()), url_hash),
        )
        conn.commit()
        return path, row["content_type"]
    finally:
        conn.close()


def cache_image(url: str, *, is_cover: bool = False, pinned: bool = False, _reader=False):
    if not IMAGE_CACHE_ENABLED:
        return None
    normalized = normalize_image_url(url)
    # A fixed set of advisory locks also coordinates web/favorite processes.
    stripe = int(_url_hash(normalized)[:4], 16) % 256
    with _disk_lock(f'.download-{stripe:02x}.lock'):
        return _cache_image_locked(normalized, is_cover=is_cover, pinned=pinned, _reader=_reader)


def _cache_image_locked(url, *, is_cover=False, pinned=False, _reader=False):
    if not IMAGE_CACHE_ENABLED:
        return None
    url = normalize_image_url(url)
    if not _valid_remote_url(url):
        return None
    cached = open_cached_image(url) if _reader else get_cached_image(url)
    if cached:
        if pinned or is_cover:
            _update_flags(url, pinned=pinned, is_cover=is_cover)
        return cached

    body, content_type = fetch_remote_image(url)

    url_hash = _url_hash(url)
    path = _cache_path(url_hash, content_type)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _disk_lock('metadata', metadata=True):
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.image-', delete=False) as output:
                temporary = Path(output.name)
                output.write(body)
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        rel_path = str(path.relative_to(CACHE_DIR)).replace("\\", "/")
        now = int(time.time())

        conn = _connect()
        try:
            conn.execute(
                """
                INSERT INTO image_cache_entries
                    (url_hash, url, content_type, size_bytes, path, pinned, is_cover, created_at, accessed_at, hit_count)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(url_hash) DO UPDATE SET
                    content_type = excluded.content_type,
                    size_bytes = excluded.size_bytes,
                    path = excluded.path,
                    pinned = CASE WHEN excluded.pinned = 1 THEN 1 ELSE image_cache_entries.pinned END,
                    is_cover = CASE WHEN excluded.is_cover = 1 THEN 1 ELSE image_cache_entries.is_cover END,
                    accessed_at = excluded.accessed_at,
                    hit_count = image_cache_entries.hit_count + 1
                """,
                (url_hash, url, content_type, len(body), rel_path, 1 if pinned else 0, 1 if is_cover else 0, now, now),
            )
            conn.commit()
        finally:
            conn.close()

        # Acquire the reader before pruning, including when this file alone
        # exceeds the disk quota. The descriptor owns the unlinked inode.
        reader = path.open('rb') if _reader else None
        try:
            prune_cache()
            return (reader if _reader else path), content_type
        except BaseException:
            if reader is not None:
                reader.close()
            raise


@_metadata_mutation
def _update_flags(url: str, *, pinned: bool = False, is_cover: bool = False) -> None:
    url = normalize_image_url(url)
    url_hash = _url_hash(url)
    conn = _connect()
    try:
        conn.execute(
            """
            UPDATE image_cache_entries
            SET pinned = CASE WHEN ? = 1 THEN 1 ELSE pinned END,
                is_cover = CASE WHEN ? = 1 THEN 1 ELSE is_cover END,
                accessed_at = ?
            WHERE url_hash = ?
            """,
            (1 if pinned else 0, 1 if is_cover else 0, int(time.time()), url_hash),
        )
        conn.commit()
    finally:
        conn.close()


def pin_article_images(article_id: int, body_html: str | None, thumb: str | None = "") -> int:
    count = 0
    for url, is_cover in collect_image_urls(body_html, thumb, body_limit=None):
        try:
            cached = cache_image(url, is_cover=is_cover, pinned=True)
            if not cached:
                continue
            url_hash = _url_hash(url)
            conn = _connect()
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO image_cache_article_images (article_id, url_hash) VALUES (?, ?)",
                    (article_id, url_hash),
                )
                conn.execute(
                    "UPDATE image_cache_entries SET pinned = 1 WHERE url_hash = ?",
                    (url_hash,),
                )
                conn.commit()
            finally:
                conn.close()
            count += 1
        except Exception:
            continue
    return count


def unpin_article_images(article_ids: int | list[int] | tuple[int, ...]) -> None:
    """Remove one batch of article mappings and recompute pins once.

    Callers must pass all ids from a deletion batch.  This avoids competing
    full-table pin recalculations from one daemon thread per article.
    """
    ids = [article_ids] if isinstance(article_ids, int) else sorted({int(i) for i in article_ids})
    if not ids:
        return
    for attempt in range(4):
        conn = _connect()
        try:
            conn.execute("PRAGMA busy_timeout = 5000")
            placeholders = ",".join("?" * len(ids))
            conn.execute(f"DELETE FROM image_cache_article_images WHERE article_id IN ({placeholders})", ids)
            conn.execute(
                """
                UPDATE image_cache_entries
                SET pinned = CASE
                    WHEN EXISTS (
                        SELECT 1 FROM image_cache_article_images m
                        WHERE m.url_hash = image_cache_entries.url_hash
                    ) THEN 1 ELSE 0 END
                """
            )
            conn.commit()
            break
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() and "busy" not in str(exc).lower() or attempt == 3:
                raise
            time.sleep(0.1 * (2 ** attempt))
        finally:
            conn.close()
    prune_cache()


def _ensure_prefetch_workers() -> None:
    global _prefetch_started
    if _prefetch_started or not IMAGE_CACHE_ENABLED:
        return
    with _prefetch_lock:
        if _prefetch_started:
            return
        for idx in range(IMAGE_CACHE_PREFETCH_WORKERS):
            thread = threading.Thread(
                target=_prefetch_worker,
                name=f"image-cache-prefetch-{idx + 1}",
                daemon=True,
            )
            thread.start()
        _prefetch_started = True


def _prefetch_worker() -> None:
    while True:
        article_id, url, is_cover, pinned = _prefetch_queue.get()
        url_hash = _url_hash(normalize_image_url(url))
        pending_key = f"{url_hash}:{1 if pinned else 0}"
        try:
            cached = cache_image(url, is_cover=is_cover, pinned=pinned)
            if cached and pinned:
                conn = _connect()
                try:
                    conn.execute(
                        "INSERT OR IGNORE INTO image_cache_article_images (article_id, url_hash) VALUES (?, ?)",
                        (article_id, url_hash),
                    )
                    conn.execute(
                        "UPDATE image_cache_entries SET pinned = 1 WHERE url_hash = ?",
                        (url_hash,),
                    )
                    conn.commit()
                finally:
                    conn.close()
        except Exception as exc:
            log.warning("Image prefetch failed for article=%s url=%s: %s", article_id, url[:120], exc)
        finally:
            with _prefetch_lock:
                _prefetch_pending.discard(pending_key)
            _prefetch_queue.task_done()


def enqueue_article_image_prefetch(
    article_id: int,
    body_html: str | None,
    thumb: str | None = "",
    *,
    body_limit: int | None = None,
    pinned: bool = False,
) -> int:
    """Queue article images for background caching and return queued count."""
    if not IMAGE_CACHE_ENABLED:
        return 0
    if body_limit is None and not pinned:
        body_limit = IMAGE_CACHE_PREFETCH_BODY_LIMIT
    elif pinned:
        body_limit = None
    _ensure_prefetch_workers()
    queued = 0
    for url, is_cover in collect_image_urls(body_html, thumb, body_limit=body_limit):
        url_hash = _url_hash(url)
        pending_key = f"{url_hash}:{1 if pinned else 0}"
        with _prefetch_lock:
            if not pinned and get_cached_image(url):
                continue
            if pending_key in _prefetch_pending:
                continue
            _prefetch_pending.add(pending_key)
            try:
                _prefetch_queue.put_nowait((article_id, url, is_cover, pinned))
            except queue.Full:
                _prefetch_pending.discard(pending_key)
                continue
            queued += 1
    return queued


@_metadata_mutation
def prune_cache() -> int:
    if not IMAGE_CACHE_ENABLED:
        return 0
    conn = _connect()
    deleted = 0
    try:
        total = conn.execute("SELECT COALESCE(SUM(size_bytes), 0) FROM image_cache_entries").fetchone()[0]
        if total <= MAX_CACHE_BYTES:
            return 0
        rows = conn.execute(
            """
            SELECT url_hash, path, size_bytes
            FROM image_cache_entries
            WHERE pinned = 0
            ORDER BY is_cover ASC, accessed_at ASC, created_at ASC
            """
        ).fetchall()
        for row in rows:
            if total <= MAX_CACHE_BYTES:
                break
            path = CACHE_DIR / row["path"]
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            conn.execute("DELETE FROM image_cache_entries WHERE url_hash = ?", (row["url_hash"],))
            total -= row["size_bytes"] or 0
            deleted += 1
        conn.commit()
    finally:
        conn.close()
    return deleted


def cache_stats() -> dict:
    """Count and total size of cached images, plus the configured cap."""
    stats = {
        "enabled": IMAGE_CACHE_ENABLED,
        "count": 0,
        "used_bytes": 0,
        "max_bytes": MAX_CACHE_BYTES,
    }
    if not IMAGE_CACHE_ENABLED:
        return stats
    try:
        conn = _connect()
    except Exception:
        return stats
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(size_bytes), 0) AS total FROM image_cache_entries"
        ).fetchone()
        stats["count"] = int(row["n"] or 0)
        stats["used_bytes"] = int(row["total"] or 0)
    finally:
        conn.close()
    return stats


@_metadata_mutation
def evict_article_images(body_html: str | None, thumb: str | None = "",
                         article_id: int | None = None, *,
                         protected_hashes: set[str] | None = None,
                         conn: sqlite3.Connection | None = None) -> int:
    """Force-delete an article's cached image files and rows.

    Entries still pinned (shared by a favorited article) are skipped so a
    favorited article never loses its images. Unlike unpin_article_images this
    actively frees disk instead of waiting for size-triggered prune_cache."""
    if not IMAGE_CACHE_ENABLED:
        return 0
    owns_connection = conn is None
    conn = conn or _connect()
    deleted = 0
    try:
        hashes = {_url_hash(url) for url, _ in collect_image_urls(body_html, thumb, body_limit=None)}
        if article_id is not None:
            rows = conn.execute(
                "SELECT url_hash FROM image_cache_article_images WHERE article_id = ?",
                (article_id,),
            ).fetchall()
            hashes.update(r["url_hash"] for r in rows)
        for url_hash in hashes:
            # A cache entry can be used by articles other than the one being
            # removed.  ``pinned`` only tracks favorites, so it is not a safe
            # proxy for that relationship.
            if protected_hashes and url_hash in protected_hashes:
                continue
            if article_id is not None and conn.execute(
                "SELECT 1 FROM image_cache_article_images "
                "WHERE url_hash = ? AND article_id != ? LIMIT 1",
                (url_hash, article_id),
            ).fetchone():
                continue
            row = conn.execute(
                "SELECT path, pinned FROM image_cache_entries WHERE url_hash = ?",
                (url_hash,),
            ).fetchone()
            if not row or row["pinned"]:
                continue
            try:
                (CACHE_DIR / row["path"]).unlink(missing_ok=True)
            except OSError:
                pass
            conn.execute("DELETE FROM image_cache_entries WHERE url_hash = ?", (url_hash,))
            conn.execute("DELETE FROM image_cache_article_images WHERE url_hash = ?", (url_hash,))
            deleted += 1
        if article_id is not None:
            conn.execute("DELETE FROM image_cache_article_images WHERE article_id = ?", (article_id,))
        if owns_connection:
            conn.commit()
    finally:
        if owns_connection:
            conn.close()
    return deleted


def evict_unreferenced_images(referenced_hashes: set[str], *, conn: sqlite3.Connection | None = None) -> int:
    """Delete cached files not referenced by any retained article.

    This also clears stale pinned entries left behind by previously deleted
    favorites; the caller supplies hashes derived from the authoritative news
    database snapshot.
    """
    owns_connection = conn is None
    conn = conn or _connect()
    deleted = 0
    try:
        mapped_hashes = {
            row[0] for row in conn.execute("SELECT DISTINCT url_hash FROM image_cache_article_images")
        }
        rows = conn.execute("SELECT url_hash, path FROM image_cache_entries").fetchall()
        for row in rows:
            # Mappings are created for pinned/favorited articles.  Preserve
            # them even if their historic body HTML no longer carries the URL.
            if row["url_hash"] in referenced_hashes or row["url_hash"] in mapped_hashes:
                continue
            try:
                (CACHE_DIR / row["path"]).unlink(missing_ok=True)
            except OSError:
                pass
            conn.execute("DELETE FROM image_cache_entries WHERE url_hash = ?", (row["url_hash"],))
            conn.execute("DELETE FROM image_cache_article_images WHERE url_hash = ?", (row["url_hash"],))
            deleted += 1
        if owns_connection:
            conn.commit()
    finally:
        if owns_connection:
            conn.close()
    return deleted
