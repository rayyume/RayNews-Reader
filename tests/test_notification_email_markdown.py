"""Safe rendering contracts for notification email bodies."""

import pytest

import notifier


def test_markdown_renderer_preserves_supported_layout_and_images(monkeypatch):
    monkeypatch.setenv("RAYNEWS_PUBLIC_URL", "https://news.example/")
    body = """#### 标题

**重点**

- 第一项
- 第二项

[链接](https://example.com/story)

```python
print("ok")
```

| 名称 | 数量 |
| --- | ---: |
| 新闻 | 2 |

![图](https://img.example/a.png)
"""

    rendered = notifier.render_notification_email_body(body, "markdown")

    assert "<h4>标题</h4>" in rendered
    assert "<strong>重点</strong>" in rendered
    assert "<ul>" in rendered and "<li>第一项</li>" in rendered
    assert 'href="https://example.com/story"' in rendered
    assert 'rel="noopener noreferrer"' in rendered
    assert 'target="_blank"' in rendered
    assert "<pre><code" in rendered and 'print("ok")' in rendered
    assert "<table>" in rendered and "<th>名称</th>" in rendered
    assert (
        'src="https://news.example/img-cache?url='
        'https%3A%2F%2Fimg.example%2Fa.png"'
    ) in rendered
    assert 'alt="图"' in rendered


def test_markdown_renderer_removes_dangerous_html_protocols_and_attributes(monkeypatch):
    monkeypatch.setenv("RAYNEWS_PUBLIC_URL", "https://news.example")
    body = """<script>alert(1)</script>
<style>body { display: none }</style>
<form action="https://evil.example"><input name="secret"></form>
<iframe src="https://evil.example"></iframe>
<p onclick="alert(2)">safe paragraph</p>
<a href="javascript:alert(3)" onmouseover="alert(4)">bad link</a>
<img src="javascript:alert(5)" onerror="alert(6)" alt="bad">
"""

    rendered = notifier.render_notification_email_body(body, "markdown")
    lowered = rendered.lower()

    for forbidden in (
        "<script",
        "alert(1)",
        "<style",
        "display: none",
        "<form",
        "<input",
        "<iframe",
        "onclick",
        "onmouseover",
        "onerror",
        "javascript:",
        "<img",
    ):
        assert forbidden not in lowered
    assert "<p>safe paragraph</p>" in rendered
    assert "bad link" in rendered


def test_markdown_renderer_removes_comments_containing_forbidden_markup(monkeypatch):
    monkeypatch.setenv("RAYNEWS_PUBLIC_URL", "https://news.example")

    rendered = notifier.render_notification_email_body(
        '<!-- <img src="https://img.example/raw.png" onerror="steal()"> -->',
        "markdown",
    )

    assert "<!--" not in rendered
    assert "img.example" not in rendered
    assert "onerror" not in rendered


def test_markdown_renderer_removes_mso_conditional_comments(monkeypatch):
    monkeypatch.setenv("RAYNEWS_PUBLIC_URL", "https://news.example")

    rendered = notifier.render_notification_email_body(
        '<!--[if mso]><img src="https://img.example/mso.png" '
        'onerror="steal()"><![endif]-->',
        "markdown",
    )

    assert "<!--" not in rendered
    assert "[if mso]" not in rendered.lower()
    assert "img.example" not in rendered
    assert "onerror" not in rendered


def test_markdown_renderer_removes_declarations_and_processing_instructions(
    monkeypatch,
):
    monkeypatch.setenv("RAYNEWS_PUBLIC_URL", "https://news.example")

    rendered = notifier.render_notification_email_body(
        "<!DOCTYPE html>\n<?raynews unsafe?>\n正文",
        "markdown",
    )

    assert "<!DOCTYPE" not in rendered.upper()
    assert "<?" not in rendered
    assert "unsafe" not in rendered
    assert "<p>正文</p>" in rendered


def test_markdown_renderer_drops_images_without_safe_absolute_public_url(monkeypatch):
    monkeypatch.delenv("RAYNEWS_PUBLIC_URL", raising=False)

    rendered = notifier.render_notification_email_body(
        "before ![private](https://img.example/a.png) after",
        "markdown",
    )

    assert "<img" not in rendered
    assert "img.example" not in rendered


@pytest.mark.parametrize(
    "public_url",
    [
        "https://news.example/?next=x",
        "https://news.example/#fragment",
        "https://news.example/raynews",
    ],
)
def test_markdown_renderer_rejects_ambiguous_public_base_urls(
    monkeypatch,
    public_url,
):
    monkeypatch.setenv("RAYNEWS_PUBLIC_URL", public_url)

    rendered = notifier.render_notification_email_body(
        "before ![image](https://img.example/a.png) after",
        "markdown",
    )

    assert "<img" not in rendered
    assert "img.example" not in rendered


def test_plain_renderer_escapes_html_and_converts_newlines():
    assert (
        notifier.render_notification_email_body("<b>x</b>\ny", "plain")
        == "&lt;b&gt;x&lt;/b&gt;<br>y"
    )


def test_unknown_format_remains_literal_plain_text():
    assert (
        notifier.render_notification_email_body("**not bold**", "unexpected")
        == "**not bold**"
    )


def test_daily_summary_send_sanitizes_markdown_html(monkeypatch):
    captured = {}

    def fake_send_email(api_key, to_email, subject, html_body, **kwargs):
        captured.update(
            api_key=api_key,
            to_email=to_email,
            subject=subject,
            html=html_body,
            kwargs=kwargs,
        )
        return {"id": "sent"}

    monkeypatch.setattr(notifier, "send_email", fake_send_email)
    result = notifier.send_daily_summary_email(
        "resend-key",
        "reader@example.com",
        """## 今日要闻

**重点**

- 第一项
- 第二项

<script>alert(1)</script>
<p onclick="alert(2)">安全段落</p>
[危险链接](javascript:alert(3))
""",
        {
            "total_articles": 4,
            "articles_after_dedup": 3,
            "articles_selected_for_ai": 2,
            "selected_articles_with_summary": 1,
        },
    )

    assert result == {"id": "sent"}
    rendered = captured["html"]
    lowered = rendered.lower()
    assert "<h2>今日要闻</h2>" in rendered
    assert "<strong>重点</strong>" in rendered
    assert "<ul>" in rendered and "<li>第一项</li>" in rendered
    assert "<script" not in lowered
    assert "alert(1)" not in lowered
    assert "onclick" not in lowered
    assert "javascript:" not in lowered
    assert "<p>安全段落</p>" in rendered


def test_daily_summary_email_keeps_two_digit_item_numbers_visible(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        notifier, "send_email",
        lambda _key, _to, _subject, body, **_kwargs: captured.setdefault("html", body),
    )
    summary = "## 科技动态\n" + "\n".join(
        f"{number}. **新闻 {number}：** 内容 [🔗](https://example.com/{number})"
        for number in range(1, 13)
    )

    notifier.send_daily_summary_email(
        "key", "reader@example.com", summary,
        {"articles_selected_for_ai": 120, "articles_selected_for_summary": 40,
         "digest_item_count": 12},
    )

    from bs4 import BeautifulSoup
    soup = BeautifulSoup(captured["html"], "html.parser")
    numbers = [row.find("td").get_text(strip=True)
               for row in soup.select(".summary table tr")]
    assert numbers == [f"{number}." for number in range(1, 13)]
    assert not soup.select(".summary ol")
    assert soup.select_one('.summary a[href="https://example.com/10"]')
    assert "入选 12 篇" in captured["html"]


def test_event_digest_email_uses_event_stats_instead_of_zero_defaults(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        notifier, "send_email",
        lambda _key, _to, _subject, body, **_kwargs: captured.setdefault("html", body),
    )
    notifier.send_daily_summary_email(
        "key", "reader@example.com", "## 新闻\n1. 一则新闻",
        {"total_articles": 817, "events": 600, "digest_item_count": 16,
         "selected_articles_with_summary": 9},
    )
    assert "817 篇原始 · 600 篇去重 · 入选 16 篇 · 入选中 9 篇已有摘要" in captured["html"]
