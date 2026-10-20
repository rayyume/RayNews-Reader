"""Contracts for authenticated automatic-translation update notifications."""

import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import models
import web_server


@pytest.fixture
def client(monkeypatch, tmp_path):
    db_path = tmp_path / "news.db"
    sqlite3.connect(db_path).close()
    monkeypatch.setattr(web_server, "NEWS_DB", str(db_path))

    users = {
        101: {"id": 101, "role": "user"},
        303: {"id": 303, "role": "auditor"},
    }
    monkeypatch.setattr(models, "get_user", lambda user_id: users.get(user_id))
    monkeypatch.setattr(models, "record_access", lambda user_id: None)
    monkeypatch.setattr(web_server, "ai_access", lambda uid:{"view_translation":True})
    return web_server.app.test_client()


def _auth_headers(user_id=101, role="user"):
    return {"Authorization": f"Bearer {web_server.create_token(user_id, role)}"}


def _insert_article(article_id=42):
    conn = sqlite3.connect(web_server.NEWS_DB)
    conn.execute(
        """
        CREATE TABLE articles (
            id INTEGER PRIMARY KEY, title TEXT, source TEXT, feed_source TEXT,
            origin_source TEXT, summary TEXT, body_html TEXT
        )
        """
    )
    conn.execute(
        "INSERT INTO articles VALUES (?, 'English title', 'Source', 'Source', '', '', '<p>English body</p>')",
        (article_id,),
    )
    conn.commit()
    conn.close()




def test_automatic_full_body_translation_keeps_canonical_body_and_publishes_cache_update(
    client, monkeypatch
):
    _insert_article()
    monkeypatch.setattr(web_server, "_invalidate_refresh_server_cache", lambda article_id: None)

    class TranslationService:
        def __init__(self, **kwargs):
            pass

        def translate_full(self, *args, **kwargs):
            return {"title": "中文标题", "html": "<p>中文正文</p>"}

    monkeypatch.setattr(web_server, "AIService", TranslationService)

    assert web_server._translate_article_background(
        {
            "id": 42,
            "title": "English title",
            "body_html": "<p>English body</p>",
            "translate_content_needed": True,
            "translate_title_needed": True,
        },
        {"api_key": "key", "endpoint": "https://example.test", "model": "model"},
    )

    conn = sqlite3.connect(web_server.NEWS_DB)
    body_html, translation, translation_updated_at = conn.execute(
        "SELECT a.body_html, r.translation, r.translation_updated_at FROM articles a "
        "JOIN ai_results r ON r.article_id = a.id WHERE a.id = 42"
    ).fetchone()
    conn.close()
    assert body_html == "<p>English body</p>"
    assert json.loads(translation)["html"] == "<p>中文正文</p>"
    assert translation_updated_at


def test_translation_updates_do_not_change_when_summary_is_saved(client):
    web_server._init_ai_results_table()
    conn = sqlite3.connect(web_server.NEWS_DB)
    conn.execute("INSERT INTO ai_results (article_id, translation_updated_at) VALUES (42, '2026-07-19 10:00:00.000')")
    conn.commit()
    conn.close()

    web_server._save_ai_result(42, summary="摘要")
    conn = sqlite3.connect(web_server.NEWS_DB)
    unchanged = conn.execute(
        "SELECT translation_updated_at FROM ai_results WHERE article_id = 42"
    ).fetchone()[0]
    conn.close()
    assert unchanged == "2026-07-19 10:00:00.000"


def test_translation_updates_require_reader_authorization_and_reject_bad_cursors(client):
    unauthenticated = client.get("/ai/translation-updates")
    assert unauthenticated.status_code == 401

    forbidden = client.get(
        "/ai/translation-updates",
        headers=_auth_headers(303, "auditor"),
    )
    assert forbidden.status_code == 403

    for cursor in ("definitely-not-a-cursor|0", "2026-07-19 10:00:00|not-an-id"):
        malformed = client.get(
            "/ai/translation-updates",
            query_string={"since": cursor},
            headers=_auth_headers(),
        )
        assert malformed.status_code == 400
        assert malformed.get_json()["error"] == "invalid cursor"
