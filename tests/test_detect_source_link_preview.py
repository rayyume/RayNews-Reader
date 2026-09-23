import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fetcher import detect_group_source


def test_detect_group_source_uses_ifeng_preview_url_hostname():
    """Detecting source must use a real preview URL without HTML wrapping."""
    assert detect_group_source("article body", preview_url="https://news.ifeng.com/a/20260801") == ("凤凰网", "ifeng.com")


def test_detect_group_source_normalizes_mixed_case_preview_hostname():
    """The hostname comparison must be case-insensitive."""
    assert detect_group_source("article body", preview_url="HTTPS://NEWS.IFENG.COM/a/20260801") == ("凤凰网", "ifeng.com")


def test_detect_group_source_ignores_preview_url_port_when_matching_domain():
    """A URL port must not become part of its domain match."""
    assert detect_group_source("article body", preview_url="https://news.ifeng.com:8443/a/20260801") == ("凤凰网", "ifeng.com")


def test_detect_group_source_rejects_non_http_preview_url():
    """Non-web preview URLs must not identify a known publisher."""
    assert detect_group_source("article body", preview_url="ftp://news.ifeng.com/archive", channel="@feed") == ("@feed", "")


def test_detect_group_source_does_not_parse_fake_href_inside_preview_url():
    """Only the preview URL hostname, not query text resembling HTML, may match."""
    preview_url = 'https://example.invalid/?redirect="><a href="https://news.ifeng.com">'
    assert detect_group_source("article body", preview_url=preview_url) == ("example.invalid", "example.invalid")


def _long_body(paragraphs: int = 20) -> str:
    """Realistic article length so the bottom-15% window holds several chunks."""
    return "<br>".join(f"正文段落第{i}段，讲一些实际内容。" for i in range(1, paragraphs + 1))


def test_known_publisher_wins_over_unknown_link_in_bottom_window():
    """An unknown trailing link must not displace a recognized publisher.

    Articles often end with a reference or syndication link after the
    attribution; picking whichever link comes first mislabels the publisher.
    """
    content = (
        f"{_long_body()}"
        '<br><a href="https://zaobao.com/a1">早报</a>'
        '<br><a href="https://harbor-news.cn/a2">转载</a>'
    )
    assert detect_group_source(content, channel="@feed") == ("联合早报", "zaobao.com")


def test_known_publisher_wins_when_later_in_bottom_window():
    """Order within the bottom window must not change the identified publisher."""
    content = (
        f"{_long_body()}"
        '<br><a href="https://harbor-news.cn/a2">转载</a>'
        '<br><a href="https://zaobao.com/a1">早报</a>'
    )
    assert detect_group_source(content, channel="@feed") == ("联合早报", "zaobao.com")


def test_known_publisher_wins_over_unknown_preview_url():
    """A syndication preview URL must not outrank a recognized bottom link."""
    content = f'{_long_body()}<br><a href="https://jiemian.com/a1">界面</a>'
    assert detect_group_source(content, preview_url="https://random-blog.com/pv", channel="@feed") == (
        "界面新闻",
        "jiemian.com",
    )


def test_unknown_publishers_still_fall_back_to_first_domain():
    """With no identifiable domain, keep the previous first-link behavior."""
    content = (
        f"{_long_body()}"
        '<br><a href="https://harbor-news.cn/a2">某站</a>'
        '<br><a href="https://site-b.cn/a3">另一站</a>'
    )
    detected = detect_group_source(content, channel="@feed")
    assert detected[0] == detected[1] != ""
    assert detected[1] in {"harbor-news.cn", "site-b.cn"}
