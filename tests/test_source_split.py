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


def test_unclassified_single_footer_website_works_without_via():
    group, domain = fetcher.detect_group_source(
        '<p>正文</p><p><a href="https://edition.example.co.uk/story">来源网站</a></p>',
        "https://telegra.ph/story", "@feed",
    )
    assert (group, domain) == ("example.co.uk", "example.co.uk")


def test_plain_via_takes_priority_over_unrelated_preview_and_image_link():
    content = (
        '<p>新闻正文</p><p>via 知识分子 '
        '<a href="https://www.ifeng.com/photo"><img src="cover.jpg"></a></p>'
    )
    assert fetcher.detect_group_source(content, "https://telegra.ph/story", "@feed") == (
        "知识分子", ""
    )


def test_explicit_via_beats_known_reference_link():
    reference = '<a href="https://www.reuters.com/story">路透社背景</a>'
    for via, expected in (
        ('via 知识分子', ('知识分子', '')),
        ('via <a href="https://unknown.example.org/story">未知媒体</a>',
         ('example.org', 'example.org')),
    ):
        content = f'<p>新闻正文</p><p>{reference}</p><p>{via}</p>'
        assert fetcher.detect_group_source(content, "", "@feed") == expected


def test_plain_via_before_reference_link_in_same_footer_wins():
    content = (
        '<p>新闻正文</p><p>via 知识分子 '
        '<a href="https://www.reuters.com/story">背景链接</a></p>'
    )
    assert fetcher.detect_group_source(content, "", "@feed") == ('知识分子', '')


def test_classified_reference_does_not_override_unknown_explicit_via():
    content = (
        '<p>正文</p><p><a href="https://www.reuters.com/story">背景</a></p>'
        '<p>via 知识分子</p>'
    )
    history = ({'路透社': '路透社'}, {'reuters.com': '路透社'})
    assert fetcher.source_from_classified_history(content, history) is None
    assert fetcher.detect_group_source(content, '', '@feed') == ('知识分子', '')


def test_classified_telegram_reference_does_not_override_unrelated_via():
    history = ({'科技圈': '科技圈'}, {})
    footer = '<p><a href="https://t.me/tech_circle">科技圈</a></p>'
    for via in (
        '<p>via 知识分子</p>',
        '<p>via <a href="https://mp.weixin.qq.com/s/article">知识分子</a></p>',
    ):
        content = '<p>正文</p>' + footer + via
        assert fetcher.source_from_classified_history(content, history) is None
        assert fetcher.detect_group_source(content, '', '@feed') == ('知识分子', '')


def test_classified_telegram_footer_beats_weibo_reference(tmp_path, monkeypatch):
    # Reproduces article 348108: the Weibo URL is the cited original post,
    # while the Telegram footer identifies the already classified publisher.
    content = (
        '<p>小米新机开售。</p>'
        '<p><a href="https://weibo.com/1771925961/5346401887192763">小米公司</a></p>'
        '<p><a href="http://t.me/zaihuanews">科技圈</a> · '
        '<a href="https://t.me/zaihuachat">茶馆</a> · '
        '<a href="http://t.me/ZaiHuabot">投稿</a></p>'
        '<p>via <a href="https://t.me/zaihuapd/44001">在花科技圈 - Telegram Channel</a></p>'
    )
    monkeypatch.setattr(fetcher, 'DB_FILE', tmp_path / 'news.db')
    conn = fetcher.init_db()
    try:
        conn.executemany(
            "INSERT INTO source_categories (source, category, label, status) "
            "VALUES (?, 'Tech', ?, 'classified')",
            [('科技圈', '科技圈'), ('weibo.com', 'weibo.com')],
        )
        history = fetcher.classified_source_history(conn)
        assert fetcher.source_from_classified_history(content, history) == ('科技圈', '')
        fetcher.upsert_articles(conn, [{
            'id': 348108, 'source': 'weibo.com', 'group_source': 'weibo.com',
            'publisher_domain': 'weibo.com', 'source_html': content,
        }], sync_sources=False)
        assert tuple(conn.execute(
            'SELECT group_source, publisher_domain FROM articles WHERE id=348108'
        ).fetchone()) == ('科技圈', '')
    finally:
        conn.close()


def test_telegram_footer_without_via_uses_only_unambiguous_classified_channel():
    content = (
        '<p>正文</p><p><a href="https://weibo.com/user/post">原帖</a></p>'
        '<p><a href="https://t.me/tech_channel">科技圈</a> · '
        '<a href="https://t.me/chat_room">茶馆</a></p>'
    )
    history = ({'科技圈': '科技圈', '茶馆': '茶馆', 'weibo.com': 'weibo.com'},
               {'weibo.com': 'weibo.com'})
    assert fetcher.source_from_classified_history(content, history) is None
    assert fetcher.source_from_classified_history(
        content, ({'科技圈': '科技圈', 'weibo.com': 'weibo.com'},
                  {'weibo.com': 'weibo.com'})
    ) == ('科技圈', '')


def test_invalid_footer_link_is_skipped_without_aborting_batch(tmp_path, monkeypatch):
    malformed = '<p>正文</p><p>via <a href="https://[broken/path">坏链接</a></p>'
    valid = '<p>正文</p><p>via <a href="https://www.reuters.com/story">路透社</a></p>'
    assert fetcher.detect_group_source(malformed, "", "@feed") == ('@feed', '')
    assert fetcher.detect_group_source(valid, "", "@feed") == ('路透社', 'reuters.com')
    mixed = (
        '<p><a href="https://[broken/path">坏链接</a> '
        '<a href="https://www.reuters.com/story">有效来源</a></p>'
    )
    assert fetcher.detect_group_source(mixed, "", "@feed") == ('路透社', 'reuters.com')
    monkeypatch.setattr(fetcher, 'DB_FILE', tmp_path / 'news.db')
    conn = fetcher.init_db()
    try:
        fetcher.upsert_articles(conn, [
            {'id': 1, 'source': '@feed', 'source_html': malformed},
            {'id': 2, 'source': '路透社', 'source_html': valid},
        ], sync_sources=False)
        assert conn.execute('SELECT COUNT(*) FROM articles').fetchone()[0] == 2
    finally:
        conn.close()


def test_adjacent_image_anchor_is_not_a_source_link():
    content = (
        '<p>新闻正文</p><p><a href="https://www.ifeng.com/photo">'
        '<img src="cover.jpg"></a><a href="https://www.scmp.com/news">来源网站</a></p>'
    )
    assert fetcher.detect_group_source(content, "", "@feed") == ("南华早报", "scmp.com")


def test_classified_history_guides_new_articles_and_preserves_old_identity(tmp_path, monkeypatch):
    monkeypatch.setattr(fetcher, "DB_FILE", tmp_path / "news.db")
    conn = fetcher.init_db()
    try:
        conn.execute(
            "INSERT INTO source_categories (source, category, label, status) "
            "VALUES ('历史来源', 'News', '历史署名', 'manual')"
        )
        conn.execute(
            "INSERT INTO articles (id, title, source, group_source, origin_source, "
            "publisher_domain, timestamp) VALUES "
            "(1, '旧文章', '历史来源', '历史来源', '原始署名', 'example.org', 100)"
        )
        conn.commit()
        fetcher.upsert_articles(conn, [{
            "id": 2, "source": "错误来源", "group_source": "错误来源",
            "origin_source": "错误来源", "source_html": (
                '<p>正文</p><p><a href="https://other.test/image">'
                '<img src="photo.jpg"></a><a href="https://example.org/story">正文来源</a></p>'
            ),
        }], sync_sources=False)
        assert conn.execute("SELECT group_source FROM articles WHERE id=2").fetchone()[0] == "历史来源"
        fetcher.upsert_articles(conn, [{
            "id": 1, "source": "新来源", "group_source": "新来源",
            "origin_source": "新来源", "publisher_domain": "new.test",
        }], sync_sources=False)
        assert tuple(conn.execute(
            "SELECT source, group_source, origin_source, publisher_domain "
            "FROM articles WHERE id=1"
        ).fetchone()) == ("历史来源", "历史来源", "原始署名", "example.org")
    finally:
        conn.close()


def test_week_replay_reads_only_and_uses_older_training_articles(tmp_path, monkeypatch):
    from scripts.compare_source_week import compare

    db_path = tmp_path / "news.db"
    monkeypatch.setattr(fetcher, "DB_FILE", db_path)
    conn = fetcher.init_db()
    try:
        conn.execute(
            "INSERT INTO source_categories (source, category, label, status) "
            "VALUES ('历史来源', 'News', '历史来源', 'classified')"
        )
        conn.execute(
            "INSERT INTO articles (id, title, source, group_source, publisher_domain, "
            "body_html, timestamp) VALUES "
            "(1, '训练', '历史来源', '历史来源', 'example.org', '', 100), "
            "(2, '验证', '历史来源', '历史来源', '', "
            "'<p><a href=\"https://example.org/story\">来源</a></p>', 800000)"
        )
        conn.commit()
    finally:
        conn.close()
    report = compare(db_path)
    assert (report["total"], report["matched"], report["mismatched"]) == (1, 1, 0)


def test_normal_body_words_do_not_become_publishers():
    for body in (
        "<p>The official spoke as the vice chairman arrived.</p>",
        "<p>via and as part of the report.</p>",
        "<p>—— 常务副主席</p>",
    ):
        assert fetcher.detect_group_source(body, "https://telegra.ph/story", "@feed") == ("@feed", "")


def test_body_reference_is_not_treated_as_footer_source():
    body = (
        '<p>这是一段较长的新闻正文，讨论已有报道及其背景。' + '相关事实说明。' * 15
        + '<a href="https://www.ifeng.com/reference">参考报道</a></p>'
        '<p><a href="https://publisher.example.org/story">来源网站</a></p>'
    )
    assert fetcher.detect_group_source(body, "https://telegra.ph/story", "@feed") == (
        "example.org", "example.org"
    )


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
