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
