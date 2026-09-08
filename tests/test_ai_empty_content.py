"""AIService must turn a 200-but-empty completion (common on reasoning/thinking models
that exhaust max_tokens on hidden reasoning) into an actionable error, not a bare "" that
callers log as "empty" and retry forever."""

import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ai_service
import models
import network_safety
from ai_service import AIService


@pytest.fixture(autouse=True)
def _isolated_opencode_session_store(tmp_path, monkeypatch):
    """Session ids persist in app_state; point that DB at a throwaway file and
    reset the process-level cache so each test starts with a clean session."""
    monkeypatch.setattr(models, "DB_FILE", tmp_path / "opencode-sessions.db")
    monkeypatch.setattr(ai_service, "_opencode_session_cache", {})
    yield


class FakeResp:
    ok = True
    status_code = 200
    headers = {}

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def _svc(provider="openai"):
    return AIService("key", "https://opencode.ai/zen/go/v1", "m", provider_type=provider)


def _patch_post(monkeypatch, payload):
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None, **kwargs):
        captured["body"] = json
        captured.setdefault("calls", []).append(
            {"url": url, "headers": headers, "body": json}
        )
        return FakeResp(payload)

    monkeypatch.setattr(network_safety, "_send_bound_request", fake_post)
    return captured


def test_generic_openai_empty_content_does_not_name_an_unrelated_task_variable(monkeypatch):
    _patch_post(monkeypatch, {
        "choices": [{"finish_reason": "length",
                     "message": {"content": "", "reasoning_content": "lots of thinking..."}}],
    })
    with pytest.raises(RuntimeError) as ei:
        _svc().chat([{"role": "user", "content": "hi"}], max_tokens=500)
    msg = str(ei.value)
    assert "空内容" in msg
    assert "max_tokens=500" in msg          # names the exhausted budget
    assert "AI_TITLE_MAX_TOKENS" not in msg
    assert "AI_SOURCE_CLASSIFY_MAX_TOKENS" not in msg


def test_source_classification_uses_its_configurable_output_budget(monkeypatch):
    captured = _patch_post(monkeypatch, {
        "choices": [{"message": {"content": (
            '{"category":"News","label":"Test","confidence":0.9,"reason":"news"}'
        )}}],
    })

    _svc().classify_source("Test source")

    assert captured["body"]["max_tokens"] == ai_service.SOURCE_CLASSIFY_MAX_TOKENS
    assert captured["body"]["max_tokens"] >= 2048


def test_source_classification_empty_content_names_its_own_budget_variable(monkeypatch):
    _patch_post(monkeypatch, {
        "choices": [{
            "finish_reason": "length",
            "message": {"content": "", "reasoning_content": "thinking"},
        }],
    })

    with pytest.raises(RuntimeError) as ei:
        _svc().classify_source("Test source")

    msg = str(ei.value)
    assert "AI_SOURCE_CLASSIFY_MAX_TOKENS" in msg
    assert "AI_TITLE_MAX_TOKENS" not in msg


def test_title_empty_content_names_the_title_budget_variable(monkeypatch):
    _patch_post(monkeypatch, {
        "choices": [{
            "finish_reason": "length",
            "message": {"content": "", "reasoning_content": "thinking"},
        }],
    })

    with pytest.raises(RuntimeError) as ei:
        _svc().summarize_title("A long title")

    assert "AI_TITLE_MAX_TOKENS" in str(ei.value)


def test_openai_normal_content_is_returned(monkeypatch):
    _patch_post(monkeypatch, {"choices": [{"message": {"content": "  hello  "}}]})
    assert _svc().chat([{"role": "user", "content": "hi"}]) == "  hello  "


def test_deepseek_openai_request_forces_non_thinking(monkeypatch):
    captured = _patch_post(monkeypatch, {"choices": [{"message": {"content": "ok"}}]})
    AIService("key", "https://opencode.ai/zen/go/v1", "deepseek-v4-flash").chat(
        [{"role": "user", "content": "hi"}]
    )
    assert captured["body"]["thinking"] == {"type": "disabled"}


def test_non_deepseek_openai_request_does_not_send_thinking(monkeypatch):
    captured = _patch_post(monkeypatch, {"choices": [{"message": {"content": "ok"}}]})
    AIService("key", "https://api.openai.com/v1", "gpt-4o-mini").chat(
        [{"role": "user", "content": "hi"}]
    )
    assert "thinking" not in captured["body"]


def test_opencode_go_openai_requests_send_one_stable_session_per_service(monkeypatch):
    captured = _patch_post(monkeypatch, {"choices": [{"message": {"content": "ok"}}]})
    service = AIService("key", "https://opencode.ai/zen/go/v1", "deepseek-v4-flash")

    service.chat([{"role": "user", "content": "first"}])
    service.chat([{"role": "user", "content": "second"}])

    first_headers = captured["calls"][0]["headers"]
    second_headers = captured["calls"][1]["headers"]
    uuid.UUID(first_headers["x-opencode-session"])
    assert second_headers["x-opencode-session"] == first_headers["x-opencode-session"]
    assert first_headers["User-Agent"] == "RayNews-Reader/1.0"


def test_separate_opencode_go_services_use_separate_sessions(monkeypatch):
    captured = _patch_post(monkeypatch, {"choices": [{"message": {"content": "ok"}}]})

    for api_key in ("key-one", "key-two"):
        AIService(api_key, "https://opencode.ai/zen/go/v1", "deepseek-v4-flash").chat(
            [{"role": "user", "content": "hi"}]
        )

    session_ids = [call["headers"]["x-opencode-session"] for call in captured["calls"]]
    uuid.UUID(session_ids[0])
    assert session_ids[0] != session_ids[1]


def test_opencode_go_session_is_stable_across_service_instances(monkeypatch):
    """Instances are built per request/job, so a per-instance id would look like
    a brand-new client on every call. Same credentials must reuse one session."""
    captured = _patch_post(monkeypatch, {"choices": [{"message": {"content": "ok"}}]})

    for _ in range(2):
        AIService("key", "https://opencode.ai/zen/go/v1", "deepseek-v4-flash").chat(
            [{"role": "user", "content": "hi"}]
        )

    session_ids = [call["headers"]["x-opencode-session"] for call in captured["calls"]]
    uuid.UUID(session_ids[0])
    assert session_ids[0] == session_ids[1]


def test_opencode_go_session_survives_process_restart(monkeypatch):
    """The persisted id must outlive the in-process cache (a restart)."""
    _patch_post(monkeypatch, {"choices": [{"message": {"content": "ok"}}]})

    first = AIService("key", "https://opencode.ai/zen/go/v1", "deepseek-v4-flash")
    first_session = first._opencode_session()

    monkeypatch.setattr(ai_service, "_opencode_session_cache", {})

    restarted = AIService("key", "https://opencode.ai/zen/go/v1", "deepseek-v4-flash")
    assert restarted._opencode_session() == first_session


def test_opencode_go_claude_requests_send_session_header(monkeypatch):
    captured = _patch_post(monkeypatch, {
        "stop_reason": "end_turn",
        "content": [{"type": "text", "text": "ok"}],
    })

    _svc("claude").chat([{"role": "user", "content": "hi"}])

    headers = captured["calls"][0]["headers"]
    uuid.UUID(headers["x-opencode-session"])
    assert headers["User-Agent"] == "RayNews-Reader/1.0"


def test_non_opencode_provider_does_not_receive_opencode_session_header(monkeypatch):
    captured = _patch_post(monkeypatch, {"choices": [{"message": {"content": "ok"}}]})

    AIService("key", "https://api.openai.com/v1", "gpt-4o-mini").chat(
        [{"role": "user", "content": "hi"}]
    )

    assert "x-opencode-session" not in captured["calls"][0]["headers"]


def test_openai_null_content_does_not_crash_and_raises(monkeypatch):
    # content=None must not blow up later .strip() calls — it maps to the same error.
    _patch_post(monkeypatch, {"choices": [{"message": {"content": None}}]})
    with pytest.raises(RuntimeError):
        _svc().chat([{"role": "user", "content": "hi"}])


def test_claude_empty_text_raises(monkeypatch):
    _patch_post(monkeypatch, {"stop_reason": "max_tokens", "content": [{"type": "thinking", "thinking": "..."}]})
    with pytest.raises(RuntimeError) as ei:
        _svc("claude").chat([{"role": "user", "content": "hi"}], max_tokens=500)
    assert "空内容" in str(ei.value)


def test_claude_text_blocks_are_joined(monkeypatch):
    _patch_post(monkeypatch, {"content": [
        {"type": "thinking", "thinking": "ignore me"},
        {"type": "text", "text": "final answer"},
    ]})
    assert _svc("claude").chat([{"role": "user", "content": "hi"}]) == "final answer"


def test_title_calls_use_the_configurable_title_budget(monkeypatch):
    captured = _patch_post(monkeypatch, {"choices": [{"message": {"content": "短标题"}}]})
    _svc().summarize_title("Some very long original headline that must be shortened")
    assert captured["body"]["max_tokens"] == 4096
