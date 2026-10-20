
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

CREATE TABLE IF NOT EXISTS ai_configs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
    provider    TEXT    NOT NULL DEFAULT 'openai',
    api_key     TEXT    NOT NULL DEFAULT '',
    endpoint    TEXT    NOT NULL DEFAULT 'https://api.openai.com/v1',
    model       TEXT    NOT NULL DEFAULT 'gpt-4o-mini',
    provider_type TEXT NOT NULL DEFAULT 'openai'
                    CHECK(provider_type IN ('openai', 'claude')),
    enabled     INTEGER NOT NULL DEFAULT 0,
    revision    INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS user_settings (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id                 INTEGER NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
    auto_translate_title    INTEGER NOT NULL DEFAULT 0,
    auto_translate_content  INTEGER NOT NULL DEFAULT 0,
    auto_title_summary_enabled INTEGER NOT NULL DEFAULT 0,
    auto_summary_enabled    INTEGER NOT NULL DEFAULT 0,
    daily_summary_enabled   INTEGER NOT NULL DEFAULT 0,
    -- The in-app copy of the daily summary is on by default; the email copy
    -- (daily_summary_enabled above) stays opt-in because it needs an address.
    daily_summary_inapp_enabled INTEGER NOT NULL DEFAULT 1,
    theme_preference        TEXT    NOT NULL DEFAULT 'system',
    notification_config     TEXT    NOT NULL DEFAULT '{}',
    share_ai_results        INTEGER NOT NULL DEFAULT 0,
    share_view_title        INTEGER NOT NULL DEFAULT 0,
    share_view_translation  INTEGER NOT NULL DEFAULT 0,
    share_view_summary      INTEGER NOT NULL DEFAULT 0,
    share_suspended         INTEGER NOT NULL DEFAULT 0,
    share_last_check_at     TEXT,
    share_last_check_ok     INTEGER,
    share_last_check_error  TEXT,
    share_last_check_revision INTEGER,
    share_revalidation_failure_streak INTEGER NOT NULL DEFAULT 0,
    share_revalidation_failure_revision INTEGER,
    share_revalidation_last_failure_at TEXT,
    share_revalidation_last_failure_error TEXT,
    share_intent_revision   INTEGER NOT NULL DEFAULT 1
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
);