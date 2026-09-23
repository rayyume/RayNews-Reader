"""Regression tests for the security/correctness-review fixes:
1. Non-admin users can no longer rewrite the canonical (site-wide) article
   title via POST /ai/result/<id>.
2. Manually-translated article HTML is whitelist-sanitized in the browser
   before it's ever innerHTML-rendered, not just when it later enters the
   shared server-side cache.
3. Daily-summary send state is persisted per (date, user_id) instead of an
   in-memory set, so a mid-window restart can't cause a duplicate broadcast
   and a single recipient's transient failure gets retried.
4. refresh_server.py's in-memory article-detail cache is invalidated
   immediately when web_server.py updates an article's title/body, instead
   of only self-healing on the next ~15min fetcher cycle.
"""

import datetime as dt
import json
import os
import re
import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import notifier
import refresh_server
import web_server
import models


def _active_share_settings():
    return {
        "share_ai_results": 1,
        "share_suspended": 0,
        "share_last_check_ok": 1,
        "share_last_check_revision": 4,
        "share_current_config_revision": 4,
        "share_view_summary": 1,
        "share_view_translation": 1,
    }


def _auth_headers(user_id=7, role="user"):
    return {"Authorization": f"Bearer {web_server.create_token(user_id, role)}"}


@pytest.fixture
def authenticated_request(monkeypatch):
    monkeypatch.setattr(
        models,
        "get_user",
        lambda user_id: {"id": user_id, "role": "user"},
    )
    monkeypatch.setattr(models, "record_access", lambda user_id: None)


def test_unhandled_error_is_generic_but_logged_with_detail(
    monkeypatch, caplog, authenticated_request
):
    monkeypatch.setattr(web_server, "get_user_settings", lambda user_id: _active_share_settings())
    monkeypatch.setattr(
        web_server,
        "_fetch_article_body",
        lambda article_id: (_ for _ in ()).throw(RuntimeError("secret detail /app/data/news.db")),
    )

    with caplog.at_level("ERROR"):
        response = web_server.app.test_client().post(
            "/ai/result/42",
            headers=_auth_headers(),
            json={"summary": "local result"},
        )

    assert response.status_code == 500
    assert response.get_json() == {"error": "internal server error"}
    assert "secret detail /app/data/news.db" in caplog.text


def test_explicit_client_error_keeps_its_status_and_payload(
    monkeypatch, authenticated_request
):
    monkeypatch.setattr(web_server, "get_user_settings", lambda user_id: _active_share_settings())
    monkeypatch.setattr(web_server, "_fetch_article_body", lambda article_id: None)

    response = web_server.app.test_client().post(
        "/ai/result/42",
        headers=_auth_headers(),
        json={"summary": "local result"},
    )

    assert response.status_code == 404
    assert response.get_json() == {"error": "article not found"}


def test_inactive_share_user_cannot_publish_shared_result(
    monkeypatch, authenticated_request
):
    monkeypatch.setattr(web_server, "get_user_settings", lambda user_id: {})
    monkeypatch.setattr(web_server, "_fetch_article_body", lambda article_id: {"id": article_id})

    response = web_server.app.test_client().post(
        "/ai/result/42",
        headers=_auth_headers(),
        json={"summary": "local result"},
    )

    assert response.status_code == 403
    assert response.get_json() == {"error": "shared AI result publication is not active"}


def test_active_share_publication_records_private_provenance(
    tmp_path, monkeypatch, authenticated_request
):
    db_path = tmp_path / "news.db"
    legacy = sqlite3.connect(db_path)
    legacy.execute(
        """
        CREATE TABLE ai_results (
            article_id INTEGER PRIMARY KEY,
            summary TEXT,
            translation TEXT
        )
        """
    )
    legacy.commit()
    legacy.close()
    monkeypatch.setattr(web_server, "NEWS_DB", str(db_path))
    monkeypatch.setattr(web_server, "get_user_settings", lambda user_id: _active_share_settings())
    monkeypatch.setattr(
        web_server,
        "get_ai_config",
        lambda user_id: {
            "provider": "personal-provider",
            "provider_type": "openai",
            "model": "personal-model",
        },
    )
    monkeypatch.setattr(web_server, "_fetch_article_body", lambda article_id: {"id": article_id})
    client = web_server.app.test_client()

    response = client.post(
        "/ai/result/42",
        headers=_auth_headers(),
        json={
            "summary": "shared summary",
            "translation": json.dumps(
                {"title": "共享译名", "html": "<p>共享译文</p>"},
                ensure_ascii=False,
            ),
        },
    )

    assert response.status_code == 200
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = dict(conn.execute(
        """
        SELECT summary_by_user_id, summary_provider, summary_model, summary_generated_at,
               translation_by_user_id, translation_provider, translation_model,
               translation_generated_at
        FROM ai_results WHERE article_id = 42
        """
    ).fetchone())
    conn.close()
    assert row["summary_by_user_id"] == 7
    assert row["summary_provider"] == "personal-provider"
    assert row["summary_model"] == "personal-model"
    assert row["summary_generated_at"]
    assert row["translation_by_user_id"] == 7
    assert row["translation_provider"] == "personal-provider"
    assert row["translation_model"] == "personal-model"
    assert row["translation_generated_at"]

    public = client.get("/ai/result/42", headers=_auth_headers()).get_json()
    assert public["summary"] == "shared summary"
    assert "共享译文" in public["translation"]
    for private_key in row:
        assert private_key not in public


def test_automatic_summary_records_its_generation_provenance(tmp_path, monkeypatch):
    db_path = tmp_path / "news.db"
    sqlite3.connect(db_path).close()
    monkeypatch.setattr(web_server, "NEWS_DB", str(db_path))
    monkeypatch.setattr(web_server, "_get_ai_result", lambda article_id: None)
    monkeypatch.setattr(
        web_server,
        "_fetch_article_body",
        lambda article_id: {"title": "Title", "body_html": "<p>Body</p>"},
    )

    class SummaryService:
        def __init__(self, *args, **kwargs):
            pass

        def summarize(self, **kwargs):
            return "generated summary"

    monkeypatch.setattr(web_server, "_SystemAIService", SummaryService)

    web_server._generate_article_summary(
        51,
        {
            "user_id": 12,
            "api_key": "system-key",
            "endpoint": "https://provider.example",
            "provider_type": "openai",
            "model": "summary-model",
        },
    )

    conn = sqlite3.connect(db_path)
    row = conn.execute(
        """
        SELECT summary_by_user_id, summary_provider, summary_model, summary_generated_at
        FROM ai_results WHERE article_id = 51
        """
    ).fetchone()
    conn.close()
    assert row[:3] == (12, "openai", "summary-model")
    assert row[3]


def test_automatic_summary_does_not_report_unsaved_result_as_success(monkeypatch):
    monkeypatch.setattr(web_server, "_get_ai_result", lambda _id: None)
    monkeypatch.setattr(
        web_server, "_fetch_article_body",
        lambda _id: {"title": "标题", "body_html": "正文"},
    )
    monkeypatch.setattr(web_server, "_save_ai_result", lambda *_args, **_kwargs: False)

    class SummaryService:
        def __init__(self, *args, **kwargs):
            pass

        def summarize(self, **_kwargs):
            return "已生成但未保存的摘要"

    monkeypatch.setattr(web_server, "_SystemAIService", SummaryService)
    with pytest.raises(RuntimeError, match="Could not save article AI summary"):
        web_server._generate_article_summary(
            51, {"api_key": "key", "endpoint": "https://example.com", "model": "test"}
        )


def test_automatic_translation_records_its_generation_provenance(tmp_path, monkeypatch):
    db_path = tmp_path / "news.db"
    sqlite3.connect(db_path).close()
    monkeypatch.setattr(web_server, "NEWS_DB", str(db_path))
    monkeypatch.setattr(web_server, "_save_article_translation", lambda *args, **kwargs: False)
    monkeypatch.setattr(web_server, "_publish_translation_update", lambda article_id: None)

    class TranslationService:
        def __init__(self, *args, **kwargs):
            pass

        def translate_full(self, *args, **kwargs):
            return {"title": "译名", "html": "<p>译文</p>"}

    monkeypatch.setattr(web_server, "_SystemAIService", TranslationService)

    web_server._translate_article_background(
        {
            "id": 52,
            "title": "Title",
            "body_html": "<p>Body</p>",
            "translate_content_needed": True,
            "translate_title_needed": True,
        },
        {
            "user_id": 13,
            "api_key": "system-key",
            "endpoint": "https://provider.example",
            "provider_type": "claude",
            "model": "translation-model",
        },
    )

    conn = sqlite3.connect(db_path)
    row = conn.execute(
        """
        SELECT translation_by_user_id, translation_provider, translation_model,
               translation_generated_at
        FROM ai_results WHERE article_id = 52
        """
    ).fetchone()
    conn.close()
    assert row[:3] == (13, "claude", "translation-model")
    assert row[3]


def test_shared_result_write_failure_is_generic_and_logged(
    tmp_path, monkeypatch, caplog, authenticated_request
):
    db_path = tmp_path / "news.db"
    sqlite3.connect(db_path).close()
    monkeypatch.setattr(web_server, "NEWS_DB", str(db_path))
    monkeypatch.setattr(web_server, "get_user_settings", lambda user_id: _active_share_settings())
    monkeypatch.setattr(web_server, "get_ai_config", lambda user_id: {})
    monkeypatch.setattr(web_server, "_fetch_article_body", lambda article_id: {"id": article_id})
    monkeypatch.setattr(
        web_server,
        "_news_db_connect",
        lambda: (_ for _ in ()).throw(sqlite3.OperationalError("secret /app/data/news.db")),
    )

    with caplog.at_level("ERROR"):
        response = web_server.app.test_client().post(
            "/ai/result/42",
            headers=_auth_headers(),
            json={"summary": "local result"},
        )

    assert response.status_code == 500
    assert response.get_json() == {"error": "internal server error"}
    assert "secret /app/data/news.db" in caplog.text


@pytest.mark.parametrize(
    ("path", "role", "target"),
    (
        ("/ai/config", "user", "set_ai_config"),
        ("/admin/system-ai-config", "admin", "set_system_ai_config"),
    ),
)
def test_config_write_failures_are_generic_and_logged(
    path, role, target, monkeypatch, caplog
):
    monkeypatch.setattr(
        models,
        "get_user",
        lambda user_id: {"id": user_id, "role": role},
    )
    monkeypatch.setattr(models, "record_access", lambda user_id: None)
    monkeypatch.setattr(
        web_server,
        target,
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("secret config path /app/data/raynews.db")
        ),
    )

    with caplog.at_level("ERROR"):
        response = web_server.app.test_client().put(
            path,
            headers=_auth_headers(role=role),
            json={"model": "safe-model"},
        )

    assert response.status_code == 500
    assert response.get_json() == {"error": "internal server error"}
    assert "secret config path /app/data/raynews.db" in caplog.text


def test_connection_test_provider_failure_is_generic_and_logged(monkeypatch, caplog):
    class FailingService:
        def __init__(self, **kwargs):
            pass

        def test_connection(self):
            raise RuntimeError("provider response contained secret key")

    monkeypatch.setattr(web_server, "AIService", FailingService)
    with caplog.at_level("ERROR"):
        body, status = web_server._run_ai_connection_test({
            "api_key": "key",
            "endpoint": "https://provider.example",
            "model": "model",
            "provider_type": "openai",
        })

    assert status == 502
    assert body == {"error": "AI connection test failed"}
    assert "provider response contained secret [redacted]" in caplog.text


def test_ai_relay_provider_failure_is_generic_and_logged(
    monkeypatch, caplog, authenticated_request
):
    monkeypatch.setattr(
        web_server,
        "get_ai_config",
        lambda user_id: {
            "api_key": "key",
            "endpoint": "https://provider.example",
            "model": "model",
            "provider_type": "openai",
            "enabled": 1,
        },
    )

    class FailingService:
        def __init__(self, **kwargs):
            pass

        def chat(self, *args, **kwargs):
            raise RuntimeError("provider body with /app/data and secret key")

    monkeypatch.setattr(web_server, "AIService", FailingService)
    with caplog.at_level("ERROR"):
        response = web_server.app.test_client().post(
            "/ai/chat",
            headers=_auth_headers(),
            json={"messages": [{"role": "user", "content": "hello"}]},
        )

    assert response.status_code == 502
    assert response.get_json() == {"error": "AI relay failed"}
    assert "provider body with /app/data and secret [redacted]" in caplog.text


def test_notification_send_failure_is_generic_and_logged(
    monkeypatch, caplog, authenticated_request
):
    monkeypatch.setenv("RESEND_API_KEY", "server-key")
    monkeypatch.setattr(
        web_server,
        "get_user_settings",
        lambda user_id: {
            "notification_config": json.dumps(
                {"resend": {"to_email": "reader@example.com"}}
            )
        },
    )
    monkeypatch.setattr(
        notifier,
        "send_email",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("provider response exposed secret mail detail")
        ),
    )

    with caplog.at_level("ERROR"):
        response = web_server.app.test_client().post(
            "/settings/test-notification",
            headers=_auth_headers(),
        )

    assert response.status_code == 502
    assert response.get_json() == {"error": "notification send failed"}
    assert "provider response exposed secret mail detail" in caplog.text


def test_system_auto_config_carries_configured_provider(monkeypatch):
    class AutoDb:
        def execute(self, sql):
            return self

        def fetchone(self):
            return {
                "user_id": 21,
                "auto_translate_title": 0,
                "auto_translate_content": 0,
                "auto_title_summary_enabled": 0,
                "auto_summary_enabled": 1,
            }

    monkeypatch.setattr(web_server, "get_db", lambda: AutoDb())
    monkeypatch.setattr(
        web_server,
        "get_system_ai_config",
        lambda: {
            "provider": "deepseek",
            "api_key": "system-key",
            "endpoint": "https://api.deepseek.com/v1",
            "model": "deepseek-chat",
            "provider_type": "openai",
            "enabled": 1,
        },
    )

    config = web_server._system_auto_config("auto_summary_enabled")

    assert config["provider"] == "deepseek"
    assert config["provider_type"] == "openai"


def test_frontend_publishes_local_ai_results_only_while_sharing_is_active():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    start = html.index("function publishSharedAIResult(")
    end = html.index("\n}", start) + 2
    helper = html[start:end]
    script = f"""
const assert = require('assert');
const vm = require('vm');
const calls = [];
const context = {{
  authToken: 'token',
  userAutoSettings: {{ share_active: false }},
  fetch: (...args) => {{ calls.push(args); return Promise.resolve({{ ok: true }}); }},
  console,
}};
vm.createContext(context);
vm.runInContext({json.dumps(helper)}, context);
context.publishSharedAIResult(42, {{ summary: 'local result' }});
assert.equal(calls.length, 0);
context.userAutoSettings.share_active = true;
context.publishSharedAIResult(42, {{ summary: 'local result' }});
assert.equal(calls.length, 1);
assert.equal(calls[0][0], '/ai/result/42');
assert.deepEqual(JSON.parse(calls[0][1].body), {{ summary: 'local result' }});
"""
    result = subprocess.run(
        ["node", "-e", script],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_ai_save_result_only_syncs_title_for_admin():
    src = (ROOT / "web_server.py").read_text(encoding="utf-8")
    start = src.index("def ai_save_result(article_id):")
    end = src.index("def _run_ai_connection_test", start)
    block = src[start:end]
    # Any logged-in user may still contribute to the shared summary/translation
    # cache...
    assert '_save_ai_result(article_id, **kwargs)' in block
    # ...but only an admin's submission is allowed to rewrite the article's
    # canonical, site-wide-visible title.
    assert 'if translation and g.user_role == "admin":' in block
    assert "_save_article_title_update(article_id, translated_title, \"translation\")" in block


def test_frontend_sanitizes_translated_html_before_first_render():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    assert "function sanitizeTranslatedHtml(html)" in html
    # Same whitelist as web_server.py's _sanitize_translated_html, kept in sync.
    for tag in ("'p'", "'a'", "'img'", "'table'"):
        assert tag in html
    assert "SANITIZE_ALLOWED_ATTRS" in html

    apply_start = html.index("const applyTranslation = (translatedHtmlRaw, translatedTitle) => {")
    apply_end = html.index("};", apply_start)
    apply_block = html[apply_start:apply_end]
    # Sanitize must run before proxyImages()/innerHTML — this is the function
    # every render path (cached, legacy-cached, and freshly browser-generated)
    # funnels through, so fixing it here covers all three.
    assert "proxyImages(sanitizeTranslatedHtml(translatedHtmlRaw))" in apply_block


def temp_db_path():
    return ROOT / f"tmp-cache-evict-test-{uuid.uuid4().hex}.db"


def test_cache_evict_endpoint_pops_only_the_given_article():
    refresh_server.clear_article_cache()
    refresh_server._store_cached_article(5, b"stale")
    refresh_server._store_cached_article(6, b"other")
    try:
        body, status = refresh_server.api_cache_evict({"id": ["5"]})
        assert status == 200
        assert json.loads(body) == {"ok": True}
        assert refresh_server._get_cached_article(5) is None
        assert refresh_server._get_cached_article(6) == b"other"
        assert refresh_server.refresh_runtime_stats()["article_cache_bytes"] == 5
    finally:
        refresh_server.clear_article_cache()


def test_cache_evict_rejects_non_numeric_id():
    body, status = refresh_server.api_cache_evict({"id": ["not-a-number"]})
    assert status == 400


def test_title_update_invalidates_refresh_server_cache(monkeypatch):
    db_path = temp_db_path()
    calls = []

    def fake_get(url, params=None, timeout=None):
        calls.append((url, params))
        class FakeResp:
            pass
        return FakeResp()

    monkeypatch.setattr(web_server.requests, "get", fake_get)
    old_news_db = web_server.NEWS_DB
    try:
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE articles (id INTEGER PRIMARY KEY, title TEXT)")
        conn.execute("INSERT INTO articles (id, title) VALUES (1, 'Old Title')")
        conn.commit()
        conn.close()

        web_server.NEWS_DB = str(db_path)
        changed = web_server._save_article_title_update(1, "New Title", "translation")
        assert changed is True
        assert len(calls) == 1
        assert calls[0][0] == "http://127.0.0.1:8081/internal/cache-evict"
        assert calls[0][1] == {"id": 1}
    finally:
        web_server.NEWS_DB = old_news_db
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(str(db_path) + suffix)
            except FileNotFoundError:
                pass


def test_body_translation_does_not_mutate_or_invalidate_canonical_detail(monkeypatch):
    db_path = temp_db_path()
    calls = []
    monkeypatch.setattr(
        web_server.requests, "get",
        lambda url, params=None, timeout=None: calls.append((url, params)),
    )
    old_news_db = web_server.NEWS_DB
    try:
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE articles (id INTEGER PRIMARY KEY, title TEXT, body_html TEXT)")
        conn.execute("INSERT INTO articles (id, title, body_html) VALUES (1, 'Title', 'old body')")
        conn.commit()
        conn.close()

        web_server.NEWS_DB = str(db_path)
        web_server._save_article_translation(1, body_html="<p>new body</p>")
        conn = sqlite3.connect(db_path)
        body_html = conn.execute(
            "SELECT body_html FROM articles WHERE id = 1"
        ).fetchone()[0]
        conn.close()
        assert body_html == "old body"
        assert calls == []
    finally:
        web_server.NEWS_DB = old_news_db
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(str(db_path) + suffix)
            except FileNotFoundError:
                pass


class FakeUserSettingsDb:
    """Stands in for models.get_db() — only the one query
    _broadcast_daily_summary issues against it."""

    def __init__(self, rows):
        self._rows = rows

    def execute(self, sql, *args):
        assert "user_settings" in sql
        return self

    def fetchall(self):
        return self._rows


def _setup_daily_summary_test(monkeypatch, db_path, subscriber_rows, send_results):
    """send_results: dict to_email -> True (succeeds) | Exception (raises)."""
    sqlite3.connect(str(db_path)).close()  # the sends helpers no-op unless NEWS_DB exists
    monkeypatch.setenv("RESEND_API_KEY", "test-key")
    monkeypatch.setattr(web_server, "NEWS_DB", str(db_path))
    monkeypatch.setattr(web_server, "get_db", lambda: FakeUserSettingsDb(subscriber_rows))
    monkeypatch.setattr(
        web_server, "_generate_daily_summary_global",
        lambda date_str: {"summary": "today's news", "stats": {}},
    )
    # These cases cover the email leg only. Stub the in-app leg so they don't
    # reach the real app database (models.get_db()/publish_broadcast_atomically
    # open DATA_DIR/raynews.db, which the NEWS_DB patch above doesn't redirect).
    monkeypatch.setattr(
        web_server, "_deliver_daily_summary_inapp",
        lambda date_str, result: {"status": "skipped", "recipients": 0},
    )
    # Beijing 21:00 — inside the default 10-minute send window.
    fixed_now = dt.datetime(2026, 7, 10, 21, 3, tzinfo=dt.timezone(dt.timedelta(hours=8)))
    monkeypatch.setattr(web_server, "_beijing_now", lambda: fixed_now)

    sent_log = []

    def fake_send(api_key, to_email, summary, stats, idempotency_key=None):
        sent_log.append(to_email)
        outcome = send_results.get(to_email, True)
        if outcome is not True:
            raise outcome
        return True

    monkeypatch.setattr(notifier, "send_daily_summary_email", fake_send)
    return sent_log


def test_daily_summary_persists_send_state_so_restart_does_not_duplicate(monkeypatch):
    db_path = ROOT / f"tmp-daily-summary-test-{uuid.uuid4().hex}.db"
    rows = [
        {"user_id": 1, "notification_config": '{"resend":{"to_email":"a@example.com"}}'},
        {"user_id": 2, "notification_config": '{"resend":{"to_email":"b@example.com"}}'},
    ]
    sent_log = _setup_daily_summary_test(monkeypatch, db_path, rows, {})
    try:
        # First run (e.g. the scheduler's first tick inside today's window).
        result1 = web_server._broadcast_daily_summary(force=False)
        assert result1["status"] == "ok"
        assert result1["sent"] == 2
        assert sorted(sent_log) == ["a@example.com", "b@example.com"]

        # Simulate a process restart: in the old code this reset the
        # in-memory "already sent today" set. The persisted table must still
        # remember both recipients were already sent, so a second tick in
        # the same window (as would happen right after a restart) must NOT
        # re-send.
        sent_log.clear()
        result2 = web_server._broadcast_daily_summary(force=False)
        assert result2["status"] == "skipped"
        assert result2["reason"] == "already sent today"
        assert sent_log == []
    finally:
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(str(db_path) + suffix)
            except FileNotFoundError:
                pass


def test_daily_summary_retries_only_the_recipient_who_previously_failed(monkeypatch):
    db_path = ROOT / f"tmp-daily-summary-test-{uuid.uuid4().hex}.db"
    rows = [
        {"user_id": 1, "notification_config": '{"resend":{"to_email":"ok@example.com"}}'},
        {"user_id": 2, "notification_config": '{"resend":{"to_email":"bad@example.com"}}'},
    ]
    sent_log = _setup_daily_summary_test(
        monkeypatch, db_path, rows, {"bad@example.com": RuntimeError("smtp down")},
    )
    try:
        result1 = web_server._broadcast_daily_summary(force=False)
        assert result1["status"] == "ok"
        assert result1["sent"] == 1  # only ok@example.com succeeded
        assert sorted(sent_log) == ["bad@example.com", "ok@example.com"]

        # Fix the transient failure, then simulate the scheduler's next tick.
        sent_log.clear()
        monkeypatch.setattr(
            notifier, "send_daily_summary_email",
            lambda api_key, to_email, summary, stats, idempotency_key=None: sent_log.append(to_email),
        )
        result2 = web_server._broadcast_daily_summary(force=False)
        assert result2["status"] == "ok"
        assert result2["sent"] == 1
        # Only the previously-failed recipient gets retried — the one that
        # already succeeded must not receive a duplicate email.
        assert sent_log == ["bad@example.com"]
    finally:
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(str(db_path) + suffix)
            except FileNotFoundError:
                pass


def test_daily_summary_force_resends_regardless_of_persisted_history(monkeypatch):
    db_path = ROOT / f"tmp-daily-summary-test-{uuid.uuid4().hex}.db"
    rows = [{"user_id": 1, "notification_config": '{"resend":{"to_email":"a@example.com"}}'}]
    sent_log = _setup_daily_summary_test(monkeypatch, db_path, rows, {})
    try:
        web_server._broadcast_daily_summary(force=False)
        assert sent_log == ["a@example.com"]

        sent_log.clear()
        result = web_server._broadcast_daily_summary(force=True)
        assert result["status"] == "ok"
        assert result["sent"] == 1
        assert sent_log == ["a@example.com"]  # admin's manual re-send goes through
    finally:
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(str(db_path) + suffix)
            except FileNotFoundError:
                pass


SECURITY_HEADERS = {
    "x-content-type-options": "nosniff",
    "x-frame-options": "SAMEORIGIN",
    "referrer-policy": "strict-origin-when-cross-origin",
}
NGINX_SECURITY_HEADERS_INCLUDE = "/etc/nginx/snippets/raynews-security-headers.conf"


def test_malicious_origin_receives_no_cors_header_from_auth_health():
    response = web_server.app.test_client().get(
        "/auth/health", headers={"Origin": "https://evil.example"}
    )

    assert response.status_code == 200
    assert "Access-Control-Allow-Origin" not in response.headers


def test_same_origin_proxy_configuration_has_no_cors_policy():
    web_server_source = (ROOT / "web_server.py").read_text(encoding="utf-8")
    nginx_config = (ROOT / "nginx.conf").read_text(encoding="utf-8")

    assert "flask_cors" not in web_server_source
    assert "CORS(app)" not in web_server_source
    assert not re.search(
        r"add_header\s+Access-Control-Allow-[^;]*\*", nginx_config,
    )
    assert re.search(r"\bmap\b[^;{]*\$http_origin", nginx_config) is None
    assert "proxy_hide_header Access-Control-Allow-Origin;" in nginx_config


def test_nginx_security_headers_are_included_where_add_header_resets_inheritance():
    snippet = ROOT / "nginx-security-headers.conf"
    assert snippet.read_text(encoding="utf-8") == (
        "add_header X-Content-Type-Options nosniff always;\n"
        "add_header X-Frame-Options SAMEORIGIN always;\n"
        "add_header Referrer-Policy strict-origin-when-cross-origin always;\n"
    )

    nginx_config = (ROOT / "nginx.conf").read_text(encoding="utf-8")
    assert f"include {NGINX_SECURITY_HEADERS_INCLUDE};" in nginx_config
    assert "client_max_body_size 2m;" in nginx_config
    location_blocks = re.findall(
        r"^    location\b.*?^    }", nginx_config, flags=re.MULTILINE | re.DOTALL
    )
    assert location_blocks
    for location in location_blocks:
        if "add_header" in location:
            assert f"include {NGINX_SECURITY_HEADERS_INCLUDE};" in location


def test_dockerfile_installs_nginx_security_headers_snippet():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert (
        "COPY nginx-security-headers.conf "
        "/etc/nginx/snippets/raynews-security-headers.conf"
    ) in dockerfile


def _docker_is_usable():
    try:
        return subprocess.run(
            ["docker", "info"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=10,
        ).returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


@pytest.mark.skipif(not _docker_is_usable(), reason="Docker daemon is unavailable")
def test_container_routes_have_security_headers_without_cors_reflection():
    import socket
    import time
    from urllib.request import Request, urlopen

    image = "raynews-security-plan"
    container = f"raynews-security-test-{uuid.uuid4().hex}"
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]

    build = subprocess.run(
        ["docker", "build", "-t", image, "."], cwd=ROOT,
        capture_output=True, text=True, timeout=300,
    )
    assert build.returncode == 0, build.stderr or build.stdout
    nginx_test = subprocess.run(
        ["docker", "run", "--rm", image, "nginx", "-t"],
        capture_output=True, text=True, timeout=60,
    )
    assert nginx_test.returncode == 0, nginx_test.stderr or nginx_test.stdout

    try:
        started = subprocess.run(
            ["docker", "run", "-d", "--name", container, "-p", f"127.0.0.1:{port}:80", image],
            capture_output=True, text=True, timeout=60,
        )
        assert started.returncode == 0, started.stderr or started.stdout
        for _ in range(30):
            try:
                with urlopen(f"http://127.0.0.1:{port}/", timeout=1):
                    break
            except Exception:
                time.sleep(0.5)
        else:
            logs = subprocess.run(
                ["docker", "logs", container], capture_output=True, text=True, timeout=30,
            )
            pytest.fail(logs.stderr or logs.stdout)

        for path in ("/", "/auth/health", "/api/news", "/img-cache"):
            request = Request(
                f"http://127.0.0.1:{port}{path}",
                headers={"Origin": "https://evil.example"}, method="HEAD",
            )
            try:
                with urlopen(request, timeout=10) as response:
                    headers = response.headers
            except Exception as exc:
                headers = getattr(exc, "headers", None)
                assert headers is not None, exc
            assert "Access-Control-Allow-Origin" not in headers
            for name, value in SECURITY_HEADERS.items():
                assert headers.get(name) == value
    finally:
        subprocess.run(
            ["docker", "rm", "-f", container], stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=30,
        )
