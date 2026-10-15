import concurrent.futures
import json
import os
import signal
import sqlite3
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import fetcher
import refresh_server


@pytest.fixture(autouse=True)
def _isolated_fetcher_log_dispatcher(monkeypatch):
    """Give each test a stoppable dispatcher without leaking daemon threads."""
    log_queue = refresh_server._FetcherLogQueue(
        maxsize=refresh_server.FETCHER_LOG_QUEUE_MAX_LINES
    )
    monkeypatch.setattr(
        refresh_server,
        "_FETCHER_LOG_QUEUE",
        log_queue,
        raising=False,
    )
    monkeypatch.setattr(
        refresh_server,
        "_FETCHER_LOG_DISPATCHER_LOCK",
        threading.Lock(),
        raising=False,
    )
    monkeypatch.setattr(
        refresh_server,
        "_FETCHER_LOG_DISPATCHER_THREAD",
        None,
        raising=False,
    )

    yield log_queue

    dispatcher = refresh_server._FETCHER_LOG_DISPATCHER_THREAD
    if dispatcher is not None and dispatcher.is_alive():
        try:
            log_queue.put_nowait(None)
        except refresh_server.queue.Full:
            log_queue.get_nowait()
            log_queue.put_nowait(None)
        dispatcher.join(timeout=2)
        assert not dispatcher.is_alive()


def _use_python_fetcher(monkeypatch, source):
    monkeypatch.setattr(
        refresh_server,
        "FETCHER_COMMAND",
        (sys.executable, "-u", "-c", source),
    )


def test_fetcher_process_streams_large_stdout_and_stderr_without_deadlock(
    tmp_path, monkeypatch,
):
    release_path = tmp_path / "release-fetcher-output"
    _use_python_fetcher(
        monkeypatch,
        f"""
import pathlib
import sys
import time

print("ready", flush=True)
print("err-ready", file=sys.stderr, flush=True)
release = pathlib.Path({str(release_path)!r})
while not release.exists():
    time.sleep(0.01)
for number in range(5000):
    print(f"stdout-{{number}}", flush=True)
    print(f"stderr-{{number}}", file=sys.stderr, flush=True)
""",
    )
    ready = threading.Event()
    stderr_ready = threading.Event()
    stdout_finished = threading.Event()
    stderr_finished = threading.Event()
    sink_lines = []

    def sink(message, flush=False):
        assert flush is True
        sink_lines.append(message)
        if message == "[fetcher:stdout] ready":
            ready.set()
        if message == "[fetcher:stderr] err-ready":
            stderr_ready.set()
        if message == "[fetcher:stdout] stdout-4999":
            stdout_finished.set()
        if message == "[fetcher:stderr] stderr-4999":
            stderr_finished.set()

    monkeypatch.setattr(refresh_server, "print", sink, raising=False)

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            refresh_server._run_fetcher_process,
            os.environ.copy(),
            5,
        )
        try:
            assert ready.wait(timeout=2), "first line was not forwarded in real time"
            assert stderr_ready.wait(timeout=2), "stderr was not forwarded in real time"
            assert not future.done(), "helper waited for exit before forwarding output"
        finally:
            release_path.write_text("continue", encoding="utf-8")
        result = future.result(timeout=10)

    assert result["returncode"] == 0
    assert result["timed_out"] is False
    assert result["stdout_tail"].splitlines() == [
        f"stdout-{number}" for number in range(4950, 5000)
    ]
    assert result["stderr_tail"].splitlines() == [
        f"stderr-{number}" for number in range(4950, 5000)
    ]
    assert len(result["stdout_tail"].encode("utf-8")) <= 16 * 1024
    assert len(result["stderr_tail"].encode("utf-8")) <= 16 * 1024
    assert stdout_finished.wait(timeout=2)
    assert stderr_finished.wait(timeout=2)
    assert "[fetcher:stdout] stdout-4999" in sink_lines
    assert "[fetcher:stderr] stderr-4999" in sink_lines


def test_fetcher_process_truncates_one_overlong_utf8_line_from_the_tail(
    monkeypatch,
):
    _use_python_fetcher(
        monkeypatch,
        'print("BEGIN-" + ("界" * 10000) + "-END", flush=True)',
    )
    monkeypatch.setattr(refresh_server, "print", lambda *args, **kwargs: None, raising=False)

    result = refresh_server._run_fetcher_process(os.environ.copy(), timeout=5)

    assert result["returncode"] == 0
    assert result["stdout_tail"].endswith("-END")
    assert "BEGIN-" not in result["stdout_tail"]
    assert len(result["stdout_tail"].splitlines()) == 1
    assert len(result["stdout_tail"].encode("utf-8")) <= 16 * 1024


def test_fetcher_process_applies_utf8_byte_limit_across_retained_lines(monkeypatch):
    _use_python_fetcher(
        monkeypatch,
        'for number in range(30): print(f"{number:02d}-" + ("界" * 400))',
    )
    monkeypatch.setattr(refresh_server, "print", lambda *args, **kwargs: None, raising=False)

    result = refresh_server._run_fetcher_process(os.environ.copy(), timeout=5)

    retained_lines = result["stdout_tail"].splitlines()
    assert retained_lines[-1].startswith("29-")
    assert not any(line.startswith("00-") for line in retained_lines)
    assert len(result["stdout_tail"].encode("utf-8")) <= 16 * 1024


def test_fetcher_process_returns_nonzero_exit_code_and_both_tails(monkeypatch):
    _use_python_fetcher(
        monkeypatch,
        'import sys; print("out"); print("err", file=sys.stderr); raise SystemExit(7)',
    )
    monkeypatch.setattr(refresh_server, "print", lambda *args, **kwargs: None, raising=False)

    result = refresh_server._run_fetcher_process(os.environ.copy(), timeout=5)

    assert result == {
        "returncode": 7,
        "stdout_tail": "out",
        "stderr_tail": "err",
        "timed_out": False,
    }


def test_fetcher_process_sink_exception_does_not_stop_drain_or_tail(
    monkeypatch,
):
    _use_python_fetcher(
        monkeypatch,
        'for number in range(5000): print(f"line-{number}", flush=True)',
    )
    sink_called = threading.Event()
    thread_failures = []

    def raising_sink(*args, **kwargs):
        sink_called.set()
        raise OSError("sink closed")

    monkeypatch.setattr(refresh_server, "print", raising_sink, raising=False)
    monkeypatch.setattr(
        threading,
        "excepthook",
        lambda args: thread_failures.append(args.exc_value),
    )

    result = refresh_server._run_fetcher_process(os.environ.copy(), timeout=5)

    assert sink_called.wait(timeout=1)
    assert result["returncode"] == 0
    assert result["timed_out"] is False
    assert result["stdout_tail"].splitlines() == [
        f"line-{number}" for number in range(4950, 5000)
    ]
    assert thread_failures == []


def test_fetcher_process_timeout_does_not_wait_for_blocked_sink(monkeypatch):
    _use_python_fetcher(
        monkeypatch,
        'import time; print("blocked", flush=True); time.sleep(30)',
    )
    sink_entered = threading.Event()
    release_sink = threading.Event()
    finished = threading.Event()
    result_box = {}

    def blocking_sink(*args, **kwargs):
        sink_entered.set()
        release_sink.wait(timeout=10)

    monkeypatch.setattr(refresh_server, "print", blocking_sink, raising=False)

    def run_helper():
        try:
            result_box["result"] = refresh_server._run_fetcher_process(
                os.environ.copy(), timeout=0.3
            )
        finally:
            finished.set()

    worker = threading.Thread(target=run_helper, daemon=True)
    worker.start()
    assert sink_entered.wait(timeout=2)
    try:
        completed_without_sink = finished.wait(timeout=1.5)
    finally:
        release_sink.set()
        worker.join(timeout=2)

    assert completed_without_sink
    assert result_box["result"] == {
        "returncode": None,
        "stdout_tail": "blocked",
        "stderr_tail": "",
        "timed_out": True,
    }


def test_fetcher_processes_share_one_bounded_dispatcher_when_sink_blocks(
    monkeypatch,
    _isolated_fetcher_log_dispatcher,
):
    _use_python_fetcher(
        monkeypatch,
        'for number in range(1000): print(f"blocked-{number}", flush=True)',
    )
    sink_entered = threading.Event()
    release_sink = threading.Event()
    baseline_threads = set(threading.enumerate())

    def permanently_blocking_sink(*args, **kwargs):
        sink_entered.set()
        release_sink.wait()

    monkeypatch.setattr(
        refresh_server,
        "print",
        permanently_blocking_sink,
        raising=False,
    )

    try:
        results = [
            refresh_server._run_fetcher_process(os.environ.copy(), timeout=2)
            for _ in range(5)
        ]
        assert sink_entered.wait(timeout=2)
        dispatcher_threads = [
            thread
            for thread in threading.enumerate()
            if thread not in baseline_threads
            and thread.name == "fetcher-log-forwarder"
        ]
        assert len(dispatcher_threads) <= 1
        assert refresh_server._FETCHER_LOG_DISPATCHER_THREAD in dispatcher_threads
        assert _isolated_fetcher_log_dispatcher.qsize() == (
            refresh_server.FETCHER_LOG_QUEUE_MAX_LINES
        )
        assert all(
            result["returncode"] == 0 and result["timed_out"] is False
            for result in results
        )
    finally:
        release_sink.set()


def test_fetcher_log_dispatcher_start_is_idempotent_under_concurrency(
    _isolated_fetcher_log_dispatcher,
):
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        queues = list(
            executor.map(
                lambda _: refresh_server._ensure_fetcher_log_dispatcher(),
                range(32),
            )
        )

    assert all(log_queue is _isolated_fetcher_log_dispatcher for log_queue in queues)
    dispatchers = [
        thread
        for thread in threading.enumerate()
        if thread.name == "fetcher-log-forwarder"
    ]
    assert dispatchers == [refresh_server._FETCHER_LOG_DISPATCHER_THREAD]


def test_fetcher_timeout_tail_is_snapshotted_after_final_reader_drain(monkeypatch):
    _use_python_fetcher(
        monkeypatch,
        """
import signal
import time

signal.signal(signal.SIGTERM, signal.SIG_IGN)
number = 0
while True:
    print(f"line-{number}", flush=True)
    number += 1
    time.sleep(0.001)
""",
    )
    monkeypatch.setattr(refresh_server, "print", lambda *args, **kwargs: None, raising=False)
    monkeypatch.setattr(refresh_server, "FETCHER_READER_JOIN_SECONDS", 2.0)
    real_tail = refresh_server._BoundedLineTail
    tails = []

    class SlowRecordingTail(real_tail):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            tails.append(self)

        def append(self, line):
            super().append(line)
            time.sleep(0.002)

    monkeypatch.setattr(refresh_server, "_BoundedLineTail", SlowRecordingTail)

    result = refresh_server._run_fetcher_process(os.environ.copy(), timeout=0.5)

    assert result["timed_out"] is True
    assert result["stdout_tail"] == tails[0].value()
    assert result["stdout_tail"].splitlines()[-1].startswith("line-")


@pytest.mark.parametrize("failure_stage", ["construct", "start"])
def test_fetcher_process_thread_setup_failure_kills_and_reaps_child(
    failure_stage, tmp_path, monkeypatch
):
    survived_path = tmp_path / f"fetcher-survived-{failure_stage}"
    _use_python_fetcher(
        monkeypatch,
        (
            "import pathlib, signal, time; "
            "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "time.sleep(1); "
            f"pathlib.Path({str(survived_path)!r}).write_text('alive')"
        ),
    )
    monkeypatch.setattr(refresh_server, "print", lambda *args, **kwargs: None, raising=False)
    real_popen = refresh_server.subprocess.Popen
    real_thread = refresh_server.threading.Thread
    children = []
    created_threads = []

    def recording_popen(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        children.append(process)
        return process

    def failing_thread(*args, **kwargs):
        name = kwargs.get("name")
        if failure_stage == "construct" and name == "fetcher-stderr-reader":
            raise RuntimeError("thread construct failed")
        thread = real_thread(*args, **kwargs)
        created_threads.append(thread)
        if failure_stage == "start" and name == "fetcher-stderr-reader":
            def fail_start():
                raise RuntimeError("thread start failed")

            thread.start = fail_start
        return thread

    monkeypatch.setattr(refresh_server.subprocess, "Popen", recording_popen)
    monkeypatch.setattr(refresh_server.threading, "Thread", failing_thread)
    needed_test_cleanup = False

    try:
        with pytest.raises(RuntimeError, match=f"thread {failure_stage} failed"):
            refresh_server._run_fetcher_process(os.environ.copy(), timeout=5)
    finally:
        if children and children[0].returncode is None:
            needed_test_cleanup = True
            try:
                os.killpg(children[0].pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            children[0].wait()
        for pipe_name in ("stdout", "stderr"):
            if children:
                pipe = getattr(children[0], pipe_name)
                if pipe and not pipe.closed:
                    pipe.close()
        for thread in created_threads:
            if thread.ident is not None and thread.name != "fetcher-log-forwarder":
                thread.join(timeout=1)

    assert needed_test_cleanup is False
    assert children[0].returncode is not None
    assert children[0].stdout.closed
    assert children[0].stderr.closed
    with pytest.raises(ChildProcessError):
        os.waitpid(children[0].pid, os.WNOHANG)
    assert all(
        not thread.is_alive()
        for thread in created_threads
        if thread.name != "fetcher-log-forwarder"
    )
    time.sleep(1.1)
    assert not survived_path.exists()


def test_fetcher_process_timeout_kills_process_group_and_reaps_child(
    tmp_path, monkeypatch
):
    survived_path = tmp_path / "grandchild-survived"
    grandchild_source = (
        "import pathlib, signal, time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "time.sleep(2); "
        f"pathlib.Path({str(survived_path)!r}).write_text('alive')"
    )
    _use_python_fetcher(
        monkeypatch,
        f"""
import signal
import subprocess
import sys
import time

subprocess.Popen([sys.executable, "-c", {grandchild_source!r}])
signal.signal(signal.SIGTERM, signal.SIG_IGN)
print("started", flush=True)
time.sleep(30)
""",
    )
    monkeypatch.setattr(refresh_server, "print", lambda *args, **kwargs: None, raising=False)
    real_popen = refresh_server.subprocess.Popen
    children = []
    popen_options = []

    def recording_popen(*args, **kwargs):
        popen_options.append(kwargs)
        process = real_popen(*args, **kwargs)
        children.append(process)
        return process

    monkeypatch.setattr(refresh_server.subprocess, "Popen", recording_popen)

    started_at = time.monotonic()
    try:
        result = refresh_server._run_fetcher_process(os.environ.copy(), timeout=1)
        elapsed = time.monotonic() - started_at
    finally:
        for child in children:
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait()

    assert elapsed < 3
    assert result == {
        "returncode": None,
        "stdout_tail": "started",
        "stderr_tail": "",
        "timed_out": True,
    }
    assert popen_options[0]["start_new_session"] is True
    assert children[0].returncode is not None
    with pytest.raises(ChildProcessError):
        os.waitpid(children[0].pid, os.WNOHANG)
    time.sleep(1.2)
    assert not survived_path.exists(), "grandchild escaped the timed-out process group"


def test_fetcher_process_does_not_deadlock_when_exited_parent_leaves_pipe_open(
    tmp_path, monkeypatch
):
    ready_path = tmp_path / "grandchild-ready"
    grandchild_source = (
        "import pathlib, signal, time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"pathlib.Path({str(ready_path)!r}).write_text('ready'); "
        "time.sleep(30)"
    )
    _use_python_fetcher(
        monkeypatch,
        f"""
import pathlib
import os
import subprocess
import sys
import time

subprocess.Popen([sys.executable, "-c", {grandchild_source!r}])
ready = pathlib.Path({str(ready_path)!r})
while not ready.exists():
    time.sleep(0.01)
print("parent-exit", flush=True)
os._exit(0)
""",
    )
    monkeypatch.setattr(refresh_server, "print", lambda *args, **kwargs: None, raising=False)
    real_popen = refresh_server.subprocess.Popen
    real_killpg = refresh_server.os.killpg
    children = []
    lifecycle_events = []

    def recording_popen(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        real_wait = process.wait

        def recording_wait(*wait_args, **wait_kwargs):
            lifecycle_events.append("wait")
            return real_wait(*wait_args, **wait_kwargs)

        process.wait = recording_wait
        children.append(process)
        return process

    def recording_killpg(pid, sig):
        lifecycle_events.append(f"signal:{sig}")
        return real_killpg(pid, sig)

    monkeypatch.setattr(refresh_server.subprocess, "Popen", recording_popen)
    monkeypatch.setattr(refresh_server.os, "killpg", recording_killpg)
    finished = threading.Event()
    result_box = {}

    def run_helper():
        try:
            result_box["result"] = refresh_server._run_fetcher_process(
                os.environ.copy(), timeout=0.5
            )
        finally:
            finished.set()

    worker = threading.Thread(target=run_helper, daemon=True)
    started_at = time.monotonic()
    worker.start()
    try:
        completed_without_deadlock = finished.wait(timeout=1.5)
    finally:
        for child in children:
            try:
                real_killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        worker.join(timeout=2)

    assert completed_without_deadlock
    assert time.monotonic() - started_at < 3
    assert children[0].returncode == 0
    assert lifecycle_events.index(f"signal:{signal.SIGTERM}") < lifecycle_events.index(
        f"signal:{signal.SIGKILL}"
    ) < lifecycle_events.index("wait")
    assert result_box["result"] == {
        "returncode": None,
        "stdout_tail": "parent-exit",
        "stderr_tail": "",
        "timed_out": True,
    }


class _TrackingLock:
    def __init__(self):
        self.held = False

    def __enter__(self):
        assert not self.held
        self.held = True

    def __exit__(self, exc_type, exc, traceback):
        self.held = False


class _LockGuardedDict(dict):
    def __init__(self, lock, *args, **kwargs):
        self._lock = lock
        super().__init__(*args, **kwargs)

    def __len__(self):
        assert self._lock.held
        return super().__len__()

    def values(self):
        raise AssertionError("runtime stats must use the maintained byte counter")


def test_refresh_runtime_stats_reads_cache_bytes_items_and_inflight_under_lock(
    monkeypatch,
):
    lock = _TrackingLock()
    monkeypatch.setattr(refresh_server, "_article_cache_lock", lock)
    monkeypatch.setattr(
        refresh_server,
        "_article_cache",
        _LockGuardedDict(lock, {10: b"abc", 11: b"12345"}),
    )
    monkeypatch.setattr(
        refresh_server,
        "_article_cache_inflight",
        _LockGuardedDict(lock, {12: threading.Event()}),
    )
    monkeypatch.setattr(refresh_server, "_article_cache_bytes", 8, raising=False)

    stats = refresh_server.refresh_runtime_stats()

    assert stats == {
        "article_cache_items": 2,
        "article_cache_bytes": 8,
        "article_cache_inflight": 1,
    }


@pytest.mark.parametrize("remote_ip", ["127.0.0.1", "::1"])
def test_internal_runtime_stats_allows_immediate_loopback_peers(
    remote_ip, monkeypatch
):
    calls = []
    payload = {
        "article_cache_items": 2,
        "article_cache_bytes": 8,
        "article_cache_inflight": 1,
    }
    monkeypatch.setattr(
        refresh_server, "refresh_runtime_stats", lambda: payload
    )
    monkeypatch.setattr(
        refresh_server,
        "send_json",
        lambda handler, body, status=200: calls.append((json.loads(body), status)),
    )
    monkeypatch.setattr(
        refresh_server,
        "send_text",
        lambda handler, body, status=200: calls.append((body, status)),
    )
    handler = refresh_server.Handler.__new__(refresh_server.Handler)
    handler.path = "/internal/runtime-stats"
    handler.client_address = (remote_ip, 54321)
    handler.headers = {"X-Forwarded-For": "198.51.100.10"}

    refresh_server.Handler.do_GET(handler)

    assert calls == [(payload, 200)]


@pytest.mark.parametrize("remote_ip", ["198.51.100.10", "2001:db8::10"])
def test_internal_runtime_stats_rejects_non_loopback_even_with_spoofed_forwarding(
    remote_ip, monkeypatch
):
    calls = []
    monkeypatch.setattr(
        refresh_server,
        "send_json",
        lambda handler, body, status=200: calls.append((json.loads(body), status)),
    )
    monkeypatch.setattr(
        refresh_server,
        "send_text",
        lambda handler, body, status=200: calls.append((body, status)),
    )
    handler = refresh_server.Handler.__new__(refresh_server.Handler)
    handler.path = "/internal/runtime-stats"
    handler.client_address = (remote_ip, 54321)
    handler.headers = {"X-Forwarded-For": "127.0.0.1", "X-Real-IP": "::1"}

    refresh_server.Handler.do_GET(handler)

    assert calls == [({"error": "forbidden"}, 403)]


def test_parse_datetime_falls_back_to_current_beijing_time(monkeypatch):
    real_datetime = fetcher.datetime

    class FixedDatetime:
        @classmethod
        def fromisoformat(cls, value):
            raise ValueError(value)

        @classmethod
        def now(cls, tz=None):
            return real_datetime(2026, 6, 18, 9, 30, tzinfo=tz)

    monkeypatch.setattr(fetcher, "datetime", FixedDatetime)

    parsed = fetcher.parse_datetime("not-an-iso-time")

    assert parsed["date"] == "2026-06-18"
    assert parsed["time"] == "09:30"
    assert parsed["timestamp"] > 0


def test_fetcher_tracks_failed_message_count_instead_of_boolean():
    source = (ROOT / "fetcher.py").read_text(encoding="utf-8")

    assert "failed_count = 0" in source
    assert "failed_count += 1" in source
    assert "if failed_count:" in source
    assert "{failed_count} message(s) failed" in source
    assert "any_failed = False" not in source
    assert "any_failed = True" not in source


def test_news_json_mirror_is_loaded_from_sqlite_in_stable_recent_order():
    source = (ROOT / "fetcher.py").read_text(encoding="utf-8")

    loader = source[
        source.index("def _load_recent_articles_for_mirror("):
        source.index("def write_news_json_mirror(")
    ]
    assert "FROM articles" in loader
    assert "ORDER BY timestamp DESC, id DESC" in loader
    assert "LIMIT ?" in loader


def test_fulltext_fetches_use_a_shorter_timeout_than_the_telegram_list_page():
    source = (ROOT / "fetcher.py").read_text(encoding="utf-8")

    assert "FULLTEXT_TIMEOUT = 10" in source
    telegraph = source[source.index("def fetch_telegraph("):source.index("def fetch_wechat_article(")]
    wechat = source[source.index("def fetch_wechat_article("):source.index("def process_message(")]
    # Telegraph gets the tightened timeout because a failed fetch is retried by
    # backfill_missing_fulltext(); WeChat keeps the longer dedicated timeout since it has
    # no backfill safety net, so a short one would permanently downgrade fetchable posts.
    assert "timeout=FULLTEXT_TIMEOUT" in telegraph
    assert "timeout=WECHAT_FULLTEXT_TIMEOUT" in wechat
    assert "WECHAT_FULLTEXT_TIMEOUT = 20" in source
    # The Telegram list-page fetch keeps the longer REQUEST_TIMEOUT — only the
    # outbound full-text fetches (which can hang on a slow third-party site) were
    # tightened.
    telegram_page = source[source.index("def fetch_telegram_page("):source.index("def parse_messages(")]
    assert "timeout=REQUEST_TIMEOUT" in telegram_page


def test_run_does_not_retain_or_repeat_all_streamed_entries_at_cycle_end():
    source = (ROOT / "fetcher.py").read_text(encoding="utf-8")
    run_block = source[source.index("def run():"):source.index("def write_news_json_mirror(")]

    assert "new_entries" not in run_block
    assert "failed_batches.append(stream_batch)" in run_block
    assert "for failed_batch in failed_batches:" in run_block
    assert "maintain_source_categories(conn, force=False)" in run_block
    assert "upsert_articles(conn," not in run_block


def test_news_json_write_is_best_effort_and_does_not_block_sqlite_sync():
    source = (ROOT / "fetcher.py").read_text(encoding="utf-8")
    run_block = source[source.index("def run():"):source.index("def write_news_json_mirror(")]

    sqlite_sync_at = run_block.index("sqlite_sync_started_at")
    news_json_at = run_block.index("write_news_json_mirror(mirror_entries)")
    assert sqlite_sync_at < news_json_at
    news_json_call_block = run_block[news_json_at - 60:news_json_at + 120]
    assert "try:" in news_json_call_block
    assert "except Exception as e:" in news_json_call_block

    mirror_source = source[source.index("def write_news_json_mirror("):]
    assert "NEWS_JSON_MIRROR_LIMIT" in mirror_source
    assert "json.dumps(output, ensure_ascii=False)" in mirror_source
    assert "indent=2" not in mirror_source


def test_article_detail_sanitizes_dangerous_html_without_losing_images(tmp_path, monkeypatch):
    # tmp_path keeps the scratch DB out of the repo; monkeypatch.setattr restores
    # refresh_server's module globals on teardown so later tests don't inherit a
    # deleted DB path (which used to leave stray tmp-hardening-*.db files behind).
    db_path = tmp_path / "hardening.db"
    try:
        conn = sqlite3.connect(db_path)
        conn.execute(
            """
            CREATE TABLE articles (
                id INTEGER PRIMARY KEY,
                title TEXT,
                source TEXT,
                feed_source TEXT,
                origin_source TEXT,
                time TEXT,
                date TEXT,
                timestamp INTEGER,
                thumb TEXT,
                has_full_content INTEGER,
                telegraph_url TEXT,
                body_html TEXT,
                summary TEXT
            )
            """
        )
        conn.execute(
            "INSERT INTO articles "
            "(id, title, source, feed_source, origin_source, time, date, timestamp, thumb, "
            "has_full_content, telegraph_url, body_html, summary) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                1,
                "Unsafe",
                "Feed",
                "Feed",
                "",
                "09:00",
                "2026-06-18",
                1,
                "",
                1,
                "",
                '<p onclick="steal()">Hello <a href="javascript:steal()">bad</a>'
                '<img src="https://example.com/a.jpg" onerror="steal()">'
                '<script>steal()</script><iframe src="https://evil.example"></iframe></p>',
                "summary",
            ),
        )
        conn.commit()
        conn.close()

        monkeypatch.setattr(refresh_server, "DB_FILE", db_path)
        monkeypatch.setattr(refresh_server, "_schema_ready", False)
        refresh_server.clear_article_cache()

        data = json.loads(refresh_server.api_news_detail(1).decode("utf-8"))
    finally:
        # Drop the cached detail so a later test doesn't read this article back; the DB
        # file itself lives under tmp_path and is cleaned up by pytest automatically.
        refresh_server.clear_article_cache()

    assert "script" not in data["body_html"].lower()
    assert "iframe" not in data["body_html"].lower()
    assert "onclick" not in data["body_html"].lower()
    assert "onerror" not in data["body_html"].lower()
    assert "javascript:" not in data["body_html"].lower()
    assert 'src="https://example.com/a.jpg"' in data["body_html"]


def test_refresh_article_detail_cache_computes_once_per_article():
    source = (ROOT / "refresh_server.py").read_text(encoding="utf-8")

    assert "_article_cache_inflight" in source
    assert "event.wait()" in source
    assert "_article_cache_inflight[article_id]" in source


def test_news_db_connection_is_thread_local_not_process_wide():
    # The news.db connection must be per-thread: a single process-wide connection
    # shared across Werkzeug's request threads let concurrent cursors/transactions
    # (a slow /ai/ or admin write vs. a /auth/refresh/status read) corrupt each other.
    source = (ROOT / "web_server.py").read_text(encoding="utf-8")
    local_pos = source.index("_news_conn_local = threading.local()")
    get_news_pos = source.index("def _get_news_db()")
    # Bound the block by the *next* top-level def rather than a byte count, so
    # growing _get_news_db() (e.g. adding the stale-path check) doesn't silently
    # slide the assertions below out of the window.
    block_end = source.index("\ndef ", get_news_pos + 1)
    block = source[local_pos:block_end]

    assert "getattr(_news_conn_local, \"conn\", None)" in block
    assert "_news_conn_local.conn = conn" in block
    # No resurrected process-wide shared connection.
    assert "_news_conn = None" not in source


def test_ai_result_save_uses_single_upsert_statement():
    source = (ROOT / "web_server.py").read_text(encoding="utf-8")
    start = source.index("def _save_ai_result")
    end = source.index("# ", start)
    block = source[start:end]

    assert "ON CONFLICT(article_id) DO UPDATE SET" in block
    assert "existing = _get_ai_result" not in block


def test_image_prefetch_cache_check_and_pending_mark_share_lock():
    source = (ROOT / "image_cache.py").read_text(encoding="utf-8")
    start = source.index("def enqueue_article_image_prefetch")
    end = source.index("def prune_cache", start)
    block = source[start:end]
    lock_pos = block.index("with _prefetch_lock:")
    cache_check_pos = block.index("get_cached_image(url)")
    pending_add_pos = block.index("_prefetch_pending.add(pending_key)")

    assert lock_pos < cache_check_pos < pending_add_pos


def test_service_worker_auth_requests_bypass_cache():
    sw = (ROOT / "frontend" / "sw.js").read_text(encoding="utf-8")

    assert "url.pathname.startsWith('/auth/')" in sw
    assert "return;" in sw[sw.index("url.pathname.startsWith('/auth/')"):sw.index("url.pathname.startsWith('/api/')")]


def test_api_fetch_returns_structured_auth_expired_result():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    start = html.index("async function apiFetch")
    end = html.index("async function parseJsonResponse", start)
    block = html[start:end]

    assert "return { error: 'auth_expired', status: 401 };" in block
    assert "return null;" not in block


def test_schema_ready_is_guarded_by_event_instead_of_unsynchronized_fast_path():
    source = (ROOT / "refresh_server.py").read_text(encoding="utf-8")
    start = source.index("def ensure_schema_once")
    end = source.index("def ensure_article_title_columns", start)
    block = source[start:end]

    assert "_schema_ready_event" in source
    assert "_schema_ready_event.is_set()" in block
    assert "_schema_ready =" not in block


def test_schema_once_does_not_latch_before_articles_exists(tmp_path, monkeypatch):
    conn = sqlite3.connect(tmp_path / "news.db")
    monkeypatch.setattr(refresh_server, "_schema_ready", False)
    refresh_server._schema_ready_event.clear()
    try:
        refresh_server.ensure_schema_once(conn)

        assert refresh_server._schema_ready is False
        assert not refresh_server._schema_ready_event.is_set()
    finally:
        conn.close()


def test_warmup_runs_before_startup_fetch():
    source = Path(refresh_server.__file__).read_text(encoding="utf-8")
    main = source[source.index('if __name__ == "__main__":'):]

    assert main.index("_warm_news_schema()") < main.index('start_refresh_job("startup")')


def test_warm_news_schema_returns_readiness_and_closes_connection(tmp_path, monkeypatch):
    conn = sqlite3.connect(tmp_path / "news.db")
    previous_event_state = refresh_server._schema_ready_event.is_set()
    monkeypatch.setattr(refresh_server, "get_db", lambda: conn)
    monkeypatch.setattr(refresh_server, "_schema_ready", True)
    refresh_server._schema_ready_event.set()
    try:
        assert refresh_server._warm_news_schema() is True
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")
    finally:
        refresh_server._schema_ready_event.clear()
        if previous_event_state:
            refresh_server._schema_ready_event.set()


def test_sanitize_strips_telegraph_spacer_brs_but_keeps_intext_breaks():
    html = (
        "<article>"
        "<p>First paragraph.</p>"
        "<br><p>Second paragraph.</p>"
        "<br><br><br><p>Third after triple spacer.</p>"
        "<figure><img src=\"https://x/i.jpg\"/><br>"
        "<figcaption>Caption line one<br>line two</figcaption></figure>"
        "</article>"
    )
    out = refresh_server._sanitize_article_html(html)

    # All spacer <br>s directly between blocks / in the figure are gone,
    # so paragraphs sit adjacent and rely on CSS margins for spacing.
    assert "</p><p>" in out
    assert "<br><p>" not in out
    assert "<img" in out and "<figcaption>" in out
    assert "<img/><figcaption>" in out.replace(' src="https://x/i.jpg"', "")
    # Genuine in-text line break inside the figcaption is preserved.
    assert "Caption line one<br/>line two" in out
