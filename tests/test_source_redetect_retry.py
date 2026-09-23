import sqlite3

import fetcher
import web_server


def test_unreliable_detection_preserves_source_and_remains_retryable(tmp_path, monkeypatch):
    db_path = tmp_path / "news.db"
    monkeypatch.setattr(fetcher, "DB_FILE", db_path)
    monkeypatch.setattr(web_server, "NEWS_DB", str(db_path))
    conn = fetcher.init_db()
    conn.execute(
        "INSERT INTO articles "
        "(id, title, source, feed_source, group_source, origin_source, timestamp) "
        "VALUES (1, '普通新闻', '原有来源', '@raysrss', '原有来源', '原始署名', 100)"
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(web_server, "_fetch_telegram_message_content", lambda _id: "")
    first = web_server._redetect_article_sources_work(10, 1, False, pending_only=True)
    assert first["skipped"] == 1
    with sqlite3.connect(db_path) as check:
        row = check.execute(
            "SELECT source, group_source, origin_source, source_detection_version, "
            "source_detection_last_attempt_at FROM articles WHERE id = 1"
        ).fetchone()
        assert row[:4] == ("原有来源", "原有来源", "原始署名", 0)
        assert row[4] > 0
        check.execute("UPDATE articles SET source_detection_last_attempt_at = 0 WHERE id = 1")
        check.commit()
    monkeypatch.setattr(
        fetcher, "detect_group_source", lambda *_args: ("可靠新来源", "")
    )
    second = web_server._redetect_article_sources_work(10, 0, False, pending_only=True)
    assert second["updated"] == 1
    with sqlite3.connect(db_path) as check:
        row = check.execute(
            "SELECT source, group_source, origin_source, source_detection_version "
            "FROM articles WHERE id = 1"
        ).fetchone()
        assert row == ("可靠新来源", "可靠新来源", "可靠新来源", 1)


def test_redetection_replaces_legacy_wechat_qq_domain(tmp_path, monkeypatch):
    db_path = tmp_path / "news.db"
    monkeypatch.setattr(fetcher, "DB_FILE", db_path)
    monkeypatch.setattr(web_server, "NEWS_DB", str(db_path))
    conn = fetcher.init_db()
    conn.execute(
        "INSERT INTO articles "
        "(id, title, source, feed_source, group_source, publisher_domain, "
        "origin_source, body_html, timestamp) "
        "VALUES (1, '新闻', 'qq.com', '@raysrss', 'qq.com', 'qq.com', 'qq.com', "
        "'<br>via <a href=\"https://mp.weixin.qq.com/s/abc\">知识分子</a>', 100)"
    )
    conn.commit()
    conn.close()
    result = web_server._redetect_article_sources_work(10, 0, False, pending_only=True)
    assert result["updated"] == 1
    with sqlite3.connect(db_path) as check:
        assert check.execute(
            "SELECT source, publisher_domain, origin_source FROM articles WHERE id = 1"
        ).fetchone() == ("知识分子", "", "知识分子")


def test_pending_backfill_does_not_replace_source_from_stored_domain_alone(tmp_path, monkeypatch):
    db_path = tmp_path / "news.db"
    monkeypatch.setattr(fetcher, "DB_FILE", db_path)
    monkeypatch.setattr(web_server, "NEWS_DB", str(db_path))
    conn = fetcher.init_db()
    conn.execute(
        "INSERT INTO articles "
        "(id, title, source, feed_source, group_source, publisher_domain, "
        "origin_source, timestamp) "
        "VALUES (1, '新闻', '人工来源', '@raysrss', '人工来源', 'example.com', '人工署名', 100)"
    )
    conn.commit()
    conn.close()
    result = web_server._redetect_article_sources_work(10, 0, False, pending_only=True)
    assert result["skipped"] == 1
    with sqlite3.connect(db_path) as check:
        assert check.execute(
            "SELECT source, publisher_domain, origin_source, source_detection_version "
            "FROM articles WHERE id = 1"
        ).fetchone() == ("人工来源", "example.com", "人工署名", 0)


def test_network_quota_skips_do_not_cool_down_unrequested_articles(tmp_path, monkeypatch):
    db_path = tmp_path / "news.db"
    monkeypatch.setattr(fetcher, "DB_FILE", db_path)
    monkeypatch.setattr(web_server, "NEWS_DB", str(db_path))
    conn = fetcher.init_db()
    for article_id in range(1, 12):
        conn.execute(
            "INSERT INTO articles (id, title, source, feed_source, group_source, timestamp) "
            "VALUES (?, '普通新闻', '@raysrss', '@raysrss', '@raysrss', ?)",
            (article_id, 12 - article_id),
        )
    conn.commit()
    conn.close()
    calls = []

    def fetch_message(article_id):
        calls.append(article_id)
        return "<br>via 知识分子" if article_id == 11 else ""

    monkeypatch.setattr(web_server, "_fetch_telegram_message_content", fetch_message)
    first = web_server._redetect_article_sources_work(100, 10, False, pending_only=True)
    assert first["telegram_checked"] == 10
    assert calls == list(range(1, 11))
    with sqlite3.connect(db_path) as check:
        assert check.execute(
            "SELECT source_detection_last_attempt_at FROM articles WHERE id = 11"
        ).fetchone()[0] == 0
    second = web_server._redetect_article_sources_work(100, 10, False, pending_only=True)
    assert second["telegram_checked"] == 1
    assert calls[-1] == 11
    with sqlite3.connect(db_path) as check:
        assert check.execute(
            "SELECT source, source_detection_version FROM articles WHERE id = 11"
        ).fetchone() == ("知识分子", 1)


def test_manual_category_migrates_when_its_feed_finishes_despite_other_pending_feed(
        tmp_path, monkeypatch):
    db_path = tmp_path / "news.db"
    monkeypatch.setattr(fetcher, "DB_FILE", db_path)
    monkeypatch.setattr(web_server, "NEWS_DB", str(db_path))
    conn = fetcher.init_db()
    conn.execute(
        "INSERT INTO source_categories (source, category, label, status) "
        "VALUES ('@feed-a', 'Tech', '甲订阅', 'manual')"
    )
    conn.executemany(
        "INSERT INTO articles "
        "(id, title, source, feed_source, group_source, source_detection_version, timestamp) "
        "VALUES (?, '普通新闻', ?, ?, ?, ?, ?)",
        [
            (1, '甲发布者', '@feed-a', '甲发布者', 1, 3),
            (2, '甲发布者', '@feed-a', '甲发布者', 0, 2),
            (3, '@feed-b', '@feed-b', '@feed-b', 0, 1),
        ],
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(web_server, "_fetch_telegram_message_content", lambda _id: "")
    _, remaining, migrated = web_server._run_source_identity_backfill_once()
    assert remaining == 2
    assert migrated == 0
    with sqlite3.connect(db_path) as check:
        check.execute("UPDATE articles SET source_detection_version = 1 WHERE id = 2")
        check.commit()
    _, remaining, migrated = web_server._run_source_identity_backfill_once()
    assert remaining == 1
    assert migrated == 1
    with sqlite3.connect(db_path) as check:
        assert check.execute(
            "SELECT category, label, status FROM source_categories WHERE source = '甲发布者'"
        ).fetchone() == ("Tech", "甲订阅", "manual")
