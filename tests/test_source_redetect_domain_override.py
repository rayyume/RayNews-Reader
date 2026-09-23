import sqlite3

import fetcher
import web_server


def test_single_source_redetect_reapplies_mapping_from_stored_domain(tmp_path, monkeypatch):
    db_path = tmp_path / "news.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE articles (id INTEGER PRIMARY KEY, title TEXT, source TEXT, "
        "feed_source TEXT, group_source TEXT, publisher_domain TEXT, source_detection_version INTEGER, "
        "origin_source TEXT, body_html TEXT, summary TEXT, telegraph_url TEXT, timestamp INTEGER)"
    )
    conn.execute(
        "INSERT INTO articles VALUES (1, 'Story', 'Old Publisher', '@feed', 'Old Publisher', "
        "'example.com', 1, 'Old Publisher', '', '', '', 1)"
    )
    conn.execute(
        "CREATE TABLE publisher_domains (domain TEXT PRIMARY KEY, group_source TEXT, "
        "enabled INTEGER, is_builtin INTEGER, updated_at TEXT DEFAULT '')"
    )
    conn.execute(
        "INSERT INTO publisher_domains (domain, group_source, enabled, is_builtin) "
        "VALUES ('example.com', 'New Publisher', 1, 0)"
    )
    conn.commit()
    monkeypatch.setattr(web_server, "_get_news_db", lambda: conn)
    monkeypatch.setattr(web_server, "_fetch_telegram_message_content", lambda _article_id: "")
    monkeypatch.setattr(fetcher, "detect_group_source", lambda *_args: ("Old Publisher", ""))

    with web_server.app.test_request_context(
        "/sources/redetect-single", method="POST", json={"source": "Old Publisher"}
    ):
        response = web_server.redetect_single_source.__wrapped__()

    assert response.get_json()["updated"] == 1
    row = conn.execute("SELECT source, group_source, publisher_domain FROM articles WHERE id = 1").fetchone()
    assert tuple(row) == ("New Publisher", "New Publisher", "example.com")
    conn.close()


def test_single_source_redetect_does_not_treat_display_label_as_classified_source(
        tmp_path, monkeypatch):
    db_path = tmp_path / "news.db"
    monkeypatch.setattr(fetcher, "DB_FILE", db_path)
    conn = fetcher.init_db()
    conn.execute(
        "INSERT INTO source_categories (source, category, label, status) "
        "VALUES ('Example Media', 'News', 'Example', 'classified')"
    )
    conn.execute(
        "INSERT INTO articles (id, title, source, feed_source, group_source, "
        "body_html, timestamp) VALUES "
        "(1, 'Story', 'Example', '@feed', 'Example', '<p>via 知识分子</p>', 1)"
    )
    conn.commit()
    monkeypatch.setattr(web_server, "_get_news_db", lambda: conn)
    monkeypatch.setattr(web_server, "_fetch_telegram_message_content", lambda _id: "")
    with web_server.app.test_request_context(
        "/sources/redetect-single", method="POST", json={"source": "Example"}
    ):
        response = web_server.redetect_single_source.__wrapped__()
    assert response.get_json()["updated"] == 1
    assert conn.execute("SELECT source FROM articles WHERE id=1").fetchone()[0] == "知识分子"
    conn.close()
