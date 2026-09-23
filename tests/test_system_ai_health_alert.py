"""Admins are told when the system AI stops working — not just at 21:30.

Every background job (auto summary/translation/title, source classification)
and the daily summary share the one admin-configured system AI. A dead key used
to surface only as log lines plus, hours later, the daily-summary failure alert.
Consecutive failures across all of those jobs now raise one alert, and the next
real success arms a recovery notice after the configured stability window.
"""

import os
import sqlite3
import subprocess
import sys
import threading
import time as stdlib_time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import web_server
import models


@pytest.fixture
def isolated_app_state_db(tmp_path, monkeypatch):
    """Run persisted incident assertions against an isolated app-state DB."""
    models.close_db()
    monkeypatch.setattr(models, "DB_FILE", tmp_path / "system-ai-health.db")
    monkeypatch.setattr(
        web_server,
        "SYSTEM_AI_LAST_FAILURE_MARKER_FILE",
        tmp_path / "system-ai-last-failure.marker",
        raising=False,
    )
    models.get_db()
    try:
        yield
    finally:
        models.close_db()


@pytest.fixture
def restart_system_ai_health_process():
    """Rebuild only module-local health state, preserving the durable DB."""
    def restart():
        with web_server._system_ai_health_lock:
            web_server._system_ai_health.clear()
            web_server._system_ai_health.update(
                {
                    "failures": 0,
                    "alerted": False,
                    "last_error": "",
                    "jobs": [],
                    "last_failure_at": 0.0,
                    "last_success_at": 0.0,
                    "failure_timestamp_dirty": False,
                }
            )

    return restart


def test_zero_stability_window_closes_after_a_real_success(isolated_app_state_db):
    models.set_app_state("incident", "1")
    models.set_app_state("failed", "100")
    models.set_app_state("succeeded", "101")

    assert models.complete_app_state_incident_if_stable(
        "incident", "notified", "failed", "succeeded", 0, now=10_000
    ) == "1"
    assert models.get_app_state("incident") == "0"
    assert models.get_app_state("notified") == "10000.0"


def test_removed_success_threshold_emits_a_startup_migration_warning(tmp_path):
    environment = os.environ.copy()
    environment.update({
        "DATA_DIR": str(tmp_path),
        "SYSTEM_AI_RECOVERY_SUCCESS_THRESHOLD": "5",
    })

    result = subprocess.run(
        [sys.executable, "-c", "import web_server"],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert result.returncode == 0
    output = result.stdout + result.stderr
    assert "SYSTEM_AI_RECOVERY_SUCCESS_THRESHOLD" in output
    assert "SYSTEM_AI_RECOVERY_STABILITY_SECONDS" in output


def test_stable_complete_rechecks_timestamps_in_transaction(isolated_app_state_db):
    models.set_app_state("incident", "1")
    models.set_app_state("failed", "100")
    models.set_app_state("succeeded", "101")

    assert models.complete_app_state_incident_if_stable(
        "incident", "notified", "failed", "succeeded", 60, now=161
    ) == "1"
    assert models.get_app_state("incident") == "0"
    assert models.get_app_state("notified") == "161.0"


def test_stable_complete_waits_for_writer_before_reading_timestamps(
    isolated_app_state_db, monkeypatch
):
    """A failure committed by the current writer must prevent stale closure.

    Holding SQLite's write lock before the helper starts makes the ordering
    observable: the helper must wait, acquire ``BEGIN IMMEDIATE``, and only
    then read the timestamps. Reading before the lock would close this event
    from the stale ``failed=100`` snapshot.
    """
    models.set_app_state("incident", "1")
    models.set_app_state("failed", "100")
    models.set_app_state("succeeded", "101")
    writer = sqlite3.connect(models.DB_FILE, timeout=30)
    writer.execute("PRAGMA busy_timeout=30000")
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("UPDATE app_state SET value = '160' WHERE key = 'failed'")
    outcome = {}
    beginning_transaction = threading.Event()
    real_get_db = models.get_db

    class SignalingConnection:
        def __init__(self, connection):
            self._connection = connection

        def execute(self, sql, *args):
            if sql == "BEGIN IMMEDIATE":
                beginning_transaction.set()
            return self._connection.execute(sql, *args)

        def __getattr__(self, name):
            return getattr(self._connection, name)

    def get_signaling_db():
        connection = real_get_db()
        if threading.current_thread().name == "stable-completion-worker":
            return SignalingConnection(connection)
        return connection

    monkeypatch.setattr(models, "get_db", get_signaling_db)

    def complete_after_writer():
        try:
            outcome["result"] = models.complete_app_state_incident_if_stable(
                "incident", "notified", "failed", "succeeded", 60, now=161
            )
        except Exception as exc:  # Surface worker failures in the test thread.
            outcome["error"] = exc
        finally:
            models.close_db()

    worker = threading.Thread(
        target=complete_after_writer, name="stable-completion-worker"
    )
    try:
        worker.start()
        assert beginning_transaction.wait(timeout=2)
        assert worker.is_alive()
        writer.commit()
        worker.join(timeout=2)
    finally:
        if writer.in_transaction:
            writer.rollback()
        writer.close()
        worker.join(timeout=2)

    assert not worker.is_alive()
    assert "error" not in outcome
    assert outcome["result"] == "0"
    assert models.get_app_state("incident") == "1"


def test_atomic_app_state_reset_rolls_back_all_keys_on_write_failure(
    isolated_app_state_db,
):
    values = {
        "incident": "1",
        "notified": "900",
        "failed": "1000",
        "succeeded": "1001",
    }
    for key, value in values.items():
        models.set_app_state(key, value)
    db = models.get_db()
    db.execute(
        "CREATE TEMP TRIGGER fail_app_state_reset "
        "BEFORE UPDATE ON app_state "
        "WHEN NEW.key = 'notified' AND NEW.value = '0' "
        "BEGIN SELECT RAISE(ABORT, 'reset failed'); END"
    )
    db.commit()

    with pytest.raises(sqlite3.IntegrityError, match="reset failed"):
        models.set_app_state_values({key: "0" for key in values})

    assert {key: models.get_app_state(key) for key in values} == values


@pytest.fixture
def alerts(monkeypatch, tmp_path):
    sent = []
    monkeypatch.setattr(
        web_server,
        "SYSTEM_AI_LAST_FAILURE_MARKER_FILE",
        tmp_path / "system-ai-last-failure.marker",
        raising=False,
    )
    monkeypatch.setattr(web_server, "list_users", lambda: [
        {"id": 1, "role": "admin"}, {"id": 2, "role": "user"}, {"id": 3, "role": "admin"},
    ])
    monkeypatch.setattr(web_server, "_notify_user",
                        lambda user_id, ntype, title, body: (sent.append(
                            {"user_id": user_id, "type": ntype, "title": title, "body": body})
                            or True))
    web_server._reset_system_ai_health()
    yield sent
    web_server._reset_system_ai_health()


def _fail(times, job="自动摘要", error="401 invalid api key"):
    for _ in range(times):
        web_server._note_system_ai_failure(job, error)


def _success(times):
    for _ in range(times):
        web_server._note_system_ai_success()


def _recover_after_stability(clock):
    """Record one real success, then let the periodic stability check close."""
    clock["now"] += 1
    _success(1)
    clock["now"] += web_server.SYSTEM_AI_RECOVERY_STABILITY_SECONDS
    return web_server._maybe_recover_stale_system_ai_incident()


def test_daily_summary_deduplication_recognizes_only_notified_incidents(
    isolated_app_state_db,
):
    """Only the administrator-visible state 1 suppresses a daily alert."""
    models.set_app_state(web_server.SYSTEM_AI_ALERTED_STATE_KEY, "1")
    assert web_server._system_ai_incident_is_notified() is True

    models.set_app_state(web_server.SYSTEM_AI_ALERTED_STATE_KEY, "2")
    assert web_server._system_ai_incident_is_notified() is False


def test_quiet_day_recovers_after_one_success_and_stability_window(
    isolated_app_state_db, alerts, monkeypatch
):
    clock = {"now": 1_000.0}
    monkeypatch.setattr(web_server.time, "time", lambda: clock["now"])
    _fail(web_server.SYSTEM_AI_FAILURE_ALERT_THRESHOLD)
    alerts.clear()

    clock["now"] += 1
    web_server._note_system_ai_success()
    clock["now"] += web_server.SYSTEM_AI_RECOVERY_STABILITY_SECONDS + 1

    assert web_server._maybe_recover_stale_system_ai_incident() is True
    assert [item["type"] for item in alerts] == ["system_ai_recovered"] * 2


def test_failure_after_success_prevents_stable_recovery(
    isolated_app_state_db, alerts, monkeypatch
):
    clock = {"now": 1_000.0}
    monkeypatch.setattr(web_server.time, "time", lambda: clock["now"])
    _fail(web_server.SYSTEM_AI_FAILURE_ALERT_THRESHOLD)
    clock["now"] += 1
    web_server._note_system_ai_success()
    clock["now"] += 10
    web_server._note_system_ai_failure("自动翻译", "503")
    clock["now"] += web_server.SYSTEM_AI_RECOVERY_STABILITY_SECONDS + 1

    assert web_server._maybe_recover_stale_system_ai_incident() is False
    assert web_server.get_app_state(web_server.SYSTEM_AI_ALERTED_STATE_KEY) == "1"


def test_stable_recovery_silently_closes_a_cooldown_incident(
    isolated_app_state_db, alerts, monkeypatch
):
    clock = {"now": 1_000.0}
    monkeypatch.setattr(web_server.time, "time", lambda: clock["now"])
    _fail(web_server.SYSTEM_AI_FAILURE_ALERT_THRESHOLD)
    assert _recover_after_stability(clock) is True
    alerts.clear()

    _fail(web_server.SYSTEM_AI_FAILURE_ALERT_THRESHOLD)
    assert web_server.get_app_state(web_server.SYSTEM_AI_ALERTED_STATE_KEY) == "2"
    clock["now"] += 1
    web_server._note_system_ai_success()
    clock["now"] += web_server.SYSTEM_AI_RECOVERY_STABILITY_SECONDS + 1

    assert web_server._maybe_recover_stale_system_ai_incident() is True
    assert web_server.get_app_state(web_server.SYSTEM_AI_ALERTED_STATE_KEY) == "0"
    assert alerts == []


def test_health_timestamps_are_written_under_lock_before_early_returns(
    alerts, monkeypatch
):
    clock = {"now": 1_000.0}
    writes = []
    monkeypatch.setattr(web_server.time, "time", lambda: clock["now"])

    def record_locked_write(key, value):
        if key in {
            web_server.SYSTEM_AI_LAST_FAILURE_STATE_KEY,
            web_server.SYSTEM_AI_LAST_SUCCESS_STATE_KEY,
        } and value != "0":
            assert web_server._system_ai_health_lock.locked()
            writes.append((key, value))

    monkeypatch.setattr(web_server, "set_app_state", record_locked_write)
    with web_server._system_ai_health_lock:
        web_server._system_ai_health["alerted"] = True

    # This takes the existing ``alerted`` early return, but the new failure is
    # still the timestamp that the stable-recovery transaction must observe.
    web_server._note_system_ai_failure("自动翻译", "503")
    assert writes == [(web_server.SYSTEM_AI_LAST_FAILURE_STATE_KEY, "1000.0")]
    assert web_server._system_ai_health["last_failure_at"] == 1_000.0

    clock["now"] += 1
    with web_server._system_ai_health_lock:
        web_server._system_ai_health["alerted"] = False
    monkeypatch.setattr(web_server, "_system_ai_incident_is_active", lambda: False)
    web_server._note_system_ai_success()
    assert writes[-1] == (web_server.SYSTEM_AI_LAST_SUCCESS_STATE_KEY, "1001.0")
    assert web_server._system_ai_health["last_success_at"] == 1_001.0


def test_failed_failure_timestamp_write_blocks_stable_recovery(
    isolated_app_state_db, alerts, monkeypatch
):
    clock = {"now": 1_000.0}
    monkeypatch.setattr(web_server.time, "time", lambda: clock["now"])
    _fail(web_server.SYSTEM_AI_FAILURE_ALERT_THRESHOLD)
    alerts.clear()
    clock["now"] = 1_001.0
    web_server._note_system_ai_success()

    real_set_app_state = web_server.set_app_state
    failed_once = {"value": False}

    def fail_next_failure_timestamp(key, value):
        if (
            key == web_server.SYSTEM_AI_LAST_FAILURE_STATE_KEY
            and not failed_once["value"]
        ):
            failed_once["value"] = True
            raise sqlite3.OperationalError("one-shot timestamp write failure")
        return real_set_app_state(key, value)

    monkeypatch.setattr(web_server, "set_app_state", fail_next_failure_timestamp)
    clock["now"] = 4_000.0
    # Alert bookkeeping remains best-effort and must not break the provider's
    # caller even though this latest timestamp cannot be made durable.
    web_server._note_system_ai_failure("自动翻译", "503")
    assert failed_once["value"] is True
    assert web_server.get_app_state(
        web_server.SYSTEM_AI_LAST_FAILURE_STATE_KEY
    ) == "1000.0"

    # The durable snapshot looks stable (1001 > 1000 and 3601 seconds quiet),
    # but the real latest failure was only 601 seconds ago. Fail closed rather
    # than letting the periodic helper close the incident from stale data.
    clock["now"] = 4_601.0
    assert web_server._maybe_recover_stale_system_ai_incident() is False
    assert web_server.get_app_state(web_server.SYSTEM_AI_ALERTED_STATE_KEY) == "1"
    assert alerts == []


def test_dirty_failure_remains_fail_closed_after_process_restart(
    isolated_app_state_db, alerts, monkeypatch, restart_system_ai_health_process
):
    clock = {"now": 1_000.0}
    monkeypatch.setattr(web_server.time, "time", lambda: clock["now"])
    _fail(web_server.SYSTEM_AI_FAILURE_ALERT_THRESHOLD)
    alerts.clear()
    clock["now"] = 1_001.0
    web_server._note_system_ai_success()

    real_set_app_state = web_server.set_app_state
    failed_once = {"value": False}

    def fail_next_failure_timestamp(key, value):
        if (
            key == web_server.SYSTEM_AI_LAST_FAILURE_STATE_KEY
            and not failed_once["value"]
        ):
            failed_once["value"] = True
            raise sqlite3.OperationalError("one-shot timestamp write failure")
        return real_set_app_state(key, value)

    monkeypatch.setattr(web_server, "set_app_state", fail_next_failure_timestamp)
    clock["now"] = 4_000.0
    web_server._note_system_ai_failure("自动翻译", "503")
    restart_system_ai_health_process()

    clock["now"] = 4_601.0
    assert web_server._maybe_recover_stale_system_ai_incident() is False
    assert web_server.get_app_state(web_server.SYSTEM_AI_ALERTED_STATE_KEY) == "1"
    assert alerts == []


def test_dirty_failure_restart_and_fresh_success_waits_full_real_window(
    isolated_app_state_db, alerts, monkeypatch, restart_system_ai_health_process
):
    clock = {"now": 1_000.0}
    monkeypatch.setattr(web_server.time, "time", lambda: clock["now"])
    _fail(web_server.SYSTEM_AI_FAILURE_ALERT_THRESHOLD)
    alerts.clear()
    clock["now"] = 1_001.0
    web_server._note_system_ai_success()
    real_set_app_state = web_server.set_app_state
    failed_once = {"value": False}

    def fail_latest_failure(key, value):
        if key == web_server.SYSTEM_AI_LAST_FAILURE_STATE_KEY and not failed_once["value"]:
            failed_once["value"] = True
            raise sqlite3.OperationalError("one-shot timestamp write failure")
        return real_set_app_state(key, value)

    monkeypatch.setattr(web_server, "set_app_state", fail_latest_failure)
    clock["now"] = 4_000.0
    web_server._note_system_ai_failure("自动翻译", "503")
    restart_system_ai_health_process()
    clock["now"] = 4_001.0
    web_server._note_system_ai_success()

    clock["now"] = 4_601.0
    assert web_server._maybe_recover_stale_system_ai_incident() is False
    assert web_server.get_app_state(web_server.SYSTEM_AI_ALERTED_STATE_KEY) == "1"
    assert alerts == []


def test_peer_process_observes_failure_whose_db_timestamp_write_was_lost(
    isolated_app_state_db, alerts, monkeypatch, restart_system_ai_health_process
):
    clock = {"now": 1_000.0}
    monkeypatch.setattr(web_server.time, "time", lambda: clock["now"])
    _fail(web_server.SYSTEM_AI_FAILURE_ALERT_THRESHOLD)
    alerts.clear()
    clock["now"] = 1_001.0
    web_server._note_system_ai_success()
    process_a_health = dict(web_server._system_ai_health)

    restart_system_ai_health_process()
    real_set_app_state = web_server.set_app_state
    failed_once = {"value": False}

    def fail_peer_db_timestamp(key, value):
        if key == web_server.SYSTEM_AI_LAST_FAILURE_STATE_KEY and not failed_once["value"]:
            failed_once["value"] = True
            raise sqlite3.OperationalError("peer timestamp write failure")
        return real_set_app_state(key, value)

    monkeypatch.setattr(web_server, "set_app_state", fail_peer_db_timestamp)
    clock["now"] = 4_000.0
    web_server._note_system_ai_failure("自动翻译", "503")
    monkeypatch.setattr(web_server, "_system_ai_health", process_a_health)

    clock["now"] = 4_601.0
    assert web_server._maybe_recover_stale_system_ai_incident() is False
    assert web_server.get_app_state(web_server.SYSTEM_AI_ALERTED_STATE_KEY) == "1"
    assert alerts == []


def test_clean_persisted_success_recovers_after_restart_at_window_expiry(
    isolated_app_state_db, alerts, monkeypatch, restart_system_ai_health_process
):
    clock = {"now": 1_000.0}
    monkeypatch.setattr(web_server.time, "time", lambda: clock["now"])
    _fail(web_server.SYSTEM_AI_FAILURE_ALERT_THRESHOLD)
    alerts.clear()
    clock["now"] = 1_001.0
    web_server._note_system_ai_success()
    restart_system_ai_health_process()

    clock["now"] = 4_601.0
    assert web_server._maybe_recover_stale_system_ai_incident() is True
    assert [item["type"] for item in alerts] == ["system_ai_recovered"] * 2


def test_durable_closed_incident_clears_stale_in_memory_alerted(
    isolated_app_state_db, alerts, monkeypatch
):
    """Another process closed the incident to '0' first; this process must not
    keep a stale alerted=True that mutes the next real outage.

    The durable close (prior_state == '0') returns False (no recovery notice)
    but still resets the in-memory flag so a subsequent failure can alert again.
    """
    clock = {"now": 1_000.0}
    monkeypatch.setattr(web_server.time, "time", lambda: clock["now"])
    _fail(web_server.SYSTEM_AI_FAILURE_ALERT_THRESHOLD)
    alerts.clear()

    # Another process closed the incident durably; this process still holds a
    # stale alerted=True and a stale last_failure timestamp.
    web_server.set_app_state(web_server.SYSTEM_AI_ALERTED_STATE_KEY, "0")
    web_server.set_app_state(web_server.SYSTEM_AI_LAST_FAILURE_STATE_KEY, "1000")
    web_server.set_app_state(web_server.SYSTEM_AI_LAST_SUCCESS_STATE_KEY, "1001")
    with web_server._system_ai_health_lock:
        web_server._system_ai_health["alerted"] = True

    clock["now"] += web_server.SYSTEM_AI_RECOVERY_STABILITY_SECONDS + 1
    assert web_server._maybe_recover_stale_system_ai_incident() is False
    assert alerts == []
    assert web_server._system_ai_health["alerted"] is False

    # A subsequent failure can alert again because the stale flag was cleared.
    _fail(web_server.SYSTEM_AI_FAILURE_ALERT_THRESHOLD)
    assert [item["type"] for item in alerts] == ["system_ai_failed"] * 2


def test_real_success_after_clean_restart_arms_stable_recovery(
    isolated_app_state_db, alerts, monkeypatch, restart_system_ai_health_process
):
    clock = {"now": 1_000.0}
    monkeypatch.setattr(web_server.time, "time", lambda: clock["now"])
    _fail(web_server.SYSTEM_AI_FAILURE_ALERT_THRESHOLD)
    alerts.clear()
    restart_system_ai_health_process()

    clock["now"] = 1_001.0
    web_server._note_system_ai_success()
    clock["now"] = 4_601.0

    assert web_server._maybe_recover_stale_system_ai_incident() is True
    assert [item["type"] for item in alerts] == ["system_ai_recovered"] * 2


def test_later_successful_failure_write_clears_dirty_state(
    isolated_app_state_db, alerts, monkeypatch
):
    clock = {"now": 1_000.0}
    monkeypatch.setattr(web_server.time, "time", lambda: clock["now"])
    _fail(web_server.SYSTEM_AI_FAILURE_ALERT_THRESHOLD)
    real_set_app_state = web_server.set_app_state
    failed_once = {"value": False}

    def fail_next_failure_timestamp(key, value):
        if (
            key == web_server.SYSTEM_AI_LAST_FAILURE_STATE_KEY
            and not failed_once["value"]
        ):
            failed_once["value"] = True
            raise sqlite3.OperationalError("one-shot timestamp write failure")
        return real_set_app_state(key, value)

    monkeypatch.setattr(web_server, "set_app_state", fail_next_failure_timestamp)
    clock["now"] = 4_000.0
    web_server._note_system_ai_failure("自动翻译", "503")
    assert web_server._system_ai_health["failure_timestamp_dirty"] is True

    clock["now"] = 4_001.0
    web_server._note_system_ai_failure("自动翻译", "503")
    assert web_server._system_ai_health["failure_timestamp_dirty"] is False
    assert web_server.get_app_state(
        web_server.SYSTEM_AI_LAST_FAILURE_STATE_KEY
    ) == "4001.0"


def test_stable_recovery_serializes_memory_reset_with_new_failures(
    isolated_app_state_db, alerts, monkeypatch
):
    transaction_entered = threading.Event()
    release_transaction = threading.Event()

    def blocked_stable_complete(*args, **kwargs):
        transaction_entered.set()
        assert release_transaction.wait(timeout=2)
        return "1"

    monkeypatch.setattr(
        web_server,
        "complete_app_state_incident_if_stable",
        blocked_stable_complete,
        raising=False,
    )
    with web_server._system_ai_health_lock:
        web_server._system_ai_health.update(
            {
                "failures": 3,
                "alerted": True,
            }
        )

    recovery = threading.Thread(
        target=web_server._maybe_recover_stale_system_ai_incident
    )
    failure = threading.Thread(
        target=web_server._note_system_ai_failure,
        args=("自动翻译", "503"),
    )
    recovery.start()
    assert transaction_entered.wait(timeout=2)
    failure.start()
    stdlib_time.sleep(0.05)
    assert failure.is_alive()
    release_transaction.set()
    recovery.join(timeout=2)
    failure.join(timeout=2)

    assert not recovery.is_alive()
    assert not failure.is_alive()
    assert web_server._system_ai_health["failures"] == 1


def test_reset_holds_health_lock_until_durable_state_is_cleared(
    isolated_app_state_db, alerts, monkeypatch
):
    clock = {"now": 4_601.0}
    monkeypatch.setattr(web_server.time, "time", lambda: clock["now"])
    web_server.set_app_state(web_server.SYSTEM_AI_ALERTED_STATE_KEY, "1")
    web_server.set_app_state(web_server.SYSTEM_AI_LAST_FAILURE_STATE_KEY, "1000")
    web_server.set_app_state(web_server.SYSTEM_AI_LAST_SUCCESS_STATE_KEY, "1001")
    alerts.clear()
    with web_server._system_ai_health_lock:
        web_server._system_ai_health.update(
            {"failure_timestamp_dirty": True}
        )

    reset_write_entered = threading.Event()
    release_reset_write = threading.Event()
    stable_lock_state_ready = threading.Event()
    stable_lock_blocked = threading.Event()
    stable_finished = threading.Event()
    original_health_lock = web_server._system_ai_health_lock
    real_set_app_state = web_server.set_app_state
    real_atomic_set = getattr(models, "set_app_state_values", None)
    blocked_once = {"value": False}
    errors = []

    class TrackingLock:
        def __enter__(self):
            if threading.current_thread().name == "stable-during-reset":
                if original_health_lock.acquire(blocking=False):
                    stable_lock_state_ready.set()
                    return self
                stable_lock_blocked.set()
                stable_lock_state_ready.set()
            original_health_lock.acquire()
            return self

        def __exit__(self, exc_type, exc, traceback):
            original_health_lock.release()

        def locked(self):
            return original_health_lock.locked()

    def pause_first_reset_write():
        if not blocked_once["value"]:
            blocked_once["value"] = True
            reset_write_entered.set()
            assert release_reset_write.wait(timeout=2)

    def blocked_single_set(key, value):
        pause_first_reset_write()
        return real_set_app_state(key, value)

    def blocked_atomic_set(values):
        pause_first_reset_write()
        if real_atomic_set is not None:
            return real_atomic_set(values)
        for key, value in values.items():
            real_set_app_state(key, value)

    def run_reset():
        try:
            web_server._reset_system_ai_health()
        except Exception as exc:
            errors.append(exc)

    def run_stable_recovery():
        try:
            web_server._maybe_recover_stale_system_ai_incident()
        except Exception as exc:
            errors.append(exc)
        finally:
            stable_finished.set()

    monkeypatch.setattr(web_server, "_system_ai_health_lock", TrackingLock())
    monkeypatch.setattr(web_server, "set_app_state", blocked_single_set)
    monkeypatch.setattr(
        web_server, "set_app_state_values", blocked_atomic_set, raising=False
    )
    reset = threading.Thread(target=run_reset, name="health-reset")
    stable = threading.Thread(target=run_stable_recovery, name="stable-during-reset")

    reset.start()
    assert reset_write_entered.wait(timeout=2)
    stable.start()
    assert stable_lock_state_ready.wait(timeout=2)
    if not stable_lock_blocked.is_set():
        assert stable_finished.wait(timeout=2)
    release_reset_write.set()
    reset.join(timeout=2)
    stable.join(timeout=2)

    assert stable_lock_blocked.is_set()
    assert not reset.is_alive()
    assert not stable.is_alive()
    assert errors == []
    assert alerts == []
    assert web_server.get_app_state(web_server.SYSTEM_AI_ALERTED_STATE_KEY) == "0"


def test_failed_durable_reset_remains_fail_closed(
    isolated_app_state_db, alerts, monkeypatch
):
    clock = {"now": 4_601.0}
    monkeypatch.setattr(web_server.time, "time", lambda: clock["now"])
    web_server.set_app_state(web_server.SYSTEM_AI_ALERTED_STATE_KEY, "1")
    web_server.set_app_state(web_server.SYSTEM_AI_LAST_FAILURE_STATE_KEY, "1000")
    web_server.set_app_state(web_server.SYSTEM_AI_LAST_SUCCESS_STATE_KEY, "1001")
    with web_server._system_ai_health_lock:
        web_server._system_ai_health.update(
            {"failure_timestamp_dirty": True}
        )
    alerts.clear()

    real_set_app_state = web_server.set_app_state
    real_atomic_set = getattr(models, "set_app_state_values", None)
    failed_once = {"value": False}

    def fail_once_then(call):
        if not failed_once["value"]:
            failed_once["value"] = True
            raise sqlite3.OperationalError("one-shot reset failure")
        return call()

    monkeypatch.setattr(
        web_server,
        "set_app_state",
        lambda key, value: fail_once_then(lambda: real_set_app_state(key, value)),
    )
    monkeypatch.setattr(
        web_server,
        "set_app_state_values",
        lambda values: fail_once_then(
            lambda: real_atomic_set(values)
            if real_atomic_set is not None
            else [real_set_app_state(key, value) for key, value in values.items()]
        ),
        raising=False,
    )

    web_server._reset_system_ai_health()

    assert failed_once["value"] is True
    assert web_server._system_ai_health["failure_timestamp_dirty"] is True
    assert web_server._maybe_recover_stale_system_ai_incident() is False
    assert web_server.get_app_state(web_server.SYSTEM_AI_ALERTED_STATE_KEY) == "1"
    assert alerts == []


def test_reset_marker_floor_write_failure_keeps_current_floor_fail_closed(
    isolated_app_state_db, alerts, monkeypatch
):
    clock = {"now": 1_000.0}
    monkeypatch.setattr(web_server.time, "time", lambda: clock["now"])
    _fail(web_server.SYSTEM_AI_FAILURE_ALERT_THRESHOLD)
    alerts.clear()
    clock["now"] = 1_001.0
    web_server._note_system_ai_success()
    real_write_marker = web_server._write_system_ai_failure_marker
    failed_once = {"value": False}

    def fail_reset_floor_once(epoch):
        if not failed_once["value"]:
            failed_once["value"] = True
            raise OSError("one-shot reset marker write failure")
        return real_write_marker(epoch)

    monkeypatch.setattr(web_server, "_write_system_ai_failure_marker", fail_reset_floor_once)
    clock["now"] = 4_601.0
    web_server._reset_system_ai_health()

    assert web_server._maybe_recover_stale_system_ai_incident() is False
    assert web_server.get_app_state(web_server.SYSTEM_AI_ALERTED_STATE_KEY) == "1"
    assert alerts == []


def test_marker_clear_failure_after_db_reset_does_not_mute_new_incident(
    isolated_app_state_db, alerts, monkeypatch
):
    clock = {"now": 1_000.0}
    monkeypatch.setattr(web_server.time, "time", lambda: clock["now"])
    _fail(web_server.SYSTEM_AI_FAILURE_ALERT_THRESHOLD)
    alerts.clear()
    real_clear_marker = web_server._clear_system_ai_failure_marker
    failed_once = {"value": False}

    def fail_clear_once():
        if not failed_once["value"]:
            failed_once["value"] = True
            raise OSError("one-shot marker clear failure")
        return real_clear_marker()

    monkeypatch.setattr(web_server, "_clear_system_ai_failure_marker", fail_clear_once)
    clock["now"] = 2_000.0
    web_server._reset_system_ai_health()
    assert web_server.get_app_state(web_server.SYSTEM_AI_ALERTED_STATE_KEY) == "0"

    clock["now"] = 2_001.0
    _fail(web_server.SYSTEM_AI_FAILURE_ALERT_THRESHOLD)
    assert [item["type"] for item in alerts] == ["system_ai_failed"] * 2
    assert web_server.get_app_state(web_server.SYSTEM_AI_ALERTED_STATE_KEY) == "1"


def test_non_finite_failure_marker_fails_closed(
    isolated_app_state_db, alerts, monkeypatch
):
    clock = {"now": 1_000.0}
    monkeypatch.setattr(web_server.time, "time", lambda: clock["now"])
    _fail(web_server.SYSTEM_AI_FAILURE_ALERT_THRESHOLD)
    alerts.clear()
    clock["now"] = 1_001.0
    web_server._note_system_ai_success()
    Path(web_server.SYSTEM_AI_LAST_FAILURE_MARKER_FILE).write_text("nan", encoding="utf-8")

    clock["now"] = 4_601.0
    assert web_server._maybe_recover_stale_system_ai_incident() is False
    assert web_server.get_app_state(web_server.SYSTEM_AI_ALERTED_STATE_KEY) == "1"
    assert alerts == []


def test_reset_system_ai_health_clears_stability_timestamps(
    isolated_app_state_db, alerts
):
    web_server.set_app_state(web_server.SYSTEM_AI_LAST_FAILURE_STATE_KEY, "100")
    web_server.set_app_state(web_server.SYSTEM_AI_LAST_SUCCESS_STATE_KEY, "101")
    with web_server._system_ai_health_lock:
        web_server._system_ai_health.update(
            {
                "last_failure_at": 100.0,
                "last_success_at": 101.0,
                "failure_timestamp_dirty": True,
            }
        )

    web_server._reset_system_ai_health()

    assert web_server.get_app_state(web_server.SYSTEM_AI_LAST_FAILURE_STATE_KEY) == "0"
    assert web_server.get_app_state(web_server.SYSTEM_AI_LAST_SUCCESS_STATE_KEY) == "0"
    assert web_server._system_ai_health["last_failure_at"] == 0.0
    assert web_server._system_ai_health["last_success_at"] == 0.0
    assert web_server._system_ai_health["failure_timestamp_dirty"] is False


def test_daily_loop_runs_stable_recovery_in_its_own_try(monkeypatch):
    events = []
    sleeps = []

    class StopLoop(RuntimeError):
        pass

    def fail_summary():
        events.append("summary")
        raise RuntimeError("summary failed")

    def fail_recovery():
        events.append("recovery")
        raise RuntimeError("recovery failed")

    def stop_after_one_tick(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 2:
            raise StopLoop

    monkeypatch.setattr(web_server, "_send_daily_summaries", fail_summary)
    monkeypatch.setattr(
        web_server, "_maybe_recover_stale_system_ai_incident", fail_recovery,
        raising=False,
    )
    monkeypatch.setattr(web_server, "prune_access_log", lambda: events.append("prune"))
    monkeypatch.setattr(web_server.time, "sleep", stop_after_one_tick)

    with pytest.raises(StopLoop):
        web_server._daily_summary_loop()

    assert events == ["summary", "recovery", "prune"]
    assert sleeps == [15, 60]


def test_a_few_failures_stay_quiet(alerts):
    _fail(web_server.SYSTEM_AI_FAILURE_ALERT_THRESHOLD - 1)
    assert alerts == []


def test_the_threshold_alerts_every_admin_once(alerts):
    _fail(web_server.SYSTEM_AI_FAILURE_ALERT_THRESHOLD)

    assert [a["user_id"] for a in alerts] == [1, 3]
    assert all(a["type"] == "system_ai_failed" for a in alerts)
    assert "401 invalid api key" in alerts[0]["body"]
    assert "自动摘要" in alerts[0]["body"]

    # A provider that keeps failing must not keep notifying.
    _fail(20)
    assert len(alerts) == 2


def test_undelivered_system_ai_alert_is_retried(isolated_app_state_db, alerts, monkeypatch):
    deliveries = []

    def deliver(*args, **kwargs):
        deliveries.append((args, kwargs))
        return 0 if len(deliveries) == 1 else 1

    monkeypatch.setattr(web_server, "_notify_admins", deliver)

    _fail(web_server.SYSTEM_AI_FAILURE_ALERT_THRESHOLD)

    assert len(deliveries) == 1
    assert web_server.get_app_state(web_server.SYSTEM_AI_ALERTED_STATE_KEY) == "0"
    assert web_server.get_app_state(
        web_server.SYSTEM_AI_ALERT_LAST_NOTIFIED_STATE_KEY
    ) == "0"

    _fail(1)

    assert len(deliveries) == 2
    assert web_server.get_app_state(web_server.SYSTEM_AI_ALERTED_STATE_KEY) == "1"


def test_the_alert_names_every_affected_job(alerts):
    jobs = ("自动翻译", "标题精简", "每日摘要")
    for job in jobs:                       # one failure each…
        _fail(1, job=job)
    _fail(max(web_server.SYSTEM_AI_FAILURE_ALERT_THRESHOLD - len(jobs), 0), job=jobs[-1])

    body = alerts[0]["body"]
    for job in jobs:
        assert job in body


def test_a_success_before_the_threshold_resets_the_streak(alerts):
    _fail(web_server.SYSTEM_AI_FAILURE_ALERT_THRESHOLD - 1)
    web_server._note_system_ai_success()
    _fail(web_server.SYSTEM_AI_FAILURE_ALERT_THRESHOLD - 1)

    assert alerts == []


def test_rapid_successes_do_not_recover_before_the_stability_window(
    isolated_app_state_db, alerts, monkeypatch
):
    clock = {"now": 1_000.0}
    monkeypatch.setattr(web_server.time, "time", lambda: clock["now"])
    _fail(web_server.SYSTEM_AI_FAILURE_ALERT_THRESHOLD)
    alerts.clear()

    clock["now"] += 1
    _success(20)

    assert alerts == []
    assert web_server.get_app_state(web_server.SYSTEM_AI_ALERTED_STATE_KEY) == "1"


def test_recovery_is_announced_once_after_an_alert(
    isolated_app_state_db, alerts, monkeypatch
):
    clock = {"now": 1_000.0}
    monkeypatch.setattr(web_server.time, "time", lambda: clock["now"])
    _fail(web_server.SYSTEM_AI_FAILURE_ALERT_THRESHOLD)
    alerts.clear()

    assert _recover_after_stability(clock) is True
    assert [a["type"] for a in alerts] == ["system_ai_recovered"] * 2

    # Nothing further to announce while it keeps working.
    alerts.clear()
    web_server._note_system_ai_success()
    clock["now"] += web_server.SYSTEM_AI_RECOVERY_STABILITY_SECONDS
    assert web_server._maybe_recover_stale_system_ai_incident() is False
    assert alerts == []


def test_system_ai_recovery_requires_at_least_one_real_success(
    isolated_app_state_db, alerts, monkeypatch
):
    clock = {"now": 1_000.0}
    monkeypatch.setattr(web_server.time, "time", lambda: clock["now"])
    _fail(web_server.SYSTEM_AI_FAILURE_ALERT_THRESHOLD)
    alerts.clear()

    clock["now"] += web_server.SYSTEM_AI_RECOVERY_STABILITY_SECONDS + 1
    assert web_server._maybe_recover_stale_system_ai_incident() is False
    assert alerts == []

    web_server._note_system_ai_success()
    assert web_server._maybe_recover_stale_system_ai_incident() is True
    assert [item["type"] for item in alerts] == ["system_ai_recovered"] * 2


def test_cooldown_suppresses_a_second_system_ai_incident(
    isolated_app_state_db, alerts, monkeypatch
):
    clock = {"now": 1_000.0}
    monkeypatch.setattr(web_server.time, "time", lambda: clock["now"])
    _fail(web_server.SYSTEM_AI_FAILURE_ALERT_THRESHOLD)
    assert _recover_after_stability(clock) is True
    alerts.clear()

    _fail(web_server.SYSTEM_AI_FAILURE_ALERT_THRESHOLD)
    assert _recover_after_stability(clock) is True

    assert alerts == []


def test_cooldown_incident_stays_silent_and_closes_after_successes(
    isolated_app_state_db, alerts, monkeypatch
):
    """A muted incident must be state 2, then close without a recovery email.

    This catches both a regression that reuses state 1 during cooldown (which
    would repeat the outage notice) and one that leaves state 2 stuck active.
    """
    clock = {"now": 1_000.0}
    monkeypatch.setattr(web_server.time, "time", lambda: clock["now"])

    _fail(web_server.SYSTEM_AI_FAILURE_ALERT_THRESHOLD)
    assert _recover_after_stability(clock) is True
    alerts.clear()

    _fail(web_server.SYSTEM_AI_FAILURE_ALERT_THRESHOLD)
    assert web_server.get_app_state(web_server.SYSTEM_AI_ALERTED_STATE_KEY) == "2"
    assert alerts == []

    assert _recover_after_stability(clock) is True
    assert web_server.get_app_state(web_server.SYSTEM_AI_ALERTED_STATE_KEY) == "0"
    assert alerts == []


def test_system_ai_alert_can_be_sent_after_cooldown(
    isolated_app_state_db, alerts, monkeypatch
):
    clock = {"now": 1_000.0}
    monkeypatch.setattr(web_server.time, "time", lambda: clock["now"])
    _fail(web_server.SYSTEM_AI_FAILURE_ALERT_THRESHOLD)
    assert _recover_after_stability(clock) is True
    alerts.clear()

    clock["now"] += web_server.SYSTEM_AI_ALERT_COOLDOWN_SECONDS + 1
    _fail(web_server.SYSTEM_AI_FAILURE_ALERT_THRESHOLD)

    assert [item["type"] for item in alerts] == ["system_ai_failed"] * 2


def test_recovery_records_cooldown_before_email_delivery(
    isolated_app_state_db, alerts, monkeypatch
):
    clock = {"now": 1_000.0}
    monkeypatch.setattr(web_server.time, "time", lambda: clock["now"])
    _fail(web_server.SYSTEM_AI_FAILURE_ALERT_THRESHOLD)

    # Simulate the legacy post-delivery timestamp write being interrupted.
    monkeypatch.setattr(web_server, "_record_system_ai_notification_time", lambda: None)
    assert _recover_after_stability(clock) is True

    assert float(web_server.get_app_state(
        web_server.SYSTEM_AI_ALERT_LAST_NOTIFIED_STATE_KEY
    )) == clock["now"]


def test_a_new_outage_after_a_recovery_is_suppressed_during_cooldown(
    isolated_app_state_db, alerts, monkeypatch
):
    clock = {"now": 1_000.0}
    monkeypatch.setattr(web_server.time, "time", lambda: clock["now"])
    _fail(web_server.SYSTEM_AI_FAILURE_ALERT_THRESHOLD)
    assert _recover_after_stability(clock) is True
    alerts.clear()

    _fail(web_server.SYSTEM_AI_FAILURE_ALERT_THRESHOLD)
    assert web_server.get_app_state(web_server.SYSTEM_AI_ALERTED_STATE_KEY) == "2"
    assert alerts == []


def test_saving_a_new_system_ai_config_clears_the_muted_flag(alerts):
    _fail(web_server.SYSTEM_AI_FAILURE_ALERT_THRESHOLD)
    alerts.clear()

    # The admin swaps in another key; it is broken too and must alert again.
    web_server._reset_system_ai_health()
    _fail(web_server.SYSTEM_AI_FAILURE_ALERT_THRESHOLD)

    assert [a["type"] for a in alerts] == ["system_ai_failed"] * 2


def test_the_alert_never_raises_into_the_calling_job(alerts, monkeypatch):
    def boom():
        raise RuntimeError("db gone")
    monkeypatch.setattr(web_server, "list_users", boom)

    _fail(web_server.SYSTEM_AI_FAILURE_ALERT_THRESHOLD)  # must not raise


def test_system_ai_failure_redacts_secrets_from_state_alert_and_log(
    alerts,
    monkeypatch,
    capsys,
):
    configured_secret = "random-system-credential-4d91b7"
    bearer_secret = "bearer-health-secret"
    parameter_secret = "parameter-health-secret"
    monkeypatch.setattr(
        web_server,
        "get_system_ai_config",
        lambda: {"api_key": configured_secret},
    )
    error = (
        f"provider echoed {configured_secret}; "
        f"Authorization: Bearer {bearer_secret}; "
        f"api_key={parameter_secret}"
    )

    _fail(web_server.SYSTEM_AI_FAILURE_ALERT_THRESHOLD, error=error)

    serialized = repr(web_server._system_ai_health) + repr(alerts) + capsys.readouterr().out
    assert "[redacted]" in serialized
    for secret in (configured_secret, bearer_secret, parameter_secret):
        assert secret not in serialized


def test_auto_summary_redacts_secret_before_persistence_and_logging(
    alerts,
    monkeypatch,
    capsys,
):
    configured_secret = "random-background-credential-83c2e1"
    captured = []
    monkeypatch.setattr(
        web_server,
        "get_system_ai_config",
        lambda: {"api_key": configured_secret},
    )
    monkeypatch.setattr(
        web_server,
        "_get_auto_summary_users",
        lambda: [{"user_id": 1}],
    )
    monkeypatch.setattr(
        web_server,
        "_fetch_unsummarized_articles",
        lambda _limit: [{"id": 99, "title": "secret regression"}],
    )
    monkeypatch.setattr(
        web_server,
        "_generate_article_summary",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError(f"provider echoed {configured_secret}")
        ),
    )
    monkeypatch.setattr(
        web_server,
        "_save_ai_result",
        lambda article_id, **kwargs: captured.append((article_id, kwargs)) or True,
    )

    web_server._run_auto_summary_once()

    serialized = repr(captured) + capsys.readouterr().out
    assert configured_secret not in serialized
    assert "[redacted]" in serialized


def test_generation_failure_feeds_the_streak(news_db_free, monkeypatch, alerts):
    class BoomService:
        def __init__(self, **kwargs):
            pass

        def batch_digest_signals(self, articles):
            raise RuntimeError("502 upstream")

    monkeypatch.setattr(web_server, "get_system_ai_config",
                        lambda: {"enabled": True, "api_key": "k", "endpoint": "e", "model": "m"})
    monkeypatch.setattr(web_server, "_fetch_articles_by_date",
                        lambda date_str, include_shared_summary=False: [{"id": 1, "title": "t"}])
    monkeypatch.setattr(web_server, "_dedup_articles", lambda articles: articles)
    monkeypatch.setattr(web_server, "AIService", BoomService)

    for _ in range(web_server.SYSTEM_AI_FAILURE_ALERT_THRESHOLD):
        assert web_server._generate_daily_summary_global("2026-07-10") is None

    assert [a["type"] for a in alerts] == ["system_ai_failed"] * 2
    assert "502 upstream" in alerts[0]["body"]


@pytest.fixture
def news_db_free(monkeypatch, tmp_path):
    """Point NEWS_DB at a path with no database, so the cache helpers no-op."""
    monkeypatch.setattr(web_server, "NEWS_DB", str(tmp_path / "absent.db"))


def test_admin_connection_test_arms_time_based_recovery_without_waiting_for_a_job(
    isolated_app_state_db, alerts, monkeypatch
):
    from flask import g

    clock = {"now": 1_000.0}
    monkeypatch.setattr(web_server.time, "time", lambda: clock["now"])
    _fail(web_server.SYSTEM_AI_FAILURE_ALERT_THRESHOLD)
    alerts.clear()

    monkeypatch.setattr(web_server, "get_system_ai_config", lambda: {"enabled": True})
    monkeypatch.setattr(web_server, "_run_ai_connection_test", lambda config: ({"ok": True}, 200))
    clock["now"] += 1
    with web_server.app.test_request_context("/admin/system-ai-config/test", method="POST"):
        g.user_id = 1
        g.user_role = "admin"
        web_server.admin_system_ai_test_connection.__wrapped__()

    assert alerts == []
    clock["now"] += web_server.SYSTEM_AI_RECOVERY_STABILITY_SECONDS
    assert web_server._maybe_recover_stale_system_ai_incident() is True

    assert [a["type"] for a in alerts] == ["system_ai_recovered"] * 2


def test_a_failing_admin_connection_test_does_not_push_the_streak(alerts, monkeypatch):
    from flask import g

    _fail(web_server.SYSTEM_AI_FAILURE_ALERT_THRESHOLD - 1)
    monkeypatch.setattr(web_server, "get_system_ai_config", lambda: {"enabled": True})
    monkeypatch.setattr(web_server, "_run_ai_connection_test",
                        lambda config: ({"error": "401"}, 400))
    with web_server.app.test_request_context("/admin/system-ai-config/test", method="POST"):
        g.user_id = 1
        g.user_role = "admin"
        web_server.admin_system_ai_test_connection.__wrapped__()

    assert alerts == []


def test_the_evening_retry_chain_alone_reaches_the_threshold(news_db_free, monkeypatch, alerts):
    """A day with no pending article work still reports the outage.

    The article jobs only call the AI when they have something to process, so on
    a quiet day the daily-summary chain is the only caller: 21:00 plus three
    retries, four attempts. The threshold has to sit under that or the outage
    would be invisible until the 21:30 daily-summary alert.
    """
    assert web_server.SYSTEM_AI_FAILURE_ALERT_THRESHOLD <= 4

    class BoomService:
        def __init__(self, **kwargs):
            pass

        def batch_digest_signals(self, articles):
            raise RuntimeError("401 invalid api key")

    monkeypatch.setattr(web_server, "get_system_ai_config",
                        lambda: {"enabled": True, "api_key": "k", "endpoint": "e", "model": "m"})
    monkeypatch.setattr(web_server, "_fetch_articles_by_date",
                        lambda date_str, include_shared_summary=False: [{"id": 1, "title": "t"}])
    monkeypatch.setattr(web_server, "_dedup_articles", lambda articles: articles)
    monkeypatch.setattr(web_server, "AIService", BoomService)

    attempts = 1 + web_server.DAILY_SUMMARY_MAX_RETRIES
    for _ in range(attempts):
        web_server._generate_daily_summary_global("2026-07-10")

    assert [a["type"] for a in alerts] == ["system_ai_failed"] * 2
    assert "每日摘要" in alerts[0]["body"]


# ─── A cleared/disabled config, without probing anything ───────────────


@pytest.fixture
def admin_with_auto_jobs(tmp_path, monkeypatch):
    """An admin with a background AI job switched on, on a real settings DB."""
    import uuid

    import models

    db_path = tmp_path / f"auto-config-{uuid.uuid4().hex}.db"
    old_db_file = models.DB_FILE
    models.close_db()
    models.DB_FILE = db_path
    try:
        models.get_db()
        admin = models.create_user("admin@example.com", "pw", "A", role="admin")["id"]
        models.set_user_settings(admin, auto_summary_enabled=1)
        monkeypatch.setattr(web_server, "get_db", models.get_db)
        yield admin
    finally:
        models.close_db()
        models.DB_FILE = old_db_file


def test_an_enabled_job_with_no_usable_system_ai_alerts(admin_with_auto_jobs, alerts, monkeypatch):
    # The jobs skip this state without calling the provider, so the
    # misconfiguration itself has to be what gets counted.
    monkeypatch.setattr(web_server, "get_system_ai_config", lambda: {"enabled": 0, "api_key": ""})

    for _ in range(web_server.SYSTEM_AI_FAILURE_ALERT_THRESHOLD):
        assert web_server._system_auto_config("auto_summary_enabled") is None

    assert [a["type"] for a in alerts] == ["system_ai_failed"] * 2
    assert "未配置或未启用" in alerts[0]["body"]
    assert "服务端 API 配置" in alerts[0]["body"]


def test_a_key_that_is_present_but_empty_counts_the_same(admin_with_auto_jobs, alerts, monkeypatch):
    monkeypatch.setattr(web_server, "get_system_ai_config",
                        lambda: {"enabled": 1, "api_key": "", "endpoint": "e", "model": "m"})

    for _ in range(web_server.SYSTEM_AI_FAILURE_ALERT_THRESHOLD):
        web_server._system_auto_config("auto_summary_enabled")

    assert [a["type"] for a in alerts] == ["system_ai_failed"] * 2


def test_no_enabled_job_means_no_alert_however_broken_the_config(tmp_path, alerts, monkeypatch):
    import uuid

    import models

    db_path = tmp_path / f"auto-config-off-{uuid.uuid4().hex}.db"
    old_db_file = models.DB_FILE
    models.close_db()
    models.DB_FILE = db_path
    try:
        models.get_db()
        models.create_user("admin@example.com", "pw", "A", role="admin")
        monkeypatch.setattr(web_server, "get_db", models.get_db)
        monkeypatch.setattr(web_server, "get_system_ai_config", lambda: None)

        for _ in range(10):
            assert web_server._system_auto_config("auto_summary_enabled") is None

        assert alerts == []
    finally:
        models.close_db()
        models.DB_FILE = old_db_file


def test_a_usable_config_is_returned_and_counts_nothing(admin_with_auto_jobs, alerts, monkeypatch):
    monkeypatch.setattr(web_server, "get_system_ai_config", lambda: {
        "enabled": 1, "api_key": "k", "endpoint": "e", "model": "m", "provider_type": "openai",
    })

    config = web_server._system_auto_config("auto_summary_enabled")

    assert config["api_key"] == "k"
    assert alerts == []


def test_fixing_the_config_sends_the_recovery_notice(admin_with_auto_jobs, alerts, monkeypatch):
    clock = {"now": 1_000.0}
    monkeypatch.setattr(web_server.time, "time", lambda: clock["now"])
    monkeypatch.setattr(web_server, "get_system_ai_config", lambda: {"enabled": 0, "api_key": ""})
    for _ in range(web_server.SYSTEM_AI_FAILURE_ALERT_THRESHOLD):
        web_server._system_auto_config("auto_summary_enabled")
    alerts.clear()

    # The admin saves a working config and the next job call succeeds.
    assert _recover_after_stability(clock) is True

    assert [a["type"] for a in alerts] == ["system_ai_recovered"] * 2


# ─── One outage, one alert — across restarts too ───────────────────────


def test_a_restart_mid_outage_does_not_re_alert(admin_with_auto_jobs, alerts, monkeypatch):
    monkeypatch.setattr(web_server, "get_system_ai_config", lambda: {"enabled": 0, "api_key": ""})
    for _ in range(web_server.SYSTEM_AI_FAILURE_ALERT_THRESHOLD):
        web_server._system_auto_config("auto_summary_enabled")
    assert len(alerts) == 2

    # Restart: the in-memory streak is gone, the outage and the settings DB are not.
    web_server._system_ai_health.update(
        {"failures": 0, "alerted": False, "last_error": "", "jobs": []})
    for _ in range(3 * web_server.SYSTEM_AI_FAILURE_ALERT_THRESHOLD):
        web_server._system_auto_config("auto_summary_enabled")

    assert len(alerts) == 2   # still the one alert from before the restart


def test_after_recovery_a_new_outage_is_suppressed_across_a_restart(admin_with_auto_jobs, alerts,
                                                                     monkeypatch):
    clock = {"now": 1_000.0}
    monkeypatch.setattr(web_server.time, "time", lambda: clock["now"])
    monkeypatch.setattr(web_server, "get_system_ai_config", lambda: {"enabled": 0, "api_key": ""})
    for _ in range(web_server.SYSTEM_AI_FAILURE_ALERT_THRESHOLD):
        web_server._system_auto_config("auto_summary_enabled")
    assert _recover_after_stability(clock) is True  # fixed and stable
    alerts.clear()

    web_server._system_ai_health.update(
        {"failures": 0, "alerted": False, "last_error": "", "jobs": []})   # restart
    for _ in range(web_server.SYSTEM_AI_FAILURE_ALERT_THRESHOLD):
        web_server._system_auto_config("auto_summary_enabled")

    assert alerts == []


def test_the_recovery_notice_is_owed_even_if_the_alert_predates_the_restart(
        admin_with_auto_jobs, alerts, monkeypatch):
    clock = {"now": 1_000.0}
    monkeypatch.setattr(web_server.time, "time", lambda: clock["now"])
    monkeypatch.setattr(web_server, "get_system_ai_config", lambda: {"enabled": 0, "api_key": ""})
    for _ in range(web_server.SYSTEM_AI_FAILURE_ALERT_THRESHOLD):
        web_server._system_auto_config("auto_summary_enabled")
    alerts.clear()
    web_server._system_ai_health.update(
        {"failures": 0, "alerted": False, "last_error": "", "jobs": []})   # restart

    assert _recover_after_stability(clock) is True

    assert [a["type"] for a in alerts] == ["system_ai_recovered"] * 2


# ─── Only real provider calls move the health signal ───────────────────


def test_a_cached_result_does_not_count_as_the_ai_working(alerts, monkeypatch):
    """The reported flapping: 失败/恢复 pairs every 30 seconds.

    A cached summary finishes a job iteration without touching the AI. Counting
    that as a success cleared the outage the failing calls had just reported, so
    the admin got an alert, a recovery notice, an alert…
    """
    monkeypatch.setattr(web_server, "_get_ai_result",
                        lambda article_id: {"summary": "cached"})

    web_server._note_system_ai_failure("自动摘要", "AI API HTTP 401: Invalid API key.")
    web_server._note_system_ai_failure("自动翻译", "AI API HTTP 401: Invalid API key.")

    summary, cached = web_server._generate_article_summary(
        1, {"api_key": "k", "endpoint": "e", "model": "m"})
    assert (summary, cached) == ("cached", True)
    assert alerts == []          # nothing was called, so nothing recovered

    web_server._note_system_ai_failure("标题精简", "AI API HTTP 401: Invalid API key.")

    assert [a["type"] for a in alerts] == ["system_ai_failed"] * 2
    # And the outage stays reported — no recovery notice from further cache hits.
    alerts.clear()
    web_server._generate_article_summary(1, {"api_key": "k", "endpoint": "e", "model": "m"})
    assert alerts == []


def test_a_real_call_that_returns_arms_stable_recovery(
    isolated_app_state_db, alerts, monkeypatch
):
    clock = {"now": 1_000.0}
    monkeypatch.setattr(web_server.time, "time", lambda: clock["now"])
    class FakeService:
        def __init__(self, **kwargs):
            pass

        def summarize(self, article_text="", title=""):
            return "fresh summary"

    monkeypatch.setattr(web_server, "_get_ai_result", lambda article_id: None)
    monkeypatch.setattr(web_server, "_fetch_article_body",
                        lambda article_id: {"body_html": "b", "title": "t"})
    monkeypatch.setattr(web_server, "_save_ai_result", lambda *args, **kwargs: None)
    monkeypatch.setattr(web_server, "AIService", FakeService)

    for _ in range(web_server.SYSTEM_AI_FAILURE_ALERT_THRESHOLD):
        web_server._note_system_ai_failure("自动摘要", "AI API HTTP 401: Invalid API key.")
    alerts.clear()

    clock["now"] += 1
    summary, cached = web_server._generate_article_summary(
        1, {"api_key": "k", "endpoint": "e", "model": "m"})
    assert (summary, cached) == ("fresh summary", False)
    assert alerts == []

    clock["now"] += web_server.SYSTEM_AI_RECOVERY_STABILITY_SECONDS
    assert web_server._maybe_recover_stale_system_ai_incident() is True
    assert [a["type"] for a in alerts] == ["system_ai_recovered"] * 2


def test_a_failing_call_is_recorded_from_the_call_site(alerts, monkeypatch):
    class BoomService:
        def __init__(self, **kwargs):
            pass

        def summarize(self, article_text="", title=""):
            raise RuntimeError("AI API HTTP 401: Invalid API key.")

    monkeypatch.setattr(web_server, "_get_ai_result", lambda article_id: None)
    monkeypatch.setattr(web_server, "_fetch_article_body",
                        lambda article_id: {"body_html": "b", "title": "t"})
    monkeypatch.setattr(web_server, "AIService", BoomService)

    for _ in range(web_server.SYSTEM_AI_FAILURE_ALERT_THRESHOLD):
        with pytest.raises(RuntimeError):
            web_server._generate_article_summary(
                1, {"api_key": "k", "endpoint": "e", "model": "m"})

    assert [a["type"] for a in alerts] == ["system_ai_failed"] * 2
    assert "401" in alerts[0]["body"]
    assert "自动摘要" in alerts[0]["body"]


# ─── Every server-side job runs on the server API ──────────────────────


def test_source_classification_uses_the_server_api(monkeypatch):
    monkeypatch.setattr(web_server, "get_system_ai_config", lambda: {
        "enabled": 1, "api_key": "server-key", "endpoint": "e", "model": "m",
        "provider_type": "openai",
    })

    configs = web_server._get_source_classification_users()

    assert len(configs) == 1
    assert configs[0]["api_key"] == "server-key"


def test_source_classification_stops_when_the_server_api_is_unusable(monkeypatch):
    for config in (None, {"enabled": 0, "api_key": "k"}, {"enabled": 1, "api_key": ""}):
        monkeypatch.setattr(web_server, "get_system_ai_config", lambda config=config: config)
        assert web_server._get_source_classification_users() == []


def test_admin_triggered_classification_also_uses_the_server_api(monkeypatch):
    """The 管理员设置 → 订阅源 buttons must not spend whoever clicked's own key.

    Source labels are site-wide, so both the synchronous batch and the
    background job run on the server API, and refuse with a message pointing at
    it when it isn't configured.
    """
    from flask import g

    used = []
    monkeypatch.setattr(web_server, "_get_news_db", lambda: object())
    monkeypatch.setattr(web_server, "get_ai_config",
                        lambda user_id: {"api_key": "personal", "enabled": 1, "endpoint": "e",
                                         "model": "m"})
    monkeypatch.setattr(web_server, "_classify_source_batch",
                        lambda config, limit=50, force=False: (
                            used.append(config["api_key"]),
                            {"processed": [], "failed": [], "remaining": 0},
                        )[1])
    monkeypatch.setattr(web_server, "get_system_ai_config", lambda: {
        "enabled": 1, "api_key": "server-key", "endpoint": "e", "model": "m",
        "provider_type": "openai",
    })

    with web_server.app.test_request_context("/sources/classify", method="POST", json={}):
        g.user_id = 1
        g.user_role = "admin"
        web_server.classify_sources.__wrapped__()
    assert used == ["server-key"]

    # No server API configured: refuse, and say where to configure it.
    monkeypatch.setattr(web_server, "get_system_ai_config", lambda: None)
    with web_server.app.test_request_context("/sources/classify", method="POST", json={}):
        g.user_id = 1
        g.user_role = "admin"
        body, status = web_server.classify_sources.__wrapped__()
    assert status == 400
    assert "服务端 API" in body.get_json()["error"]
    assert used == ["server-key"]        # the personal key was never reached
