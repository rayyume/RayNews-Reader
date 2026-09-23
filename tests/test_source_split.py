import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import fetcher


def test_process_message_keeps_feed_source_separate_from_publisher_group():
    original_fetch_telegraph = fetcher.fetch_telegraph
    try:
        fetcher.fetch_telegraph = lambda _url: {
            "body_html": "<article><p>Siri changes in iOS 27.</p></article>",
            "images": [],
            "char_count": 27,
            "detected_source": "MacRumors",
        }
        msg = {
            "id": 1,
            "feed_source": "@techfeed",
            "datetime": "2026-06-05T00:00:00+00:00",
            "text": "Siri in iOS 27",
            "html": (
                '<a href="https://telegra.ph/Siri-in-iOS-27-06-02">Siri in iOS 27</a>'
                '<br>via <a href="https://t.me/techfeed">Tech Feed - Telegram Channel</a>'
            ),
            "images": [],
            "videos": [],
            "link_preview_url": "https://telegra.ph/Siri-in-iOS-27-06-02",
            "link_preview_title": "Siri in iOS 27",
        }

        entry = fetcher.process_message(msg, 1001)

        assert entry["source"] == "MacRumors"
        assert entry["group_source"] == "MacRumors"
        assert entry["feed_source"] == "@techfeed"
        assert entry["origin_source"] == "MacRumors"
    finally:
        fetcher.fetch_telegraph = original_fetch_telegraph


def test_raysrss_example_groups_by_publisher_domain():
    content = (
        '<article>Markets move</article><br>via '
        '<a href="https://www.scmp.com/business/article/123">South China Morning Post</a>'
    )
    group, domain = fetcher.detect_group_source(content, "https://telegra.ph/story", "@raysrss")
    assert (group, domain) == ("南华早报", "scmp.com")


def test_unknown_publishers_group_by_registrable_domain_without_network():
    group, domain = fetcher.detect_group_source(
        '<br>via <a href="https://edition.example.co.uk/story">Publisher</a>',
        "",
        "@raysrss",
    )
    assert (group, domain) == ("example.co.uk", "example.co.uk")


def test_wechat_via_uses_account_name_instead_of_qq_platform():
    group, domain = fetcher.detect_group_source(
        '<br>via <a href="https://mp.weixin.qq.com/s/abc">知识分子</a>',
        "https://mp.weixin.qq.com/s/abc", "@raysrss",
    )
    assert (group, domain) == ("知识分子", "")


def test_upsert_reuses_domain_mapping_per_batch_and_observes_later_edits(tmp_path, monkeypatch):
    from source_categories import save_publisher_domain

    monkeypatch.setattr(fetcher, "DB_FILE", tmp_path / "news.db")
    conn = fetcher.init_db()
    calls = []
    lookup = fetcher.publisher_source_for_domain

    def counted_lookup(conn, domain):
        calls.append(domain)
        return lookup(conn, domain)

    monkeypatch.setattr(fetcher, "publisher_source_for_domain", counted_lookup)
    try:
        save_publisher_domain(conn, "example.com", "Old Publisher")
        entries = [{"id": i, "publisher_domain": "example.com"} for i in (1, 2)]
        fetcher.upsert_articles(conn, entries, sync_sources=False)
        assert calls == ["example.com"]
        assert {row[0] for row in conn.execute("SELECT group_source FROM articles")} == {"Old Publisher"}
        save_publisher_domain(conn, "example.com", "New Publisher")
        fetcher.upsert_articles(conn, entries, sync_sources=False)
        assert calls == ["example.com", "example.com"]
        assert {row[0] for row in conn.execute("SELECT group_source FROM articles")} == {"New Publisher"}
    finally:
        conn.close()


if __name__ == "__main__":
    test_process_message_keeps_feed_source_separate_from_publisher_group()
    print("ok")
