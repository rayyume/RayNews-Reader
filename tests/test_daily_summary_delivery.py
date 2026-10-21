"""Fixed-schedule generation and dual-channel delivery of the daily summary.

Generation is server-owned (system AI, once per Beijing day at
DAILY_SUMMARY_HOUR); users can no longer trigger it with their own key. The
in-app copy goes to everyone by default and is independent of the email copy.
"""

import datetime as dt
import os
import sqlite3
import sys
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import models
import ai_access
import pytest
import web_server


def _beijing(hour, minute=0):
    return dt.datetime(2026, 7, 10, hour, minute, tzinfo=dt.timezone(dt.timedelta(hours=8)))


@pytest.fixture(autouse=True)
def isolated_news_db(tmp_path, monkeypatch):
    path = tmp_path / 'news.db'
    sqlite3.connect(path).close()
    monkeypatch.setattr(web_server, 'NEWS_DB', str(path))


class DailySummaryInAppDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.db_path = ROOT / f"tmp-daily-inapp-{uuid.uuid4().hex}.db"
        self.old_db_file = models.DB_FILE
        models.close_db()
        models.DB_FILE = self.db_path
        models.get_db()
        self.user_a = models.create_user("a@example.com", "pw", "A")["id"]
        self.user_b = models.create_user("b@example.com", "pw", "B")["id"]
        for uid in (self.user_a, self.user_b):
            ai_access.set_permissions(uid, {"daily_digest": True})
            models.set_user_settings(uid, view_daily_digest=1)
        self.result = {"summary": "## 今日要闻\n- 一条", "article_count": 7, "stats": {}}

    def tearDown(self):
        models.close_db()
        models.DB_FILE = self.old_db_file
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(str(self.db_path) + suffix)
            except FileNotFoundError:
                pass

    def test_delivers_to_every_user_including_those_without_a_settings_row(self):
        # user_b never opened settings, so it has no user_settings row at all.
        models.set_user_settings(self.user_a, theme_preference="dark")

        outcome = web_server._deliver_daily_summary_inapp("2026-07-10", self.result)

        self.assertEqual(outcome["status"], "ok")
        self.assertEqual(outcome["recipients"], 2)
        for user_id in (self.user_a, self.user_b):
            items = models.list_notifications(user_id)
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["type"], "daily_summary")
            self.assertEqual(items[0]["title"], "RayNews每日总结")
            self.assertEqual(items[0]["format"], "markdown")
            self.assertEqual(items[0]["body"], self.result["summary"])
            self.assertEqual(models.count_unread_notifications(user_id), 1)

    def test_repeated_scheduler_ticks_do_not_duplicate_the_same_day(self):
        # The scheduler ticks once a minute across a ten-minute window, and a
        # restart inside that window replays the same date.
        first = web_server._deliver_daily_summary_inapp("2026-07-10", self.result)
        second = web_server._deliver_daily_summary_inapp("2026-07-10", self.result)

        self.assertEqual(first["status"], "ok")
        self.assertEqual(second["status"], "skipped")
        self.assertEqual(len(models.list_notifications(self.user_a)), 1)

    def test_a_new_day_delivers_again(self):
        web_server._deliver_daily_summary_inapp("2026-07-10", self.result)
        web_server._deliver_daily_summary_inapp("2026-07-11", self.result)
        self.assertEqual(len(models.list_notifications(self.user_a)), 2)

    def test_opted_out_user_is_excluded_but_others_still_receive_it(self):
        models.set_user_settings(self.user_a, daily_summary_inapp_enabled=0)

        outcome = web_server._deliver_daily_summary_inapp("2026-07-10", self.result)

        self.assertEqual(outcome["recipients"], 1)
        self.assertEqual(models.list_notifications(self.user_a), [])
        self.assertEqual(len(models.list_notifications(self.user_b)), 1)

    def test_in_app_preference_defaults_on_for_existing_and_new_rows(self):
        # A settings row created for an unrelated reason must still be a recipient.
        models.set_user_settings(self.user_a, theme_preference="light")
        settings = models.get_user_settings(self.user_a)
        self.assertEqual(settings["daily_summary_inapp_enabled"], 1)
        self.assertIn(self.user_a, models.get_daily_summary_inapp_user_ids())

    def test_in_app_failure_does_not_raise_into_the_scheduler(self):
        def boom():
            raise RuntimeError("db gone")

        original = web_server.get_daily_summary_inapp_user_ids
        web_server.get_daily_summary_inapp_user_ids = boom
        try:
            outcome = web_server._deliver_daily_summary_inapp("2026-07-10", self.result)
        finally:
            web_server.get_daily_summary_inapp_user_ids = original
        self.assertEqual(outcome["status"], "error")


class DailySummaryScheduleTests(unittest.TestCase):
    """Generation must not depend on the email channel being usable."""

    def setUp(self):
        self.calls = []
        self.original_generate = web_server._generate_daily_summary_global
        self.original_inapp = web_server._deliver_daily_summary_inapp
        self.original_email = web_server._deliver_daily_summary_email
        self.original_now = web_server._daily_summary_now

        web_server._generate_daily_summary_global = lambda date_str: (
            self.calls.append(("generate", date_str)),
            {"summary": "s", "article_count": 1, "stats": {}},
        )[1]
        web_server._deliver_daily_summary_inapp = lambda date_str, result: (
            self.calls.append(("inapp", date_str)), {"status": "ok", "recipients": 3}
        )[1]
        web_server._deliver_daily_summary_email = lambda date_str, result, force=False: (
            self.calls.append(("email", date_str)), {"status": "ok", "sent": 0}
        )[1]

    def tearDown(self):
        web_server._generate_daily_summary_global = self.original_generate
        web_server._deliver_daily_summary_inapp = self.original_inapp
        web_server._deliver_daily_summary_email = self.original_email
        web_server._daily_summary_now = self.original_now

    def test_generation_runs_before_either_channel(self):
        web_server._daily_summary_now = lambda: _beijing(web_server.DAILY_SUMMARY_HOUR, 1)
        outcome = web_server._broadcast_daily_summary(force=False)
        self.assertEqual([c[0] for c in self.calls], ["generate", "inapp", "email"])
        self.assertEqual(outcome["inapp"], {"status": "ok", "recipients": 3})

    def test_missing_resend_key_does_not_block_in_app_delivery(self):
        # The email leg owns the RESEND_API_KEY check now; the real one is
        # exercised here rather than the stub.
        web_server._deliver_daily_summary_email = self.original_email
        web_server._daily_summary_now = lambda: _beijing(web_server.DAILY_SUMMARY_HOUR, 1)
        old_key = os.environ.pop("RESEND_API_KEY", None)
        try:
            outcome = web_server._broadcast_daily_summary(force=False)
        finally:
            if old_key is not None:
                os.environ["RESEND_API_KEY"] = old_key
        self.assertIn(("inapp", "2026-07-10"), self.calls)
        self.assertEqual(outcome["inapp"]["status"], "ok")

    def test_outside_the_window_nothing_is_generated(self):
        web_server._daily_summary_now = lambda: _beijing(9, 0)
        outcome = web_server._broadcast_daily_summary(force=False)
        self.assertEqual(outcome["status"], "skipped")
        self.assertEqual(self.calls, [])


class DailySummaryRouteTests(unittest.TestCase):
    def setUp(self):
        self.original_now = web_server._daily_summary_now
        self.original_cache = web_server._get_daily_summary_global_cache

    def tearDown(self):
        web_server._daily_summary_now = self.original_now
        web_server._get_daily_summary_global_cache = self.original_cache

    def test_manual_generation_is_refused(self):
        client = web_server.app.test_client()
        with web_server.app.test_request_context():
            pass
        # No auth header: the role decorator rejects first, which is enough to
        # prove there is no unauthenticated generation path. The refusal for an
        # authenticated caller is asserted on the handler directly below.
        resp = client.post("/ai/daily-summary")
        self.assertIn(resp.status_code, (401, 403))

    def test_manual_generation_handler_never_calls_the_ai(self):
        import inspect

        source = inspect.getsource(web_server.ai_daily_summary)
        self.assertIn("403", source)
        # It must not reach for the caller's own AI config any more.
        self.assertNotIn("get_ai_config", source)
        self.assertNotIn("AIService", source)

    def test_today_reports_scheduled_before_generation_time(self):
        web_server._get_daily_summary_global_cache = lambda date_str: None
        web_server._daily_summary_now = lambda: _beijing(9, 0)
        with web_server.app.test_request_context("/ai/daily-summary/today"):
            from flask import g

            g.user_id = 1
            g.user_role = "user"
            payload = web_server.ai_daily_summary_today.__wrapped__().get_json()
        self.assertEqual(payload["status"], "scheduled")
        self.assertEqual(
            payload["generate_at"],
            f"{web_server.DAILY_SUMMARY_HOUR:02d}:{web_server.DAILY_SUMMARY_MINUTE:02d}",
        )

    def test_today_reports_generating_inside_the_window(self):
        web_server._get_daily_summary_global_cache = lambda date_str: None
        web_server._daily_summary_now = lambda: _beijing(web_server.DAILY_SUMMARY_HOUR, 2)
        with web_server.app.test_request_context("/ai/daily-summary/today"):
            from flask import g

            g.user_id = 1
            g.user_role = "user"
            payload = web_server.ai_daily_summary_today.__wrapped__().get_json()
        self.assertEqual(payload["status"], "generating")

    def test_today_reports_unavailable_long_after_the_window(self):
        web_server._get_daily_summary_global_cache = lambda date_str: None
        web_server._daily_summary_now = lambda: _beijing(23, 59)
        with web_server.app.test_request_context("/ai/daily-summary/today"):
            from flask import g

            g.user_id = 1
            g.user_role = "user"
            payload = web_server.ai_daily_summary_today.__wrapped__().get_json()
        self.assertEqual(payload["status"], "unavailable")


class DailySummaryFrontendContractTests(unittest.TestCase):
    """The ✨ panel must not offer generation, and must explain the schedule."""

    @classmethod
    def setUpClass(cls):
        cls.html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")

    def test_no_manual_generation_controls_remain(self):
        self.assertNotIn("generateDailySummary", self.html)
        self.assertNotIn("生成每日总结", self.html)
        self.assertNotIn("重新生成", self.html)
        # No client-side POST to the retired generation route.
        self.assertNotIn("'/ai/daily-summary'", self.html)

    def test_scheduled_state_explains_the_time_and_links_to_push_settings(self):
        self.assertIn("每日总结将在每天 ", self.html)
        self.assertIn("esc(timezone)", self.html)
        self.assertIn("function openDailySummaryPushSettings()", self.html)
        self.assertIn("每日总结推送设置", self.html)
        # The jump target has to exist for scrollIntoView to land anywhere.
        self.assertIn('id="dailySummaryPushSection"', self.html)
        self.assertIn("switchSettingsTab('notify')", self.html)

    def test_in_app_delivery_toggle_is_wired_to_the_settings_payload(self):
        self.assertIn('id="dailySummaryInappToggle"', self.html)
        self.assertIn("daily_summary_inapp_enabled:", self.html)
        # Absent field must read as on, matching the server-side default.
        self.assertIn("data.daily_summary_inapp_enabled === undefined", self.html)


if __name__ == "__main__":
    unittest.main()

@pytest.fixture(autouse=True)
def authorized_daily_route(monkeypatch, request):
    if request.node.cls and request.node.cls.__name__ == 'DailySummaryRouteTests':
        monkeypatch.setattr(web_server, 'ai_permissions', lambda uid:{'daily_digest':True})
        monkeypatch.setattr(web_server, 'daily_enabled', lambda uid:True)
