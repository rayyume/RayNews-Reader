"""Regression coverage for durable shared-AI suspension state."""

import os
import sqlite3
import threading
import uuid
from pathlib import Path

import pytest

import models
import web_server

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def share_env(tmp_path, monkeypatch):
    news_path = tmp_path / "news.db"
    sqlite3.connect(news_path).close()
    monkeypatch.setattr(web_server, "NEWS_DB", str(news_path))
    db_path = ROOT / f"tmp-share-recovery-{uuid.uuid4().hex}.db"
    old_db_file = models.DB_FILE
    models.close_db()
    models.DB_FILE = db_path
    models.get_db()
    user = models.create_user("share@example.com", "pw", "share-user")
    client = web_server.app.test_client()
    try:
        yield client, user["id"]
    finally:
        models.close_db()
        models.DB_FILE = old_db_file
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(str(db_path) + suffix)
            except FileNotFoundError:
                pass


def auth_headers(user_id: int, role: str = "user") -> dict:
    return {"Authorization": f"Bearer {web_server.create_token(user_id, role)}"}


_SHARE_HEALTH_KEYS = {
    "share_suspended",
    "share_last_check_at",
    "share_last_check_ok",
    "share_last_check_error",
    "share_last_check_revision",
    "share_revalidation_failure_streak",
    "share_revalidation_failure_revision",
    "share_revalidation_last_failure_at",
    "share_revalidation_last_failure_error",
}


def _set_user_settings(user_id: int, **kwargs):
    """Test setup helper that keeps user intent and server health writes explicit."""
    health = {key: kwargs.pop(key) for key in tuple(kwargs) if key in _SHARE_HEALTH_KEYS}
    settings = models.set_user_settings(user_id, **kwargs)
    return models.set_share_health(user_id, **health) if health else settings


def test_share_suspended_defaults_false_and_round_trips(share_env):
    _, user_id = share_env
    settings = _set_user_settings(
        user_id,
        share_ai_results=1,
        share_view_title=1,
        share_last_check_ok=1,
    )
    assert settings["share_suspended"] == 0

    settings = _set_user_settings(user_id, share_suspended=1)
    assert settings["share_suspended"] == 1
    assert settings["share_ai_results"] == 1
    assert settings["share_view_title"] == 1


def test_real_legacy_app_db_preserves_share_intent_when_revision_columns_migrate(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "legacy-raynews.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL UNIQUE,
            password TEXT NOT NULL,
            nickname TEXT NOT NULL DEFAULT '',
            role TEXT NOT NULL DEFAULT 'user',
            avatar_url TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE ai_configs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL UNIQUE,
            provider TEXT NOT NULL DEFAULT 'openai',
            api_key TEXT NOT NULL DEFAULT '',
            endpoint TEXT NOT NULL DEFAULT 'https://api.openai.com/v1',
            model TEXT NOT NULL DEFAULT 'gpt-4o-mini',
            provider_type TEXT NOT NULL DEFAULT 'openai',
            enabled INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE user_settings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL UNIQUE,
            auto_translate_title INTEGER NOT NULL DEFAULT 0,
            auto_translate_content INTEGER NOT NULL DEFAULT 0,
            auto_title_summary_enabled INTEGER NOT NULL DEFAULT 0,
            auto_summary_enabled INTEGER NOT NULL DEFAULT 0,
            daily_summary_enabled INTEGER NOT NULL DEFAULT 0,
            theme_preference TEXT NOT NULL DEFAULT 'system',
            notification_config TEXT NOT NULL DEFAULT '{}',
            share_ai_results INTEGER NOT NULL DEFAULT 0,
            share_view_title INTEGER NOT NULL DEFAULT 0,
            share_view_translation INTEGER NOT NULL DEFAULT 0,
            share_view_summary INTEGER NOT NULL DEFAULT 0,
            share_last_check_at TEXT,
            share_last_check_ok INTEGER,
            share_last_check_error TEXT
        );
        INSERT INTO users (id, email, password, nickname, role)
        VALUES (7, 'legacy@example.com', 'hash', 'legacy', 'user');
        INSERT INTO ai_configs (user_id, api_key, enabled)
        VALUES (7, 'legacy-key', 1);
        INSERT INTO user_settings (
            user_id, share_ai_results, share_view_title,
            share_view_translation, share_view_summary,
            share_last_check_at, share_last_check_ok
        ) VALUES (7, 1, 1, 0, 1, '2026-07-27T10:00:00', 1);
        """
    )
    conn.commit()
    conn.close()

    models.close_db()
    monkeypatch.setattr(models, "DB_FILE", db_path)
    settings = models.get_user_settings(7)
    config = models.get_ai_config(7)

    assert settings["share_ai_results"] == 1
    assert settings["share_view_title"] == 1
    assert settings["share_view_translation"] == 0
    assert settings["share_view_summary"] == 1
    assert settings["share_suspended"] == 0
    assert settings["share_last_check_revision"] is None
    assert settings["share_revalidation_failure_streak"] == 0
    assert settings["share_revalidation_failure_revision"] is None
    assert settings["share_revalidation_last_failure_at"] is None
    assert settings["share_revalidation_last_failure_error"] is None
    assert settings["share_intent_revision"] == 1
    assert config["revision"] == 1
    assert web_server.is_share_active(settings) is False
    models.close_db()


def test_app_db_migration_propagates_nonduplicate_operational_error():
    raw = sqlite3.connect(":memory:")
    raw.execute(
        """
        CREATE TABLE ai_configs (
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL UNIQUE,
            provider_type TEXT NOT NULL DEFAULT 'openai'
        )
        """
    )

    class BrokenMigrationConnection:
        def executescript(self, sql):
            return raw.executescript(sql)

        def execute(self, sql, *args, **kwargs):
            if sql.startswith("ALTER TABLE ai_configs ADD COLUMN revision"):
                raise sqlite3.OperationalError("database disk image is malformed")
            return raw.execute(sql, *args, **kwargs)

        def commit(self):
            return raw.commit()

    with pytest.raises(sqlite3.OperationalError, match="malformed"):
        models._initialize_db(BrokenMigrationConnection())


@pytest.mark.parametrize(
    ("settings", "expected"),
    (
        (None, False),
        ({}, False),
        ({
            "share_ai_results": 1,
            "share_suspended": 0,
            "share_last_check_ok": 1,
            "share_last_check_revision": 2,
            "share_current_config_revision": 2,
        }, True),
        ({
            "share_ai_results": 1,
            "share_suspended": 0,
            "share_last_check_ok": 1,
            "share_last_check_revision": None,
            "share_current_config_revision": 2,
        }, False),
        ({"share_ai_results": 1, "share_suspended": 1, "share_last_check_ok": 1}, False),
        ({"share_ai_results": 1, "share_suspended": 0, "share_last_check_ok": 0}, False),
        ({"share_ai_results": 0, "share_suspended": 0, "share_last_check_ok": 1}, False),
    ),
)
def test_is_share_active_requires_intent_health_and_no_suspension(settings, expected):
    assert web_server.is_share_active(settings) is expected


def test_suspension_hides_cached_summary_and_translation_without_clearing_preferences(
    share_env, monkeypatch
):
    client, user_id = share_env
    _set_user_settings(
        user_id,
        share_ai_results=1,
        share_view_title=1,
        share_view_summary=1,
        share_view_translation=1,
        share_last_check_ok=0,
        share_suspended=1,
    )
    monkeypatch.setattr(
        web_server,
        "_get_ai_result",
        lambda article_id, **kwargs: {
            "summary": "shared summary",
            "summary_error": "old error",
            "summary_error_at": "2026-07-27T10:00:00",
            "translation": "shared translation",
            "updated_at": "2026-07-27T10:00:00",
        },
    )

    response = client.get("/ai/result/42", headers=auth_headers(user_id))

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["updated_at"] == "2026-07-27T10:00:00"
    assert payload["access"]["share_active"] is False
    assert not {"summary", "translation"} & payload.keys()
    settings = models.get_user_settings(user_id)
    assert settings["share_ai_results"] == 1
    assert settings["share_view_title"] == 1
    assert settings["share_view_summary"] == 1
    assert settings["share_view_translation"] == 1
    assert settings["share_suspended"] == 1


@pytest.mark.parametrize(
    ("suspended", "view_translation", "view_title", "has_translation", "has_title"),
    (
        (1, 1, 1, False, False),
        (0, 1, 0, True, False),
        (0, 0, 1, False, False),
        (0, 1, 1, True, True),
    ),
)
def test_translation_cache_route_gates_html_and_embedded_title_independently(
    share_env,
    monkeypatch,
    suspended,
    view_translation,
    view_title,
    has_translation,
    has_title,
):
    client, user_id = share_env
    _set_user_settings(
        user_id,
        share_ai_results=1,
        share_view_title=view_title,
        share_view_translation=view_translation,
        share_view_summary=0,
        share_suspended=suspended,
        share_last_check_ok=0 if suspended else 1,
    )
    config = models.set_ai_config(user_id, api_key="key", enabled=1)
    web_server._apply_share_connectivity_result(
        user_id,
        not suspended,
        "AI API HTTP 401" if suspended else "",
        config_revision=config["revision"],
    )
    monkeypatch.setattr(
        web_server,
        "_get_ai_result",
        lambda article_id, **kwargs: {
            "translation": '{"title":"共享译名","html":"<p>共享译文</p>"}',
            "updated_at": "now",
        },
    )

    data = client.get("/ai/result/42", headers=auth_headers(user_id)).get_json()

    assert ("translation" in data) is has_translation
    if has_translation:
        payload = __import__("json").loads(data["translation"])
        assert payload["html"] == "<p>共享译文</p>"
        assert bool(payload.get("title")) is has_title


def opted_in(user_id: int, *, suspended: int = 0):
    return _set_user_settings(
        user_id,
        share_ai_results=1,
        share_view_title=1,
        share_view_translation=0,
        share_view_summary=1,
        share_suspended=suspended,
        share_last_check_ok=0 if suspended else 1,
    )


def test_settings_returns_intent_suspension_and_effective_state(share_env):
    client, user_id = share_env
    opted_in(user_id, suspended=1)

    response = client.get("/settings", headers=auth_headers(user_id))

    assert response.status_code == 200
    data = response.get_json()
    assert data["share_ai_results"] == 1
    assert data["share_suspended"] == 1
    assert data["share_active"] is False
    assert "share_intent_revision" not in data


def test_settings_cannot_forge_server_owned_share_health(share_env):
    client, user_id = share_env
    config = models.set_ai_config(user_id, api_key="expired-key", enabled=1)
    _set_user_settings(
        user_id,
        share_ai_results=1,
        share_view_summary=1,
        theme_preference="system",
    )
    assert models.apply_share_connectivity_transition(
        user_id,
        expected_suspended=0,
        expected_config_revision=config["revision"],
        next_suspended=1,
        checked_at="2026-07-28T08:00:00",
        check_ok=0,
        error="AI API HTTP 401",
    )

    response = client.put(
        "/settings",
        headers=auth_headers(user_id),
        json={
            "theme_preference": "dark",
            "share_suspended": 0,
            "share_last_check_ok": 1,
            "share_last_check_at": "2099-01-01T00:00:00",
            "share_last_check_error": "",
            "share_last_check_revision": config["revision"],
            "share_current_config_revision": config["revision"],
            "share_intent_revision": 999,
            "share_active": True,
        },
    )

    assert response.status_code == 200
    assert response.get_json()["share_active"] is False
    settings = models.get_user_settings(user_id)
    assert settings["theme_preference"] == "dark"
    assert settings["share_suspended"] == 1
    assert settings["share_last_check_ok"] == 0
    assert settings["share_last_check_at"] == "2026-07-28T08:00:00"
    assert settings["share_last_check_error"] == "AI API HTTP 401"
    assert settings["share_last_check_revision"] == config["revision"]


def test_frontend_keeps_paused_preferences_visible_but_disabled():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    load_start = html.index("async function loadShareTab()")
    load_end = html.index("async function saveShareConfig()", load_start)
    block = html[load_start:load_end]
    assert "share_suspended" in html
    assert "share_active" in html
    assert "共享已暂停" in block
    assert "el.disabled = !masterOn || suspended" in html
    title_start = html.index("function displayTitle(")
    title_end = html.index("\n}", title_start)
    assert "share_active" in html[title_start:title_end]


def test_favorites_and_source_history_return_original_titles_for_effective_title_gate(
    share_env, tmp_path, monkeypatch
):
    """Both user-visible title lists need the original title for the client gate."""
    client, user_id = share_env
    news_db = tmp_path / "news.db"
    conn = sqlite3.connect(news_db)
    conn.execute(
        """
        CREATE TABLE articles (
            id INTEGER PRIMARY KEY,
            title TEXT NOT NULL DEFAULT '',
            original_title TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL DEFAULT '',
            feed_source TEXT NOT NULL DEFAULT '',
            origin_source TEXT NOT NULL DEFAULT '',
            date TEXT DEFAULT '',
            time TEXT DEFAULT '',
            timestamp INTEGER DEFAULT 0,
            thumb TEXT DEFAULT '',
            has_full_content INTEGER DEFAULT 0
        )
        """
    )
    conn.execute(
        "CREATE TABLE source_aliases (alias_source TEXT PRIMARY KEY, target_source TEXT NOT NULL)"
    )
    conn.execute(
        """INSERT INTO articles
        (id, title, original_title, source, feed_source, origin_source, date, time, timestamp)
        VALUES (42, 'Shared translation', 'Original title', 'Feed', 'Feed', '', '2026-07-27', '10:00', 1)"""
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(web_server, "NEWS_DB", str(news_db))
    monkeypatch.setattr(web_server, "_news_conn_local", threading.local())
    assert models.add_favorite(user_id, 42)

    favorites = client.get("/favorites", headers=auth_headers(user_id))
    source_history = client.get(
        "/sources/articles?source=Feed", headers=auth_headers(user_id)
    )

    assert favorites.status_code == 200
    favorite = favorites.get_json()["items"][0]
    assert favorite["title"] == "Shared translation"
    assert favorite["original_title"] == "Original title"
    assert source_history.status_code == 200
    source_item = source_history.get_json()["items"][0]
    assert source_item["title"] == "Shared translation"
    assert source_item["original_title"] == "Original title"


def test_failed_check_suspends_without_clearing_preferences(share_env, monkeypatch):
    _, user_id = share_env
    opted_in(user_id)
    notices = []
    monkeypatch.setattr(web_server, "_notify_user", lambda *args, **kwargs: notices.append(args))

    result = web_server._apply_share_connectivity_result(
        user_id, False, "AI API HTTP 401", "2026-07-27T10:00:00"
    )

    settings = models.get_user_settings(user_id)
    assert result == "suspended"
    assert settings["share_ai_results"] == 1
    assert settings["share_view_title"] == 1
    assert settings["share_view_translation"] == 0
    assert settings["share_view_summary"] == 1
    assert settings["share_suspended"] == 1
    assert settings["share_last_check_ok"] == 0
    assert len(notices) == 1
    assert notices[0][1] == "share_suspended"


def test_repeated_failure_updates_health_without_duplicate_notice(share_env, monkeypatch):
    _, user_id = share_env
    opted_in(user_id, suspended=1)
    notices = []
    monkeypatch.setattr(web_server, "_notify_user", lambda *args, **kwargs: notices.append(args))

    result = web_server._apply_share_connectivity_result(
        user_id, False, "still unavailable", "2026-07-27T11:00:00"
    )

    assert result == "unchanged"
    assert notices == []


def test_success_restores_exact_preferences_once(share_env, monkeypatch):
    _, user_id = share_env
    opted_in(user_id, suspended=1)
    notices = []
    monkeypatch.setattr(web_server, "_notify_user", lambda *args, **kwargs: notices.append(args))

    result = web_server._apply_share_connectivity_result(
        user_id, True, checked_at="2026-07-27T12:00:00"
    )

    settings = models.get_user_settings(user_id)
    assert result == "restored"
    assert settings["share_suspended"] == 0
    assert settings["share_view_title"] == 1
    assert settings["share_view_translation"] == 0
    assert settings["share_view_summary"] == 1
    assert settings["share_last_check_ok"] == 1
    assert len(notices) == 1
    assert notices[0][1] == "share_restored"

    assert web_server._apply_share_connectivity_result(
        user_id, True, checked_at="2026-07-27T12:01:00"
    ) == "unchanged"
    assert len(notices) == 1


def test_explicitly_disabled_user_is_never_auto_restored(share_env, monkeypatch):
    _, user_id = share_env
    _set_user_settings(
        user_id,
        share_ai_results=0,
        share_suspended=0,
        share_last_check_ok=0,
    )
    notices = []
    monkeypatch.setattr(web_server, "_notify_user", lambda *args, **kwargs: notices.append(args))

    result = web_server._apply_share_connectivity_result(user_id, True)

    assert result == "not_opted_in"
    assert models.get_user_settings(user_id)["share_ai_results"] == 0
    assert notices == []


def test_share_error_is_bounded_and_redacts_api_key(share_env, monkeypatch):
    _, user_id = share_env
    opted_in(user_id)
    notices = []
    monkeypatch.setattr(web_server, "_notify_user", lambda *args, **kwargs: notices.append(args))

    secret = "sk-super-secret-provider-key"
    web_server._apply_share_connectivity_result(user_id, False, f"provider rejected {secret} " + "x" * 400)

    settings = models.get_user_settings(user_id)
    assert secret not in settings["share_last_check_error"]
    assert settings["share_last_check_error"] == settings["share_last_check_error"].strip()
    assert len(settings["share_last_check_error"]) <= 300
    assert secret not in notices[0][3]


def test_suspended_opted_in_user_remains_scheduled_for_revalidation(share_env):
    _, user_id = share_env
    opted_in(user_id, suspended=1)
    assert user_id in models.get_users_with_share_enabled()


def test_periodic_revalidation_restores_suspended_user(share_env, monkeypatch):
    _, user_id = share_env
    opted_in(user_id, suspended=1)
    config = models.set_ai_config(user_id, api_key="key", enabled=1)
    monkeypatch.setattr(
        web_server,
        "get_ai_config",
        lambda uid: {
            "base_url": "https://provider.example",
            "api_key": "key",
            "revision": config["revision"],
        },
    )
    monkeypatch.setattr(
        web_server,
        "_run_ai_connection_test",
        lambda config: ({"ok": True}, 200),
    )
    notices = []
    monkeypatch.setattr(
        web_server,
        "_notify_user",
        lambda uid, *args, **kwargs: notices.append(uid),
    )
    monkeypatch.setattr(web_server.time, "sleep", lambda seconds: None)

    web_server._run_ai_share_revalidation_once()

    settings = models.get_user_settings(user_id)
    assert settings["share_suspended"] == 0
    assert web_server.is_share_active(settings) is True
    assert notices == [user_id]


def test_periodic_revalidation_sleeps_only_between_users(share_env, monkeypatch):
    _, user_id = share_env
    second_user = models.create_user("share-second@example.com", "pw", "share-second")["id"]
    for uid in (user_id, second_user):
        opted_in(uid)
        models.set_ai_config(uid, api_key=f"key-{uid}", enabled=1)
    sleeps = []
    monkeypatch.setattr(web_server, "_run_ai_connection_test", lambda config: ({"ok": True}, 200))
    monkeypatch.setattr(web_server.time, "sleep", sleeps.append)

    web_server._run_ai_share_revalidation_once()

    assert sleeps == [0.5]


def test_periodic_revalidation_does_not_sleep_after_only_user(share_env, monkeypatch):
    _, user_id = share_env
    opted_in(user_id)
    models.set_ai_config(user_id, api_key="key", enabled=1)
    sleeps = []
    monkeypatch.setattr(web_server, "_run_ai_connection_test", lambda config: ({"ok": True}, 200))
    monkeypatch.setattr(web_server.time, "sleep", sleeps.append)

    web_server._run_ai_share_revalidation_once()

    assert sleeps == []


def _run_concurrent_connectivity_results(user_id: int, *calls, apply_result=None):
    start = threading.Barrier(len(calls))
    results = []
    errors = []
    lock = threading.Lock()
    apply_result = apply_result or web_server._apply_share_connectivity_result

    def run(args):
        try:
            models.close_db()
            start.wait(timeout=5)
            result = apply_result(user_id, *args)
            with lock:
                results.append(result)
        except Exception as exc:  # pragma: no cover - surfaced below
            with lock:
                errors.append(exc)
        finally:
            models.close_db()

    threads = [threading.Thread(target=run, args=(args,)) for args in calls]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert not [thread for thread in threads if thread.is_alive()]
    assert errors == []
    return results


def test_concurrent_failures_suspend_once_and_send_one_notification(share_env, monkeypatch):
    _, user_id = share_env
    opted_in(user_id)
    notices = []
    notices_lock = threading.Lock()
    original_get_settings = web_server.get_user_settings
    both_reads = threading.Barrier(2)
    reads_lock = threading.Lock()
    reads = 0

    def synchronize_initial_read(uid):
        nonlocal reads
        settings = original_get_settings(uid)
        with reads_lock:
            reads += 1
            synchronize = reads <= 2
        if synchronize:
            both_reads.wait(timeout=5)
        return settings

    def record_notice(*args, **kwargs):
        with notices_lock:
            notices.append(args)

    monkeypatch.setattr(web_server, "get_user_settings", synchronize_initial_read)
    monkeypatch.setattr(web_server, "_notify_user", record_notice)

    results = _run_concurrent_connectivity_results(
        user_id,
        (False, "AI API HTTP 401", "2026-07-27T13:00:00"),
        (False, "AI API HTTP 401", "2026-07-27T13:00:01"),
    )

    assert sorted(results) == ["suspended", "unchanged"]
    assert [notice[1] for notice in notices] == ["share_suspended"]
    assert models.get_user_settings(user_id)["share_suspended"] == 1


def test_concurrent_successes_restore_once_and_send_one_notification(share_env, monkeypatch):
    _, user_id = share_env
    opted_in(user_id, suspended=1)
    notices = []
    notices_lock = threading.Lock()
    original_get_settings = web_server.get_user_settings
    both_reads = threading.Barrier(2)
    reads_lock = threading.Lock()
    reads = 0

    def synchronize_initial_read(uid):
        nonlocal reads
        settings = original_get_settings(uid)
        with reads_lock:
            reads += 1
            synchronize = reads <= 2
        if synchronize:
            both_reads.wait(timeout=5)
        return settings

    def record_notice(*args, **kwargs):
        with notices_lock:
            notices.append(args)

    monkeypatch.setattr(web_server, "get_user_settings", synchronize_initial_read)
    monkeypatch.setattr(web_server, "_notify_user", record_notice)

    results = _run_concurrent_connectivity_results(
        user_id,
        (True, "", "2026-07-27T14:00:00"),
        (True, "", "2026-07-27T14:00:01"),
    )

    assert sorted(results) == ["restored", "unchanged"]
    assert [notice[1] for notice in notices] == ["share_restored"]
    assert models.get_user_settings(user_id)["share_suspended"] == 0


def test_opt_out_winning_race_prevents_transition_notification(share_env, monkeypatch):
    _, user_id = share_env
    opted_in(user_id)
    notices = []
    original_get_settings = web_server.get_user_settings
    read_complete = threading.Event()
    release_transition = threading.Event()

    def pause_after_read(uid):
        settings = original_get_settings(uid)
        read_complete.set()
        assert release_transition.wait(timeout=5)
        return settings

    monkeypatch.setattr(web_server, "get_user_settings", pause_after_read)
    monkeypatch.setattr(web_server, "_notify_user", lambda *args, **kwargs: notices.append(args))
    result = []

    def apply_failure():
        try:
            models.close_db()
            result.append(web_server._apply_share_connectivity_result(user_id, False, "AI API HTTP 401"))
        finally:
            models.close_db()

    worker = threading.Thread(target=apply_failure)
    worker.start()
    assert read_complete.wait(timeout=5)
    _set_user_settings(user_id, share_ai_results=0)
    release_transition.set()
    worker.join(timeout=10)

    assert not worker.is_alive()
    assert result == ["not_opted_in"]
    assert notices == []
    settings = models.get_user_settings(user_id)
    assert settings["share_ai_results"] == 0
    assert settings["share_suspended"] == 0


@pytest.mark.parametrize(
    ("initial_suspended", "first_ok", "later_ok", "final_suspended"),
    (
        (0, False, True, 0),
        (1, True, False, 1),
    ),
)
def test_later_opposite_current_probe_retries_after_suspension_cas_mismatch(
    share_env,
    monkeypatch,
    initial_suspended,
    first_ok,
    later_ok,
    final_suspended,
):
    """A same-revision opposite probe must apply after the earlier real edge."""
    _, user_id = share_env
    opted_in(user_id, suspended=initial_suspended)
    revision = _config_revision(user_id)
    notices = []
    notices_lock = threading.Lock()
    original_get_settings = web_server.get_user_settings
    original_transition = web_server.apply_share_connectivity_transition
    both_initial_reads = threading.Barrier(2)
    reads_lock = threading.Lock()
    reads = 0
    first_transition_done = threading.Event()
    first_target = 0 if first_ok else 1
    later_target = 0 if later_ok else 1

    def synchronize_initial_read(uid):
        nonlocal reads
        settings = original_get_settings(uid)
        with reads_lock:
            reads += 1
            synchronize = reads <= 2
        if synchronize:
            both_initial_reads.wait(timeout=5)
        return settings

    def order_transitions(*args, **kwargs):
        expected = kwargs["expected_suspended"]
        target = kwargs["next_suspended"]
        if target == later_target and expected == initial_suspended:
            assert first_transition_done.wait(timeout=5)
        claimed = original_transition(*args, **kwargs)
        if target == first_target and expected == initial_suspended:
            first_transition_done.set()
        return claimed

    def record_notice(*args, **kwargs):
        with notices_lock:
            notices.append(args)

    monkeypatch.setattr(web_server, "get_user_settings", synchronize_initial_read)
    monkeypatch.setattr(
        web_server, "apply_share_connectivity_transition", order_transitions
    )
    monkeypatch.setattr(web_server, "_notify_user", record_notice)

    results = _run_concurrent_connectivity_results(
        user_id,
        (first_ok, "" if first_ok else "AI API HTTP 401", "2026-07-27T14:30:00", revision),
        (later_ok, "" if later_ok else "AI API HTTP 401", "2026-07-27T14:30:01", revision),
    )

    assert sorted(results) == ["restored", "suspended"]
    assert models.get_user_settings(user_id)["share_suspended"] == final_suspended
    assert sorted(notice[1] for notice in notices) == [
        "share_restored",
        "share_suspended",
    ]


def test_share_error_drops_provider_body_and_redacts_common_credentials(share_env, monkeypatch):
    _, user_id = share_env
    opted_in(user_id)
    notices = []
    monkeypatch.setattr(web_server, "_notify_user", lambda *args, **kwargs: notices.append(args))
    secrets = [
        "bearer-secret-value",
        "header-secret-value",
        "provider-api-key-value",
        "query-key-value",
        "form-token-value",
        "non-sk-provider-key-value",
    ]
    raw_error = (
        "AI API HTTP 401: provider response body: "
        "Bearer bearer-secret-value; Authorization: Bearer header-secret-value; "
        "api_key=provider-api-key-value&key=query-key-value&token=form-token-value; "
        "x-api-key: non-sk-provider-key-value"
    )

    web_server._apply_share_connectivity_result(user_id, False, raw_error)

    persisted = models.get_user_settings(user_id)["share_last_check_error"]
    notification_body = notices[0][3]
    assert persisted == "AI API HTTP 401"
    for secret in secrets:
        assert secret not in persisted
        assert secret not in notification_body
    assert "provider response body" not in persisted


def test_scheduled_error_is_sanitized_from_wrapper_through_settings_endpoint(
    share_env,
):
    client, user_id = share_env
    revision = _validated_opted_in(user_id)
    secrets = (
        "bearer-scheduled-secret",
        "scheduled-api-key",
        "scheduled-provider-body",
    )
    raw_error = (
        "AI API HTTP 401: provider response body scheduled-provider-body; "
        "Authorization: Bearer bearer-scheduled-secret; "
        "api_key=scheduled-api-key"
    )

    result = web_server._apply_share_background_connectivity_result(
        user_id,
        False,
        raw_error,
        checked_at="2026-07-30T10:00:00",
        config_revision=revision,
    )
    response = client.get("/settings", headers=auth_headers(user_id))

    assert result == "pending_failure"
    assert response.status_code == 200
    persisted = models.get_user_settings(user_id)
    data = response.get_json()
    assert persisted["share_revalidation_last_failure_error"] == "AI API HTTP 401"
    assert data["share_revalidation_last_failure_error"] == "AI API HTTP 401"
    serialized = f"{persisted!r}\n{response.get_data(as_text=True)}"
    for secret in secrets:
        assert secret not in serialized
    assert "provider response body" not in serialized


def test_saving_new_api_tests_and_restores_suspended_share(share_env, monkeypatch):
    client, user_id = share_env
    opted_in(user_id, suspended=1)
    monkeypatch.setattr(
        web_server,
        "_run_ai_connection_test",
        lambda config: ({"ok": True, "response": "pong"}, 200),
    )
    notices = []
    monkeypatch.setattr(web_server, "_notify_user", lambda *args, **kwargs: notices.append(args))

    response = client.put(
        "/ai/config",
        json={
            "provider": "OpenAI",
            "api_key": "replacement-key",
            "endpoint": "https://api.openai.com/v1",
            "model": "gpt-4o-mini",
            "provider_type": "openai",
            "enabled": 1,
        },
        headers=auth_headers(user_id),
    )

    assert response.status_code == 200
    assert response.get_json()["share_check"] == {
        "status": "restored",
        "restored": True,
    }
    assert models.get_user_settings(user_id)["share_suspended"] == 0
    assert [notice[1] for notice in notices] == ["share_restored"]


def test_failed_saved_config_remains_saved_but_share_stays_suspended(share_env, monkeypatch):
    client, user_id = share_env
    opted_in(user_id, suspended=1)
    monkeypatch.setattr(
        web_server,
        "_run_ai_connection_test",
        lambda config: ({"error": "AI API HTTP 401"}, 502),
    )

    response = client.put(
        "/ai/config",
        json={"api_key": "still-invalid", "enabled": 1},
        headers=auth_headers(user_id),
    )

    assert response.status_code == 200
    data = response.get_json()
    assert data["has_api_key"] is True
    assert data["share_check"]["status"] == "unchanged"
    assert data["share_check"]["restored"] is False
    assert "401" in data["share_check"]["error"]
    assert models.get_user_settings(user_id)["share_suspended"] == 1


def test_manual_connection_test_can_restore(share_env, monkeypatch):
    client, user_id = share_env
    opted_in(user_id, suspended=1)
    monkeypatch.setattr(
        web_server,
        "_run_ai_connection_test",
        lambda config: ({"ok": True, "response": "pong"}, 200),
    )
    monkeypatch.setattr(web_server, "_notify_user", lambda *args, **kwargs: None)

    response = client.post("/ai/test-connection", headers=auth_headers(user_id))

    assert response.status_code == 200
    assert response.get_json()["share_check"]["restored"] is True
    assert models.get_user_settings(user_id)["share_suspended"] == 0


def test_failed_share_enable_keeps_saved_preferences_and_pauses_existing_opt_in(
    share_env, monkeypatch
):
    client, user_id = share_env
    opted_in(user_id)
    models.set_ai_config(user_id, api_key="key", enabled=1)
    monkeypatch.setattr(
        web_server,
        "_run_ai_connection_test",
        lambda config: ({"error": "AI API HTTP 401"}, 502),
    )

    response = client.put(
        "/settings",
        json={"share_ai_results": 1, "share_view_translation": 1},
        headers=auth_headers(user_id),
    )

    assert response.status_code == 400
    assert response.get_json()["share_check"] == {
        "ok": False,
        "status": "paused",
        "error": "AI API HTTP 401",
    }
    settings = models.get_user_settings(user_id)
    assert settings["share_ai_results"] == 1
    assert settings["share_view_title"] == 1
    assert settings["share_view_translation"] == 0
    assert settings["share_view_summary"] == 1
    assert settings["share_suspended"] == 1


def test_share_enable_restores_prior_suspension_and_returns_active_state(share_env, monkeypatch):
    client, user_id = share_env
    opted_in(user_id, suspended=1)
    models.set_ai_config(user_id, api_key="key", enabled=1)
    monkeypatch.setattr(
        web_server,
        "_run_ai_connection_test",
        lambda config: ({"ok": True, "response": "pong"}, 200),
    )
    notices = []
    monkeypatch.setattr(web_server, "_notify_user", lambda *args, **kwargs: notices.append(args))

    response = client.put(
        "/settings",
        json={"share_ai_results": 1, "share_view_translation": 1},
        headers=auth_headers(user_id),
    )

    assert response.status_code == 200
    data = response.get_json()
    assert data["share_active"] is True
    assert data["share_suspended"] == 0
    assert data["share_view_title"] == 1
    assert data["share_view_translation"] == 1
    assert data["share_view_summary"] == 1
    assert [notice[1] for notice in notices] == ["share_restored"]


def test_manual_success_after_explicit_opt_out_does_not_restore_sharing(share_env, monkeypatch):
    client, user_id = share_env
    opted_in(user_id, suspended=1)
    models.set_ai_config(user_id, api_key="key", enabled=1)
    disabled = client.put(
        "/settings",
        json={"share_ai_results": 0},
        headers=auth_headers(user_id),
    )
    assert disabled.status_code == 200
    monkeypatch.setattr(
        web_server,
        "_run_ai_connection_test",
        lambda config: ({"ok": True, "response": "pong"}, 200),
    )

    response = client.post("/ai/test-connection", headers=auth_headers(user_id))

    assert response.status_code == 200
    assert "share_check" not in response.get_json()
    settings = models.get_user_settings(user_id)
    assert settings["share_ai_results"] == 0
    assert settings["share_suspended"] == 0
    assert all(settings[key] == 0 for key in (
        "share_view_title", "share_view_translation", "share_view_summary",
    ))


def test_manual_connection_failure_redacts_provider_error_in_every_response_field(
    share_env, monkeypatch
):
    client, user_id = share_env
    opted_in(user_id)
    raw_error = (
        "AI API HTTP 401: provider response body: Bearer bearer-secret-value; "
        "api_key=provider-api-key-value&token=form-token-value"
    )
    monkeypatch.setattr(
        web_server,
        "_run_ai_connection_test",
        lambda config: ({"error": raw_error}, 502),
    )

    response = client.post("/ai/test-connection", headers=auth_headers(user_id))

    assert response.status_code == 502
    data = response.get_json()
    assert data["error"] == "AI API HTTP 401"
    assert data["share_check"]["error"] == "AI API HTTP 401"
    serialized = str(data)
    for secret in ("bearer-secret-value", "provider-api-key-value", "form-token-value"):
        assert secret not in serialized
    assert "provider response body" not in serialized


def test_first_share_enable_failure_reports_not_opted_in_not_paused(share_env, monkeypatch):
    client, user_id = share_env
    monkeypatch.setattr(
        web_server,
        "_run_ai_connection_test",
        lambda config: ({"error": "AI API HTTP 401"}, 502),
    )

    response = client.put(
        "/settings",
        json={"share_ai_results": 1},
        headers=auth_headers(user_id),
    )

    assert response.status_code == 400
    assert response.get_json()["share_check"] == {
        "ok": False,
        "status": "not_opted_in",
        "error": "AI API HTTP 401",
    }
    assert models.get_user_settings(user_id) is None


def _config_revision(user_id: int) -> int:
    config = models.get_ai_config(user_id)
    if not config:
        config = models.set_ai_config(user_id, api_key="initial-key", enabled=1)
    return config["revision"]


def test_old_slow_failure_cannot_override_new_fast_success(share_env, monkeypatch):
    _, user_id = share_env
    opted_in(user_id)
    old_revision = _config_revision(user_id)
    notices = []
    monkeypatch.setattr(web_server, "_notify_user", lambda *args, **kwargs: notices.append(args))

    models.set_ai_config(user_id, api_key="replacement-key")
    new_revision = _config_revision(user_id)
    assert new_revision > old_revision
    assert web_server._apply_share_connectivity_result(
        user_id, True, config_revision=new_revision
    ) == "unchanged"
    before = models.get_user_settings(user_id)

    assert web_server._apply_share_connectivity_result(
        user_id, False, "AI API HTTP 401", config_revision=old_revision
    ) == "stale"
    assert models.get_user_settings(user_id) == before
    assert notices == []


def test_old_slow_success_cannot_override_new_fast_failure(share_env, monkeypatch):
    _, user_id = share_env
    opted_in(user_id, suspended=1)
    old_revision = _config_revision(user_id)
    notices = []
    monkeypatch.setattr(web_server, "_notify_user", lambda *args, **kwargs: notices.append(args))

    models.set_ai_config(user_id, api_key="replacement-key")
    new_revision = _config_revision(user_id)
    assert web_server._apply_share_connectivity_result(
        user_id, False, "AI API HTTP 401", config_revision=new_revision
    ) == "unchanged"
    before = models.get_user_settings(user_id)

    assert web_server._apply_share_connectivity_result(
        user_id, True, config_revision=old_revision
    ) == "stale"
    assert models.get_user_settings(user_id) == before
    assert notices == []


def test_old_manual_probe_after_new_save_is_stale(share_env, monkeypatch):
    _, user_id = share_env
    opted_in(user_id)
    old_revision = _config_revision(user_id)
    notices = []
    monkeypatch.setattr(web_server, "_notify_user", lambda *args, **kwargs: notices.append(args))

    models.set_ai_config(user_id, api_key="replacement-key")
    before = models.get_user_settings(user_id)
    share_check = web_server._share_check_after_personal_api_test(
        user_id, {"error": "AI API HTTP 401"}, 502, old_revision
    )

    assert share_check == {
        "status": "stale",
        "restored": False,
        "error": "AI API HTTP 401",
    }
    assert models.get_user_settings(user_id) == before
    assert notices == []


def test_old_manual_probe_after_opt_out_does_not_change_health_or_notify(share_env, monkeypatch):
    _, user_id = share_env
    opted_in(user_id)
    old_revision = _config_revision(user_id)
    notices = []
    monkeypatch.setattr(web_server, "_notify_user", lambda *args, **kwargs: notices.append(args))
    _set_user_settings(user_id, share_ai_results=0, share_suspended=0)
    before = models.get_user_settings(user_id)

    share_check = web_server._share_check_after_personal_api_test(
        user_id, {"ok": True}, 200, old_revision
    )

    assert share_check is None
    assert models.get_user_settings(user_id) == before
    assert notices == []


def _validated_opted_in(user_id: int, *, suspended: int = 0) -> int:
    opted_in(user_id, suspended=suspended)
    config = models.set_ai_config(user_id, api_key="validated-key", enabled=1)
    assert web_server._apply_share_connectivity_result(
        user_id, not suspended, config_revision=config["revision"]
    ) in {"unchanged", "restored"}
    return config["revision"]


def test_periodic_failure_needs_two_consecutive_cycles(share_env, monkeypatch):
    _, user_id = share_env
    revision = _validated_opted_in(user_id)
    monkeypatch.setattr(
        web_server,
        "_run_ai_connection_test",
        lambda config: ({"error": "AI API HTTP 503"}, 502),
    )
    notices = []
    monkeypatch.setattr(
        web_server, "_notify_user", lambda *args, **kwargs: notices.append(args)
    )

    web_server._run_ai_share_revalidation_once()
    first = models.get_user_settings(user_id)
    assert first["share_revalidation_failure_streak"] == 1
    assert first["share_suspended"] == 0
    assert web_server.is_share_active(first) is True
    assert notices == []

    web_server._run_ai_share_revalidation_once()
    second = models.get_user_settings(user_id)
    assert second["share_revalidation_failure_streak"] == 2
    assert second["share_suspended"] == 1
    assert second["share_last_check_revision"] == revision
    assert [notice[1] for notice in notices] == ["share_suspended"]


def test_concurrent_duplicate_failures_from_same_scheduled_cycle_count_once(
    share_env, monkeypatch
):
    _, user_id = share_env
    revision = _validated_opted_in(user_id)
    notices = []
    notices_lock = threading.Lock()

    def record_notice(*args, **kwargs):
        with notices_lock:
            notices.append(args)

    monkeypatch.setattr(web_server, "_notify_user", record_notice)

    results = _run_concurrent_connectivity_results(
        user_id,
        (False, "AI API HTTP 503", "2026-07-30T10:00:00", revision),
        (False, "AI API HTTP 503", "2026-07-30T10:00:00", revision),
        apply_result=web_server._apply_share_background_connectivity_result,
    )

    assert sorted(results) == ["pending_failure", "unchanged"]
    settings = models.get_user_settings(user_id)
    assert settings["share_revalidation_failure_streak"] == 1
    assert settings["share_suspended"] == 0
    assert notices == []


def test_periodic_revalidation_continues_after_one_users_state_application_error(
    share_env, monkeypatch, capsys
):
    _, first_user_id = share_env
    second_user_id = models.create_user(
        "share-second@example.com", "pw", "share-second"
    )["id"]
    for user_id in (first_user_id, second_user_id):
        _validated_opted_in(user_id)

    original_record_failure = web_server.record_share_revalidation_failure

    def fail_first_state_application(user_id, *args, **kwargs):
        if user_id == first_user_id:
            raise RuntimeError("state application exploded")
        return original_record_failure(user_id, *args, **kwargs)

    monkeypatch.setattr(
        web_server,
        "get_users_with_share_enabled",
        lambda: [first_user_id, second_user_id],
    )
    monkeypatch.setattr(
        web_server,
        "_run_ai_connection_test",
        lambda config: ({"error": "AI API HTTP 503"}, 502),
    )
    monkeypatch.setattr(
        web_server, "record_share_revalidation_failure", fail_first_state_application
    )
    monkeypatch.setattr(web_server.time, "sleep", lambda seconds: None)

    web_server._run_ai_share_revalidation_once()

    assert (
        models.get_user_settings(first_user_id)[
            "share_revalidation_failure_streak"
        ]
        == 0
    )
    assert (
        models.get_user_settings(second_user_id)[
            "share_revalidation_failure_streak"
        ]
        == 1
    )
    output = capsys.readouterr().out
    assert str(first_user_id) in output
    assert "state application exploded" in output


def test_scheduled_failure_transaction_rolls_back_non_sqlite_exceptions(
    share_env, monkeypatch
):
    _, user_id = share_env
    revision = _validated_opted_in(user_id)
    raw_db = models.get_db()

    class FailingUpdateConnection:
        def __init__(self, connection):
            self.connection = connection
            self.rollback_calls = 0

        def execute(self, sql, *args, **kwargs):
            if sql.startswith("UPDATE user_settings"):
                raise RuntimeError("state update hook failed")
            return self.connection.execute(sql, *args, **kwargs)

        def commit(self):
            return self.connection.commit()

        def rollback(self):
            self.rollback_calls += 1
            return self.connection.rollback()

    failing_db = FailingUpdateConnection(raw_db)
    monkeypatch.setattr(models, "get_db", lambda: failing_db)

    with pytest.raises(RuntimeError, match="state update hook failed"):
        models.record_share_revalidation_failure(
            user_id,
            revision,
            "2026-07-30T10:00:00",
            "AI API HTTP 503",
        )

    assert failing_db.rollback_calls == 1
    assert raw_db.in_transaction is False


def test_scheduled_success_resets_failure_streak_before_next_failure(
    share_env, monkeypatch
):
    _, user_id = share_env
    revision = _validated_opted_in(user_id)
    notices = []
    monkeypatch.setattr(
        web_server, "_notify_user", lambda *args, **kwargs: notices.append(args)
    )

    assert web_server._apply_share_background_connectivity_result(
        user_id,
        False,
        "AI API HTTP 503",
        "2026-07-30T10:00:00",
        revision,
    ) == "pending_failure"
    assert models.get_user_settings(user_id)["share_revalidation_failure_streak"] == 1

    assert web_server._apply_share_background_connectivity_result(
        user_id,
        True,
        checked_at="2026-07-30T10:30:00",
        config_revision=revision,
    ) == "unchanged"
    after_success = models.get_user_settings(user_id)
    assert after_success["share_revalidation_failure_streak"] == 0

    assert web_server._apply_share_background_connectivity_result(
        user_id,
        False,
        "AI API HTTP 503",
        "2026-07-30T11:00:00",
        revision,
    ) == "pending_failure"
    after_next_failure = models.get_user_settings(user_id)
    assert after_next_failure["share_revalidation_failure_streak"] == 1
    assert after_next_failure["share_suspended"] == 0
    assert notices == []


def test_manual_failure_after_pending_scheduled_failure_suspends_immediately(
    share_env, monkeypatch
):
    _, user_id = share_env
    revision = _validated_opted_in(user_id)
    notices = []
    monkeypatch.setattr(
        web_server, "_notify_user", lambda *args, **kwargs: notices.append(args)
    )
    assert web_server._apply_share_background_connectivity_result(
        user_id,
        False,
        "AI API HTTP 503",
        "2026-07-30T10:00:00",
        revision,
    ) == "pending_failure"

    result = web_server._apply_share_connectivity_result(
        user_id,
        False,
        "AI API HTTP 401",
        "2026-07-30T10:01:00",
        revision,
    )

    assert result == "suspended"
    settings = models.get_user_settings(user_id)
    assert settings["share_suspended"] == 1
    assert settings["share_revalidation_failure_streak"] == 0
    assert [notice[1] for notice in notices] == ["share_suspended"]


def test_manual_success_clears_pending_scheduled_failure(share_env, monkeypatch):
    _, user_id = share_env
    revision = _validated_opted_in(user_id)
    notices = []
    monkeypatch.setattr(
        web_server, "_notify_user", lambda *args, **kwargs: notices.append(args)
    )
    assert web_server._apply_share_background_connectivity_result(
        user_id,
        False,
        "AI API HTTP 503",
        "2026-07-30T10:00:00",
        revision,
    ) == "pending_failure"

    result = web_server._apply_share_connectivity_result(
        user_id,
        True,
        checked_at="2026-07-30T10:01:00",
        config_revision=revision,
    )

    assert result == "unchanged"
    settings = models.get_user_settings(user_id)
    assert settings["share_revalidation_failure_streak"] == 0
    assert settings["share_revalidation_failure_revision"] is None
    assert settings["share_revalidation_last_failure_at"] is None
    assert settings["share_revalidation_last_failure_error"] is None
    assert notices == []


def test_stale_background_result_leaves_current_state_untouched(
    share_env, monkeypatch
):
    _, user_id = share_env
    old_revision = _validated_opted_in(user_id)
    models.set_ai_config(user_id, api_key="replacement-key")
    before = models.get_user_settings(user_id)
    notices = []
    monkeypatch.setattr(
        web_server, "_notify_user", lambda *args, **kwargs: notices.append(args)
    )

    result = web_server._apply_share_background_connectivity_result(
        user_id,
        False,
        "AI API HTTP 503",
        "2026-07-30T10:00:00",
        old_revision,
    )

    assert result == "stale"
    assert models.get_user_settings(user_id) == before
    assert notices == []


def test_missing_ai_config_uses_revision_zero_and_suspends_after_two_cycles(
    share_env, monkeypatch
):
    _, user_id = share_env
    _set_user_settings(
        user_id,
        share_ai_results=1,
        share_view_summary=1,
        share_suspended=0,
        share_last_check_ok=0,
    )
    notices = []
    monkeypatch.setattr(
        web_server, "_notify_user", lambda *args, **kwargs: notices.append(args)
    )

    first = web_server._apply_share_background_connectivity_result(
        user_id,
        False,
        "AI API HTTP 503",
        checked_at="2026-07-30T10:00:00",
    )
    second = web_server._apply_share_background_connectivity_result(
        user_id,
        False,
        "AI API HTTP 503",
        checked_at="2026-07-30T11:00:00",
    )

    assert (first, second) == ("pending_failure", "suspended")
    settings = models.get_user_settings(user_id)
    assert settings["share_revalidation_failure_streak"] == 2
    assert settings["share_revalidation_failure_revision"] == 0
    assert settings["share_suspended"] == 1
    assert settings["share_last_check_revision"] == 0
    assert [notice[1] for notice in notices] == ["share_suspended"]


def test_explicit_opt_out_clears_pending_scheduled_failure(share_env):
    client, user_id = share_env
    revision = _validated_opted_in(user_id)
    assert models.record_share_revalidation_failure(
        user_id,
        revision,
        "2026-07-30T10:00:00",
        "AI API HTTP 503",
    ) == "pending_failure"

    response = client.put(
        "/settings",
        json={"share_ai_results": 0},
        headers=auth_headers(user_id),
    )

    assert response.status_code == 200
    settings = models.get_user_settings(user_id)
    assert settings["share_suspended"] == 0
    assert settings["share_revalidation_failure_streak"] == 0
    assert settings["share_revalidation_failure_revision"] is None
    assert settings["share_revalidation_last_failure_at"] is None
    assert settings["share_revalidation_last_failure_error"] is None


def test_share_revalidation_failure_fields_migrate_and_round_trip(share_env):
    _, user_id = share_env
    settings = _set_user_settings(user_id, theme_preference="system")
    assert settings["share_revalidation_failure_streak"] == 0
    assert settings["share_revalidation_failure_revision"] is None
    assert settings["share_revalidation_last_failure_at"] is None
    assert settings["share_revalidation_last_failure_error"] is None

    settings = models.set_share_health(
        user_id,
        share_revalidation_failure_streak=1,
        share_revalidation_failure_revision=7,
        share_revalidation_last_failure_at="2026-07-30T10:00:00",
        share_revalidation_last_failure_error="AI API HTTP 503",
    )
    assert settings["share_revalidation_failure_streak"] == 1
    assert settings["share_revalidation_failure_revision"] == 7
    assert settings["share_revalidation_last_failure_at"] == "2026-07-30T10:00:00"
    assert settings["share_revalidation_last_failure_error"] == "AI API HTTP 503"


def test_first_scheduled_failure_is_durable_without_revoking_access(share_env):
    _, user_id = share_env
    revision = _validated_opted_in(user_id)

    result = models.record_share_revalidation_failure(
        user_id,
        revision,
        "2026-07-30T10:00:00",
        "AI API HTTP 503",
    )

    assert result == "pending_failure"
    settings = models.get_user_settings(user_id)
    assert settings["share_revalidation_failure_streak"] == 1
    assert settings["share_revalidation_failure_revision"] == revision
    assert settings["share_suspended"] == 0
    assert settings["share_last_check_ok"] == 1
    assert web_server.is_share_active(settings) is True


def test_second_scheduled_failure_atomically_suspends(share_env):
    _, user_id = share_env
    revision = _validated_opted_in(user_id)
    models.record_share_revalidation_failure(
        user_id, revision, "2026-07-30T10:00:00", "AI API HTTP 503"
    )

    result = models.record_share_revalidation_failure(
        user_id, revision, "2026-07-30T11:00:00", "AI API HTTP 503"
    )

    assert result == "suspended"
    settings = models.get_user_settings(user_id)
    assert settings["share_revalidation_failure_streak"] == 2
    assert settings["share_suspended"] == 1
    assert settings["share_last_check_ok"] == 0
    assert settings["share_last_check_revision"] == revision
    assert web_server.is_share_active(settings) is False


def test_concurrent_second_scheduled_failures_suspend_once_and_notify_once(
    share_env, monkeypatch
):
    _, user_id = share_env
    revision = _validated_opted_in(user_id)
    assert models.record_share_revalidation_failure(
        user_id,
        revision,
        "2026-07-30T10:00:00",
        "AI API HTTP 503",
    ) == "pending_failure"
    notices = []
    notices_lock = threading.Lock()

    def record_notice(*args, **kwargs):
        with notices_lock:
            notices.append(args)

    monkeypatch.setattr(web_server, "_notify_user", record_notice)

    results = _run_concurrent_connectivity_results(
        user_id,
        (False, "AI API HTTP 503", "2026-07-30T11:00:00", revision),
        (False, "AI API HTTP 503", "2026-07-30T11:00:01", revision),
        apply_result=web_server._apply_share_background_connectivity_result,
    )

    assert sorted(results) == ["suspended", "unchanged"]
    assert [notice[1] for notice in notices] == ["share_suspended"]


def test_scheduled_failure_for_opted_out_user_is_ignored(share_env):
    _, user_id = share_env
    revision = _validated_opted_in(user_id)
    _set_user_settings(user_id, share_ai_results=0)

    result = models.record_share_revalidation_failure(
        user_id, revision, "2026-07-30T10:00:00", "AI API HTTP 503"
    )

    assert result == "not_opted_in"
    assert models.get_user_settings(user_id)["share_revalidation_failure_streak"] == 0


def test_scheduled_failure_for_mismatched_config_revision_is_stale(share_env):
    _, user_id = share_env
    revision = _validated_opted_in(user_id)

    result = models.record_share_revalidation_failure(
        user_id, revision + 1, "2026-07-30T10:00:00", "AI API HTTP 503"
    )

    assert result == "stale"
    assert models.get_user_settings(user_id)["share_revalidation_failure_streak"] == 0


def test_first_scheduled_failure_for_new_revision_starts_new_streak(share_env):
    _, user_id = share_env
    old_revision = _validated_opted_in(user_id)
    assert models.record_share_revalidation_failure(
        user_id, old_revision, "2026-07-30T10:00:00", "AI API HTTP 503"
    ) == "pending_failure"
    new_revision = models.set_ai_config(user_id, api_key="replacement-key")["revision"]

    result = models.record_share_revalidation_failure(
        user_id, new_revision, "2026-07-30T11:00:00", "AI API HTTP 503"
    )

    assert result == "pending_failure"
    settings = models.get_user_settings(user_id)
    assert settings["share_revalidation_failure_streak"] == 1
    assert settings["share_revalidation_failure_revision"] == new_revision


def test_scheduled_failure_for_suspended_user_does_not_create_another_edge(share_env):
    _, user_id = share_env
    revision = _validated_opted_in(user_id, suspended=1)

    result = models.record_share_revalidation_failure(
        user_id, revision, "2026-07-30T10:00:00", "AI API HTTP 503"
    )

    assert result == "unchanged"
    settings = models.get_user_settings(user_id)
    assert settings["share_revalidation_failure_streak"] == 0
    assert settings["share_revalidation_failure_revision"] is None


@pytest.mark.parametrize(
    ("next_suspended", "check_ok", "error"),
    (
        (0, 1, None),
        (1, 0, "AI API HTTP 401"),
    ),
)
def test_applied_connectivity_result_clears_scheduled_failure(
    share_env, next_suspended, check_ok, error
):
    _, user_id = share_env
    revision = _validated_opted_in(user_id)
    assert models.record_share_revalidation_failure(
        user_id, revision, "2026-07-30T10:00:00", "AI API HTTP 503"
    ) == "pending_failure"

    changed = models.apply_share_connectivity_transition(
        user_id,
        expected_suspended=0,
        expected_config_revision=revision,
        next_suspended=next_suspended,
        checked_at="2026-07-30T10:01:00",
        check_ok=check_ok,
        error=error,
    )

    assert changed is True
    settings = models.get_user_settings(user_id)
    assert settings["share_revalidation_failure_streak"] == 0
    assert settings["share_revalidation_failure_revision"] is None
    assert settings["share_revalidation_last_failure_at"] is None
    assert settings["share_revalidation_last_failure_error"] is None


@pytest.mark.parametrize("make_ineligible", ("opt_out", "new_revision"))
def test_ineligible_connectivity_result_cannot_clear_scheduled_failure(
    share_env, make_ineligible
):
    _, user_id = share_env
    revision = _validated_opted_in(user_id)
    assert models.record_share_revalidation_failure(
        user_id, revision, "2026-07-30T10:00:00", "AI API HTTP 503"
    ) == "pending_failure"
    if make_ineligible == "opt_out":
        _set_user_settings(user_id, share_ai_results=0)
    else:
        models.set_ai_config(user_id, api_key="replacement-key")

    changed = models.apply_share_connectivity_transition(
        user_id,
        expected_suspended=0,
        expected_config_revision=revision,
        next_suspended=0,
        checked_at="2026-07-30T10:01:00",
        check_ok=1,
        error=None,
    )

    assert changed is False
    settings = models.get_user_settings(user_id)
    assert settings["share_revalidation_failure_streak"] == 1
    assert settings["share_revalidation_failure_revision"] == revision
    assert settings["share_revalidation_last_failure_at"] == "2026-07-30T10:00:00"
    assert settings["share_revalidation_last_failure_error"] == "AI API HTTP 503"


def test_new_config_save_is_effectively_inactive_until_its_probe_finishes(share_env, monkeypatch):
    client, user_id = share_env
    old_revision = _validated_opted_in(user_id)
    settings = models.get_user_settings(user_id)
    assert settings["share_last_check_revision"] == old_revision
    assert web_server.is_share_active(settings) is True
    probe_started = threading.Event()
    allow_probe = threading.Event()
    probe_response = {}
    notices = []
    monkeypatch.setattr(web_server, "_notify_user", lambda *args, **kwargs: notices.append(args))

    def delayed_probe(config):
        if config["api_key"] == "new-key":
            probe_started.set()
            assert allow_probe.wait(timeout=5)
        return {"ok": True, "response": "pong"}, 200

    def save_new_config():
        try:
            worker_client = web_server.app.test_client()
            probe_response["response"] = worker_client.put(
                "/ai/config",
                json={"api_key": "new-key", "enabled": 1},
                headers=auth_headers(user_id),
            )
        finally:
            models.close_db()

    monkeypatch.setattr(
        web_server,
        "_get_ai_result",
        lambda article_id, **kwargs: {"summary": "shared", "updated_at": "now"},
    )
    monkeypatch.setattr(web_server, "_run_ai_connection_test", delayed_probe)

    worker = threading.Thread(target=save_new_config)
    worker.start()
    assert probe_started.wait(timeout=5)

    settings = models.get_user_settings(user_id)
    assert settings["share_suspended"] == 0
    assert settings["share_last_check_revision"] == old_revision
    assert settings["share_current_config_revision"] > old_revision
    assert web_server.is_share_active(settings) is False
    assert client.get("/settings", headers=auth_headers(user_id)).get_json()["share_active"] is False
    result = client.get("/ai/result/42", headers=auth_headers(user_id)).get_json()
    assert result["updated_at"] == "now"
    assert result["access"]["share_active"] is False
    assert not {"summary", "translation"} & result.keys()
    assert notices == []

    allow_probe.set()
    worker.join(timeout=10)
    assert not worker.is_alive()
    assert probe_response["response"].status_code == 200
    assert probe_response["response"].get_json()["share_check"]["status"] == "unchanged"
    assert web_server.is_share_active(models.get_user_settings(user_id)) is True
    assert notices == []


def test_new_config_success_validates_its_revision_without_fake_restore_notice(share_env, monkeypatch):
    _, user_id = share_env
    old_revision = _validated_opted_in(user_id)
    notices = []
    monkeypatch.setattr(web_server, "_notify_user", lambda *args, **kwargs: notices.append(args))
    config = models.set_ai_config(user_id, api_key="new-key")

    assert web_server._apply_share_connectivity_result(
        user_id, True, config_revision=config["revision"]
    ) == "unchanged"

    settings = models.get_user_settings(user_id)
    assert settings["share_last_check_revision"] == config["revision"]
    assert settings["share_last_check_revision"] > old_revision
    assert web_server.is_share_active(settings) is True
    assert notices == []


def test_new_config_failure_records_its_revision_and_pauses(share_env, monkeypatch):
    _, user_id = share_env
    _validated_opted_in(user_id)
    notices = []
    monkeypatch.setattr(web_server, "_notify_user", lambda *args, **kwargs: notices.append(args))
    config = models.set_ai_config(user_id, api_key="new-key")

    assert web_server._apply_share_connectivity_result(
        user_id, False, "AI API HTTP 401", config_revision=config["revision"]
    ) == "suspended"

    settings = models.get_user_settings(user_id)
    assert settings["share_suspended"] == 1
    assert settings["share_last_check_ok"] == 0
    assert settings["share_last_check_revision"] == config["revision"]
    assert web_server.is_share_active(settings) is False
    assert [notice[1] for notice in notices] == ["share_suspended"]


def test_stale_manual_and_periodic_probes_cannot_block_current_settings_validation(
    share_env, monkeypatch
):
    client, user_id = share_env
    old_revision = _validated_opted_in(user_id)
    old_config = models.get_ai_config(user_id)
    new_config = models.set_ai_config(user_id, api_key="new-key")
    before = models.get_user_settings(user_id)
    notices = []
    monkeypatch.setattr(web_server, "_notify_user", lambda *args, **kwargs: notices.append(args))

    manual = web_server._share_check_after_personal_api_test(
        user_id, {"error": "AI API HTTP 401"}, 502, old_revision
    )
    monkeypatch.setattr(web_server, "get_ai_config", lambda uid: old_config)
    monkeypatch.setattr(web_server, "_run_ai_connection_test", lambda config: ({"ok": True}, 200))
    monkeypatch.setattr(web_server.time, "sleep", lambda seconds: None)
    web_server._run_ai_share_revalidation_once()

    assert manual["status"] == "stale"
    assert models.get_user_settings(user_id) == before
    assert notices == []

    monkeypatch.setattr(web_server, "get_ai_config", models.get_ai_config)
    response = client.put(
        "/settings",
        json={"share_ai_results": 1},
        headers=auth_headers(user_id),
    )

    assert response.status_code == 200
    assert models.get_user_settings(user_id)["share_last_check_revision"] == new_config["revision"]
    assert response.get_json()["share_active"] is True


def test_settings_enable_rejects_config_revision_changed_during_validation(
    share_env, monkeypatch
):
    client, user_id = share_env
    old_config = models.set_ai_config(user_id, api_key="old-key", enabled=1)
    _set_user_settings(
        user_id,
        share_ai_results=0,
        share_view_title=0,
        share_view_translation=0,
        share_view_summary=0,
        share_suspended=0,
        share_last_check_ok=0,
    )
    probe_started = threading.Event()
    allow_probe = threading.Event()
    enable_response = {}
    notices = []
    monkeypatch.setattr(web_server, "_notify_user", lambda *args, **kwargs: notices.append(args))

    def delayed_probe(config):
        assert config["revision"] == old_config["revision"]
        probe_started.set()
        assert allow_probe.wait(timeout=5)
        return {"ok": True, "response": "pong"}, 200

    def enable_sharing():
        try:
            worker_client = web_server.app.test_client()
            enable_response["response"] = worker_client.put(
                "/settings",
                json={"share_ai_results": 1, "share_view_title": 1},
                headers=auth_headers(user_id),
            )
        finally:
            models.close_db()

    monkeypatch.setattr(web_server, "_run_ai_connection_test", delayed_probe)
    worker = threading.Thread(target=enable_sharing)
    worker.start()
    assert probe_started.wait(timeout=5)

    saved = client.put(
        "/ai/config",
        json={"api_key": "new-key", "enabled": 1},
        headers=auth_headers(user_id),
    )
    assert saved.status_code == 200
    assert "share_check" not in saved.get_json()
    assert models.get_ai_config(user_id)["revision"] > old_config["revision"]

    allow_probe.set()
    worker.join(timeout=10)
    assert not worker.is_alive()

    response = enable_response["response"]
    assert response.status_code == 409
    assert response.get_json()["share_check"] == {
        "ok": False,
        "status": "stale_validation",
        "error": "AI config changed during validation; retry enabling sharing",
    }
    settings = models.get_user_settings(user_id)
    assert settings["share_ai_results"] == 0
    assert settings["share_view_title"] == 0
    assert settings["share_suspended"] == 0
    assert web_server.is_share_active(settings) is False
    assert notices == []


def test_slow_settings_restore_cannot_overwrite_later_explicit_opt_out(
    share_env, monkeypatch
):
    client, user_id = share_env
    opted_in(user_id, suspended=1)
    models.set_ai_config(user_id, api_key="validated-key", enabled=1)
    observed_revision = models.get_user_settings(user_id)["share_intent_revision"]
    probe_started = threading.Event()
    allow_probe = threading.Event()
    restore_response = {}
    notices = []

    def delayed_probe(config):
        probe_started.set()
        assert allow_probe.wait(timeout=5)
        return {"ok": True, "response": "pong"}, 200

    def restore_sharing():
        try:
            worker_client = web_server.app.test_client()
            restore_response["response"] = worker_client.put(
                "/settings",
                json={
                    "share_ai_results": 1,
                    "share_view_title": 1,
                    "share_view_translation": 1,
                    "share_view_summary": 1,
                },
                headers=auth_headers(user_id),
            )
        finally:
            models.close_db()

    monkeypatch.setattr(web_server, "_run_ai_connection_test", delayed_probe)
    monkeypatch.setattr(
        web_server, "_notify_user", lambda *args, **kwargs: notices.append(args)
    )
    worker = threading.Thread(target=restore_sharing)
    worker.start()
    assert probe_started.wait(timeout=5)

    disabled = client.put(
        "/settings",
        json={"share_ai_results": 0},
        headers=auth_headers(user_id),
    )
    assert disabled.status_code == 200
    opted_out = models.get_user_settings(user_id)
    assert opted_out["share_intent_revision"] > observed_revision

    allow_probe.set()
    worker.join(timeout=10)
    assert not worker.is_alive()

    response = restore_response["response"]
    assert response.status_code == 409
    assert response.get_json()["share_check"]["status"] == "stale_settings"
    settings = models.get_user_settings(user_id)
    assert settings["share_ai_results"] == 0
    assert settings["share_suspended"] == 0
    assert settings["share_view_title"] == 0
    assert settings["share_view_translation"] == 0
    assert settings["share_view_summary"] == 0
    assert settings["share_intent_revision"] == opted_out["share_intent_revision"]
    assert notices == []
