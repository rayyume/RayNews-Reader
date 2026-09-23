#!/usr/bin/env python3
"""Tiny HTTP server: runs fetcher.py on GET /refresh, periodic auto-refresh, and serves SQLite-backed API."""
import http.server
import ipaddress
import atexit
import subprocess
import json
import sys
import logging
import threading
import urllib.parse
import os
import queue
import sqlite3
import re
import signal
import time
import uuid
from collections import OrderedDict, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bs4 import BeautifulSoup

from image_cache import (
    cache_image,
    enqueue_article_image_prefetch,
    fetch_remote_image,
    get_cached_image,
)
from news_schema import (
    enable_wal_mode,
    ensure_article_schema,
    ensure_article_title_columns as _ensure_article_title_columns_shared,
)
from source_categories import (
    CATEGORY_NAMES, CATEGORY_ORDER, ensure_article_source_columns,
    maintain_source_categories, source_rows, category_definitions, valid_category,
    UNCATEGORIZED,
)

REFRESH_INTERVAL = 900  # 15 minutes
LOCK_FILE = "/tmp/raynews-fetcher.lock"
DATA_DIR = Path(os.environ.get("DATA_DIR", "/app/data"))
DB_FILE = DATA_DIR / "news.db"
NEWS_JSON_FILE = DATA_DIR / "news.json"
STATE_FILE = DATA_DIR / "fetcher_state.json"
PROGRESS_FILE = DATA_DIR / "fetch_progress.json"
FETCHER_COMMAND = ("python3", "/app/fetcher.py")
FETCHER_TAIL_MAX_LINES = 50
FETCHER_TAIL_MAX_BYTES = 16 * 1024
FETCHER_LOG_QUEUE_MAX_LINES = 128
FETCHER_READER_JOIN_SECONDS = 0.25
FETCHER_TERM_GRACE_SECONDS = 0.25
LAST_FETCH_STATUS = {
    "status": "never",
    "returncode": None,
    "stdout": "",
    "stderr": "",
    "updated_at": None,
}
REFRESH_JOB_LOCK = threading.Lock()
REFRESH_JOB = {
    "job_id": "",
    "status": "idle",
    "trigger": "",
    "started_at": None,
    "finished_at": None,
    "new_count": 0,
    "new_ids": [],
    "error": "",
}
REFRESH_JOB_HISTORY_LIMIT = 16
REFRESH_JOB_HISTORY = OrderedDict()
# Set by _run_refresh_job() right before calling run_fetcher(), so run_fetcher() can
# pass it to the fetcher.py subprocess as FETCH_JOB_ID — letting the progress file it
# writes be matched to a job by exact ID instead of a second-granularity timestamp
# heuristic. Safe as a bare global: REFRESH_JOB_LOCK already ensures only one fetch job
# runs (and thus one subprocess is spawned) at a time.
CURRENT_FETCH_JOB_ID = ""
# Thread-safe handle to the currently running fetcher subprocess so an atexit
# handler / SIGTERM reaper can terminate it when the daemon refresh-job thread
# is abandoned on shutdown (otherwise start_new_session=True orphans the child).
_LIVE_FETCHER_LOCK = threading.Lock()
_LIVE_FETCHER_PROCESS = None

logging.basicConfig(level=logging.INFO, format="%(asctime)s [refresh] %(message)s")
log = logging.getLogger("refresh")
_schema_lock = threading.Lock()
_schema_ready = False
_schema_ready_event = threading.Event()


def _nonnegative_int_env(name: str, default: int) -> int:
    """Return a non-negative integer setting, falling back on bad input."""
    try:
        value = int(os.environ.get(name, str(default)).strip())
    except (AttributeError, TypeError, ValueError):
        return default
    return value if value >= 0 else default


def _article_cache_config_from_env() -> dict[str, int]:
    """Parse article-cache limits without letting bad env values break startup."""
    return {
        "max_items": _nonnegative_int_env("ARTICLE_DETAIL_CACHE_MAX_ITEMS", 256),
        "max_mb": _nonnegative_int_env("ARTICLE_DETAIL_CACHE_MAX_MB", 64),
    }


_ARTICLE_CACHE_CONFIG = _article_cache_config_from_env()
ARTICLE_DETAIL_CACHE_MAX_ITEMS = _ARTICLE_CACHE_CONFIG["max_items"]
ARTICLE_DETAIL_CACHE_MAX_MB = _ARTICLE_CACHE_CONFIG["max_mb"]
ARTICLE_DETAIL_CACHE_MAX_BYTES = ARTICLE_DETAIL_CACHE_MAX_MB * 1024 * 1024


def ensure_schema_once(conn: sqlite3.Connection) -> None:
    global _schema_ready
    if _schema_ready_event.is_set() and _schema_ready:
        return
    with _schema_lock:
        if _schema_ready_event.is_set() and _schema_ready:
            return
        ensure_article_schema(conn)
        has_articles = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'articles'"
        ).fetchone()
        if has_articles:
            globals()["_schema_ready"] = True
            _schema_ready_event.set()


def ensure_article_title_columns(conn: sqlite3.Connection) -> None:
    """Backward-compatible title helper backed by the shared migrator."""
    _ensure_article_title_columns_shared(conn)


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_FILE), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    ensure_schema_once(conn)
    enable_wal_mode(conn)
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _warm_news_schema() -> bool:
    conn = None
    try:
        conn = get_db()
        return bool(_schema_ready_event.is_set() and _schema_ready)
    except Exception:
        log.exception("Schema warmup failed; migration remains lazy")
        return False
    finally:
        if conn is not None:
            conn.close()


# In-memory cache for article detail responses — invalidated on fetcher run
_article_cache: OrderedDict[int, bytes] = OrderedDict()
_article_cache_bytes = 0
_article_cache_lock = threading.RLock()


class _ArticleCacheFlight:
    """One producer's completion signal and response for all current waiters."""

    def __init__(self):
        self.event = threading.Event()
        self.result: bytes | None = None


_article_cache_inflight: dict[int, _ArticleCacheFlight] = {}


def _get_cached_article(article_id: int) -> bytes | None:
    """Return and touch an article cache entry; this helper acquires the cache lock."""
    with _article_cache_lock:
        payload = _article_cache.get(article_id)
        if payload is not None:
            _article_cache.move_to_end(article_id)
        return payload


def _store_cached_article(article_id: int, payload: bytes) -> bool:
    """Store under both limits, removing any old value even if replacement is rejected.

    This helper acquires the cache lock.  Treating a rejected replacement as an
    eviction prevents a stale response from surviving after a newly built response
    becomes too large for the configured cache.
    """
    global _article_cache_bytes
    with _article_cache_lock:
        previous = _article_cache.pop(article_id, None)
        if previous is not None:
            _article_cache_bytes -= len(previous)
        if (
            ARTICLE_DETAIL_CACHE_MAX_ITEMS == 0
            or ARTICLE_DETAIL_CACHE_MAX_BYTES == 0
            or len(payload) > ARTICLE_DETAIL_CACHE_MAX_BYTES
        ):
            return False
        _article_cache[article_id] = payload
        _article_cache_bytes += len(payload)
        while (
            len(_article_cache) > ARTICLE_DETAIL_CACHE_MAX_ITEMS
            or _article_cache_bytes > ARTICLE_DETAIL_CACHE_MAX_BYTES
        ):
            _, evicted = _article_cache.popitem(last=False)
            _article_cache_bytes -= len(evicted)
        return article_id in _article_cache


def _evict_cached_article(article_id: int) -> bool:
    """Remove one entry and its byte count; this helper acquires the cache lock."""
    global _article_cache_bytes
    with _article_cache_lock:
        payload = _article_cache.pop(article_id, None)
        if payload is None:
            return False
        _article_cache_bytes -= len(payload)
        return True


def refresh_runtime_stats() -> dict[str, int]:
    """Return one consistent snapshot of the private article cache state."""
    with _article_cache_lock:
        return {
            "article_cache_items": len(_article_cache),
            "article_cache_bytes": _article_cache_bytes,
            "article_cache_inflight": len(_article_cache_inflight),
        }


def _is_loopback_peer(handler: http.server.BaseHTTPRequestHandler) -> bool:
    """Trust only the TCP peer address, never client-supplied forwarding headers."""
    try:
        return ipaddress.ip_address(handler.client_address[0]).is_loopback
    except (AttributeError, IndexError, TypeError, ValueError):
        return False


def clear_article_cache():
    global _article_cache_bytes
    with _article_cache_lock:
        _article_cache.clear()
        _article_cache_bytes = 0
        _article_cache_inflight.clear()


def acquire_lock() -> bool:
    """Try to acquire a lock file atomically. Returns True if acquired."""
    try:
        os.makedirs(LOCK_FILE, exist_ok=False)
        return True
    except FileExistsError:
        return False


def release_lock():
    """Remove the lock file."""
    try:
        os.rmdir(LOCK_FILE)
    except OSError:
        pass


def _truncate_utf8_tail(value: str, max_bytes: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    return encoded[-max_bytes:].decode("utf-8", errors="ignore")


class _BoundedLineTail:
    """Retain the newest complete lines under both line and UTF-8 byte limits."""

    def __init__(self, max_lines: int, max_bytes: int):
        self._lines = deque(maxlen=max_lines)
        self._bytes = 0
        self._max_bytes = max_bytes

    def append(self, line: str) -> None:
        line = _truncate_utf8_tail(line, self._max_bytes)
        if len(self._lines) == self._lines.maxlen:
            oldest = self._lines.popleft()
            self._bytes -= len(oldest.encode("utf-8"))
            if self._lines:
                self._bytes -= 1
        if self._lines:
            self._bytes += 1
        self._lines.append(line)
        self._bytes += len(line.encode("utf-8"))
        while self._bytes > self._max_bytes:
            oldest = self._lines.popleft()
            self._bytes -= len(oldest.encode("utf-8"))
            if self._lines:
                self._bytes -= 1

    def value(self) -> str:
        return "\n".join(self._lines)


class _FetcherLogQueue(queue.Queue):
    """Bound queued logs while reserving each stream's newest line."""

    def __init__(self, maxsize: int):
        super().__init__(maxsize=maxsize)
        self._latest = {}
        self._latest_lock = threading.Lock()
        self._sequence = 0

    def offer(self, stream: str, line: str) -> None:
        with self._latest_lock:
            sequence = self._sequence
            self._sequence += 1
            self._latest[stream] = (sequence, line)
        item = (stream, sequence, line)
        try:
            self.put_nowait(item)
            return
        except queue.Full:
            pass
        try:
            self.get_nowait()
        except queue.Empty:
            pass
        try:
            self.put_nowait(item)
        except queue.Full:
            pass

    def latest_items(self):
        with self._latest_lock:
            return list(self._latest.items())


def _forward_fetcher_logs(
    log_queue: _FetcherLogQueue,
) -> None:
    """Best-effort log forwarding isolated from the pipe-drain threads."""
    forwarded_sequences = {}

    def forward(item) -> None:
        stream, sequence, line = item
        try:
            print(f"[fetcher:{stream}] {line}", flush=True)
        except BaseException:
            # Logging must never change the fetcher's exit status or stop drain.
            pass
        forwarded_sequences[stream] = max(
            sequence,
            forwarded_sequences.get(stream, -1),
        )

    while True:
        try:
            item = log_queue.get(timeout=0.05)
        except queue.Empty:
            for stream, (sequence, line) in log_queue.latest_items():
                if sequence > forwarded_sequences.get(stream, -1):
                    forward((stream, sequence, line))
            continue
        if item is None:
            break
        forward(item)

    # Queue eviction is allowed to keep pipe draining nonblocking, but make a
    # final best-effort attempt for the newest line from each stream.
    for stream, (sequence, line) in log_queue.latest_items():
        if sequence > forwarded_sequences.get(stream, -1):
            forward((stream, sequence, line))


_FETCHER_LOG_QUEUE = _FetcherLogQueue(maxsize=FETCHER_LOG_QUEUE_MAX_LINES)
_FETCHER_LOG_DISPATCHER_LOCK = threading.Lock()
_FETCHER_LOG_DISPATCHER_THREAD = None


def _ensure_fetcher_log_dispatcher() -> _FetcherLogQueue:
    """Start the single process-lifetime log dispatcher exactly once."""
    global _FETCHER_LOG_DISPATCHER_THREAD
    if _FETCHER_LOG_DISPATCHER_THREAD is not None:
        return _FETCHER_LOG_QUEUE
    with _FETCHER_LOG_DISPATCHER_LOCK:
        if _FETCHER_LOG_DISPATCHER_THREAD is None:
            dispatcher = threading.Thread(
                target=_forward_fetcher_logs,
                args=(_FETCHER_LOG_QUEUE,),
                name="fetcher-log-forwarder",
                daemon=True,
            )
            dispatcher.start()
            _FETCHER_LOG_DISPATCHER_THREAD = dispatcher
    return _FETCHER_LOG_QUEUE


def _read_fetcher_stream(
    pipe,
    stream: str,
    tail: _BoundedLineTail,
    log_queue: _FetcherLogQueue,
) -> None:
    """Drain one binary subprocess pipe without ever buffering an unbounded line."""
    line_tail = bytearray()
    has_pending_line = False

    def emit_line() -> None:
        raw_line = bytes(line_tail)
        if raw_line.endswith(b"\r"):
            raw_line = raw_line[:-1]
        line = raw_line.decode("utf-8", errors="replace")
        line = _truncate_utf8_tail(line, FETCHER_TAIL_MAX_BYTES)
        tail.append(line)
        log_queue.offer(stream, line)

    try:
        while True:
            # BufferedReader.read(size) may wait for the entire size even when a
            # flushed line is already available. One raw pipe read forwards each
            # available chunk immediately instead.
            chunk = os.read(pipe.fileno(), 4096)
            if not chunk:
                break
            pieces = chunk.split(b"\n")
            for index, piece in enumerate(pieces):
                if piece:
                    line_tail.extend(piece)
                    excess = len(line_tail) - FETCHER_TAIL_MAX_BYTES
                    if excess > 0:
                        del line_tail[:excess]
                    has_pending_line = True
                if index < len(pieces) - 1:
                    emit_line()
                    line_tail.clear()
                    has_pending_line = False
        if has_pending_line:
            emit_line()
    finally:
        pipe.close()


def _fetcher_exited_without_reaping(process) -> bool:
    """Return whether the direct child exited while leaving it waitable.

    Keeping the session leader unreaped reserves its PID/PGID until all
    process-group signals are complete, so a later killpg() cannot target a
    group that reused the fetcher's numeric ID.
    """
    return os.waitid(
        os.P_PID,
        process.pid,
        os.WEXITED | os.WNOHANG | os.WNOWAIT,
    ) is not None


def _wait_for_fetcher_exit_without_reaping(process, deadline: float) -> bool:
    while True:
        if _fetcher_exited_without_reaping(process):
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.01, remaining))


def _terminate_fetcher_group_before_reap(process) -> None:
    """Finish group termination before releasing the leader PID with wait()."""
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    time.sleep(FETCHER_TERM_GRACE_SECONDS)
    try:
        # Always follow TERM with KILL while the unreaped leader still protects
        # the PGID. This also removes TERM-ignoring descendants after the direct
        # child has already exited.
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def _close_fetcher_pipes(process) -> None:
    for pipe in (process.stdout, process.stderr):
        if pipe is not None and not pipe.closed:
            pipe.close()


def _terminate_live_fetcher_process() -> None:
    """Terminate the in-flight fetcher subprocess if one is still running.

    Used by the atexit handler and the SIGTERM reaper so a server shutdown does
    not orphan the fetcher child + log pipes when the daemon refresh-job thread
    is abandoned mid-run.
    """
    global _LIVE_FETCHER_PROCESS
    with _LIVE_FETCHER_LOCK:
        process = _LIVE_FETCHER_PROCESS
        _LIVE_FETCHER_PROCESS = None
    if process is None:
        return
    if process.poll() is not None:
        return
    _terminate_fetcher_group_before_reap(process)


atexit.register(_terminate_live_fetcher_process)


def _join_fetcher_readers(readers) -> None:
    """Bound cleanup even if a pipe reader fails to observe EOF promptly."""
    for reader in readers:
        reader.join(timeout=FETCHER_READER_JOIN_SECONDS)


def _run_fetcher_process(env, timeout):
    """Stream a fetcher child process while retaining only a bounded output tail."""
    global _LIVE_FETCHER_PROCESS
    process = None
    readers = []
    started_readers = []
    reaped = False
    try:
        process = subprocess.Popen(
            FETCHER_COMMAND,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            start_new_session=True,
        )
        with _LIVE_FETCHER_LOCK:
            _LIVE_FETCHER_PROCESS = process
        stdout_tail = _BoundedLineTail(
            FETCHER_TAIL_MAX_LINES,
            FETCHER_TAIL_MAX_BYTES,
        )
        stderr_tail = _BoundedLineTail(
            FETCHER_TAIL_MAX_LINES,
            FETCHER_TAIL_MAX_BYTES,
        )
        log_queue = _ensure_fetcher_log_dispatcher()
        readers.append(threading.Thread(
            target=_read_fetcher_stream,
            args=(process.stdout, "stdout", stdout_tail, log_queue),
            name="fetcher-stdout-reader",
            daemon=True,
        ))
        readers.append(threading.Thread(
            target=_read_fetcher_stream,
            args=(process.stderr, "stderr", stderr_tail, log_queue),
            name="fetcher-stderr-reader",
            daemon=True,
        ))
        for reader in readers:
            reader.start()
            started_readers.append(reader)

        deadline = time.monotonic() + timeout
        timed_out = not _wait_for_fetcher_exit_without_reaping(process, deadline)
        if not timed_out:
            for reader in started_readers:
                reader.join(timeout=max(0, deadline - time.monotonic()))
            timed_out = any(reader.is_alive() for reader in started_readers)

        if timed_out:
            # Signal the entire session before wait() releases the leader PID.
            _terminate_fetcher_group_before_reap(process)
            reaped = True
            returncode = None
        else:
            returncode = process.wait()
            reaped = True

    except BaseException:
        if process is not None and not reaped:
            _terminate_fetcher_group_before_reap(process)
            reaped = True
        raise
    finally:
        with _LIVE_FETCHER_LOCK:
            _LIVE_FETCHER_PROCESS = None
        if process is not None:
            _join_fetcher_readers(started_readers)
            _close_fetcher_pipes(process)
            _join_fetcher_readers(
                [reader for reader in started_readers if reader.is_alive()]
            )
    return {
        "returncode": returncode,
        "stdout_tail": stdout_tail.value(),
        "stderr_tail": stderr_tail.value(),
        "timed_out": timed_out,
    }


def run_fetcher(existing_article_ids: set[int] | None = None):
    """Run fetcher.py and return the result dict + HTTP status code."""
    baseline = (
        set(existing_article_ids)
        if existing_article_ids is not None
        else article_id_snapshot()
    )
    if not acquire_lock():
        log.warning("Fetcher already running — skipping")
        body = json.dumps({"status": "skipped", "error": "fetcher already running"}).encode()
        return body, 429
    try:
        log.info("Triggering fetcher...")
        env = {**os.environ, "FETCH_JOB_ID": CURRENT_FETCH_JOB_ID}
        result = _run_fetcher_process(env, timeout=120)
        if result["timed_out"]:
            LAST_FETCH_STATUS.update({
                "status": "error",
                "returncode": None,
                "stdout": "",
                "stderr": "timeout",
                "updated_at": int(time.time()),
            })
            log.warning("Fetcher timed out after 120 seconds")
            body = json.dumps({"status": "error", "error": "timeout"}).encode()
            return body, 500
        is_ok = result["returncode"] == 0
        payload = {
            "status": "ok" if is_ok else "error",
            "returncode": result["returncode"],
            "stdout": result["stdout_tail"],
            "stderr": result["stderr_tail"],
        }
        new_ids = []
        if is_ok:
            after_ids = article_id_snapshot()
            new_ids = sorted(after_ids - baseline)
            payload["new_ids"] = new_ids
        body = json.dumps(payload).encode()
        LAST_FETCH_STATUS.update({
            "status": "ok" if is_ok else "error",
            "returncode": result["returncode"],
            "stdout": result["stdout_tail"],
            "stderr": result["stderr_tail"],
            "updated_at": int(time.time()),
        })
        log.info(f"Fetcher done (exit={result['returncode']})")
        if is_ok:
            clear_article_cache()
            try:
                conn = sqlite3.connect(str(DB_FILE))
                conn.row_factory = sqlite3.Row
                ensure_article_source_columns(conn)
                # force=False: let maintain_source_categories()'s own
                # MAINTENANCE_THROTTLE_SECONDS throttle skip the two full-table
                # scans (source discovery, stale cleanup) when the last run was
                # recent — e.g. back-to-back manual refreshes. New/stale sources
                # are still caught within that window by whichever refresh (this
                # one, the next manual click, or the 15-minute periodic cycle)
                # lands after the throttle expires.
                # Deliberately not named `result`: that name holds the fetcher
                # subprocess's CompletedProcess, which the return value below
                # still depends on.
                maintenance = maintain_source_categories(conn, force=False)
                conn.commit()
                conn.close()
                if maintenance.get("discovered") or maintenance.get("deleted"):
                    log.info(
                        f"Source maintenance: discovered {maintenance.get('discovered', 0)}, "
                        f"cleaned up {maintenance.get('deleted', 0)} stale source(s)"
                    )
            except Exception as e:
                log.warning(f"Source cleanup failed: {e}")
            threading.Thread(
                target=enqueue_new_article_images,
                args=(new_ids,),
                daemon=True,
            ).start()
        return body, 200 if is_ok else 500
    except Exception as e:
        LAST_FETCH_STATUS.update({
            "status": "error",
            "returncode": None,
            "stdout": "",
            "stderr": str(e)[-300:],
            "updated_at": int(time.time()),
        })
        body = json.dumps({"status": "error", "error": str(e)}).encode()
        return body, 500
    finally:
        release_lock()


def article_id_snapshot() -> set[int]:
    """Return current article IDs so refresh can queue only newly inserted images."""
    conn = None
    try:
        if not DB_FILE.exists():
            return set()
        conn = sqlite3.connect(str(DB_FILE), timeout=30)
        rows = conn.execute("SELECT id FROM articles").fetchall()
        return {int(row[0]) for row in rows}
    except Exception as exc:
        log.warning(f"Article snapshot failed: {exc}")
        return set()
    finally:
        if conn:
            conn.close()


def _read_fetch_progress() -> dict | None:
    """Read the streaming-ingest progress file fetcher.py writes during a fetch cycle.
    Returns None if missing/unreadable — the caller must still work without it."""
    try:
        if not PROGRESS_FILE.exists():
            return None
        return json.loads(PROGRESS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None


def _positive_article_ids(values) -> list[int]:
    result = set()
    try:
        values = iter(values or [])
    except TypeError:
        return []
    for value in values:
        try:
            article_id = int(value)
        except (TypeError, ValueError):
            continue
        if article_id > 0:
            result.add(article_id)
    return sorted(result)


def _refresh_job_json_locked() -> bytes:
    payload = dict(REFRESH_JOB)
    baseline_ids = set(payload.pop("_baseline_ids", set()) or set())
    if payload.get("status") == "running":
        progress = _read_fetch_progress()
        # Match by exact job_id (fetcher.py stamps it from FETCH_JOB_ID) rather than a
        # second-granularity timestamp comparison — two jobs finishing/starting within
        # the same wall-clock second could otherwise let a previous cycle's progress
        # file be mistaken for this job's progress.
        if progress and progress.get("job_id") and progress.get("job_id") == payload.get("job_id"):
            if "inserted_ids" in progress:
                new_ids_so_far = [
                    article_id
                    for article_id in _positive_article_ids(progress.get("inserted_ids"))
                    if article_id not in baseline_ids
                ]
                payload["new_count_so_far"] = len(new_ids_so_far)
                payload["new_ids_so_far"] = new_ids_so_far
            else:
                # Older fetchers did not publish IDs. Preserve their diagnostic
                # progress count, but never invent IDs from that arithmetic value.
                payload["new_count_so_far"] = progress.get("inserted", 0)
    return json.dumps(payload).encode()


def get_refresh_job_status() -> bytes:
    with REFRESH_JOB_LOCK:
        return _refresh_job_json_locked()


def _remember_terminal_job_locked() -> None:
    job_id = REFRESH_JOB.get("job_id") or ""
    if not job_id or REFRESH_JOB.get("status") not in ("completed", "failed"):
        return
    terminal = dict(REFRESH_JOB)
    terminal.pop("_baseline_ids", None)
    REFRESH_JOB_HISTORY[job_id] = terminal
    REFRESH_JOB_HISTORY.move_to_end(job_id)
    while len(REFRESH_JOB_HISTORY) > REFRESH_JOB_HISTORY_LIMIT:
        REFRESH_JOB_HISTORY.popitem(last=False)


def get_refresh_job_status_response(job_id: str | None = None) -> tuple[bytes, int]:
    """Return the current job, or a bounded terminal snapshot by exact ID."""
    with REFRESH_JOB_LOCK:
        if not job_id:
            return _refresh_job_json_locked(), 200
        if REFRESH_JOB.get("job_id") == job_id:
            return _refresh_job_json_locked(), 200
        terminal = REFRESH_JOB_HISTORY.get(job_id)
        if terminal is not None:
            return json.dumps(terminal).encode(), 200
        return json.dumps({
            "status": "not_found",
            "error": "refresh job not found",
        }).encode(), 404


def _run_refresh_job(job_id: str) -> None:
    global CURRENT_FETCH_JOB_ID
    new_count = 0
    new_ids = []
    error = ""
    try:
        before_ids = article_id_snapshot()
        with REFRESH_JOB_LOCK:
            if REFRESH_JOB["job_id"] != job_id:
                return
            # The pre-job snapshot already exists for the terminal difference.
            # Reuse it to filter running progress instead of adding a status-time
            # database scan for every poll.
            REFRESH_JOB["_baseline_ids"] = before_ids
        CURRENT_FETCH_JOB_ID = job_id
        body, status = run_fetcher(before_ids)
        payload = json.loads(body)
        completed = 200 <= status < 300 and payload.get("status") == "ok"
        if completed:
            new_ids = _positive_article_ids(payload.get("new_ids"))
            new_count = len(new_ids)
        else:
            payload_error = payload.get("error")
            error = (
                payload_error
                if payload_error in ("timeout", "fetcher already running")
                else "refresh failed"
            )
    except Exception:
        completed = False
        error = "refresh failed"

    with REFRESH_JOB_LOCK:
        if REFRESH_JOB["job_id"] != job_id:
            return
        REFRESH_JOB.update({
            "status": "completed" if completed else "failed",
            "finished_at": int(time.time()),
            "new_count": new_count,
            "new_ids": new_ids if completed else [],
            "error": error,
        })
        _remember_terminal_job_locked()


def start_refresh_job(trigger: str = "manual") -> tuple[bytes, int]:
    with REFRESH_JOB_LOCK:
        if REFRESH_JOB["status"] == "running":
            return _refresh_job_json_locked(), 200
        job_id = uuid.uuid4().hex
        REFRESH_JOB.update({
            "job_id": job_id,
            "status": "running",
            "trigger": trigger,
            "started_at": int(time.time()),
            "finished_at": None,
            "new_count": 0,
            "new_ids": [],
            "_baseline_ids": set(),
            "error": "",
        })
        body = _refresh_job_json_locked()
    try:
        threading.Thread(
            target=_run_refresh_job,
            args=(job_id,),
            name=f"refresh-job-{job_id[:8]}",
            daemon=True,
        ).start()
    except Exception:
        with REFRESH_JOB_LOCK:
            if REFRESH_JOB["job_id"] == job_id and REFRESH_JOB["status"] == "running":
                REFRESH_JOB.update({
                    "status": "failed",
                    "finished_at": int(time.time()),
                    "error": "refresh failed",
                })
                _remember_terminal_job_locked()
            return _refresh_job_json_locked(), 500
    return body, 202


def enqueue_new_article_images(new_article_ids) -> None:
    """Queue image cache warmup for newly fetched articles without blocking refresh."""
    article_ids = _positive_article_ids(new_article_ids)
    if not article_ids:
        return
    conn = None
    try:
        conn = sqlite3.connect(str(DB_FILE), timeout=30)
        conn.row_factory = sqlite3.Row
        queued = 0
        for offset in range(0, len(article_ids), 500):
            batch = article_ids[offset:offset + 500]
            placeholders = ",".join("?" for _ in batch)
            rows = conn.execute(
                f"""
                SELECT id, thumb, body_html
                FROM articles
                WHERE id IN ({placeholders})
                """,
                tuple(batch),
            ).fetchall()
            for row in rows:
                queued += enqueue_article_image_prefetch(
                    row["id"], row["body_html"], row["thumb"]
                )
        if queued:
            log.info(f"Queued {queued} image(s) for background cache warmup")
    except Exception as exc:
        log.warning(f"Image prefetch enqueue failed: {exc}")
    finally:
        if conn:
            conn.close()


def enqueue_today_wsrv_article_images() -> dict[str, int]:
    """Queue all images for today's articles containing wsrv URLs."""
    result = {"articles": 0, "queued": 0}
    if not DB_FILE.exists():
        log.info("Startup wsrv image scan skipped: news db not found")
        return result

    conn = None
    try:
        today = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")
        conn = sqlite3.connect(str(DB_FILE), timeout=30)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT id, thumb, body_html
            FROM articles
            WHERE date = ?
              AND (
                LOWER(COALESCE(thumb, '')) LIKE '%wsrv.nl%'
                OR LOWER(COALESCE(body_html, '')) LIKE '%wsrv.nl%'
              )
            ORDER BY timestamp DESC
            """,
            (today,),
        ).fetchall()
        result["articles"] = len(rows)
        for row in rows:
            result["queued"] += enqueue_article_image_prefetch(
                row["id"],
                row["body_html"],
                row["thumb"],
                body_limit=None,
            )
        log.info(
            "Startup wsrv image scan: articles=%s queued=%s date=%s",
            result["articles"],
            result["queued"],
            today,
        )
    except Exception as exc:
        log.warning(f"Startup wsrv image scan failed: {exc}")
    finally:
        if conn:
            conn.close()
    return result


def _start_periodic_refresh_timer():
    timer = threading.Timer(REFRESH_INTERVAL, periodic_refresh)
    timer.daemon = True
    timer.start()


def periodic_refresh():
    """Run fetcher periodically in the background."""
    start_refresh_job("periodic")
    _start_periodic_refresh_timer()


# ─── API Handlers ─────────────────────────────────────────

def _diagnostics(count: int | None = None) -> dict:
    channel_url = (os.environ.get("TELEGRAM_CHANNEL_URL") or "").strip()
    channel = channel_url or (os.environ.get("TELEGRAM_CHANNEL") or "").strip()
    exists = DB_FILE.exists()
    try:
        db_size = DB_FILE.stat().st_size if exists else 0
    except OSError:
        db_size = 0
    news_json = {"exists": NEWS_JSON_FILE.exists(), "size": 0, "count": None}
    if NEWS_JSON_FILE.exists():
        try:
            news_json["size"] = NEWS_JSON_FILE.stat().st_size
            data = json.loads(NEWS_JSON_FILE.read_text(encoding="utf-8"))
            news_json["count"] = data.get("count", len(data.get("items", [])))
        except Exception as e:
            news_json["error"] = str(e)
    state = {"exists": STATE_FILE.exists(), "last_seen_id": None}
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            state["last_seen_id"] = data.get("last_seen_id")
        except Exception as e:
            state["error"] = str(e)
    with REFRESH_JOB_LOCK:
        refresh_job = {
            "status": REFRESH_JOB.get("status") or "idle",
            "trigger": REFRESH_JOB.get("trigger") or "",
        }
    global_article_count = None
    if exists:
        try:
            conn = sqlite3.connect(DB_FILE)
            try:
                global_article_count = int(conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0])
            finally:
                conn.close()
        except (sqlite3.Error, OSError, TypeError):
            pass
    return {
        "data_dir": str(DATA_DIR),
        "db_path": str(DB_FILE),
        "db_exists": exists,
        "db_size": db_size,
        "article_count": count,
        "global_article_count": global_article_count,
        "news_json": news_json,
        "fetcher_state": state,
        "telegram_channel_configured": bool(channel and channel != "your_channel"),
        "telegram_channel": channel if channel and channel != "your_channel" else "",
        "telegram_channel_default": not bool(channel and channel != "your_channel"),
        "last_fetch": dict(LAST_FETCH_STATUS),
        "refresh_job": refresh_job,
    }


def _public_cold_start_diagnostics(count: int | None = None) -> dict:
    """Bounded diagnostics required by the homepage during a cold start."""
    diagnostics = _diagnostics(count)
    return {
        "refresh_job": diagnostics["refresh_job"],
        "global_article_count": diagnostics["global_article_count"],
    }


def api_meta() -> bytes:
    """GET /api/meta — total article count."""
    conn = None
    try:
        conn = get_db()
        count = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
        return json.dumps({"count": count}).encode()
    except Exception:
        log.exception("Failed to read public API metadata")
        return json.dumps({"error": "internal server error"}).encode()
    finally:
        if conn:
            conn.close()


_DISPLAY_ATTRIBUTION_RE = re.compile(
    r"(?is)(?:"
    r"\s*<p[^>]*>\s*(?:出处\s*[:：]\s*|via\s*)"
    r"(?:<a\b[^>]*>.*?</a>|[^<\r\n]{1,80})\s*</p>\s*|"
    r"(?:^|[\r\n])\s*(?:出处\s*[:：]\s*|via\s*)"
    r"(?:<a\b[^>]*>.*?</a>|[^\r\n]{1,80})\s*"
    r")$"
)


def _strip_display_attribution(value: str | None) -> str | None:
    if not value:
        return value
    cleaned = value
    while True:
        next_value = _DISPLAY_ATTRIBUTION_RE.sub("", cleaned).rstrip()
        if next_value == cleaned:
            return cleaned
        cleaned = next_value


def _sanitize_article_html(value: str | None) -> str | None:
    if not value:
        return value
    soup = BeautifulSoup(value, "html.parser")
    for tag in soup.find_all(["script", "style", "iframe", "object", "embed"]):
        tag.decompose()
    # Telegraph separates blocks with standalone <br> tags (often several in a
    # row) sitting directly between <p>/<figure> elements and between <img> and
    # <figcaption>. Combined with our paragraph/figure margins these produce
    # doubled or tripled gaps. Drop those spacer <br>s so spacing matches the
    # original Telegraph layout, keeping genuine in-text line breaks inside
    # <p>, <li>, headings, figcaption, etc.
    for br in soup.find_all("br"):
        parent = br.parent
        if parent is not None and parent.name in {"article", "figure"}:
            br.decompose()
    for tag in soup.find_all(True):
        for attr, attr_value in list(tag.attrs.items()):
            attr_l = attr.lower()
            if attr_l.startswith("on"):
                del tag.attrs[attr]
                continue
            values = attr_value if isinstance(attr_value, list) else [str(attr_value)]
            joined = " ".join(str(v).strip().lower() for v in values)
            if attr_l in {"href", "src", "xlink:href", "formaction"}:
                if joined.startswith(("javascript:", "data:text/html")):
                    del tag.attrs[attr]
    return str(soup)


def _clean_article_display_fields(item: dict) -> dict:
    for field in ("summary", "body_html"):
        if field in item:
            item[field] = _strip_display_attribution(item.get(field))
    if "body_html" in item:
        item["body_html"] = _sanitize_article_html(item.get("body_html"))
    return item


def api_news_list(params: dict) -> bytes:
    """GET /api/news — paginated or incremental list (no body_html)."""
    try:
        page = int(params.get("page", ["1"])[0])
        size = int(params.get("size", ["30"])[0])
        page = max(page, 1)
        size = min(max(size, 1), 200)
        since = params.get("since", [None])[0]
        query = (params.get("q", [""])[0] or "").strip()
        category = (params.get("category", [""])[0] or "").strip()
        article_date = (params.get("date", [""])[0] or "").strip()
        sources = [
            source.strip()
            for source in params.get("source", [])
            if source and source.strip()
        ]
    except (ValueError, IndexError):
        return json.dumps({"error": "invalid params"}).encode()

    conn = None
    try:
        conn = get_db()
        source_expr = "COALESCE(NULLIF(group_source, ''), NULLIF(feed_source, ''), source)"
        clauses = []
        args = []
        if query:
            pattern = f"%{query.lower()}%"
            clauses.append(
                "(lower(title) LIKE ? "
                f"OR lower({source_expr}) LIKE ? "
                "OR lower(origin_source) LIKE ? "
                "OR lower(summary) LIKE ?)"
            )
            args.extend((pattern, pattern, pattern, pattern))
        if article_date:
            clauses.append("date = ?")
            args.append(article_date)
        if sources:
            placeholders = ",".join("?" for _ in sources)
            clauses.append(f"{source_expr} IN ({placeholders})")
            args.extend(sources)
        if category:
            if not valid_category(conn, category):
                return json.dumps({"error": "invalid category"}).encode()
            if category == UNCATEGORIZED:
                clauses.append(
                    f"{source_expr} IN "
                    "(SELECT source FROM source_categories WHERE category = ?)"
                )
            else:
                clauses.append(
                    f"{source_expr} IN "
                    "(SELECT source FROM source_categories WHERE category = ?)"
                )
            args.append(category)
        where_sql = f" WHERE {' AND '.join(clauses)}" if clauses else ""

        if since:
            since_ts = int(since)
            since_where = f"{where_sql}{' AND' if where_sql else ' WHERE'} timestamp >= ?"
            since_args = (*args, since_ts)
            rows = conn.execute(
                "SELECT id, title, original_title, COALESCE(NULLIF(group_source, ''), NULLIF(feed_source, ''), source) AS source, "
                "       feed_source, group_source, publisher_domain, origin_source, "
                "       time, date, timestamp, thumb, has_full_content, telegraph_url, summary "
                f"FROM articles{since_where} ORDER BY timestamp DESC LIMIT ?",
                (*since_args, size),
            ).fetchall()
            total = conn.execute(
                f"SELECT COUNT(*) FROM articles{since_where}",
                since_args,
            ).fetchone()[0]
        else:
            offset = (page - 1) * size
            base_select = (
                "SELECT id, title, original_title, COALESCE(NULLIF(group_source, ''), NULLIF(feed_source, ''), source) AS source, "
                "       feed_source, group_source, publisher_domain, origin_source, "
                "       time, date, timestamp, thumb, has_full_content, telegraph_url, summary "
                "FROM articles"
            )
            rows = conn.execute(
                f"{base_select}{where_sql} ORDER BY timestamp DESC LIMIT ? OFFSET ?",
                (*args, size, offset),
            ).fetchall()
            total = conn.execute(
                f"SELECT COUNT(*) FROM articles{where_sql}",
                args,
            ).fetchone()[0]

        items = [_clean_article_display_fields(dict(r)) for r in rows]
        return json.dumps({
            "items": items,
            "total": total,
            "page": page,
            "size": size,
            "diagnostics": _public_cold_start_diagnostics(total) if total == 0 else None,
        }, ensure_ascii=False).encode()
    except Exception:
        log.exception("Failed to read public news list")
        return json.dumps({
            "error": "internal server error",
            "diagnostics": _public_cold_start_diagnostics(None),
        }).encode()
    finally:
        if conn:
            conn.close()


def api_title_updates(params: dict) -> bytes:
    """GET /api/news/title-updates — lightweight title changes after cursor."""
    since = (params.get("since", [""])[0] or "").strip()
    since_ts = since
    since_id = 0
    if "|" in since:
        since_ts, since_id_text = since.rsplit("|", 1)
        try:
            since_id = int(since_id_text)
        except ValueError:
            since_id = 0
    conn = None
    try:
        conn = get_db()
        if not since:
            cursor = conn.execute("SELECT strftime('%Y-%m-%d %H:%M:%f', 'now')").fetchone()[0]
            return json.dumps({
                "items": [],
                "cursor": cursor,
            }, ensure_ascii=False).encode()
        rows = conn.execute(
            "SELECT id, title, original_title, title_updated_at, title_source "
            "FROM articles "
            "WHERE title_updated_at IS NOT NULL "
            "AND (title_updated_at > ? OR (title_updated_at = ? AND id > ?)) "
            "ORDER BY title_updated_at ASC, id ASC LIMIT 500",
            (since_ts, since_ts, since_id),
        ).fetchall()
        items = [dict(r) for r in rows]
        if items:
            for item in items:
                _evict_cached_article(int(item["id"]))
        cursor = f"{items[-1]['title_updated_at']}|{items[-1]['id']}" if items else since
        return json.dumps({
            "items": items,
            "cursor": cursor,
        }, ensure_ascii=False).encode()
    except Exception:
        log.exception("Failed to read public title updates")
        return json.dumps({"error": "internal server error"}).encode()
    finally:
        if conn:
            conn.close()


def api_cache_evict(params: dict) -> tuple[bytes, int]:
    """GET /internal/cache-evict?id=<article_id> — loopback-only, called by
    web_server.py right after it updates an article's title/body in news.db,
    so the next read doesn't return a stale cached response. Without this,
    staleness only self-heals on the next ~15min fetcher cycle (which clears
    the whole cache) or when a client happens to poll /api/news/title-updates
    (which only handles title changes, not body_html).
    """
    article_id = (params.get("id", [""])[0] or "").strip()
    if not article_id.isdigit():
        return json.dumps({"error": "invalid id"}).encode(), 400
    _evict_cached_article(int(article_id))
    return json.dumps({"ok": True}).encode(), 200


def _build_news_detail_response(article_id: int) -> bytes:
    """GET /api/news/<id> — single article with body_html (cached)."""
    conn = None
    try:
        conn = get_db()
        deleted = conn.execute(
            "SELECT 1 FROM deleted_articles WHERE article_id = ?",
            (article_id,),
        ).fetchone()
        if deleted:
            return json.dumps({"error": "not found"}).encode()
        row = conn.execute(
            "SELECT * FROM articles WHERE id = ?", (article_id,)
        ).fetchone()
        if not row:
            return json.dumps({"error": "not found"}).encode()
        item = dict(row)
        item["group_source"] = item.get("group_source") or item.get("source") or ""
        item["feed_source"] = item.get("feed_source") or item.get("source") or ""
        item["origin_source"] = item.get("origin_source") or ""
        item["source"] = item["group_source"]
        # /api/news/<id> is intentionally unauthenticated. Shared translated
        # HTML is delivered only by the authenticated /ai/result endpoint.
        item["body_html"] = (
            item.get("original_body_html") or item.get("body_html") or ""
        )
        item.pop("original_body_html", None)
        item = _clean_article_display_fields(item)
        result = json.dumps(item, ensure_ascii=False).encode()
        return result
    except Exception:
        log.exception("Failed to read public article detail")
        return json.dumps({"error": "internal server error"}).encode()
    finally:
        if conn:
            conn.close()


def api_news_detail(article_id: int) -> bytes:
    """GET /api/news/<id> - single article with body_html (cached)."""
    with _article_cache_lock:
        cached = _get_cached_article(article_id)
        if cached is not None:
            return cached
        flight = _article_cache_inflight.get(article_id)
        if flight is None:
            flight = _ArticleCacheFlight()
            _article_cache_inflight[article_id] = flight
            producer = True
        else:
            producer = False
        event = flight.event

    if not producer:
        event.wait()
        if flight.result is not None:
            return flight.result
        cached = _get_cached_article(article_id)
        if cached is not None:
            return cached
        return _build_news_detail_response(article_id)

    try:
        result = _build_news_detail_response(article_id)
        with _article_cache_lock:
            try:
                data = json.loads(result.decode("utf-8"))
            except Exception:
                data = {}
            if not data.get("error"):
                _store_cached_article(article_id, result)
            else:
                _evict_cached_article(article_id)
            flight.result = result
            _article_cache_inflight.pop(article_id, None)
            event.set()
        return result
    except Exception:
        with _article_cache_lock:
            _article_cache_inflight.pop(article_id, None)
            event.set()
        raise


def api_sources() -> bytes:
    """GET /api/sources — source category metadata."""
    conn = None
    try:
        conn = get_db()
        rows = source_rows(conn)
        definitions = category_definitions(conn)
        return json.dumps({
            "categories": [row["category"] for row in definitions],
            "category_names": {row["category"]: row["label"] for row in definitions},
            "category_definitions": definitions,
            "sources": rows,
        }, ensure_ascii=False).encode()
    except Exception:
        log.exception("Failed to read public source metadata")
        return json.dumps({"error": "internal server error"}).encode()
    finally:
        if conn:
            conn.close()


def send_json(handler, data: bytes, status=200):
    """Send a JSON response."""
    try:
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Access-Control-Allow-Origin", "*")
        handler.send_header("Content-Length", str(len(data)))
        handler.end_headers()
        handler.wfile.write(data)
    except (BrokenPipeError, ConnectionResetError):
        log.warning("Client disconnected before response could be written")


def send_text(handler, text: str, status=200):
    try:
        handler.send_response(status)
        handler.send_header("Content-Type", "text/plain")
        handler.send_header("Content-Length", str(len(text.encode())))
        handler.end_headers()
        handler.wfile.write(text.encode())
    except (BrokenPipeError, ConnectionResetError):
        pass


class Handler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path.rstrip("/")
        if path == "/refresh":
            body, status = start_refresh_job("manual")
            send_json(self, body, status)
            return
        send_text(self, "not found", 404)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/")
        params = urllib.parse.parse_qs(parsed.query)

        # ── API routes ──
        if path == "/api/meta":
            send_json(self, api_meta())
            return

        if path == "/api/news":
            send_json(self, api_news_list(params))
            return

        if path == "/api/news/title-updates":
            send_json(self, api_title_updates(params))
            return

        if path == "/api/sources":
            send_json(self, api_sources())
            return

        # /api/news/<id>
        m = re.match(r"^/api/news/(\d+)$", path)
        if m:
            send_json(self, api_news_detail(int(m.group(1))))
            return

        # ── Legacy routes ──
        if path == "/refresh":
            body, status = start_refresh_job("manual")
            send_json(self, body, status)
            return

        if path == "/refresh/status":
            job_id = (params.get("job_id", [""])[0] or "").strip()
            if job_id:
                body, status = get_refresh_job_status_response(job_id)
                send_json(self, body, status)
            else:
                send_json(self, get_refresh_job_status())
            return

        # ── Internal (loopback-only via nginx routing; not exposed under /api/) ──
        if path == "/internal/runtime-stats":
            if not _is_loopback_peer(self):
                send_json(self, json.dumps({"error": "forbidden"}).encode(), 403)
                return
            send_json(self, json.dumps(refresh_runtime_stats()).encode())
            return

        if path == "/internal/cache-evict":
            body, status = api_cache_evict(params)
            send_json(self, body, status)
            return

        if path in ("/img-cache", "/img-proxy"):
            self._handle_img_cache(params)
            return

        send_text(self, "not found", 404)

    def _handle_img_cache(self, params):
        img_url = params.get("url", [None])[0]
        if not img_url:
            send_text(self, "Missing url parameter", 400)
            return

        parsed_url = urllib.parse.urlparse(img_url)
        if parsed_url.scheme not in ("http", "https"):
            send_text(self, "Invalid URL scheme", 400)
            return

        try:
            cached = get_cached_image(img_url)
            if not cached:
                cached = cache_image(img_url)
            if not cached:
                body, content_type = fetch_remote_image(img_url)
            else:
                path, content_type = cached
                body = path.read_bytes()

            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "public, max-age=2592000")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            cached = get_cached_image(img_url)
            if cached:
                path, content_type = cached
                body = path.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Cache-Control", "public, max-age=2592000")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            log.exception("Image cache failed")
            send_text(self, "Image cache unavailable", 502)

    def log_message(self, fmt, *args):
        log.info(fmt % args)


class RayNewsThreadingHTTPServer(http.server.ThreadingHTTPServer):
    daemon_threads = True


def _handle_refresh_server_sigterm(signum, frame):
    _terminate_live_fetcher_process()
    raise SystemExit(128 + signum)


if __name__ == "__main__":
    port = 8081
    signal.signal(signal.SIGTERM, _handle_refresh_server_sigterm)
    diag = _diagnostics(None)
    log.info(
        "Startup diagnostics: data_dir=%s db_exists=%s db_size=%s telegram_configured=%s",
        diag["data_dir"],
        diag["db_exists"],
        diag["db_size"],
        diag["telegram_channel_configured"],
    )
    if diag["telegram_channel_default"]:
        log.warning("TELEGRAM_CHANNEL is not configured or still equals your_channel")
    server = RayNewsThreadingHTTPServer(("127.0.0.1", port), Handler)
    _warm_news_schema()
    start_refresh_job("startup")
    # Start periodic refresh in background
    _start_periodic_refresh_timer()
    log.info(f"Refresh + API server listening on {port} (auto-refresh every {REFRESH_INTERVAL}s)")
    threading.Thread(
        target=enqueue_today_wsrv_article_images,
        name="startup-wsrv-image-scan",
        daemon=True,
    ).start()
    server.serve_forever()
