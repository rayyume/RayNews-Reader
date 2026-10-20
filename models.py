"""RayNews data models — user auth, favorites, server AI configuration and preferences."""

import sqlite3
import json
import bcrypt
from datetime import datetime, timedelta
from pathlib import Path
import math
import os
import secrets
import threading
import time

DATA_DIR = Path(os.environ.get("DATA_DIR", "/app/data"))
DB_FILE = DATA_DIR / "raynews.db"

# ─── Schema ───────────────────────────────────────────────────

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS users (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    email       TEXT    NOT NULL UNIQUE,
    password    TEXT    NOT NULL,
    nickname    TEXT    NOT NULL DEFAULT '',
    role        TEXT    NOT NULL DEFAULT 'user'
                        CHECK(role IN ('user', 'admin')),
    avatar_url  TEXT    NOT NULL DEFAULT '',
    token_version INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS favorites (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    article_id  INTEGER NOT NULL,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE(user_id, article_id)
);

CREATE TABLE IF NOT EXISTS user_settings (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id                 INTEGER NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
    daily_summary_enabled   INTEGER NOT NULL DEFAULT 0,
    -- The in-app copy of the daily summary is on by default; the email copy
    -- (daily_summary_enabled above) stays opt-in because it needs an address.
    daily_summary_inapp_enabled INTEGER NOT NULL DEFAULT 1,
    theme_preference        TEXT    NOT NULL DEFAULT 'system',
    notification_config     TEXT    NOT NULL DEFAULT '{}',
    view_summary INTEGER NOT NULL DEFAULT 0,
    view_title_translation INTEGER NOT NULL DEFAULT 0,
    view_translation INTEGER NOT NULL DEFAULT 0,
    view_title_shortening INTEGER NOT NULL DEFAULT 0,
    view_daily_digest INTEGER NOT NULL DEFAULT 0,
    view_tts INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS user_access_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    accessed_at TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_access_log_user_time ON user_access_log(user_id, accessed_at);

CREATE TABLE IF NOT EXISTS invitation_codes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    code        TEXT    NOT NULL UNIQUE,
    email       TEXT    NOT NULL,
    used        INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS login_failures (
    client_ip     TEXT NOT NULL,
    login         TEXT NOT NULL,
    failure_count INTEGER NOT NULL DEFAULT 0,
    locked_until  REAL NOT NULL DEFAULT 0,
    updated_at    REAL NOT NULL,
    PRIMARY KEY (client_ip, login)
);

CREATE TABLE IF NOT EXISTS register_attempts (
    client_ip     TEXT PRIMARY KEY,
    failure_count INTEGER NOT NULL DEFAULT 0,
    locked_until  REAL NOT NULL DEFAULT 0,
    updated_at    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS invite_request_limits (
    email             TEXT PRIMARY KEY,
    last_success_at   REAL,
    reservation_token TEXT,
    reserved_at       REAL
);

CREATE TABLE IF NOT EXISTS system_ai_config (
    id          INTEGER PRIMARY KEY CHECK(id = 1),
    provider    TEXT    NOT NULL DEFAULT 'openai',
    api_key     TEXT    NOT NULL DEFAULT '',
    endpoint    TEXT    NOT NULL DEFAULT 'https://api.openai.com/v1',
    model       TEXT    NOT NULL DEFAULT 'gpt-4o-mini',
    provider_type TEXT NOT NULL DEFAULT 'openai'
                    CHECK(provider_type IN ('openai', 'claude')),
    enabled     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS notifications (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    type        TEXT    NOT NULL DEFAULT 'general',
    title       TEXT    NOT NULL,
    body        TEXT    NOT NULL DEFAULT '',
    format      TEXT    NOT NULL DEFAULT 'plain',
    created_at  TEXT    NOT NULL,
    read_at     TEXT
);

CREATE INDEX IF NOT EXISTS idx_notifications_user_time ON notifications(user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS broadcast_publications (
    broadcast_id TEXT    PRIMARY KEY,
    title        TEXT    NOT NULL DEFAULT '',
    recipients   INTEGER NOT NULL DEFAULT 0,
    email        INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT    NOT NULL
);

-- Small, long-lived process state that must survive a restart. Notification
-- de-duplication lives here: an "already told the admins" flag kept in memory
-- would re-fire on every container restart, turning one outage into a stream of
-- identical alerts.
CREATE TABLE IF NOT EXISTS app_state (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);"""

# ─── Connection ───────────────────────────────────────────────

_db_local = threading.local()
_db_init_lock = threading.Lock()
_initialized_db_paths: set[str] = set()


def _table_columns(db: sqlite3.Connection, table: str) -> set[str]:
    return {
        str(row[1])
        for row in db.execute(f"PRAGMA table_info({table})").fetchall()
    }


def _add_column_if_missing(
    db: sqlite3.Connection,
    table: str,
    column: str,
    definition: str,
) -> None:
    """Add an app-DB column without hiding unrelated operational failures."""
    if column in _table_columns(db, table):
        return
    try:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    except sqlite3.OperationalError as exc:
        # Only a concurrent identical migration may be accepted, and only
        # after the schema itself confirms that the requested column exists.
        refreshed = _table_columns(db, table)
        if (
            "duplicate column name" in str(exc).lower()
            and column in refreshed
        ):
            return
        raise


def _initialize_db(db: sqlite3.Connection) -> None:
    """Create and migrate the model database on a newly opened connection."""
    db.executescript(SCHEMA_SQL)
    for table, column, definition in (
        (
            "user_settings",
            "theme_preference",
            "TEXT NOT NULL DEFAULT 'system'",
        ),
        # ALTER TABLE ADD COLUMN backfills every existing row with the default,
        # so accounts that predate in-app summary delivery get it switched on
        # without a separate backfill statement.
        (
            "user_settings",
            "daily_summary_inapp_enabled",
            "INTEGER NOT NULL DEFAULT 1",
        ),
        ("users", "visit_count", "INTEGER NOT NULL DEFAULT 0"),
        ("users", "last_seen_at", "TEXT NOT NULL DEFAULT ''"),
        ("users", "token_version", "INTEGER NOT NULL DEFAULT 0"),
        # notifications gained a body format ('plain'|'markdown') after the
        # table already shipped, so existing DBs need the column backfilled.
        ("notifications", "format", "TEXT NOT NULL DEFAULT 'plain'"),
    ):
        _add_column_if_missing(db, table, column, definition)
    # Registration now requires a username, stored in the nickname column.
    # Backfill existing accounts that predate this requirement: keep their
    # nickname if they set one, otherwise fall back to their email.
    db.execute("UPDATE users SET nickname = email WHERE nickname = ''")
    # The "preview" role has been retired: registration now grants "user"
    # directly, and "who's pending" is tracked via unused invitation_codes
    # instead. Promote any leftover preview accounts from before this change.
    # (The role CHECK constraint above only drops 'preview' for brand new
    # databases — existing databases keep their original, more permissive
    # constraint rather than paying for a table-rebuild migration just to
    # tighten it; the application layer already never assigns 'preview'.)
    db.execute("UPDATE users SET role = 'user' WHERE role = 'preview'")
    db.commit()
    from ai_access import migrate_accounts
    migrate_accounts(db, SCHEMA_SQL)


def close_db() -> None:
    """Close the model connection owned by the calling thread, if any."""
    db = getattr(_db_local, "connection", None)
    if db is not None:
        db.close()
    _db_local.connection = None
    _db_local.path = None


def get_db() -> sqlite3.Connection:
    """Return a SQLite connection owned by the calling thread.

    sqlite3 connections may not safely execute statements or share transaction
    state across Flask request threads. Keeping one connection per thread lets
    WAL serialize writers at SQLite's transaction boundary instead of letting
    one request commit or corrupt another request's work.
    """
    DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    db_path = str(DB_FILE.resolve())
    db = getattr(_db_local, "connection", None)
    if db is not None and getattr(_db_local, "path", None) == db_path:
        return db
    if db is not None:
        close_db()

    db = sqlite3.connect(db_path, timeout=30)
    db.row_factory = sqlite3.Row
    try:
        # Foreign-key enforcement and busy_timeout are per connection.
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=30000")
        # The schema/migrations perform writes. Run them once per database
        # path in this process so independent request connections cannot race
        # them or pay migration cost on every request thread.
        with _db_init_lock:
            if db_path not in _initialized_db_paths:
                db.execute("PRAGMA journal_mode=WAL")
                _initialize_db(db)
                _initialized_db_paths.add(db_path)
    except Exception:
        db.close()
        raise
    _db_local.connection = db
    _db_local.path = db_path
    return db


# ─── User helpers ─────────────────────────────────────────────

BCRYPT_MAX_PASSWORD_BYTES = 72


def password_within_bcrypt_limit(password: str) -> bool:
    return len(password.encode("utf-8")) <= BCRYPT_MAX_PASSWORD_BYTES


def hash_password(password: str) -> str:
    password_bytes = password.encode("utf-8")
    if len(password_bytes) > BCRYPT_MAX_PASSWORD_BYTES:
        raise ValueError("password must be at most 72 UTF-8 bytes")
    return bcrypt.hashpw(password_bytes, bcrypt.gensalt()).decode()


def verify_password(password: str, hashed: str) -> bool:
    # bcrypt versions before 5.0 silently ignored bytes after byte 72. Keep
    # those existing accounts usable while rejecting overlong new passwords.
    password_bytes = password.encode("utf-8")[:BCRYPT_MAX_PASSWORD_BYTES]
    return bcrypt.checkpw(password_bytes, hashed.encode())


def create_user(email: str, password: str, nickname: str = "",
                role: str = "user") -> dict | None:
    db = get_db()
    # Check username uniqueness if provided
    if nickname:
        existing = db.execute(
            "SELECT 1 FROM users WHERE nickname = ? AND nickname != ''",
            (nickname,),
        ).fetchone()
        if existing:
            return None  # duplicate username
    try:
        cur = db.execute(
            "INSERT INTO users (email, password, nickname, role) VALUES (?, ?, ?, ?)",
            (email, hash_password(password), nickname, role),
        )
        db.commit()
        return get_user(cur.lastrowid)
    except sqlite3.IntegrityError:
        return None  # duplicate email


def create_registered_user(
    email: str,
    password: str,
    nickname: str,
    invite_code: str = "",
) -> tuple[dict | None, bool]:
    """Atomically create a registered user and assign the initial admin.

    The boolean is true only when the returned user is the initial admin.
    A dedicated write transaction serializes the empty-database decision, so
    concurrent unauthenticated registrations cannot both become admins (or
    leave a second account behind without a valid invite).
    """
    # Reject known-invalid requests before paying bcrypt's CPU cost. These
    # checks are repeated under the write lock below because another request
    # can change users or invitations while the password is being hashed.
    preflight_db = get_db()
    if preflight_db.execute(
        "SELECT 1 FROM users WHERE email = ?",
        (email,),
    ).fetchone():
        return None, False
    if nickname and preflight_db.execute(
        "SELECT 1 FROM users WHERE nickname = ? AND nickname != ''",
        (nickname,),
    ).fetchone():
        return None, False
    has_users = preflight_db.execute("SELECT 1 FROM users LIMIT 1").fetchone()
    if has_users and (
        not invite_code
        or preflight_db.execute(
            "SELECT 1 FROM invitation_codes "
            "WHERE code = ? AND email = ? AND used = 0",
            (invite_code, email),
        ).fetchone() is None
    ):
        return None, False

    password_hash = hash_password(password)
    db_path = str(DB_FILE.resolve())
    db = sqlite3.connect(db_path, timeout=30)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=30000")
        db.execute("BEGIN IMMEDIATE")

        is_initial_admin = (
            db.execute("SELECT 1 FROM users LIMIT 1").fetchone() is None
        )
        role = "admin" if is_initial_admin else "user"

        if db.execute(
            "SELECT 1 FROM users WHERE email = ?",
            (email,),
        ).fetchone():
            db.rollback()
            return None, False

        if not is_initial_admin:
            if not invite_code:
                db.rollback()
                return None, False
            invitation = db.execute(
                "SELECT 1 FROM invitation_codes "
                "WHERE code = ? AND email = ? AND used = 0",
                (invite_code, email),
            ).fetchone()
            if invitation is None:
                db.rollback()
                return None, False

        if nickname and db.execute(
            "SELECT 1 FROM users WHERE nickname = ? AND nickname != ''",
            (nickname,),
        ).fetchone():
            db.rollback()
            return None, False

        cur = db.execute(
            "INSERT INTO users (email, password, nickname, role) "
            "VALUES (?, ?, ?, ?)",
            (email, password_hash, nickname, role),
        )
        if not is_initial_admin:
            used = db.execute(
                "UPDATE invitation_codes SET used = 1 "
                "WHERE code = ? AND email = ? AND used = 0",
                (invite_code, email),
            )
            if used.rowcount != 1:
                db.rollback()
                return None, False

        row = db.execute(
            "SELECT id, email, nickname, role, avatar_url, created_at, "
            "visit_count, last_seen_at, token_version FROM users WHERE id = ?",
            (cur.lastrowid,),
        ).fetchone()
        db.commit()
        return dict(row), is_initial_admin
    except sqlite3.IntegrityError:
        db.rollback()
        return None, False
    finally:
        db.close()


def get_user(user_id: int) -> dict | None:
    db = get_db()
    row = db.execute(
        "SELECT id, email, nickname, role, avatar_url, created_at, "
        "visit_count, last_seen_at, token_version FROM users WHERE id = ?",
        (user_id,),
    ).fetchone()
    return dict(row) if row else None


def get_user_by_email(email: str) -> dict | None:
    db = get_db()
    row = db.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    return dict(row) if row else None


def get_user_by_username(username: str) -> dict | None:
    """Look up a user by nickname/username."""
    db = get_db()
    row = db.execute("SELECT * FROM users WHERE nickname = ?", (username,)).fetchone()
    return dict(row) if row else None


def update_user(user_id: int, **kwargs) -> dict | None:
    if "role" in kwargs:
        raise ValueError(
            "role changes must use set_user_role_and_rotate_token_version"
        )
    allowed = {"nickname", "avatar_url"}
    updates = {k: v for k, v in kwargs.items() if k in allowed}
    if not updates:
        return get_user(user_id)
    # Check nickname uniqueness if updating nickname
    if "nickname" in updates:
        nickname = updates["nickname"]
        if nickname:
            existing = get_user_by_username(nickname)
            if existing and existing["id"] != user_id:
                return None  # nickname taken by another user
    sets = ", ".join(f"{k} = ?" for k in updates)
    vals = list(updates.values()) + [user_id]
    db = get_db()
    db.execute(f"UPDATE users SET {sets} WHERE id = ?", vals)
    db.commit()
    return get_user(user_id)


def set_user_role_and_rotate_token_version(
    user_id: int,
    role: str,
) -> dict | None:
    """Set a role and revoke existing tokens in one atomic UPDATE.

    Reapplying the current role is intentionally idempotent and leaves the
    token version unchanged.
    """
    db = get_db()
    row = db.execute(
        "UPDATE users SET "
        "token_version = token_version + CASE WHEN role <> ? THEN 1 ELSE 0 END, "
        "role = ? WHERE id = ? "
        "RETURNING id, email, nickname, role, avatar_url, created_at, "
        "visit_count, last_seen_at, token_version",
        (role, role, user_id),
    ).fetchone()
    db.commit()
    return dict(row) if row else None


def delete_user(user_id: int) -> bool:
    db = get_db()
    cur = db.execute("DELETE FROM users WHERE id = ?", (user_id,))
    db.commit()
    return cur.rowcount > 0


def rotate_token_version(user_id: int) -> bool:
    db = get_db()
    cur = db.execute(
        "UPDATE users SET token_version = token_version + 1 WHERE id = ?",
        (user_id,),
    )
    db.commit()
    return cur.rowcount == 1


def list_users() -> list[dict]:
    db = get_db()
    rows = db.execute(
        "SELECT id, email, nickname, role, avatar_url, created_at, "
        "visit_count, last_seen_at, token_version FROM users ORDER BY id"
    ).fetchall()
    return [dict(r) for r in rows]


def get_first_admin_email() -> str:
    db = get_db()
    row = db.execute(
        "SELECT email FROM users WHERE role = 'admin' ORDER BY id LIMIT 1"
    ).fetchone()
    return row["email"] if row else ""


def count_users() -> int:
    db = get_db()
    return db.execute("SELECT COUNT(*) FROM users").fetchone()[0]


# ─── Authentication rate limits ─────────────────────────────

AUTH_RATE_LIMIT_SECONDS = 15 * 60
AUTH_FAILURE_LIMIT = 5
REGISTER_RATE_LIMIT_SECONDS = 15 * 60
REGISTER_ATTEMPT_LIMIT = 10
REGISTER_ATTEMPT_STALE_SECONDS = 30 * 60
INVITE_RATE_LIMIT_SECONDS = 60
INVITE_RESERVATION_SECONDS = INVITE_RATE_LIMIT_SECONDS


def _normalized_rate_limit_login(login: str) -> str:
    return (login or "").strip().casefold()


def login_retry_after(
    client_ip: str,
    login: str,
    *,
    now: float | None = None,
) -> int:
    """Return remaining lock seconds for one trusted IP/account pair."""
    current = time.time() if now is None else float(now)
    row = get_db().execute(
        "SELECT locked_until FROM login_failures "
        "WHERE client_ip = ? AND login = ?",
        (client_ip, _normalized_rate_limit_login(login)),
    ).fetchone()
    if row is None:
        return 0
    return max(0, math.ceil(float(row["locked_until"]) - current))


def admit_login_attempt(
    client_ip: str,
    login: str,
    *,
    now: float | None = None,
) -> tuple[bool, int]:
    """Atomically reserve one of the five permitted password verifications.

    Attempts are counted before password verification so parallel requests
    cannot all pass an unlocked pre-check. The fifth admission establishes the
    lock but remains permitted; later admissions receive ``(False, retry)``.
    A successful admitted login clears the pessimistic count afterward.
    """
    current = time.time() if now is None else float(now)
    normalized_login = _normalized_rate_limit_login(login)
    db = get_db()
    db.execute("BEGIN IMMEDIATE")
    try:
        row = db.execute(
            "SELECT failure_count, locked_until, updated_at "
            "FROM login_failures WHERE client_ip = ? AND login = ?",
            (client_ip, normalized_login),
        ).fetchone()
        if row and float(row["locked_until"]) > current:
            db.commit()
            return False, math.ceil(float(row["locked_until"]) - current)

        if row and current - float(row["updated_at"]) < AUTH_RATE_LIMIT_SECONDS:
            failure_count = int(row["failure_count"]) + 1
        else:
            failure_count = 1
        locked_until = (
            current + AUTH_RATE_LIMIT_SECONDS
            if failure_count >= AUTH_FAILURE_LIMIT
            else 0
        )
        db.execute(
            "INSERT INTO login_failures "
            "(client_ip, login, failure_count, locked_until, updated_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(client_ip, login) DO UPDATE SET "
            "failure_count = excluded.failure_count, "
            "locked_until = excluded.locked_until, "
            "updated_at = excluded.updated_at",
            (
                client_ip,
                normalized_login,
                failure_count,
                locked_until,
                current,
            ),
        )
        db.commit()
        return True, max(0, math.ceil(locked_until - current))
    except Exception:
        db.rollback()
        raise


def reset_login_failures(client_ip: str, login: str) -> None:
    db = get_db()
    db.execute(
        "DELETE FROM login_failures WHERE client_ip = ? AND login = ?",
        (client_ip, _normalized_rate_limit_login(login)),
    )
    db.commit()


def admit_register_attempt(
    client_ip: str,
    *,
    now: float | None = None,
) -> tuple[bool, int]:
    """Atomically reserve one of the permitted registration attempts."""
    current = time.time() if now is None else float(now)
    db = get_db()
    db.execute("BEGIN IMMEDIATE")
    try:
        db.execute(
            "DELETE FROM register_attempts WHERE updated_at < ?",
            (current - REGISTER_ATTEMPT_STALE_SECONDS,),
        )
        row = db.execute(
            "SELECT failure_count, locked_until, updated_at "
            "FROM register_attempts WHERE client_ip = ?",
            (client_ip,),
        ).fetchone()
        if row and float(row["locked_until"]) > current:
            db.commit()
            return False, max(1, math.ceil(float(row["locked_until"]) - current))

        if row and current - float(row["updated_at"]) < REGISTER_RATE_LIMIT_SECONDS:
            failure_count = int(row["failure_count"]) + 1
        else:
            failure_count = 1
        locked_until = (
            current + REGISTER_RATE_LIMIT_SECONDS
            if failure_count >= REGISTER_ATTEMPT_LIMIT
            else 0
        )
        db.execute(
            "INSERT INTO register_attempts "
            "(client_ip, failure_count, locked_until, updated_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(client_ip) DO UPDATE SET "
            "failure_count = excluded.failure_count, "
            "locked_until = excluded.locked_until, "
            "updated_at = excluded.updated_at",
            (client_ip, failure_count, locked_until, current),
        )
        db.commit()
        return True, max(0, math.ceil(locked_until - current))
    except Exception:
        db.rollback()
        raise


def reset_register_attempts(client_ip: str) -> None:
    db = get_db()
    db.execute(
        "DELETE FROM register_attempts WHERE client_ip = ?",
        (client_ip,),
    )
    db.commit()


def claim_invite_request(
    email: str,
    *,
    now: float | None = None,
) -> tuple[str | None, int]:
    """Reserve an invite send, returning ``(token, retry_after)``.

    Only ``complete_invite_request(..., succeeded=True)`` starts the rolling
    cooldown. The short reservation prevents simultaneous requests from both
    sending while still allowing a failed delivery to release the allowance.
    """
    current = time.time() if now is None else float(now)
    normalized_email = (email or "").strip().lower()
    db = get_db()
    db.execute("BEGIN IMMEDIATE")
    try:
        row = db.execute(
            "SELECT last_success_at, reservation_token, reserved_at "
            "FROM invite_request_limits WHERE email = ?",
            (normalized_email,),
        ).fetchone()
        if row and row["last_success_at"] is not None:
            elapsed = current - float(row["last_success_at"])
            if elapsed < INVITE_RATE_LIMIT_SECONDS:
                retry_after = max(1, math.ceil(INVITE_RATE_LIMIT_SECONDS - elapsed))
                db.commit()
                return None, retry_after

        if (
            row
            and row["reservation_token"]
            and row["reserved_at"] is not None
            and current - float(row["reserved_at"]) < INVITE_RESERVATION_SECONDS
        ):
            retry_after = max(
                1,
                math.ceil(
                    INVITE_RESERVATION_SECONDS
                    - (current - float(row["reserved_at"]))
                ),
            )
            db.commit()
            return None, retry_after

        token = secrets.token_urlsafe(24)
        db.execute(
            "INSERT INTO invite_request_limits "
            "(email, last_success_at, reservation_token, reserved_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(email) DO UPDATE SET "
            "reservation_token = excluded.reservation_token, "
            "reserved_at = excluded.reserved_at",
            (
                normalized_email,
                row["last_success_at"] if row else None,
                token,
                current,
            ),
        )
        db.commit()
        return token, 0
    except Exception:
        db.rollback()
        raise


def complete_invite_request(
    email: str,
    reservation_token: str,
    *,
    succeeded: bool,
    now: float | None = None,
) -> bool:
    """Complete a reserved invite send; failures never start a cooldown."""
    current = time.time() if now is None else float(now)
    normalized_email = (email or "").strip().lower()
    db = get_db()
    if succeeded:
        cur = db.execute(
            "UPDATE invite_request_limits "
            "SET last_success_at = ?, reservation_token = NULL, reserved_at = NULL "
            "WHERE email = ? AND reservation_token = ?",
            (current, normalized_email, reservation_token),
        )
    else:
        cur = db.execute(
            "UPDATE invite_request_limits "
            "SET reservation_token = NULL, reserved_at = NULL "
            "WHERE email = ? AND reservation_token = ?",
            (normalized_email, reservation_token),
        )
        db.execute(
            "DELETE FROM invite_request_limits "
            "WHERE email = ? AND last_success_at IS NULL "
            "AND reservation_token IS NULL",
            (normalized_email,),
        )
    db.commit()
    return cur.rowcount == 1


# ─── Access Stats ──────────────────────────────────────────

_ACCESS_THROTTLE_SECONDS = 300  # collapse bursts of requests into one "visit"
_ACCESS_LOG_RETENTION_SECONDS = 90 * 24 * 3600
_ACCESS_LOG_PRUNE_INTERVAL_SECONDS = 3600
_access_log_prune_lock = threading.Lock()
_last_access_log_prune_at = 0.0


def record_access(user_id: int) -> None:
    """Record at most one visit per user across the app every five minutes.

    Calls inside the global per-user throttle window perform no write: they do
    not update ``last_seen_at`` or insert an access-log row.  At the window
    boundary, the conditional UPDATE is the concurrency guard; only its winner
    increments the visit count and inserts the corresponding detail record.
    """
    db = get_db()
    row = db.execute("SELECT last_seen_at FROM users WHERE id = ?", (user_id,)).fetchone()
    if row is None:
        return
    now = datetime.utcnow()
    last_seen = row["last_seen_at"]
    if last_seen:
        try:
            last_dt = datetime.strptime(last_seen, "%Y-%m-%d %H:%M:%S")
            if (now - last_dt).total_seconds() < _ACCESS_THROTTLE_SECONDS:
                return
        except ValueError:
            pass

    now_str = now.strftime("%Y-%m-%d %H:%M:%S")
    cutoff = (now - timedelta(seconds=_ACCESS_THROTTLE_SECONDS)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    updated = db.execute(
        "UPDATE users "
        "SET visit_count = visit_count + 1, last_seen_at = ? "
        "WHERE id = ? AND ("
        "last_seen_at IS NULL OR last_seen_at = '' OR last_seen_at < ?)",
        (now_str, user_id, cutoff),
    )
    if updated.rowcount != 1:
        db.rollback()
        return
    db.execute(
        "INSERT INTO user_access_log (user_id, accessed_at) VALUES (?, ?)",
        (user_id, now_str),
    )
    db.commit()


def prune_access_log(*, now: float | None = None) -> int:
    """Delete old access details at most once per successful hourly run."""
    global _last_access_log_prune_at
    current = time.time() if now is None else float(now)
    with _access_log_prune_lock:
        if current - _last_access_log_prune_at < _ACCESS_LOG_PRUNE_INTERVAL_SECONDS:
            return 0
        db = get_db()
        try:
            deleted = db.execute(
                "DELETE FROM user_access_log "
                "WHERE accessed_at < "
                "strftime('%Y-%m-%d %H:%M:%S', ?, 'unixepoch')",
                (current - _ACCESS_LOG_RETENTION_SECONDS,),
            )
            count = max(0, int(deleted.rowcount))
            db.commit()
        except Exception:
            db.rollback()
            raise
        _last_access_log_prune_at = current
        return count


def count_active_users_since(days: int) -> int:
    db = get_db()
    row = db.execute(
        "SELECT COUNT(*) FROM users WHERE last_seen_at >= datetime('now', ?)",
        (f"-{int(days)} days",),
    ).fetchone()
    return int(row[0]) if row else 0


# ─── Favorites ─────────────────────────────────────────────


def add_favorite(user_id: int, article_id: int) -> dict | None:
    db = get_db()
    try:
        cur = db.execute(
            "INSERT INTO favorites (user_id, article_id) VALUES (?, ?)",
            (user_id, article_id),
        )
        db.commit()
        row = db.execute(
            "SELECT id, user_id, article_id, created_at FROM favorites WHERE id = ?",
            (cur.lastrowid,),
        ).fetchone()
        return dict(row) if row else None
    except sqlite3.IntegrityError:
        return None  # already favorited


def remove_favorite(user_id: int, article_id: int) -> bool:
    db = get_db()
    cur = db.execute(
        "DELETE FROM favorites WHERE user_id = ? AND article_id = ?",
        (user_id, article_id),
    )
    db.commit()
    return cur.rowcount > 0


def get_favorites(user_id: int) -> list[dict]:
    db = get_db()
    rows = db.execute(
        "SELECT article_id, created_at FROM favorites WHERE user_id = ? ORDER BY created_at DESC",
        (user_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_all_favorite_article_ids() -> list[int]:
    db = get_db()
    rows = db.execute(
        "SELECT DISTINCT article_id FROM favorites ORDER BY article_id"
    ).fetchall()
    return [int(r["article_id"]) for r in rows]


def is_favorited(user_id: int, article_id: int) -> bool:
    db = get_db()
    row = db.execute(
        "SELECT 1 FROM favorites WHERE user_id = ? AND article_id = ?",
        (user_id, article_id),
    ).fetchone()
    return row is not None


def count_article_favorites(article_id: int) -> int:
    db = get_db()
    row = db.execute(
        "SELECT COUNT(*) AS count FROM favorites WHERE article_id = ?",
        (article_id,),
    ).fetchone()
    return int(row["count"] if row else 0)


# ─── AI Config ─────────────────────────────────────────────


def get_system_ai_config() -> dict:
    db = get_db()
    row = db.execute(
        "SELECT provider, api_key, endpoint, model, provider_type, enabled "
        "FROM system_ai_config WHERE id = 1"
    ).fetchone()
    if row:
        return dict(row)
    return {
        "provider": "openai", "api_key": "", "endpoint": "https://api.openai.com/v1",
        "model": "gpt-4o-mini", "provider_type": "openai", "enabled": 0,
    }


def set_system_ai_config(**kwargs) -> dict:
    """Upsert the singleton system AI config. Allowed keys: provider, api_key, endpoint, model, provider_type, enabled."""
    allowed = {"provider", "api_key", "endpoint", "model", "provider_type", "enabled"}
    updates = {k: v for k, v in kwargs.items() if k in allowed}
    if not updates:
        return get_system_ai_config()
    db = get_db()
    db.execute("INSERT OR IGNORE INTO system_ai_config (id) VALUES (1)")
    sets = ", ".join(f"{k} = ?" for k in updates)
    vals = list(updates.values())
    db.execute(f"UPDATE system_ai_config SET {sets} WHERE id = 1", vals)
    db.commit()
    return get_system_ai_config()


# ─── User Settings ─────────────────────────────────────────


def get_user_settings(user_id: int) -> dict | None:
    row = get_db().execute("SELECT * FROM user_settings WHERE user_id = ?", (user_id,)).fetchone()
    return dict(row) if row else None


def get_app_state(key: str, default: str = "") -> str:
    db = get_db()
    row = db.execute("SELECT value FROM app_state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_app_state(key: str, value: str) -> None:
    db = get_db()
    db.execute(
        "INSERT INTO app_state (key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (key, str(value), datetime.now().isoformat(timespec="seconds")),
    )
    db.commit()


def set_app_state_values(values: dict[str, str]) -> None:
    """Atomically update a group of app-state values."""
    if not values:
        return
    db = get_db()
    updated_at = datetime.now().isoformat(timespec="seconds")
    try:
        db.execute("BEGIN IMMEDIATE")
        db.executemany(
            "INSERT INTO app_state (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            [(key, str(value), updated_at) for key, value in values.items()],
        )
        db.commit()
    except Exception:
        db.rollback()
        raise


def advance_app_state_epoch(key: str, epoch: float) -> None:
    """Atomically raise an epoch-valued app-state key, never lower it."""
    target = float(epoch)
    if not math.isfinite(target) or target < 0:
        raise ValueError("epoch must be finite and non-negative")
    db = get_db()
    try:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT value FROM app_state WHERE key = ?", (key,)).fetchone()
        try:
            current = float(row["value"]) if row else 0.0
        except (TypeError, ValueError):
            current = 0.0
        if current >= target:
            db.rollback()
            return
        db.execute(
            "INSERT INTO app_state (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (key, str(target), datetime.now().isoformat(timespec="seconds")),
        )
        db.commit()
    except Exception:
        db.rollback()
        raise


def claim_app_state_flag(key: str) -> bool:
    """Set a flag to '1' and report whether this caller is the one that set it.

    One transaction, so two threads (or a restarted process racing a running
    one) cannot both read "not yet notified" and both send the notification.
    """
    db = get_db()
    try:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT value FROM app_state WHERE key = ?", (key,)).fetchone()
        if row and row["value"] == "1":
            db.rollback()
            return False
        db.execute(
            "INSERT INTO app_state (key, value, updated_at) VALUES (?, '1', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = '1', updated_at = excluded.updated_at",
            (key, datetime.now().isoformat(timespec="seconds")),
        )
        db.commit()
        return True
    except Exception:
        db.rollback()
        raise


def claim_app_state_incident(
    key: str,
    last_notified_key: str,
    cooldown_seconds: float,
    now: float | None = None,
) -> str:
    """Atomically start an app-state incident.

    Returns ``"notify"`` when this is a newly eligible incident,
    ``"suppressed"`` when it is within the notification cooldown, and
    ``"active"`` when another caller already owns an incident. State ``"1"``
    means notified; ``"2"`` means active but deliberately silent.
    """
    db = get_db()
    current = time.time() if now is None else float(now)
    cooldown = max(0.0, float(cooldown_seconds))
    try:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT value FROM app_state WHERE key = ?", (key,)).fetchone()
        if row and row["value"] in {"1", "2"}:
            db.rollback()
            return "active"
        # The value is an epoch timestamp for numeric cooldown math; app_state
        # updated_at remains its independent ISO-8601 audit timestamp.
        last_row = db.execute(
            "SELECT value FROM app_state WHERE key = ?", (last_notified_key,)
        ).fetchone()
        try:
            last_notified = float(last_row["value"]) if last_row else 0.0
        except (TypeError, ValueError):
            last_notified = 0.0
        in_cooldown = bool(last_notified) and current - last_notified < cooldown
        state, result = ("2", "suppressed") if in_cooldown else ("1", "notify")
        db.execute(
            "INSERT INTO app_state (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (key, state, datetime.now().isoformat(timespec="seconds")),
        )
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise


def complete_app_state_incident(
    key: str,
    last_notified_key: str,
    now: float | None = None,
) -> str:
    """Atomically close an incident and start cooldown for a notified one.

    ``app_state.value`` for ``last_notified_key`` is intentionally an epoch
    timestamp because cooldown calculations use numeric seconds. ``updated_at``
    remains the table's ISO-8601 audit field and is never used for that math.
    """
    db = get_db()
    current = time.time() if now is None else float(now)
    try:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT value FROM app_state WHERE key = ?", (key,)).fetchone()
        prior = str(row["value"]) if row else "0"
        updated_at = datetime.now().isoformat(timespec="seconds")
        db.execute(
            "INSERT INTO app_state (key, value, updated_at) VALUES (?, '0', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = '0', updated_at = excluded.updated_at",
            (key, updated_at),
        )
        if prior == "1":
            db.execute(
                "INSERT INTO app_state (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                (last_notified_key, str(current), updated_at),
            )
        db.commit()
        return prior
    except Exception:
        db.rollback()
        raise


def complete_app_state_incident_if_stable(
    key: str,
    last_notified_key: str,
    last_failure_key: str,
    last_success_key: str,
    stability_seconds: float,
    now: float | None = None,
) -> str:
    """Atomically close an incident after a quiet, successful interval.

    The write transaction begins before any incident or timestamp read so a
    concurrent failure cannot be committed between the eligibility check and
    the close. A zero stability window removes the delay but still requires a
    real success after the latest failure.
    """
    window = max(0.0, float(stability_seconds))

    db = get_db()
    current = time.time() if now is None else float(now)
    try:
        db.execute("BEGIN IMMEDIATE")
        rows = db.execute(
            "SELECT key, value FROM app_state WHERE key IN (?, ?, ?, ?)",
            (key, last_notified_key, last_failure_key, last_success_key),
        ).fetchall()
        values = {str(row["key"]): str(row["value"]) for row in rows}
        prior = values.get(key, "0")
        try:
            last_failure = float(values.get(last_failure_key, "0"))
            last_success = float(values.get(last_success_key, "0"))
        except (TypeError, ValueError):
            last_failure = last_success = 0.0

        if not (
            prior in {"1", "2"}
            and last_failure > 0
            and last_success > last_failure
            and current - last_failure >= window
        ):
            db.rollback()
            return "0"

        updated_at = datetime.now().isoformat(timespec="seconds")
        db.execute(
            "INSERT INTO app_state (key, value, updated_at) VALUES (?, '0', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = '0', updated_at = excluded.updated_at",
            (key, updated_at),
        )
        if prior == "1":
            db.execute(
                "INSERT INTO app_state (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                (last_notified_key, str(current), updated_at),
            )
        db.commit()
        return prior
    except Exception:
        db.rollback()
        raise


def get_daily_summary_inapp_user_ids() -> list[int]:
    """Recipients with digest access, the master switch and in-app delivery on."""
    db = get_db()
    rows = db.execute(
        "SELECT u.id FROM users AS u "
        "LEFT JOIN user_settings AS s ON s.user_id = u.id "
        "WHERE COALESCE(s.daily_summary_inapp_enabled, 1) = 1 "
        "ORDER BY u.id"
    ).fetchall()
    from ai_access import daily_enabled
    return [int(r["id"]) for r in rows if daily_enabled(int(r["id"]))]


def set_user_settings(user_id: int, **kwargs) -> dict:
    from ai_access import PREFERENCES
    allowed = set(PREFERENCES) | {"daily_summary_enabled", "daily_summary_inapp_enabled", "theme_preference", "notification_config"}
    updates = {key: value for key, value in kwargs.items() if key in allowed}
    db = get_db()
    if updates:
        db.execute("INSERT OR IGNORE INTO user_settings(user_id) VALUES(?)", (user_id,))
        db.execute("UPDATE user_settings SET " + ",".join(key + "=?" for key in updates) + " WHERE user_id=?", [*updates.values(), user_id])
        db.commit()
    return get_user_settings(user_id) or {}


def add_notification(user_id: int, ntype: str, title: str, body: str = "",
                     fmt: str = "plain") -> int:
    """Insert an in-app notification for a user. Returns the new row id.

    fmt is 'plain' (client renders newline→<br>) or 'markdown' (client runs it
    through renderMarkdown + sanitizer). The raw body text is stored as-is;
    every client render path escapes/sanitizes, so markup here is inert.
    """
    db = get_db()
    # Local timestamps match other account notification records.
    now = datetime.now().isoformat(timespec="seconds")
    cur = db.execute(
        "INSERT INTO notifications (user_id, type, title, body, format, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, ntype, title, body, fmt, now),
    )
    db.commit()
    return cur.lastrowid


def add_notification_bulk(user_ids: list[int], ntype: str, title: str,
                          body: str = "", fmt: str = "plain") -> int:
    """Insert the same notification for many users in one transaction (used by
    the admin site-wide broadcast). Returns the number of rows inserted."""
    if not user_ids:
        return 0
    db = get_db()
    now = datetime.now().isoformat(timespec="seconds")
    rows = [(uid, ntype, title, body, fmt, now) for uid in user_ids]
    db.executemany(
        "INSERT INTO notifications (user_id, type, title, body, format, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        rows,
    )
    db.commit()
    return len(rows)


def publish_broadcast_atomically(
    user_ids: list[int], broadcast_id: str, title: str, body: str,
    fmt: str, email: bool, ntype: str = "admin_broadcast",
) -> tuple[bool, dict]:
    """Commit a site-wide in-app broadcast as one transaction.

    Returns ``(True, result)`` when this call publishes a new broadcast, or
    ``(False, result)`` when ``broadcast_id`` was already committed. A fan-out
    failure rolls back both the claim row and all notification rows, so a retry
    with the same id can safely try again.

    ``ntype`` lets non-admin publishers reuse this claim-then-fan-out unit. The
    daily summary passes a date-derived ``broadcast_id``, which is what stops
    the 21:00 scheduler — it ticks once a minute through a ten-minute window,
    and restarts inside that window — from inserting the same summary twice.
    """
    # This transaction must not use get_db(): that connection is reused for the
    # whole life of the calling thread, and every other model helper commits on
    # it. A commit from any of them (the email fan-out below calls into several)
    # would split this multi-statement unit, leaving the claim row committed
    # without its notification rows. A short-lived connection gives this unit its
    # own transaction boundary; WAL + busy_timeout let writers serialize normally.
    db = sqlite3.connect(str(DB_FILE), timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA busy_timeout=30000")
    now = datetime.now().isoformat(timespec="seconds")
    try:
        db.execute("BEGIN IMMEDIATE")
        claimed = db.execute(
            "INSERT OR IGNORE INTO broadcast_publications "
            "(broadcast_id, title, recipients, email, created_at) VALUES (?, '', 0, 0, ?)",
            (broadcast_id, now),
        ).rowcount > 0
        if not claimed:
            row = db.execute(
                "SELECT recipients, email FROM broadcast_publications WHERE broadcast_id = ?",
                (broadcast_id,),
            ).fetchone()
            db.rollback()
            return False, {
                "recipients": int(row["recipients"]),
                "email": bool(row["email"]),
            }

        db.executemany(
            "INSERT INTO notifications (user_id, type, title, body, format, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [(uid, ntype, title, body, fmt, now) for uid in user_ids],
        )
        db.execute(
            "UPDATE broadcast_publications SET title = ?, recipients = ?, email = ? "
            "WHERE broadcast_id = ?",
            (title, len(user_ids), int(email), broadcast_id),
        )
        db.commit()
        return True, {"recipients": len(user_ids), "email": bool(email)}
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def list_notifications(user_id: int, limit: int = 100) -> list[dict]:
    """Newest first, with a second bounded window that keeps old unread rows
    reachable.

    Sorting unread rows ahead of read rows made the list jump: marking the
    newest row read moved it below every unread row and could push it outside
    the LIMIT entirely. The union keeps the latest `limit` rows in stable
    chronological order and adds up to `limit` unread rows that would otherwise
    be hidden. As those are marked read, the next unread window becomes visible.
    """
    from ai_access import daily_enabled
    digest_filter = "" if daily_enabled(user_id) else " AND type != 'daily_summary'"
    db = get_db()
    rows = db.execute(
        "WITH recent AS ("
        "  SELECT id, type, title, body, format, created_at, read_at "
        f"  FROM notifications WHERE user_id = ? {digest_filter} ORDER BY id DESC LIMIT ?"
        "), unread AS ("
        "  SELECT id, type, title, body, format, created_at, read_at "
        f"  FROM notifications WHERE user_id = ? AND read_at IS NULL {digest_filter} "
        "  ORDER BY id DESC LIMIT ?"
        ") "
        "SELECT * FROM recent UNION SELECT * FROM unread ORDER BY id DESC",
        (user_id, limit, user_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def count_unread_notifications(user_id: int) -> int:
    from ai_access import daily_enabled
    condition = "" if daily_enabled(user_id) else " AND type != 'daily_summary'"
    row = get_db().execute("SELECT COUNT(*) FROM notifications WHERE user_id=? AND read_at IS NULL" + condition, (user_id,)).fetchone()
    return int(row[0])


def mark_notification_read(user_id: int, notification_id: int) -> bool:
    """Mark one notification read. User-scoped; idempotent (already-read rows
    are untouched and return False)."""
    db = get_db()
    now = datetime.now().isoformat(timespec="seconds")
    cur = db.execute(
        "UPDATE notifications SET read_at = ? "
        "WHERE id = ? AND user_id = ? AND read_at IS NULL",
        (now, notification_id, user_id),
    )
    db.commit()
    return cur.rowcount > 0


def mark_all_notifications_read(user_id: int) -> int:
    """Mark every unread notification for one user as read."""
    db = get_db()
    now = datetime.now().isoformat(timespec="seconds")
    cur = db.execute(
        "UPDATE notifications SET read_at = ? WHERE user_id = ? AND read_at IS NULL",
        (now, user_id),
    )
    db.commit()
    return cur.rowcount


def delete_notification(user_id: int, notification_id: int) -> bool:
    """Delete one user-owned notification; unknown rows are a no-op."""
    db = get_db()
    cur = db.execute(
        "DELETE FROM notifications WHERE id = ? AND user_id = ?",
        (notification_id, user_id),
    )
    db.commit()
    return cur.rowcount > 0


def delete_all_notifications(user_id: int) -> int:
    """Delete all notifications owned by one user."""
    db = get_db()
    cur = db.execute("DELETE FROM notifications WHERE user_id = ?", (user_id,))
    db.commit()
    return cur.rowcount


def get_broadcast_publication(broadcast_id: str) -> dict | None:
    db = get_db()
    row = db.execute(
        "SELECT broadcast_id, title, recipients, email, created_at "
        "FROM broadcast_publications WHERE broadcast_id = ?",
        (broadcast_id,),
    ).fetchone()
    return dict(row) if row else None


# ─── Invitation Codes ──────────────────────────────────────


def create_invitation_code(email: str) -> str:
    """Generate a code, atomically invalidating older codes for the email."""
    db = get_db()
    for attempt in range(10):
        code = secrets.token_hex(4).upper()  # 8 hex chars
        try:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "UPDATE invitation_codes SET used = 1 "
                "WHERE email = ? AND used = 0",
                (email,),
            )
            db.execute(
                "INSERT INTO invitation_codes (code, email) VALUES (?, ?)",
                (code, email),
            )
            db.commit()
            return code
        except sqlite3.IntegrityError:
            db.rollback()
            continue  # code collision, retry
        except Exception:
            db.rollback()
            raise
    raise Exception("failed to generate unique invitation code")


def validate_invitation_code(code: str, email: str) -> bool:
    """Check if code is valid and tied to this email, not yet used."""
    db = get_db()
    row = db.execute(
        "SELECT 1 FROM invitation_codes WHERE code = ? AND email = ? AND used = 0",
        (code, email),
    ).fetchone()
    return row is not None


def use_invitation_code(code: str) -> bool:
    """Mark code as used. Returns True if successful."""
    db = get_db()
    cur = db.execute(
        "UPDATE invitation_codes SET used = 1 WHERE code = ? AND used = 0",
        (code,),
    )
    db.commit()
    return cur.rowcount > 0


def list_pending_invitations() -> list[dict]:
    """Invite-code requests that haven't been used to complete registration yet."""
    db = get_db()
    rows = db.execute(
        "SELECT id, code, email, created_at FROM invitation_codes "
        "WHERE used = 0 ORDER BY created_at DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def count_pending_invitations() -> int:
    db = get_db()
    return db.execute(
        "SELECT COUNT(*) FROM invitation_codes WHERE used = 0"
    ).fetchone()[0]


def delete_invitation_code(invitation_id: int) -> bool:
    """Revoke a pending (unused) invitation request."""
    db = get_db()
    cur = db.execute(
        "DELETE FROM invitation_codes WHERE id = ? AND used = 0",
        (invitation_id,),
    )
    db.commit()
    return cur.rowcount > 0


def delete_invitation_code_by_code(code: str) -> bool:
    """Roll back a just-created code (e.g. the notification email failed to send)."""
    db = get_db()
    cur = db.execute(
        "DELETE FROM invitation_codes WHERE code = ? AND used = 0",
        (code,),
    )
    db.commit()
    return cur.rowcount > 0
