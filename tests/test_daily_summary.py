import os
import re
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ai_service import AIService


class FakeDailyAI(AIService):
    def __init__(self, replies=None, errors=None):
        super().__init__("key", "https://api.example.com/v1", "model")
        self.calls = []
        self.replies = list(replies or [])
        self.errors = list(errors or [])

    def chat(self, messages, max_tokens=2000, temperature=0.3):
        self.calls.append({
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        })
        if self.errors:
            raise self.errors.pop(0)
        if self.replies:
            return self.replies.pop(0)
        return (
            "## 政经新闻\n"
            "1. **重点政策动态：** 重要政策事件影响市场预期。 "
            "[🔗](https://news.rayyu.me/#/article/26-06-10-1)\n"
        )


def _article(idx, title, summary="", source="测试源", date="2026-06-10"):
    return {
        "id": idx,
        "date": date,
        "source": source,
        "title": title,
        "summary": summary,
        "body_html": "",
    }


class DailySummaryTests(unittest.TestCase):
    def test_uses_selection_stage_and_filters_invalid_summary(self):
        env = {
            "AI_DAILY_TARGET_ITEMS": "4",
            "AI_DAILY_MIN_ITEMS": "3",
            "AI_DAILY_MAX_ITEMS": "5",
            "AI_DAILY_MAX_CANDIDATES": "8",
        }
        with patch.dict(os.environ, env, clear=False):
            bad_summary = (
                "via 金十数据：您提供的文章内容仅为“via 金十数据”，缺少具体正文信息。"
                "请补充需要摘要的文章全文，以便我为您生成200字以内的简洁中文摘要。"
            )
            articles = [
                _article(1, "央行开展公开市场操作稳定流动性", bad_summary, "金十数据"),
                _article(2, "OpenAI 发布新的企业 AI 工具", "OpenAI 发布企业工具，强化数据分析和协作能力。", "科技源"),
                _article(3, "多地优化消费补贴政策", "多地推出消费补贴，带动家电和汽车消费。", "财经源"),
                _article(4, "芯片公司公布季度财报", "芯片公司营收增长，数据中心需求继续走强。", "科技源"),
            ]
            selection = '{"selected_ids":[1,2,3,4]}'
            final = (
                "## 政经新闻\n"
                "1. **央行稳定流动性：** 央行开展公开市场操作以稳定资金面。 "
                "[🔗](https://news.rayyu.me/#/article/26-06-10-1)\n"
                "## 科技动态\n"
                "1. **OpenAI 推出企业工具：** OpenAI 强化企业数据分析和协作能力。 "
                "[🔗](https://news.rayyu.me/#/article/26-06-10-2)\n"
                "2. **芯片公司财报增长：** 芯片公司受数据中心需求带动实现营收增长。 "
                "[🔗](https://news.rayyu.me/#/article/26-06-10-4)\n"
            )
            svc = FakeDailyAI(replies=[selection, final])

            result = svc.daily_summary(articles)

        self.assertEqual(len(svc.calls), 2)
        selection_prompt = "\n".join(m["content"] for m in svc.calls[0]["messages"])
        self.assertIn("选题编辑", selection_prompt)
        self.assertNotIn("请补充需要摘要的文章全文", selection_prompt)
        self.assertIn("央行开展公开市场操作稳定流动性", selection_prompt)
        self.assertEqual(result["stats"]["daily_target_items"], 4)
        self.assertEqual(result["stats"]["articles_selected_for_summary"], 4)
        self.assertIs(result["stats"]["selection_ai_used"], True)

    def test_fallback_is_dynamic_and_does_not_fill_categories(self):
        env = {
            "AI_DAILY_TARGET_ITEMS": "12",
            "AI_DAILY_MIN_ITEMS": "10",
            "AI_DAILY_MAX_ITEMS": "14",
            "AI_DAILY_MAX_CANDIDATES": "30",
        }
        with patch.dict(os.environ, env, clear=False):
            articles = []
            for i in range(1, 19):
                articles.append(_article(i, f"AI 芯片与模型新闻 {i}", f"AI 芯片公司发布重要进展 {i}。", "科技源"))
            for i in range(19, 23):
                articles.append(_article(i, f"央行政策新闻 {i}", f"央行政策影响市场预期 {i}。", "政经源"))
            articles.append(_article(99, "via 金十数据", "via 金十数据", "金十数据"))

            svc = FakeDailyAI(errors=[RuntimeError("AI API HTTP 400: Content Exists Risk")])

            result = svc.daily_summary(articles)

        summary = result["summary"]
        self.assertNotIn("via 金十数据", summary)
        self.assertNotIn("（无相关新闻）", summary)
        item_count = len(re.findall(r"^\d+\.\s+\*\*", summary, flags=re.M))
        self.assertGreaterEqual(item_count, 10)
        self.assertLessEqual(item_count, 14)
        tech_count = summary.split("## 科技动态", 1)[1].split("##", 1)[0].count(". **")
        news_count = summary.split("## 政经新闻", 1)[1].split("##", 1)[0].count(". **")
        self.assertGreater(tech_count, news_count)

    def test_long_unpunctuated_english_summary_uses_word_boundary(self):
        env = {
            "AI_DAILY_TARGET_ITEMS": "1",
            "AI_DAILY_MIN_ITEMS": "1",
            "AI_DAILY_MAX_ITEMS": "2",
            "AI_DAILY_MAX_CANDIDATES": "2",
            "AI_DAILY_SUMMARY_CHARS": "44",
        }
        article = _article(
            7,
            "OpenAI platform update expands enterprise developer workflows",
            (
                "OpenAI platform update expands enterprise developer "
                "workflows with automation observability governance controls"
            ),
            "Tech Source",
        )
        final = (
            "## 科技动态\n"
            "1. **OpenAI 更新企业平台：** OpenAI 扩展企业开发者工作流能力。 "
            "[🔗](https://news.rayyu.me/#/article/26-06-10-7)\n"
        )
        with patch.dict(os.environ, env, clear=False):
            svc = FakeDailyAI(replies=['{"selected_ids":[7]}', final])
            svc.daily_summary([article])

        selection_prompt = "\n".join(m["content"] for m in svc.calls[0]["messages"])
        summary_line = re.search(r"内容摘要：(.+)", selection_prompt).group(1)
        self.assertEqual(summary_line, "OpenAI platform update expands enterprise")

    def test_short_but_complete_looking_ai_output_is_not_cached_as_full_digest(self):
        env = {
            "AI_DAILY_TARGET_ITEMS": "4",
            "AI_DAILY_MIN_ITEMS": "3",
            "AI_DAILY_MAX_ITEMS": "5",
            "AI_DAILY_MAX_CANDIDATES": "8",
        }
        articles = [
            _article(i, f"央行政策新闻 {i}", f"央行发布政策措施 {i}，影响市场预期。")
            for i in range(1, 6)
        ]
        one_item = (
            "## 政经新闻\n"
            "1. **央行政策：** 央行发布政策措施。 "
            "[🔗](https://news.rayyu.me/#/article/26-06-10-1)\n"
        )
        with patch.dict(os.environ, env, clear=False):
            svc = FakeDailyAI(replies=['{"selected_ids":[1,2,3,4]}', one_item, ""])
            result = svc.daily_summary(articles)

        self.assertEqual(len(svc.calls), 3)  # selection, final, continuation
        self.assertGreaterEqual(
            len(re.findall(r"^\d+\.\s+\*\*", result["summary"], flags=re.M)), 3
        )
        self.assertEqual(result["stats"]["fallback_reason"], "AI output looked truncated")


if __name__ == "__main__":
    unittest.main()
