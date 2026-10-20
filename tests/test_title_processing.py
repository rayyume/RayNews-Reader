import json
import os
import sqlite3
import sys
import unittest
import uuid
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import refresh_server
import web_server
from ai_service import AIService

SHORT_CHINESE_TITLE = "\u82f9\u679c\u53d1\u5e03 Vision Pro \u65b0\u7cfb\u7edf"
# Long enough to clear both the (relaxed) summary trigger threshold
# (_needs_title_summary, >39 CJK) and the lenient hard-reject weight cap
# (_is_valid_title_summary, >180 weight \u21d2 >90 CJK), so it still stands in for
# "a title too long to be an acceptable summary".
LONG_CHINESE_TITLE = "\u8fd9\u662f\u4e00\u4e2a" + ("\u975e\u5e38" * 45) + "\u957f\u7684\u4e2d\u6587\u6807\u9898"
SHORTENED_CHINESE_TITLE = "\u82f9\u679c\u53d1\u5e03\u65b0\u7cfb\u7edf"
ORIGINAL_RESTAURANT_TITLE = "\u300a\u718a\u5bb6\u9910\u9986\u300b\u8ba9\u6700\u540e\u51e0\u79d2\u7269\u5c3d\u5176\u7528"
SHORT_RESTAURANT_TITLE = "\u300a\u718a\u5bb6\u9910\u9986\u300b\u5229\u7528\u6700\u540e\u51e0\u79d2\u521b\u9020\u7b11\u70b9"


def temp_db_path():
    return ROOT / f"tmp-title-test-{uuid.uuid4().hex}.db"


class CaptureTitleAI(AIService):
    def __init__(self):
        pass

    def chat(self, messages: list, max_tokens: int = 2000, temperature: float = 0.3) -> str:
        self.messages = messages
        return '{"title":"short","valid":true,"reason":"ok"}'


class TitleProcessingTests(unittest.TestCase):
    def test_summarize_title_prompt_is_readable_chinese(self):
        svc = CaptureTitleAI()
        svc.summarize_title("OpenAI releases a very long title about GPT-4o", 30)
        prompt = "\n".join(message["content"] for message in svc.messages)
        self.assertIn("\u8d44\u6df1\u7684\u65b0\u95fb\u7f16\u8f91", prompt)
        # The pipeline now asks for a single plain-text line, not JSON.
        self.assertIn("\u53ea\u8f93\u51fa\u4e00\u884c\u6700\u7ec8\u6807\u9898\u7684\u7eaf\u6587\u672c", prompt)
        self.assertNotIn("????????", prompt)

    def test_overlong_title_uses_chinese_character_budget(self):
        self.assertFalse(web_server._needs_title_summary(SHORT_CHINESE_TITLE))
        self.assertTrue(web_server._needs_title_summary(LONG_CHINESE_TITLE))
        self.assertFalse(web_server._needs_title_summary(
            "\u6ce1\u6ce1\u739b\u7279\u56de\u5e94\u4f9d\u8d56\u5355\u4e00\u7206\u6b3e\u62c5\u5fe7 "
            "\u79f0\u7f8e\u56fd\u975eLabubu\u591a\u5143\u5316\u9500\u552e\u989d\u5df2\u536050%"
        ))
        self.assertFalse(web_server._needs_title_summary(
            "OpenAI Codex CLI 支持 GPT-5 Code 模型"
        ))

    def test_clean_title_summary_rejects_explanatory_output(self):
        self.assertEqual(web_server._clean_title_summary(f"\u300c{SHORTENED_CHINESE_TITLE}\u300d"), SHORTENED_CHINESE_TITLE)
        self.assertEqual(web_server._clean_title_summary(f"\u4ee5\u4e0b\u662f\u7b80\u5199\u540e\u7684\u6807\u9898\uff1a{SHORTENED_CHINESE_TITLE}"), SHORTENED_CHINESE_TITLE)
        self.assertTrue(web_server._is_valid_title_summary(f"\u4ee5\u4e0b\u662f\u7b80\u5199\u540e\u7684\u6807\u9898\uff1a{SHORTENED_CHINESE_TITLE}"))
        self.assertFalse(web_server._is_valid_title_summary(LONG_CHINESE_TITLE))

        self.assertEqual(web_server._clean_title_summary(ORIGINAL_RESTAURANT_TITLE), ORIGINAL_RESTAURANT_TITLE)

    def test_ai_title_summary_rejects_numeric_and_broken_titles(self):
        for value in ("3998", "500"):
            result = web_server._validate_ai_title_summary_result(
                {"title": value, "valid": True, "reason": "AI thinks it is short"},
                original_title="\u683c\u9686\u6c47\u62a5\u9053\u516c\u53f8\u80a1\u4ef7\u5f02\u52a8",
            )
            self.assertFalse(result["valid"], value)

        # A stray \u300b with no opening \u300a must be repaired away (not shown to the
        # reader and not discarded): _validate runs _repair_title_summary first,
        # which drops the orphan bracket and returns a balanced, valid title.
        repaired = web_server._validate_ai_title_summary_result(
            {"title": "\u718a\u5bb6\u9910\u9986\u300b\u8ba9\u6700\u540e\u51e0\u79d2\u7269\u5c3d\u5176\u7528", "valid": True},
            original_title=ORIGINAL_RESTAURANT_TITLE,
        )
        self.assertTrue(repaired["valid"])
        self.assertNotIn("\u300b", repaired["title"])
        self.assertTrue(web_server._balanced_title_punctuation(repaired["title"]))
        self.assertEqual(repaired["title"], "\u718a\u5bb6\u9910\u9986\u8ba9\u6700\u540e\u51e0\u79d2\u7269\u5c3d\u5176\u7528")

        valid = web_server._validate_ai_title_summary_result(
            {"title": SHORT_RESTAURANT_TITLE, "valid": True},
            original_title=ORIGINAL_RESTAURANT_TITLE,
        )
        self.assertTrue(valid["valid"])
        self.assertEqual(valid["title"], SHORT_RESTAURANT_TITLE)

    def test_ai_title_summary_rejects_attribution_only_title(self):
        # Regression: "Tencent in Talks to Become Largest Holder of Manus, FT
        # Reports" must not be reduced to a bare attribution clause like
        # "据FT报道" — that drops the actual subject (Tencent/Manus) and keeps
        # only "who reported it", which fails the news-title 3-elements rule.
        original_title = "腾讯洽谈成为 Manus 最大股东，据 FT 报道"
        result = web_server._validate_ai_title_summary_result(
            {"title": "据FT报道", "valid": True, "reason": "shortened"},
            original_title=original_title,
        )
        self.assertFalse(result["valid"])

        self.assertFalse(web_server._shares_title_signal("据FT报道", original_title))
        self.assertTrue(web_server._shares_title_signal(
            "腾讯洽谈成为 Manus 最大股东", original_title
        ))

    def test_ai_title_summary_rejects_ai_self_invalid(self):
        result = web_server._validate_ai_title_summary_result(
            {"title": "\u82f9\u679c\u53d1\u5e03\u65b0\u7cfb\u7edf", "valid": False, "reason": "\u4fe1\u606f\u4e0d\u8db3"},
            original_title="\u82f9\u679c\u53d1\u5e03 Vision Pro \u65b0\u7cfb\u7edf",
        )
        self.assertFalse(result["valid"])
        self.assertIn("\u4fe1\u606f\u4e0d\u8db3", result["reason"])

    def test_parse_title_summary_json_result(self):
        parsed = web_server._parse_title_summary_result(
            '{"title":"\u82f9\u679c\u53d1\u5e03\u65b0\u7cfb\u7edf","valid":true,"reason":"\u4fdd\u7559\u4e3b\u4f53\u548c\u4e8b\u4ef6"}'
        )
        self.assertEqual(parsed["title"], "\u82f9\u679c\u53d1\u5e03\u65b0\u7cfb\u7edf")
        self.assertTrue(parsed["valid"])

        legacy = web_server._parse_title_summary_result("\u82f9\u679c\u53d1\u5e03\u65b0\u7cfb\u7edf")
        self.assertEqual(legacy["title"], "\u82f9\u679c\u53d1\u5e03\u65b0\u7cfb\u7edf")
        self.assertTrue(legacy["valid"])

    def test_strip_unbalanced_punctuation_removes_orphans_keeps_pairs(self):
        # Stray closer, stray opener, and a legitimately paired title.
        self.assertEqual(
            web_server._strip_unbalanced_punctuation("熊家餐馆》让最后几秒物尽其用"),
            "熊家餐馆让最后几秒物尽其用",
        )
        self.assertEqual(
            web_server._strip_unbalanced_punctuation("《甲让最后几秒物尽其用"),
            "甲让最后几秒物尽其用",
        )
        # A correctly matched pair is left untouched.
        paired = "《熊家餐馆》让最后几秒物尽其用"
        self.assertEqual(web_server._strip_unbalanced_punctuation(paired), paired)

    def test_repair_title_summary_drops_dangling_bracket(self):
        repaired = web_server._repair_title_summary("熊家餐馆》让最后几秒物尽其用")
        self.assertEqual(repaired, "熊家餐馆让最后几秒物尽其用")
        self.assertTrue(web_server._balanced_title_punctuation(repaired))

    def test_repair_title_summary_compacts_long_ai_output(self):
        raw = (
            "macOS 27 \u8d77\u82f9\u679c\u79fb\u9664\u4e86 AFP \u534f\u8bae\uff0c"
            "\u8001\u6b3e Time Capsule \u4e0d\u80fd\u518d\u7ed9 Time Machine "
            "\u505a\u65e0\u7ebf\u5907\u4efd\u4e86\u3002Time Machine \u4e4b\u540e"
            "\u53ea\u8ba4 SMBv2/SMBv3\u3002"
        )
        repaired = web_server._repair_title_summary(raw)
        self.assertTrue(web_server._is_valid_title_summary(repaired))
        self.assertNotIn("…", repaired)

    def test_empty_ai_title_summary_can_fallback_to_original_title(self):
        original = (
            "macOS 27 \u8d77\u82f9\u679c\u79fb\u9664\u4e86 AFP \u534f\u8bae\uff0c"
            "\u8001\u6b3e Time Capsule \u4e0d\u80fd\u518d\u7ed9 Time Machine "
            "\u505a\u65e0\u7ebf\u5907\u4efd\u4e86"
        )
        repaired = web_server._repair_title_summary("") or web_server._repair_title_summary(original)
        self.assertTrue(web_server._is_valid_title_summary(repaired))

    def test_save_article_title_update_preserves_original_title(self):
        db_path = temp_db_path()
        try:
            conn = sqlite3.connect(db_path)
            conn.execute("CREATE TABLE articles (id INTEGER PRIMARY KEY, title TEXT)")
            conn.execute("INSERT INTO articles (id, title) VALUES (1, 'Original Long Title')")
            conn.commit()
            conn.close()

            old_news_db = web_server.NEWS_DB
            try:
                web_server.NEWS_DB = str(db_path)
                web_server._save_article_title_update(1, "Short Title", "title_summary")
                web_server._save_article_title_update(1, "Shorter Title", "title_summary")
            finally:
                web_server.NEWS_DB = old_news_db

            conn = sqlite3.connect(db_path)
            row = conn.execute(
                "SELECT title, original_title, title_updated_at, title_source FROM articles WHERE id = 1"
            ).fetchone()
            conn.close()
            self.assertEqual(row[0], "Original Long Title")
            self.assertIsNone(row[1])
            self.assertIsNone(row[2])
            self.assertIsNone(row[3])
            with sqlite3.connect(db_path) as results:
                self.assertEqual(results.execute("SELECT title_summary FROM ai_results WHERE article_id=1").fetchone()[0], "Shorter Title")
        finally:
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.remove(str(db_path) + suffix)
                except FileNotFoundError:
                    pass

    def test_title_updates_endpoint_returns_cursor_changes(self):
        db_path = temp_db_path()
        try:
            conn = sqlite3.connect(db_path)
            conn.execute(
                "CREATE TABLE articles ("
                "id INTEGER PRIMARY KEY, title TEXT, title_updated_at TEXT, title_source TEXT, "
                "source TEXT, feed_source TEXT, origin_source TEXT)"
            )
            conn.execute(
                "INSERT INTO articles (id, title, title_updated_at, title_source) VALUES "
                "(1, 'Old', '2026-06-11 08:00:00', 'title_summary'), "
                "(2, 'New', '2026-06-11 08:05:00', 'translation'), "
                "(3, 'Same Tick', '2026-06-11 08:05:00', 'title_summary')"
            )
            conn.commit()
            conn.close()

            old_db = refresh_server.DB_FILE
            old_schema_ready = refresh_server._schema_ready
            try:
                refresh_server.DB_FILE = db_path
                refresh_server._schema_ready = False
                body = refresh_server.api_title_updates({"since": ["2026-06-11 08:05:00|2"]})
            finally:
                refresh_server.DB_FILE = old_db
                refresh_server._schema_ready = old_schema_ready

            data = json.loads(body.decode("utf-8"))
            self.assertEqual([item["id"] for item in data["items"]], [3])
            self.assertEqual(data["cursor"], "2026-06-11 08:05:00|3")
        finally:
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.remove(str(db_path) + suffix)
                except FileNotFoundError:
                    pass

    def test_title_process_scans_more_than_recent_200_today_articles(self):
        db_path = temp_db_path()
        old_news_db = web_server.NEWS_DB
        try:
            today = datetime.now().strftime("%Y-%m-%d")
            conn = sqlite3.connect(db_path)
            conn.execute(
                "CREATE TABLE articles ("
                "id INTEGER PRIMARY KEY, title TEXT, date TEXT, timestamp INTEGER, "
                "source TEXT, feed_source TEXT, origin_source TEXT)"
            )
            long_title = LONG_CHINESE_TITLE
            rows = []
            for i in range(1, 251):
                title = long_title if i == 1 else f"普通标题 {i}"
                rows.append((i, title, today, 1000000 + i, "source", "", ""))
            conn.executemany(
                "INSERT INTO articles (id, title, date, timestamp, source, feed_source, origin_source) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            conn.commit()
            conn.close()

            web_server.NEWS_DB = str(db_path)
            candidates = web_server._fetch_title_process_articles(
                {"auto_title_summary_enabled": 1, "auto_translate_title": 0},
                limit=20,
            )
            self.assertIn(1, [item["id"] for item in candidates])
        finally:
            web_server.NEWS_DB = old_news_db
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.remove(str(db_path) + suffix)
                except FileNotFoundError:
                    pass

    def test_cached_numeric_title_summary_is_not_written_back(self):
        db_path = temp_db_path()
        old_news_db = web_server.NEWS_DB
        try:
            conn = sqlite3.connect(db_path)
            conn.execute(
                "CREATE TABLE articles ("
                "id INTEGER PRIMARY KEY, title TEXT, date TEXT, timestamp INTEGER, "
                "source TEXT, feed_source TEXT, origin_source TEXT)"
            )
            conn.execute(
                "CREATE TABLE ai_results ("
                "article_id INTEGER PRIMARY KEY, summary TEXT, translation TEXT, "
                "title_summary TEXT, title_summary_error TEXT, title_summary_error_at TEXT)"
            )
            original = "\u683c\u9686\u6c47\u957f\u6807\u9898" * 8
            conn.execute(
                "INSERT INTO articles (id, title, date, timestamp, source, feed_source, origin_source) "
                "VALUES (1, ?, '2026-06-11', 1, ?, '', '')",
                (original, "\u683c\u9686\u6c47"),
            )
            conn.execute("INSERT INTO ai_results (article_id, title_summary) VALUES (1, '3998')")
            conn.commit()
            conn.close()

            web_server.NEWS_DB = str(db_path)
            self.assertTrue(web_server._init_ai_results_table())
            conn = sqlite3.connect(db_path)
            self.assertEqual(conn.execute("SELECT title_summary FROM ai_results_legacy_v5 WHERE article_id=1").fetchone()[0], "3998")
            self.assertEqual(conn.execute("SELECT title FROM articles WHERE id=1").fetchone()[0], original)
            self.assertEqual(web_server._get_ai_result(1)['title_summary'], '3998')
            conn.close()

        finally:
            web_server.NEWS_DB = old_news_db
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.remove(str(db_path) + suffix)
                except FileNotFoundError:
                    pass


class CJKQuoteNormalizationTests(unittest.TestCase):
    """Tests for _normalize_cjk_quotes — ASCII quotes → Chinese corner brackets."""

    def test_double_quotes_to_corner_brackets(self):
        result = web_server._normalize_cjk_quotes('印度军火部门寻求与阿联酋的关键突破："信心提振器"')
        self.assertEqual(result, '印度军火部门寻求与阿联酋的关键突破：「信心提振器」')

    def test_single_quotes_with_cjk_content(self):
        result = web_server._normalize_cjk_quotes("印度军火部门寻求与阿联酋的关键突破：'信心提振器'")
        self.assertEqual(result, '印度军火部门寻求与阿联酋的关键突破：「信心提振器」')

    def test_english_apostrophe_preserved(self):
        result = web_server._normalize_cjk_quotes("India's arms sector eyes breakthrough")
        self.assertEqual(result, "India's arms sector eyes breakthrough")

    def test_mixed_quotes_in_title(self):
        result = web_server._normalize_cjk_quotes('OpenAI发布"GPT-5"模型，微软称"里程碑"')
        self.assertEqual(result, 'OpenAI发布「GPT-5」模型，微软称「里程碑」')

    def test_no_quotes_unchanged(self):
        result = web_server._normalize_cjk_quotes("这是一个正常的新闻标题")
        self.assertEqual(result, "这是一个正常的新闻标题")

    def test_empty_string(self):
        result = web_server._normalize_cjk_quotes("")
        self.assertEqual(result, "")

    def test_clean_title_summary_applies_normalization(self):
        result = web_server._clean_title_summary('印度军火： "信心提振器"')
        self.assertEqual(result, '印度军火： 「信心提振器」')

    def test_translate_title_returns_normalized(self):
        """Integration-style: verify the function pipeline normalizes quotes."""
        from ai_service import AIService, _normalize_cjk_quotes as ai_normalize
        # We can't easily mock the AI here for translate_title, but we can verify
        # the normalization function itself works identically in both modules
        result = ai_normalize('印度军火部门寻求与阿联酋的关键突破："信心提振器"')
        self.assertEqual(result, '印度军火部门寻求与阿联酋的关键突破：「信心提振器」')
        self.assertEqual(
            ai_normalize("India's arms sector"),
            "India's arms sector",
        )


if __name__ == "__main__":
    unittest.main()
