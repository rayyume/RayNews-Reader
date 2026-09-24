"""Retry chain and admin alerting when the daily summary fails to generate.

A failed generation is retried every DAILY_SUMMARY_RETRY_INTERVAL_SECONDS; after
DAILY_SUMMARY_MAX_RETRIES further failures the day is given up on and every admin
is alerted (email + in-app) with the reason. The ✨ panel then shows admins — and
only admins — the reason and a manual retry button.
"""

import datetime as dt
import sqlite3
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import web_server
import models

BEIJING = dt.timezone(dt.timedelta(hours=8))
TODAY = "2026-07-10"


@pytest.fixture
def news_db(tmp_path, monkeypatch):
    db_path = tmp_path / f"news-{uuid.uuid4().hex}.db"
    sqlite3.connect(str(db_path)).close()  # the helpers no-op unless NEWS_DB exists
    monkeypatch.setattr(web_server, "NEWS_DB", str(db_path))
    monkeypatch.setattr(web_server, "_beijing_now",
                        lambda: dt.datetime(2026, 7, 10, 21, 3, tzinfo=BEIJING))
    return db_path


@pytest.fixture
def isolated_app_db(tmp_path, monkeypatch):
    """Keep the system-AI incident state out of the developer's app DB."""
    models.close_db()
    monkeypatch.setattr(models, "DB_FILE", tmp_path / "app-state.db")
    monkeypatch.setattr(
        web_server,
        "SYSTEM_AI_LAST_FAILURE_MARKER_FILE",
        tmp_path / "system-ai-last-failure.marker",
        raising=False,
    )
    models.get_db()
    web_server._reset_system_ai_health()
    try:
        yield
    finally:
        web_server._reset_system_ai_health()
        models.close_db()


@pytest.fixture
def clock(monkeypatch):
    """Drivable epoch clock — retry pacing is 10 real minutes wide."""
    state = {"now": 1_000_000}
    monkeypatch.setattr(web_server, "_epoch_now", lambda: state["now"])
    return state


def _fail_generation(monkeypatch, reason="AI 生成失败：401 invalid api key"):
    def failing(date_str):
        web_server._set_daily_summary_error(reason)
        return None
    monkeypatch.setattr(web_server, "_generate_daily_summary_global", failing)


def _stub_delivery(monkeypatch):
    monkeypatch.setattr(web_server, "_deliver_daily_summary_inapp",
                        lambda date_str, result: {"status": "ok", "recipients": 1})
    monkeypatch.setattr(web_server, "_deliver_daily_summary_email",
                        lambda date_str, result, force=False: {"status": "ok", "sent": 1})


@pytest.fixture
def alerts(monkeypatch):
    """Capture admin alerts instead of touching the user DB / Resend."""
    sent = []
    monkeypatch.setattr(web_server, "list_users", lambda: [
        {"id": 1, "role": "admin"}, {"id": 2, "role": "user"}, {"id": 3, "role": "admin"},
    ])
    monkeypatch.setattr(web_server, "_notify_user",
                        lambda user_id, ntype, title, body: (sent.append(
                            {"user_id": user_id, "type": ntype, "title": title, "body": body})
                            or True))
    return sent


def test_failure_is_recorded_with_its_reason_and_a_retry_ten_minutes_out(
        news_db, clock, alerts, monkeypatch):
    _fail_generation(monkeypatch)

    outcome = web_server._broadcast_daily_summary(force=False)

    assert outcome["status"] == "error"
    assert "401 invalid api key" in outcome["reason"]
    state = web_server._get_daily_summary_failure(TODAY)
    assert state["attempts"] == 1
    assert state["given_up"] == 0
    assert state["next_retry_at"] == clock["now"] + web_server.DAILY_SUMMARY_RETRY_INTERVAL_SECONDS
    assert alerts == []  # nothing bothers the admin until the retries are spent


def test_scheduler_retries_only_when_the_interval_has_elapsed(news_db, clock, alerts, monkeypatch):
    _fail_generation(monkeypatch)
    attempts = []
    real_generate = web_server._generate_daily_summary_global

    def counting(date_str):
        attempts.append(clock["now"])
        return real_generate(date_str)
    monkeypatch.setattr(web_server, "_generate_daily_summary_global", counting)

    web_server._send_daily_summaries()          # scheduled run, attempt 1
    assert len(attempts) == 1

    clock["now"] += 60                          # a minute later: nothing owed yet
    web_server._send_daily_summaries()
    assert len(attempts) == 1

    clock["now"] += web_server.DAILY_SUMMARY_RETRY_INTERVAL_SECONDS
    web_server._send_daily_summaries()          # retry 1
    assert len(attempts) == 2
    assert web_server._get_daily_summary_failure(TODAY)["attempts"] == 2


def test_gives_up_after_three_retries_and_alerts_every_admin_once(
        isolated_app_db, news_db, clock, alerts, monkeypatch):
    models.set_app_state(web_server.SYSTEM_AI_ALERTED_STATE_KEY, "0")
    assert models.get_app_state(web_server.SYSTEM_AI_ALERTED_STATE_KEY) == "0"
    _fail_generation(monkeypatch, "系统 AI 未配置或未启用（管理员设置 → 服务端 API）")

    web_server._send_daily_summaries()
    for _ in range(web_server.DAILY_SUMMARY_MAX_RETRIES):
        clock["now"] += web_server.DAILY_SUMMARY_RETRY_INTERVAL_SECONDS
        web_server._send_daily_summaries()

    state = web_server._get_daily_summary_failure(TODAY)
    assert state["attempts"] == 1 + web_server.DAILY_SUMMARY_MAX_RETRIES
    assert state["given_up"] == 1
    assert state["next_retry_at"] is None

    assert [a["user_id"] for a in alerts] == [1, 3]  # both admins, not the user
    assert all(a["type"] == "daily_summary_failed" for a in alerts)
    assert all("系统 AI 未配置" in a["body"] for a in alerts)

    # Later ticks neither retry nor alert a second time.
    clock["now"] += 10 * web_server.DAILY_SUMMARY_RETRY_INTERVAL_SECONDS
    assert web_server._send_daily_summaries()["reason"] == "gave up for today"
    assert len(alerts) == 2


def test_undelivered_daily_summary_alert_releases_the_claim(
        isolated_app_db, news_db, clock, alerts, monkeypatch):
    models.set_app_state(web_server.SYSTEM_AI_ALERTED_STATE_KEY, "0")
    assert models.get_app_state(web_server.SYSTEM_AI_ALERTED_STATE_KEY) == "0"
    _fail_generation(monkeypatch)
    notification_attempts = []

    def undelivered(*args, **kwargs):
        notification_attempts.append((args, kwargs))
        return 0

    monkeypatch.setattr(web_server, "_notify_admins", undelivered)

    web_server._send_daily_summaries()
    for _ in range(web_server.DAILY_SUMMARY_MAX_RETRIES):
        clock["now"] += web_server.DAILY_SUMMARY_RETRY_INTERVAL_SECONDS
        web_server._send_daily_summaries()

    state = web_server._get_daily_summary_failure(TODAY)
    assert state["given_up"] == 1
    assert state["alerted"] == 0
    assert len(notification_attempts) == 1
    assert web_server._claim_daily_summary_alert(TODAY) is True


def test_same_daily_outage_sends_only_system_ai_alert(
        isolated_app_db, news_db, clock, alerts, monkeypatch):
    """A notified AI incident replaces, but does not erase, the daily alert."""
    def failing(_date):
        web_server._note_system_ai_failure("每日摘要", "401 invalid key")
        web_server._set_daily_summary_error("AI 生成失败")
        return None

    monkeypatch.setattr(web_server, "_generate_daily_summary_global", failing)

    for _ in range(1 + web_server.DAILY_SUMMARY_MAX_RETRIES):
        web_server._broadcast_daily_summary(force=False, bypass_window=True)

    types = [alert["type"] for alert in alerts]
    assert types.count("system_ai_failed") == 2
    assert "daily_summary_failed" not in types
    assert web_server._get_daily_summary_failure(TODAY)["given_up"] == 1


def test_system_ai_claim_racing_daily_summary_claim_only_alerts_once(
        isolated_app_db, news_db, clock, alerts, monkeypatch):
    """A system-AI claim landing between the old read and the daily claim must
    not let one outage fire both alerts.

    The race: the daily-summary give-up path used to read
    ``_system_ai_incident_is_notified()`` then, only on False, call
    ``_claim_daily_summary_alert``. Those are separate transactions on two
    DBs, so a concurrent system-AI claim between them sent both
    ``system_ai_failed`` and ``daily_summary_failed`` to the same admins.

    The fix makes the daily claim the atomic gate. This test injects the
    system-AI claim at the moment the daily path touches its alert gate —
    after the old read point but before/inside the claim — and confirms only
    the system-AI alert fires (the daily claim is released as a duplicate).
    """
    models.set_app_state(web_server.SYSTEM_AI_ALERTED_STATE_KEY, "0")

    def failing(_date):
        web_server._set_daily_summary_error("AI 生成失败")
        return None

    monkeypatch.setattr(web_server, "_generate_daily_summary_global", failing)

    real_claim = web_server._claim_daily_summary_alert

    def racing_daily_claim(date_str):
        # The system-AI alert is claimed the moment the daily path touches its
        # alert gate — the same window the old read-then-claim ordering left
        # open between the read and the claim.
        models.set_app_state(web_server.SYSTEM_AI_ALERTED_STATE_KEY, "1")
        models.set_app_state(
            web_server.SYSTEM_AI_ALERT_LAST_NOTIFIED_STATE_KEY, str(clock["now"])
        )
        return real_claim(date_str)

    monkeypatch.setattr(web_server, "_claim_daily_summary_alert", racing_daily_claim)

    for _ in range(1 + web_server.DAILY_SUMMARY_MAX_RETRIES):
        web_server._broadcast_daily_summary(force=False, bypass_window=True)

    types = [alert["type"] for alert in alerts]
    assert "daily_summary_failed" not in types
    assert web_server._get_daily_summary_failure(TODAY)["given_up"] == 1


def test_daily_alert_remains_when_system_incident_was_cooldown_suppressed(
        isolated_app_db, news_db, clock, alerts, monkeypatch):
    """A muted cooldown incident must not suppress the daily-summary signal."""
    health_clock = {"now": 1_000.0}
    monkeypatch.setattr(web_server.time, "time", lambda: health_clock["now"])

    def failing(_date):
        web_server._note_system_ai_failure("每日摘要", "401 invalid key")
        web_server._set_daily_summary_error("AI 生成失败")
        return None

    _failures_to_notify = web_server.SYSTEM_AI_FAILURE_ALERT_THRESHOLD
    for _ in range(_failures_to_notify):
        web_server._note_system_ai_failure("自动摘要", "401 invalid key")
    health_clock["now"] += 1
    web_server._note_system_ai_success()
    health_clock["now"] += web_server.SYSTEM_AI_RECOVERY_STABILITY_SECONDS
    assert web_server._maybe_recover_stale_system_ai_incident() is True
    alerts.clear()

    for _ in range(_failures_to_notify):
        web_server._note_system_ai_failure("自动摘要", "401 invalid key")
    assert models.get_app_state(web_server.SYSTEM_AI_ALERTED_STATE_KEY) == "2"

    monkeypatch.setattr(web_server, "_generate_daily_summary_global", failing)
    for _ in range(1 + web_server.DAILY_SUMMARY_MAX_RETRIES):
        web_server._broadcast_daily_summary(force=False, bypass_window=True)

    assert [alert["user_id"] for alert in alerts] == [1, 3]
    assert [alert["type"] for alert in alerts] == ["daily_summary_failed"] * 2
    assert web_server._get_daily_summary_failure(TODAY)["given_up"] == 1


def test_daily_alert_fails_open_when_system_ai_incident_read_fails(
        isolated_app_db, news_db, clock, alerts, monkeypatch):
    """A broken app-state read must not silence the independent daily alert."""
    for _ in range(web_server.SYSTEM_AI_FAILURE_ALERT_THRESHOLD):
        web_server._note_system_ai_failure("自动摘要", "401 invalid key")
    assert models.get_app_state(web_server.SYSTEM_AI_ALERTED_STATE_KEY) == "1"
    alerts.clear()

    def failing(_date):
        web_server._set_daily_summary_error("AI 生成失败")
        return None

    monkeypatch.setattr(web_server, "_generate_daily_summary_global", failing)
    monkeypatch.setattr(
        web_server,
        "get_app_state",
        lambda _key: (_ for _ in ()).throw(sqlite3.OperationalError("state unavailable")),
    )
    for _ in range(1 + web_server.DAILY_SUMMARY_MAX_RETRIES):
        web_server._broadcast_daily_summary(force=False, bypass_window=True)

    assert [alert["user_id"] for alert in alerts] == [1, 3]
    assert [alert["type"] for alert in alerts] == ["daily_summary_failed"] * 2
    assert web_server._get_daily_summary_failure(TODAY)["given_up"] == 1


def test_an_admin_ad_hoc_resend_does_not_seed_the_retry_chain(news_db, clock, alerts, monkeypatch):
    # 管理员设置 → 立即发送 at an arbitrary hour must not turn a one-off failure
    # into half an hour of retries plus a failure alert to every admin.
    _fail_generation(monkeypatch)

    outcome = web_server._broadcast_daily_summary(force=True)

    assert outcome["status"] == "error"
    assert web_server._get_daily_summary_failure(TODAY) is None
    assert alerts == []


def test_a_later_success_clears_the_failure_record(news_db, clock, alerts, monkeypatch):
    _fail_generation(monkeypatch)
    web_server._send_daily_summaries()
    assert web_server._get_daily_summary_failure(TODAY)["attempts"] == 1

    monkeypatch.setattr(web_server, "_generate_daily_summary_global",
                        lambda date_str: {"summary": "s", "article_count": 3, "stats": {}})
    _stub_delivery(monkeypatch)
    clock["now"] += web_server.DAILY_SUMMARY_RETRY_INTERVAL_SECONDS
    web_server._send_daily_summaries()

    assert web_server._get_daily_summary_failure(TODAY) is None
    assert alerts == []


def test_generation_reason_is_captured_for_a_missing_system_ai(news_db, monkeypatch):
    monkeypatch.setattr(web_server, "get_system_ai_config", lambda: None)
    assert web_server._generate_daily_summary_global(TODAY) is None
    assert "系统 AI" in web_server._daily_summary_last_error


def test_generation_reason_is_captured_when_the_day_has_no_articles(news_db, monkeypatch):
    monkeypatch.setattr(web_server, "get_system_ai_config",
                        lambda: {"enabled": True, "api_key": "k", "endpoint": "e", "model": "m"})
    monkeypatch.setattr(web_server, "_fetch_articles_by_date",
                        lambda date_str, include_shared_summary=False: [])
    assert web_server._generate_daily_summary_global(TODAY) is None
    assert "没有可用于生成摘要的文章" in web_server._daily_summary_last_error


# ─── /ai/daily-summary/today ────────────────────────────────────────────


def _today_payload(role):
    from flask import g
    with web_server.app.test_request_context("/ai/daily-summary/today"):
        g.user_id = 1
        g.user_role = role
        return web_server.ai_daily_summary_today.__wrapped__().get_json()


def test_admin_sees_the_reason_and_the_retry_button_after_giving_up(
        news_db, clock, alerts, monkeypatch):
    monkeypatch.setattr(web_server, "_get_daily_summary_global_cache", lambda date_str: None)
    _fail_generation(monkeypatch)
    web_server._send_daily_summaries()
    for _ in range(web_server.DAILY_SUMMARY_MAX_RETRIES):
        clock["now"] += web_server.DAILY_SUMMARY_RETRY_INTERVAL_SECONDS
        web_server._send_daily_summaries()

    admin = _today_payload("admin")
    assert admin["status"] == "failed"
    assert admin["can_retry"] is True
    assert "401 invalid api key" in admin["error"]
    assert admin["attempts"] == 1 + web_server.DAILY_SUMMARY_MAX_RETRIES


def test_ordinary_users_never_see_the_failure_reason(news_db, clock, alerts, monkeypatch):
    monkeypatch.setattr(web_server, "_get_daily_summary_global_cache", lambda date_str: None)
    _fail_generation(monkeypatch)
    web_server._send_daily_summaries()

    user = _today_payload("user")
    assert user["status"] == "generating"  # a retry is still pending
    assert "error" not in user
    assert "can_retry" not in user

    admin = _today_payload("admin")
    assert admin["status"] == "retrying"
    assert admin["can_retry"] is True


# ─── /ai/daily-summary/retry ────────────────────────────────────────────


def _retry_response(monkeypatch):
    from flask import g
    with web_server.app.test_request_context("/ai/daily-summary/retry", method="POST"):
        g.user_id = 1
        g.user_role = "admin"
        return web_server.ai_daily_summary_retry.__wrapped__()


def test_manual_retry_is_refused_when_nothing_failed(news_db, clock, monkeypatch):
    body, status = _retry_response(monkeypatch)
    assert status == 409
    assert body.get_json()["status"] == "not_failed"


def test_manual_retry_regenerates_and_clears_the_failure(news_db, clock, alerts, monkeypatch):
    _fail_generation(monkeypatch)
    web_server._send_daily_summaries()

    monkeypatch.setattr(web_server, "_generate_daily_summary_global",
                        lambda date_str: {"summary": "s", "article_count": 3, "stats": {}})
    _stub_delivery(monkeypatch)
    payload = _retry_response(monkeypatch).get_json()

    assert payload["status"] == "completed"
    assert web_server._get_daily_summary_failure(TODAY) is None


def test_manual_retry_that_fails_again_restarts_the_automatic_chain(
        news_db, clock, alerts, monkeypatch):
    _fail_generation(monkeypatch)
    web_server._send_daily_summaries()
    for _ in range(web_server.DAILY_SUMMARY_MAX_RETRIES):
        clock["now"] += web_server.DAILY_SUMMARY_RETRY_INTERVAL_SECONDS
        web_server._send_daily_summaries()
    assert web_server._get_daily_summary_failure(TODAY)["given_up"] == 1

    body, status = _retry_response(monkeypatch)
    assert status == 502
    payload = body.get_json()
    assert payload["status"] == "retrying"
    # Counter restarted, so the scheduler owes three more automatic retries.
    state = web_server._get_daily_summary_failure(TODAY)
    assert state["attempts"] == 1
    assert state["given_up"] == 0
    assert state["alerted"] == 0


def test_retry_route_requires_an_admin():
    resp = web_server.app.test_client().post("/ai/daily-summary/retry")
    assert resp.status_code in (401, 403)


# ─── Frontend contract ──────────────────────────────────────────────────


def test_panel_shows_a_retry_button_only_in_the_failure_states():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    assert "function retryDailySummary()" in html
    assert "'/ai/daily-summary/retry'" in html
    assert 'id="dailySummaryRetryBtn"' in html
    # The button is rendered inside the failure branch, which is itself gated on
    # the caller being an admin.
    assert "isDailySummaryFailureState() && isDailySummaryAdmin()" in html
    assert "失败原因：" in html


def test_inline_pinch_zoom_is_suppressed_so_only_fullscreen_zooms():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    assert "['gesturestart', 'gesturechange', 'gestureend']" in html
    image_rule = html.split(".article-body img{", 1)[1].split("}", 1)[0]
    assert "touch-action:manipulation" in image_rule
