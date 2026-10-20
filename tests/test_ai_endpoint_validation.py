"""Persistence boundary tests for server-side AI endpoint safety."""

import socket
import logging

import pytest
import requests

from ai_service import AIService
import models
import web_server


@pytest.fixture
def ai_config_env(tmp_path):
    old_db_file = models.DB_FILE
    models.close_db()
    models.DB_FILE = tmp_path / "ai-endpoint-validation.db"
    models.get_db()
    user = models.create_user("user@example.com", "pw", "user")
    admin = models.create_user("admin@example.com", "pw", "admin", role="admin")
    client = web_server.app.test_client()
    try:
        yield client, user["id"], admin["id"]
    finally:
        models.close_db()
        models.DB_FILE = old_db_file


def _headers(user_id, role):
    return {"Authorization": f"Bearer {web_server.create_token(user_id, role)}"}


def _public_dns(*args, **kwargs):
    return [
        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 443))
    ]




def test_system_config_rejects_private_endpoint_without_persisting(ai_config_env):
    client, _, admin_id = ai_config_env
    models.set_system_ai_config(endpoint="https://api.openai.com/v1", model="before")

    response = client.put(
        "/admin/system-ai-config",
        headers=_headers(admin_id, "admin"),
        json={"endpoint": "http://127.0.0.1:11434/v1", "model": "after"},
    )

    assert response.status_code == 400
    assert response.get_json() == {"error": "AI endpoint must be a public HTTP(S) URL"}
    persisted = models.get_system_ai_config()
    assert persisted["endpoint"] == "https://api.openai.com/v1"
    assert persisted["model"] == "before"




def test_system_config_accepts_public_hostname_endpoint(ai_config_env, monkeypatch):
    client, _, admin_id = ai_config_env
    monkeypatch.setattr(socket, "getaddrinfo", _public_dns)

    response = client.put(
        "/admin/system-ai-config",
        headers=_headers(admin_id, "admin"),
        json={"endpoint": "https://provider.example/v1", "model": "public-model"},
    )

    assert response.status_code == 200
    assert models.get_system_ai_config()["endpoint"] == "https://provider.example/v1"




@pytest.mark.parametrize("suffix", ["?api_key=query-secret", "#fragment-secret"])
def test_system_config_rejects_endpoint_suffix_without_persisting(
    ai_config_env, monkeypatch, suffix
):
    client, _, admin_id = ai_config_env
    monkeypatch.setattr(socket, "getaddrinfo", _public_dns)
    models.set_system_ai_config(endpoint="https://api.openai.com/v1", model="before")

    response = client.put(
        "/admin/system-ai-config",
        headers=_headers(admin_id, "admin"),
        json={"endpoint": f"https://provider.example/v1{suffix}", "model": "after"},
    )

    assert response.status_code == 400
    assert response.get_json() == {"error": "AI endpoint must be a public HTTP(S) URL"}
    persisted = models.get_system_ai_config()
    assert persisted["endpoint"] == "https://api.openai.com/v1"
    assert persisted["model"] == "before"
    assert "secret" not in response.get_data(as_text=True)


def test_legacy_endpoint_query_is_rejected_before_network(monkeypatch):
    network_calls = []
    monkeypatch.setattr(
        "ai_service.safe_post", lambda *args, **kwargs: network_calls.append((args, kwargs))
    )

    with pytest.raises(ValueError, match="AI endpoint"):
        AIService(
            "configured-secret",
            "https://provider.example/v1?api_key=query-secret",
            "test-model",
        )

    assert network_calls == []


def test_connection_test_log_redacts_request_url_secret(monkeypatch, caplog):
    query_secret = "connection-query-secret"

    class FailingService:
        def __init__(self, **kwargs):
            pass

        def test_connection(self):
            raise requests.ConnectionError(
                f"failed for https://provider.example/v1?tenant={query_secret}"
            )

    monkeypatch.setattr(web_server, "AIService", FailingService)
    with caplog.at_level(logging.ERROR):
        body, status = web_server._run_ai_connection_test(
            {
                "api_key": "configured-key",
                "endpoint": "https://provider.example/v1",
                "model": "model",
            }
        )

    assert status == 502
    assert body == {"error": "无法连接 AI 服务。请检查网络代理配置"}
    assert query_secret not in caplog.text
    assert "https://provider.example" not in caplog.text
    assert "ConnectionError" in caplog.text




def test_api_error_redacts_known_and_labeled_secrets():
    configured_secret = "random-configured-credential-7f3a9b"
    bearer_secret = "bearer-provider-credential"
    parameter_secret = "parameter-provider-credential"
    detail = (
        f"provider echoed {configured_secret}; "
        f"Authorization: Bearer {bearer_secret}; "
        f"api_key={parameter_secret}"
    )

    class ProviderResponse:
        status_code = 401
        reason = "Unauthorized"
        text = detail

        @staticmethod
        def json():
            return {"error": {"message": detail}}

    service = AIService(
        configured_secret,
        "https://provider.example/v1",
        "test-model",
    )
    formatted = service._format_api_error(ProviderResponse())

    assert "AI API HTTP 401" in formatted
    assert "[redacted]" in formatted
    for secret in (configured_secret, bearer_secret, parameter_secret):
        assert secret not in formatted
