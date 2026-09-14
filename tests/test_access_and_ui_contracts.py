import base64
import configparser
import json
import sqlite3
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from auth_validation import is_valid_email
import models
import web_server
from source_categories import (
    init_source_categories,
    promote_user_source_settings,
    source_rows,
)


@pytest.fixture
def ai_result_client(monkeypatch):
    captured = []
    active_settings = {
        "share_ai_results": 1,
        "share_suspended": 0,
        "share_last_check_ok": 1,
        "share_last_check_revision": 1,
        "share_current_config_revision": 1,
    }
    monkeypatch.setattr(
        models,
        "get_user",
        lambda user_id: {"id": user_id, "role": "user"},
    )
    monkeypatch.setattr(models, "record_access", lambda _user_id: None)
    monkeypatch.setattr(web_server, "get_user_settings", lambda _user_id: active_settings)
    monkeypatch.setattr(web_server, "_fetch_article_body", lambda _article_id: {"id": 1})
    monkeypatch.setattr(
        web_server,
        "get_ai_config",
        lambda _user_id: {"provider": "openai", "model": "test-model"},
    )
    monkeypatch.setattr(
        web_server,
        "_save_ai_result",
        lambda article_id, **kwargs: captured.append((article_id, kwargs)) or True,
    )
    token = web_server.create_token(1, "user")
    return web_server.app.test_client(), {"Authorization": f"Bearer {token}"}, captured


@pytest.mark.parametrize(
    "payload",
    [
        {"summary": {"unexpected": "object"}},
        {"translation": ["unexpected", "array"]},
    ],
)
def test_ai_result_type_validation_returns_400(ai_result_client, payload):
    client, headers, captured = ai_result_client

    response = client.post("/ai/result/1", headers=headers, json=payload)

    assert response.status_code == 400
    assert captured == []


def test_ai_result_size_validation_rejects_oversized_field(ai_result_client):
    client, headers, captured = ai_result_client

    response = client.post(
        "/ai/result/1",
        headers=headers,
        json={"summary": "x" * 200_001},
    )

    assert response.status_code == 400
    assert captured == []


def test_ai_result_body_limit_rejects_large_irrelevant_payload(ai_result_client):
    client, headers, captured = ai_result_client
    body = json.dumps({"irrelevant": "x" * (2 * 1024 * 1024)})

    response = client.post(
        "/ai/result/1",
        headers={**headers, "Content-Type": "application/json"},
        data=body,
    )

    assert response.status_code == 413
    assert captured == []


def test_ai_result_body_limit_rejects_route_specific_oversize(ai_result_client):
    client, headers, captured = ai_result_client
    body = json.dumps({"irrelevant": "x" * (1024 * 1024)})

    response = client.post(
        "/ai/result/1",
        headers={**headers, "Content-Type": "application/json"},
        data=body,
    )

    assert response.status_code == 413
    assert captured == []


def test_ai_result_body_accepts_normal_strings(ai_result_client):
    client, headers, captured = ai_result_client

    response = client.post(
        "/ai/result/1",
        headers=headers,
        json={"summary": "normal summary", "translation": "normal translation"},
    )

    assert response.status_code == 200
    assert captured[0][1]["summary"] == "normal summary"
    assert captured[0][1]["translation"] == "normal translation"


@pytest.fixture
def avatar_client(monkeypatch, tmp_path):
    updates = []
    monkeypatch.setattr(models, "get_user", lambda user_id: {"id": user_id, "role": "user"})
    monkeypatch.setattr(models, "record_access", lambda _user_id: None)
    monkeypatch.setattr(web_server, "AVATARS_DIR", str(tmp_path / "avatars"))
    monkeypatch.setattr(
        web_server,
        "update_user",
        lambda user_id, **kwargs: updates.append((user_id, kwargs)),
    )
    token = web_server.create_token(1, "user")
    return web_server.app.test_client(), {"Authorization": f"Bearer {token}"}, tmp_path, updates


@pytest.mark.parametrize(
    ("mime", "image_bytes", "extension"),
    [
        ("image/jpeg", b"\xff\xd8\xff\xe0avatar", "jpg"),
        ("image/png", b"\x89PNG\r\n\x1a\navatar", "png"),
        ("image/gif", b"GIF89aavatar", "gif"),
        ("image/webp", b"RIFF\x08\x00\x00\x00WEBPavatar", "webp"),
    ],
)
def test_avatar_upload_accepts_supported_image_content_and_saves_matching_extension(
    avatar_client, mime, image_bytes, extension
):
    client, headers, tmp_path, updates = avatar_client
    data_url = f"data:{mime};base64,{base64.b64encode(image_bytes).decode()}"

    response = client.put("/auth/me/avatar", headers=headers, json={"image": data_url})

    assert response.status_code == 200
    assert response.json["avatar_url"].startswith(f"/avatars/1.{extension}?v=")
    assert (tmp_path / "avatars" / f"1.{extension}").read_bytes() == image_bytes
    assert updates and updates[-1][1]["avatar_url"] == response.json["avatar_url"]


def test_avatar_upload_rejects_html_disguised_as_png(avatar_client):
    client, headers, tmp_path, _updates = avatar_client
    data_url = "data:image/png;base64," + base64.b64encode(b"<html>not an image</html>").decode()

    response = client.put("/auth/me/avatar", headers=headers, json={"image": data_url})

    assert response.status_code == 400
    assert response.json == {"error": "image content does not match declared type"}
    assert not (tmp_path / "avatars").exists()


def test_avatar_upload_rejects_png_declared_as_jpeg(avatar_client):
    client, headers, tmp_path, _updates = avatar_client
    png_bytes = b"\x89PNG\r\n\x1a\navatar"
    data_url = "data:image/jpeg;base64," + base64.b64encode(png_bytes).decode()

    response = client.put("/auth/me/avatar", headers=headers, json={"image": data_url})

    assert response.status_code == 400
    assert response.json == {"error": "image content does not match declared type"}
    assert not (tmp_path / "avatars").exists()


def test_avatar_upload_rejects_malformed_base64(avatar_client):
    client, headers, tmp_path, _updates = avatar_client

    response = client.put(
        "/auth/me/avatar",
        headers=headers,
        json={"image": "data:image/png;base64,not-valid-base64!"},
    )

    assert response.status_code == 400
    assert response.json == {"error": "invalid image data"}
    assert not (tmp_path / "avatars").exists()


def test_settings_response_exposes_safe_pending_revalidation_failure_fields_only():
    """Readers need the first-failure warning details, never the internal revision."""
    response = web_server._settings_response({
        "share_revalidation_failure_streak": 1,
        "share_revalidation_last_failure_at": "2026-07-30T10:00:00",
        "share_revalidation_last_failure_error": "AI API HTTP 503",
        "share_revalidation_failure_revision": 9,
        "share_current_config_revision": 9,
    })

    assert response["share_revalidation_failure_streak"] == 1
    assert response["share_revalidation_last_failure_at"] == "2026-07-30T10:00:00"
    assert response["share_revalidation_last_failure_error"] == "AI API HTTP 503"
    assert "share_revalidation_failure_revision" not in response

    defaults = web_server._settings_response(None)
    assert defaults["share_revalidation_failure_streak"] == 0
    assert defaults["share_revalidation_last_failure_at"] is None
    assert defaults["share_revalidation_last_failure_error"] is None


def test_settings_response_hides_pending_failure_from_a_stale_config_revision():
    response = web_server._settings_response({
        "share_revalidation_failure_streak": 1,
        "share_revalidation_last_failure_at": "2026-07-30T10:00:00",
        "share_revalidation_last_failure_error": "AI API HTTP 503",
        "share_revalidation_failure_revision": 8,
        "share_current_config_revision": 9,
    })

    assert response["share_revalidation_failure_streak"] == 0
    assert response["share_revalidation_last_failure_at"] is None
    assert response["share_revalidation_last_failure_error"] is None
    assert "share_revalidation_failure_revision" not in response
    assert "share_current_config_revision" not in response


def _source_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE articles (
            id INTEGER PRIMARY KEY,
            title TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL DEFAULT '',
            feed_source TEXT NOT NULL DEFAULT '',
            timestamp INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    init_source_categories(conn)
    return conn


def test_email_validation_rejects_non_email_values():
    for value in ("", "abc", "name@", "@example.com", "a..b@example.com", "a@invalid"):
        assert not is_valid_email(value), value
    assert is_valid_email("reader+news@example.com")


def test_nginx_proxies_article_delete_routes():
    config = (ROOT / "nginx.conf").read_text(encoding="utf-8")
    assert "location /articles" in config
    section = config.split("location /articles", 1)[1].split("}", 1)[0]
    assert "proxy_pass http://127.0.0.1:8082" in section


def test_nginx_proxies_notification_list_and_read_routes():
    config = (ROOT / "nginx.conf").read_text(encoding="utf-8")
    assert "location /notifications" in config
    section = config.split("location /notifications", 1)[1].split("}", 1)[0]
    assert "proxy_pass http://127.0.0.1:8082" in section


def test_container_supervises_services_without_blocking_on_initial_fetch():
    entrypoint = (ROOT / "entrypoint.sh").read_text(encoding="utf-8")
    assert "fetcher.py" not in entrypoint
    assert "python3 /app/refresh_server.py &" not in entrypoint
    assert "python3 /app/web_server.py &" not in entrypoint
    assert "nginx -g 'daemon off;'" not in entrypoint
    assert entrypoint.rstrip().endswith("exec supervisord -c /app/supervisord.conf")

    config = configparser.ConfigParser()
    config.read(ROOT / "supervisord.conf", encoding="utf-8")
    assert config["supervisord"]["nodaemon"] == "true"
    assert config["supervisord"]["pidfile"] == "/run/supervisord.pid"
    expected_commands = {
        "program:refresh": (
            "/bin/bash -o pipefail -c 'exec python3 -u /app/supervised_pipeline.py "
            "refresh -- python3 -u /app/refresh_server.py'"
        ),
        "program:web": (
            "/bin/bash -o pipefail -c 'exec python3 -u /app/supervised_pipeline.py "
            "web -- python3 -u /app/web_server.py'"
        ),
        "program:nginx": (
            "/bin/bash -o pipefail -c 'exec python3 -u /app/supervised_pipeline.py "
            "nginx -- nginx -g \"daemon off;\"'"
        ),
    }
    for section, command in expected_commands.items():
        program = config[section]
        assert program["command"] == command
        assert program["autorestart"] == "true"
        assert program["startsecs"] == "3"
        assert program["startretries"] == "5"
        assert program["stopasgroup"] == "true"
        assert program["killasgroup"] == "true"
        assert program["stdout_logfile"] == "/dev/fd/1"
        assert program["stdout_logfile_maxbytes"] == "0"
        assert program["stderr_logfile"] == "/dev/fd/2"
        assert program["stderr_logfile_maxbytes"] == "0"

    refresh = (ROOT / "refresh_server.py").read_text(encoding="utf-8")
    main = refresh[refresh.index('if __name__ == "__main__":'):]
    assert main.index('start_refresh_job("startup")') < main.index(
        "server.serve_forever()"
    )


def test_container_image_installs_and_copies_supervisor_configuration():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "apt-get install -y --no-install-recommends nginx supervisor util-linux" in dockerfile
    assert "COPY supervisord.conf /app/supervisord.conf" in dockerfile


def test_container_creates_unprivileged_python_service_account_without_dropping_entrypoint_privileges():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    normalized = " ".join(dockerfile.replace("\\\n", "").split())
    assert "groupadd --system raynews" in normalized
    assert (
        "useradd --system --gid raynews --create-home "
        "--home-dir /home/raynews --shell /usr/sbin/nologin raynews"
    ) in normalized
    assert "mkdir -p /app/data /var/log/nginx /run/nginx" in normalized
    assert "chown -R raynews:raynews /app/data" in normalized
    assert "USER raynews" not in dockerfile


def test_supervisor_only_drops_python_service_privileges():
    config = configparser.ConfigParser()
    config.read(ROOT / "supervisord.conf", encoding="utf-8")

    for section in ("program:refresh", "program:web"):
        assert config[section]["user"] == "raynews"
        assert config[section]["environment"] == 'HOME="/home/raynews",USER="raynews"'

    assert "user" not in config["program:nginx"]
    assert "environment" not in config["program:nginx"]


def test_entrypoint_fails_clearly_when_data_directory_cannot_be_prepared():
    entrypoint = (ROOT / "entrypoint.sh").read_text(encoding="utf-8")
    assert (
        "if ! run_setup_command install install -d -o raynews -g raynews /app/data; then"
        in entrypoint
    )
    assert (
        "if ! run_setup_command chown chown -R raynews:raynews /app/data; then"
        in entrypoint
    )
    assert "if ! test -w /app/data; then" not in entrypoint
    assert (
        "run_setup_command runuser /usr/sbin/runuser -u raynews -- /bin/sh"
        in entrypoint
    )
    assert ".raynews-write-probe-$$" not in entrypoint
    assert "/usr/bin/mktemp /app/data/.raynews-write-probe.XXXXXX" in entrypoint
    assert '/bin/rm -f -- "$probe"' in entrypoint
    assert "ERROR:" in entrypoint
    assert "exit 1" in entrypoint
    assert entrypoint.index("/usr/sbin/runuser") < entrypoint.index(
        "exec supervisord -c /app/supervisord.conf"
    )


def test_compose_healthcheck_covers_all_three_service_surfaces():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    healthcheck = compose.split("    healthcheck:", 1)[1].split(
        "    restart:", 1
    )[0]
    assert "- CMD" in healthcheck
    assert "- python3" in healthcheck
    assert "- -c" in healthcheck
    assert "http://127.0.0.1/health" in healthcheck
    assert "http://127.0.0.1:8082/auth/health" in healthcheck
    assert "http://127.0.0.1:8081/refresh/status" in healthcheck
    assert "interval: 30s" in healthcheck
    assert "timeout: 10s" in healthcheck
    assert "retries: 3" in healthcheck
    assert "start_period: 30s" in healthcheck
    assert '- "8090:80"' in compose


def test_nginx_sends_access_and_error_logs_to_container_streams():
    config = (ROOT / "nginx.conf").read_text(encoding="utf-8")
    server = config.split("server {", 1)[1]
    log_format = config.split("log_format raynews", 1)[1].split(";", 1)[0]
    assert "access_log /dev/stdout raynews;" in server
    assert "error_log /dev/stderr warn;" in server
    assert "$time_local" not in log_format
    assert "$time_iso8601" not in log_format


def test_admin_source_overrides_promote_to_shared_settings():
    conn = _source_db()
    conn.executemany(
        "INSERT INTO articles (id, title, source, feed_source, timestamp) "
        "VALUES (?, ?, ?, ?, ?)",
        [
            (1, "Main", "Original Feed", "Original Feed", 2),
            (2, "Alias", "Old Alias", "Old Alias", 1),
        ],
    )
    conn.execute(
        "INSERT OR IGNORE INTO source_categories "
        "(source, category, label, status, reason) "
        "VALUES ('Original Feed', 'Info', 'Original Feed', 'pending', 'test')"
    )
    conn.execute(
        "INSERT OR IGNORE INTO source_categories "
        "(source, category, label, status, reason) "
        "VALUES ('Old Alias', 'Info', 'Old Alias', 'pending', 'test')"
    )
    conn.execute(
        "INSERT INTO user_source_categories "
        "(user_id, source, category, label, status, updated_at) "
        "VALUES (1, 'Original Feed', 'Tech', 'Shared Tech', 'manual', datetime('now'))"
    )
    conn.execute(
        "INSERT INTO user_source_aliases "
        "(user_id, alias_source, target_source, created_at) "
        "VALUES (1, 'Old Alias', 'Original Feed', datetime('now'))"
    )
    conn.commit()

    promoted = promote_user_source_settings(conn, 1)

    rows = {row["source"]: row for row in source_rows(conn)}
    assert promoted == {"categories": 1, "aliases": 1}
    assert rows["Original Feed"]["category"] == "Tech"
    assert rows["Original Feed"]["label"] == "Shared Tech"
    assert conn.execute(
        "SELECT feed_source FROM articles WHERE id = 2"
    ).fetchone()[0] == "Original Feed"
    assert conn.execute(
        "SELECT COUNT(*) FROM user_source_categories WHERE user_id = 1"
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM user_source_aliases WHERE user_id = 1"
    ).fetchone()[0] == 0


def test_source_history_has_own_status_layer_and_theme_safe_article_actions():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    assert 'id="sourceArticlesStatus"' in html
    assert "function showSourceArticlesStatus" in html
    assert "background:#161a2a" not in html
    assert 'class="article-ai-btn ai-summarize-btn"' in html


def test_source_mutation_routes_are_admin_only_and_shared():
    server = (ROOT / "web_server.py").read_text(encoding="utf-8")
    for route in (
        '"/sources", methods=["PUT"]',
        '"/sources/classify", methods=["POST"]',
        '"/sources/classify-job", methods=["POST"]',
        '"/sources/redetect-single", methods=["POST"]',
    ):
        route_pos = server.index(route)
        decorator_block = server[route_pos:route_pos + 180]
        assert '@require_role("admin")' in decorator_block, route
    assert '"sources": source_rows(conn)' in server
    assert "update_source_category(\n            conn, source, category, label" in server
    assert "user_id=g.user_id" not in server[server.index("def save_source"):server.index("def classify_sources")]


def test_mobile_edge_swipe_claims_navigation_at_touch_start():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    swipe_marker = html.index("let sx = 0, sy = 0, swiping = false")
    swipe_start = html.rindex("(function() {", 0, swipe_marker)
    swipe_end = html.index("</script>", swipe_marker)
    swipe_block = html[swipe_start:swipe_end]

    assert "if (!usesMobileArticleNavigation()) return;" in swipe_block
    assert "edgeCandidate = sx < 50;" in swipe_block
    assert "if (edgeCandidate) e.preventDefault();" in swipe_block
    assert "overlay.addEventListener('touchcancel'" in swipe_block
    assert "closeArticle();" in swipe_block


def test_mobile_back_button_is_excluded_from_edge_swipe_and_handles_touch_directly():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    swipe_marker = html.index("let sx = 0, sy = 0, swiping = false")
    swipe_start = html.rindex("(function() {", 0, swipe_marker)
    swipe_end = html.index("</script>", swipe_marker)
    swipe_block = html[swipe_start:swipe_end]
    assert "if (e.target.closest('#backBtn')) return;" in swipe_block
    assert "backBtn.addEventListener('touchstart'" in html
    assert "backBtn.addEventListener('touchend'" in html
    assert "backBtn.addEventListener('touchcancel'" in html
    assert "articleBackTouchStart = null;" in html
    assert "e.stopPropagation();" in html


def test_article_back_does_not_replace_the_whole_list():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    close_start = html.index("function closeArticle(fromHistoryNavigation = false)")
    close_end = html.index("// Handle hash-based article links", close_start)
    close_block = html[close_start:close_end]
    assert "function reconcileVisibleArticles({ animate = false } = {})" in html
    assert "function flushPendingListUpdate()" not in html
    assert "flushPendingListUpdate();" not in close_block
    assert "renderList();" not in close_block


def test_blocking_list_loading_is_only_used_when_no_articles_are_available():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    load_start = html.index("async function loadNewsPage(")
    load_end = html.index("function renderFilters()", load_start)
    load_block = html[load_start:load_end]

    assert "if (!cacheApplied && !news.length) renderColdStartSkeleton();" in load_block
    assert "else if (!cacheApplied && !news.length)" in load_block
    assert "coldStartErrorHandler || renderColdStartError" in load_block
    assert "list.innerHTML =" not in load_block


def test_news_list_uses_paged_cache_first_loading_without_full_history_requests():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    assert "const INITIAL_LOAD_SIZE = 99999" not in html
    assert "size=99999" not in html
    assert "const PAGE_SIZE = 30;" in html
    assert "function openNewsCache()" in html
    assert "async function readCachedNewsPage" in html
    assert "async function writeCachedNewsPage" in html
    assert "async function loadNewsPage(" in html
    assert "await readCachedNewsPage" in html
    assert "pageRequestController.abort()" in html
    assert "if (data.error) throw new Error(data.error);" in html
    load_start = html.index("async function loadNewsPage(")
    load_end = html.index("function renderFilters()", load_start)
    assert "list.innerHTML =" not in html[load_start:load_end]


def test_idle_refresh_only_returns_to_latest_after_five_minutes_and_new_articles():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    assert "const IDLE_LATEST_DELAY_MS = 5 * 60 * 1000;" in html
    assert "function markUserActivity(event)" in html
    assert "function hasBlockingOverlayOpen()" in html
    assert "async function showLatestAfterIdle({" in html
    idle_start = html.index("async function showLatestAfterIdle({")
    idle_end = html.index("function scheduleAdjacentPagePrefetch", idle_start)
    idle_block = html[idle_start:idle_end]
    # Idle auto-apply is scoped to whichever category is currently active, not
    # hardcoded to "all" — it reads pendingRelevantCount(activeFilter) and never
    # reassigns `filter`.
    assert "const activeFilter = filter;" in idle_block
    assert "pendingRelevantCount(activeFilter)" in idle_block
    assert "hasBlockingOverlayOpen()" in idle_block
    # The auto-scroll belongs to the refresh flow too, so a cancelled/replaced
    # flow cannot keep moving the viewport after its guarded page apply stops.
    assert "const completed = await scrollPageToTop({" in idle_block
    assert "onNearTop: applyLatest," in idle_block
    assert "auto: true," in idle_block
    assert "externalSignal," in idle_block
    assert "applicationGuard," in idle_block
    assert "if (!mayApply() || !completed) return;" in idle_block
    assert "currentPage = 1;" in idle_block
    # Only the just-consumed category's pending items are dropped, not every
    # category's — an unseen article in another category must survive.
    assert "consumePendingNewArticles(activeFilter);" in idle_block
    assert "document.addEventListener('visibilitychange'" in html
    assert "loadDataWithRetry();" not in html


def test_service_worker_normalizes_api_cache_keys():
    sw = (ROOT / "frontend" / "sw.js").read_text(encoding="utf-8")
    assert "function normalizedApiRequest(request)" in sw
    assert "url.searchParams.delete('t');" in sw
    assert "cache.match(cacheRequest)" in sw
    # network.clone() is assigned to a local before cache.put() rather than
    # inlined, but it's still the normalized cacheRequest key that gets stored.
    assert "const cloned = network.clone();" in sw
    assert "cache.put(cacheRequest, cloned)" in sw


def test_search_uses_server_side_pagination_without_limiting_database_scope():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    assert "let searchPage = 1;" in html
    assert "let searchTotal = 0;" in html
    assert "async function fetchServerSearchResults(query, page = 1" in html
    assert "async function loadMoreSearchResults()" in html
    assert "params.set('q', query);" in html
    assert "params.set('page', String(page));" in html
    assert "params.set('size', String(PAGE_SIZE));" in html
    assert "加载更多" in html


def test_search_recovery_has_retry_lifecycle_and_request_local_timeout_contracts():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    assert "function cancelSearchRetry({ resetCount = true } = {})" in html
    assert "function scheduleSearchRetry(" in html
    assert "function retryServerSearch()" in html
    assert "const controller = new AbortController();" in html
    assert "setTimeout(() => controller.abort(), SEARCH_REQUEST_TIMEOUT_MS)" in html
    assert "if (searchRequestController === controller) searchRequestController = null;" in html
    assert 'onclick="retryServerSearch()"' in html


def test_search_recovery_status_is_announced_as_a_polite_atomic_live_region():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    start = html.index('<div class="search-status" id="searchStatus"')
    status_tag = html[start:html.index(">", start)]

    assert 'role="status"' in status_tag
    assert 'aria-live="polite"' in status_tag
    assert 'aria-atomic="true"' in status_tag


def test_paged_list_mutations_keep_server_total_and_new_article_prompt():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    delete_start = html.index("async function deleteArticlesByIds(ids)")
    delete_end = html.index("async function deleteSourceArticle", delete_start)
    delete_block = html[delete_start:delete_end]
    since_start = html.index("async function loadSince(")
    since_end = html.index("function renderFilters()", since_start)
    since_block = html[since_start:since_end]
    assert "currentTotal = Math.max(0, currentTotal - deleted);" in delete_block
    assert "document.getElementById('count').textContent = currentTotal + ' 条新闻';" in delete_block
    assert "showNewArticlesPrompt();" in since_block


def test_filter_switch_rolls_back_when_target_page_cannot_load():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    start = html.index("async function selectFilter(value)")
    end = html.index("function filteredNews()", start)
    block = html[start:end]
    assert "const previousFilter = filter;" in block
    assert "const previousPage = currentPage;" in block
    assert "const loaded = await loadNewsPage" in block
    assert "filter = previousFilter;" in block
    assert "currentPage = previousPage;" in block


def test_cached_page_background_calibration_reuses_existing_cards():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    apply_start = html.index("function applyNewsPage(")
    apply_end = html.index("async function fetchNewsPage", apply_start)
    apply_block = html[apply_start:apply_end]
    load_start = html.index("async function loadNewsPage(")
    load_end = html.index("// Full snapshot compatibility wrapper", load_start)
    load_block = html[load_start:load_end]
    assert "preserveDom = false" in apply_block
    assert "if (preserveDom" in apply_block
    assert "reconcileVisibleArticles({ animate });" in apply_block
    assert "preserveDom: cacheApplied" in load_block
    assert "const showPageProgress = !cacheApplied && userInitiated;" in load_block


def test_logo_is_lightweight_and_header_button_starts_refresh_job():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    assert 'class="logo" onclick="refreshHomepage()"' in html
    assert 'id="refreshBtn" onclick="triggerRefresh()"' in html
    logo = html[html.index("async function refreshHomepage()"):html.index("async function scrollToTopAndCheckLatest")]
    assert "loadSince(cursor, { forceApply: true })" in logo
    assert logo.count("loadSince(cursor, { forceApply: true })") == 1
    assert "preparePageNavigation(1, 'all')" in logo
    assert "scrollPageToTop({" in logo
    assert "requestRefreshOnce" not in logo
    assert "triggerRefresh(" not in logo


def test_view_bound_refresh_work_is_cancelled_by_navigation_and_backgrounding():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    for start, end in (
        ("async function restoreListStateFromUrl(", "function setPageLoading"),
        ("async function selectFilter(", "function filteredNews"),
        ("function openArticle(", "function fetchArticleDetail"),
    ):
        block = html[html.index(start):html.index(end, html.index(start))]
        assert "cancelViewBoundRefreshWork();" in block
    # goToPage() deliberately does NOT cancel an in-flight manual refresh — its DOM
    # application is already gated by viewIsCurrent()/flowIsCurrent() (see
    # triggerRefresh()), so navigating away should let the job keep running rather
    # than silently aborting it. See test_go_to_page_does_not_cancel_refresh_flow.
    go_to_page = html[html.index("async function goToPage("):html.index("function waitForScrollTop")]
    assert "cancelViewBoundRefreshWork();" not in go_to_page
    visibility = html[
        html.index("document.addEventListener('visibilitychange', () =>"):
        html.index("window.addEventListener('focus', onReturnToForeground)")
    ]
    assert "if (document.hidden) cancelViewBoundRefreshWork();" in visibility
    helper = html[
        html.index("function cancelViewBoundRefreshWork()"):
        html.index("function rebuildCategoryMap")
    ]
    assert "cancelRefreshFlow();" in helper
    assert "cancelStartupEmptyRevalidation();" in helper


def test_go_to_page_does_not_cancel_refresh_flow():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    go_to_page = html[html.index("async function goToPage("):html.index("function waitForScrollTop")]
    assert "cancelViewBoundRefreshWork();" not in go_to_page
    assert "cancelRefreshFlow();" not in go_to_page


def test_manual_refresh_posts_once_then_polls_status():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    block = html[html.index("async function triggerRefresh("):html.index("function setRefreshRunning", html.index("async function triggerRefresh("))]
    assert block.count("await requestRefreshOnce(flowController.signal)") == 1
    assert "await pollRefreshJob(data.job_id, 135000, flowController.signal, handleRefreshProgress)" in block
    assert "isTransientRefreshError" not in block
    assert "await delay(800)" not in block
    assert "activeFilter: filter," in block
    assert "navigationSequence: pageNavigationSequence," in block
    assert "requestSequence: pageRequestSequence," in block
    assert "pendingRequestSequence: pageRequestPendingSequence," in block
    assert "refreshView.pendingRequestSequence === 0" in block
    assert "&& !pageNavigationPending" in block
    assert block.count("loadNewsPage(1, {") == 1
    assert "refreshView.page === 1" in block
    assert "filter === refreshView.activeFilter" in block
    assert "!hasBlockingOverlayOpen()" in block
    assert "applicationGuard," in block


def test_list_request_pending_marker_is_owned_by_matching_request():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    block = html[html.index("async function loadNewsPage("):html.index("async function loadNewsPageRequest(")]
    assert "pageRequestPendingSequence = requestSeq;" in block
    assert "if (pageRequestPendingSequence === requestSeq)" in block
    assert "pageRequestPendingSequence = 0;" in block


def test_refresh_status_polling_uses_authenticated_get_and_bounded_wait():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    assert "async function requestRefreshStatusOnce(" in html
    assert "async function pollRefreshJob(" in html
    request = html[html.index("async function requestRefreshStatusOnce("):html.index("async function pollRefreshJob(")]
    poll = html[html.index("async function pollRefreshJob("):html.index("function rebuildCategoryMap")]
    assert "fetch('/auth/refresh/status?job_id=' + encodeURIComponent(jobId)" in request
    assert "'Authorization': 'Bearer ' + authToken" in request
    assert "cache: 'no-store'" in request
    assert "await abortableDelay(Math.min(delayMs, beforeDelayMs), flowSignal);" in poll
    # Each status request is capped at 5s (never the full remaining deadline).
    assert "status = await requestRefreshStatusOnce(jobId, Math.min(5000, remainingMs), flowSignal);" in poll
    assert "if (status.job_id !== jobId)" in poll
    assert "status.status === 'completed' || status.status === 'failed'" in poll
    assert "throw new Error('刷新状态查询超时，请稍后查看最新文章');" in poll
    assert "signal: controller.signal" in request
    assert "clearTimeout(timeout);" in request


def test_refresh_status_polling_tolerates_a_run_of_transient_failures():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    poll = html[html.index("async function pollRefreshJob("):html.index("function rebuildCategoryMap")]
    assert "MAX_CONSECUTIVE_TRANSIENT_FAILURES" in poll
    assert "isTransientRefreshError(error)" in poll
    assert "consecutiveTransientFailures = 0;" in poll
    assert "continue;" in poll
    assert "FIRST_POLL_DELAY_MS" in poll
    assert "POLL_INTERVAL_MS" in poll


def test_manual_refresh_feeds_new_articles_into_pending_prompt_when_not_on_page_one():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    trigger = html[html.index("async function triggerRefresh("):html.index("function setRefreshRunning")]
    new_count_check = trigger.index("Number(status.new_count || 0) > 0")
    load_since_call = trigger.index("await loadSince(latestKnownTimestamp || sinceCursor, {")
    assert load_since_call > new_count_check
    assert "externalSignal: flowController.signal" in trigger[load_since_call:]
    assert "applicationGuard: flowIsCurrent" in trigger[load_since_call:]
    assert "let page1BranchRan = false;" in trigger
    assert "page1BranchRan = true;" in trigger
    assert "!page1BranchRan" in trigger
    assert trigger.index("const sinceCursor =") < trigger.index("await requestRefreshOnce(")


def test_refresh_running_state_only_disables_refresh_button():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    block = html[html.index("function setRefreshRunning("):html.index("function buildNewsPageParams")]
    assert "btn.disabled = isRunning;" in block
    assert "document.querySelector('.logo')" not in block
    assert "scrollTopBtn" not in block
    assert "manual-refreshing" not in block


def test_manual_refresh_new_article_prompt_is_not_suppressed_while_running():
    # showNewArticlesPrompt() deliberately does NOT bail out on refreshInProgress —
    # triggerRefresh()'s immediate loadSince(..., { manual: true }) check (see
    # test_manual_refresh_checks_for_already_fetched_articles_immediately) can
    # surface a bubble well before the job itself finishes, and the button's
    # running state (potentially 10-30s) shouldn't suppress that for the whole
    # duration.
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    prompt = html[html.index("function showNewArticlesPrompt()"):html.index("function hideNewArticlesPrompt()")]
    assert "if (refreshInProgress) return;" not in prompt
    trigger = html[html.index("async function triggerRefresh("):html.index("function setRefreshRunning")]
    consume = "consumePendingNewArticles(refreshView.activeFilter);"
    assert consume in trigger
    assert trigger.index(consume) < trigger.index("setRefreshRunning(false);")
    assert trigger.index("setRefreshRunning(false);") < trigger.index("showNewArticlesPrompt();")


def test_manual_refresh_checks_for_already_fetched_articles_immediately():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    trigger = html[html.index("async function triggerRefresh("):html.index("function setRefreshRunning")]
    immediate_start = "const immediateCheck = loadSince(sinceCursor,"
    immediate_end = trigger.index("await requestRefreshOnce(")
    immediate_block = trigger[trigger.index(immediate_start):immediate_end]
    # The immediate database check starts before the manual job, and its accepted
    # article IDs feed the unified flow-scoped discovery Set rather than a separate
    # numeric counter. Keep this contract independent of formatting/layout.
    assert "manual: true" in immediate_block
    assert "onDiscovered: mergeDiscovered" in immediate_block
    assert "async function loadSince(" in html
    since_block = html[html.index("async function loadSince("):html.index("function rebuildSourceFilterGroups")]
    assert "if (!manual && (deferForRefresh || refreshInProgress)) return added;" in since_block


def test_manual_refresh_uses_structured_error_messages():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    trigger_start = html.index("async function triggerRefresh(")
    trigger_end = html.index("function setRefreshRunning(", trigger_start)
    trigger_block = html[trigger_start:trigger_end]

    assert "function refreshErrorMessage" in html
    assert "async function parseRefreshResponse" in html
    assert "async function requestRefreshOnce(signal)" in html
    assert "async function requestRefreshStatusOnce(" in html
    assert "async function pollRefreshJob(jobId" in html
    assert "await requestRefreshOnce(flowController.signal);" in trigger_block
    assert "await pollRefreshJob(data.job_id, 135000, flowController.signal, handleRefreshProgress);" in trigger_block
    assert "showToast('❌ 刷新失败: ' + (e.message || '网络错误'))" not in trigger_block


def test_manual_refresh_proxies_short_start_and_status_requests():
    source = (ROOT / "web_server.py").read_text(encoding="utf-8")
    assert '@app.route("/auth/refresh", methods=["POST"])' in source
    assert 'http_req.post("http://127.0.0.1:8081/refresh", timeout=5)' in source
    assert '@app.route("/auth/refresh/status", methods=["GET"])' in source
    assert 'http_req.get("http://127.0.0.1:8081/refresh/status", timeout=5)' in source
    assert "timeout=150" not in source[source.index("def protected_refresh"):source.index("# ─── Health", source.index("def protected_refresh"))]


def test_auth_proxy_has_explicit_short_timeouts():
    nginx = (ROOT / "nginx.conf").read_text(encoding="utf-8")
    block = nginx[nginx.index("location /auth/"):nginx.index("location ^~ /avatars/")]
    assert "proxy_connect_timeout 5s;" in block
    assert "proxy_send_timeout 30s;" in block
    assert "proxy_read_timeout 30s;" in block


def test_web_server_uses_per_thread_news_db_connection():
    # The real cross-request stall behind "反代错误" wasn't single-threaded serving
    # (threaded=True is already Flask's default, so it was never single-threaded) — it
    # was the process-wide shared news.db connection several request threads drove at
    # once, letting a slow /ai/ or admin write interleave with a /auth/refresh/status
    # read on the same connection/transaction. Each thread must get its own connection.
    source = (ROOT / "web_server.py").read_text(encoding="utf-8")
    assert "app.run(host=\"127.0.0.1\", port=port, debug=False, threaded=True)" in source
    # No process-wide shared connection object; connections are thread-local.
    assert "_news_conn_local = threading.local()" in source
    assert "_news_conn = None" not in source
    assert "conn = getattr(_news_conn_local, \"conn\", None)" in source
    assert "_news_conn_local.conn = conn" in source


def test_article_images_retry_with_cache_busting_when_mobile_runtime_loses_them():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")

    assert "function cacheBustedImageSrc(src, attempt)" in html
    assert "function recoverImageLoad(img)" in html
    assert "document.addEventListener('error', event => {" in html
    assert "event.target instanceof HTMLImageElement" in html
    assert "url.searchParams.set('img_retry'," in html
    assert "img.dataset.originalSrc" in html
    assert "img.dataset.imgFailed" in html
    assert "img.dataset.imgRetry" in html
    assert "function shouldRetryBrokenImage(img)" in html
    assert "if (!shouldRetryBrokenImage(img)) return;" in html
    assert "img.dataset.imgFailed !== '1'" in html
    assert "!img.complete" in html
    assert "document.addEventListener('visibilitychange', retryBrokenVisibleImages);" in html
    assert "window.addEventListener('online', retryBrokenVisibleImages);" in html


def test_pagination_uses_double_buffer_and_switches_during_scroll():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    start = html.index("async function goToPage(page)")
    end = html.index("function waitForScrollTop(", start)
    block = html[start:end]
    assert "const data = await preparePageNavigation(page, activeFilter);" in block
    assert block.index("const data = await preparePageNavigation") < block.index("scrollPageToTop(")
    assert "onNearTop:" in block
    assert "applyPageDuringScroll(data, page, activeFilter)" in block
    assert "setPageNavigationPending(true);" in block
    assert "setPageNavigationPending(false);" in block
    assert "async function preparePageNavigation(" in html


def test_adjacent_pages_are_buffered_immediately_and_cover_images_are_warmed():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    assert "const PAGE_MEMORY_BUFFER_LIMIT = 3;" in html
    assert "const PAGE_COVER_PRELOAD_LIMIT = 8;" in html
    assert "const pageMemoryBuffer = new Map();" in html
    assert "const pagePrefetchPromises = new Map();" in html
    assert "function rememberBufferedPage(" in html
    assert "function warmPageCoverImages(" in html
    prefetch_start = html.index("async function prefetchNewsPage(")
    prefetch_end = html.index("function cancelActiveAutoMotion()", prefetch_start)
    prefetch_block = html[prefetch_start:prefetch_end]
    assert "pagePrefetchPromises.get(key)" in prefetch_block
    assert "pagePrefetchPromises.set(key, task)" in prefetch_block
    assert "pagePrefetchPromises.delete(key)" in prefetch_block
    prepare_start = html.index("async function preparePageNavigation(")
    prepare_end = html.index("// Full snapshot compatibility wrapper", prepare_start)
    prepare_block = html[prepare_start:prepare_end]
    assert "const pendingPrefetch = pagePrefetchPromises.get(key);" in prepare_block
    assert "const prefetched = await pendingPrefetch;" in prepare_block


def test_must_be_fresh_navigation_has_a_downgrade_budget():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    prepare_start = html.index("async function preparePageNavigation(")
    prepare_end = html.index("// Full snapshot compatibility wrapper", prepare_start)
    prepare_block = html[prepare_start:prepare_end]
    assert "const NAVIGATION_FRESH_BUDGET_MS = 2500;" in prepare_block
    assert "await withPromiseTimeout(networkPromise, null, NAVIGATION_FRESH_BUDGET_MS);" in prepare_block
    assert "peekStaleBufferedPage(page, activeFilter)" in prepare_block
    assert "applyPageCalibrationWhenActive(result.data, page, activeFilter, requestSeq)" in prepare_block
    assert "function peekStaleBufferedPage(page, activeFilter)" in html
    start = html.index("function scheduleAdjacentPagePrefetch(")
    end = html.index("async function loadNewsPage(", start)
    block = html[start:end]
    assert "prefetchNewsPage(page - 1, activeFilter);" in block
    assert "prefetchNewsPage(page + 1, activeFilter);" in block
    assert "requestIdleCallback" not in block
    assert "setTimeout(run" not in block


def test_scroll_to_top_exposes_a_single_near_top_page_swap():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    start = html.index("function scrollPageToTop(")
    end = html.index("function stabilizePageTop()", start)
    block = html[start:end]
    assert "onNearTop" in block
    assert "progress >= 0.75" in block
    assert "remaining <= PAGE_SWITCH_TOP_THRESHOLD" in block
    assert "nearTopApplied" in block
    assert "applyNearTop();" in block
    assert "const duration = Math.min(640, Math.max(340" in block
    assert "motion.cancelled" in block


def test_page_swap_locks_list_height_and_disables_scroll_anchoring():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    assert ".list.page-transitioning{overflow-anchor:none" in html
    start = html.index("function applyPageDuringScroll(")
    end = html.index("async function goToPage(", start)
    block = html[start:end]
    assert "list.style.minHeight" in block
    assert "list.classList.add('page-transitioning')" in block
    assert "applyNewsPage(data, page, activeFilter" in block
    go_start = html.index("async function goToPage(", end)
    go_end = html.index("function waitForScrollTop(", go_start)
    assert "releasePageTransitionLock(transitionList);" in html[go_start:go_end]


def test_mobile_pull_to_refresh_is_not_registered():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    assert 'id="pullIndicator"' not in html
    assert "Mobile: pull-to-refresh on homepage" not in html
    assert "function resetPullIndicator()" not in html


def test_mobile_cold_start_resets_scroll_before_and_after_bootstrap():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    assert "function resetMobileColdStartScroll()" in html
    start = html.index("async function bootstrapNews(")
    end = html.index("// Initial load", start)
    block = html[start:end]
    assert "resetMobileColdStartScroll();" in block
    initial_start = html.index("// Initial load")
    initial_end = html.index("// Restore sidebar preference", initial_start)
    initial_block = html[initial_start:initial_end]
    assert initial_block.index("resetMobileColdStartScroll();") < initial_block.index("bootstrapNews();")
    assert "history.scrollRestoration = 'manual';" in html
    assert "pageshow" not in initial_block


def test_cold_start_renders_categories_immediately_and_runs_requests_in_parallel():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    boot = html[html.index("async function bootstrapNews("):html.index("// Initial load")]
    assert boot.index("renderTopCatBar();") < boot.index("loadSourceCategories(")
    assert "const sourcePromise = loadSourceCategories({" in boot
    assert "onMetadataReady: resolveSourceMetadata," in boot
    assert "const newsPromise = loadNewsPage(1, {" in boot
    assert "useCache: true," in boot
    assert "networkRetries: 1," in boot
    assert "await Promise.allSettled([sourcePromise, newsPromise, todayCountPromise]);" in boot
    assert "await loadSourceCategories();" not in boot


def test_cached_source_metadata_is_rendered_before_network_fetch():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    start = html.index("async function loadSourceCategories(")
    block = html[start:html.index("async function scheduleSourceMetadataRetry", start)]
    # The network fetch now lives in a helper; the cache must still be applied first.
    source_fetch = "await fetchSourceMetadata(networkTimeoutMs, externalSignal)"
    assert block.index("rebuildCategoryMap(cached.data.sources);") < block.index(source_fetch)
    assert block.index("renderFilters();") < block.index(source_fetch)


def test_cold_start_source_metadata_cache_read_is_not_capped_at_page_timeout():
    """The cold-start metadata cache read must use a larger budget than the 500ms
    used for page snapshots, or a slow first IndexedDB open leaves the drawer empty."""
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    start = html.index("async function loadSourceCategories(")
    block = html[start:html.index("async function scheduleSourceMetadataRetry", start)]
    assert "cacheTimeoutMs = 5000" in block
    assert "readNewsCacheEntry('source-metadata'), null, cacheTimeoutMs" in block


def test_source_metadata_retry_is_scheduled_on_failure():
    """A failed/aborted bootstrap fetch must queue a backoff retry so the drawer
    repopulates without the user opening the admin Sources tab."""
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    assert "if (!succeeded && mayApply()) {" in html
    assert "scheduleSourceMetadataRetry({ externalSignal, applicationGuard });" in html
    assert "async function scheduleSourceMetadataRetry(" in html
    # The admin Sources tab must also warm the cold-start cache.
    tab = html[html.index("async function loadSourcesTab("):
               html.index("async function refreshSourceDependentViews(")]
    assert "persistSourceMetadata(data);" in tab


def test_source_metadata_recovers_on_foreground_and_redrives_deep_link():
    """A PWA backgrounded through the whole retry window must self-heal on resume,
    and a successful retry must re-drive any stuck source deep link."""
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    # Foreground resume re-attempts while metadata never loaded.
    fg = html[html.index("function onReturnToForeground()"):
              html.index("document.addEventListener('visibilitychange', () =>")]
    assert "if (!sourceMetadataNetworkOk) scheduleSourceMetadataRetry({ immediate: true });" in fg
    # The retry loop bails (doesn't consume the slot) while hidden, and re-drives the
    # deep link on success.
    retry = html[html.index("async function scheduleSourceMetadataRetry("):
                 html.index("function sourceLabel")]
    assert "if (document.hidden) return;" in retry
    assert "retrySourceDeepLink();" in retry


def test_foreground_resume_uses_retrying_incremental_check():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    fg = html[html.index("function onReturnToForeground()"):
              html.index("document.addEventListener('visibilitychange', () =>")]
    assert "checkForNewArticlesAfterForegroundResume(cursor)" in fg
    assert "loadSince(cursor);" not in fg
    helper = html[
        html.index("async function checkForNewArticlesAfterForegroundResume("):
        html.index("let lastForegroundSyncAt = 0;")
    ]
    assert "result !== -1" in helper
    assert "document.hidden" in helper
    assert "latestKnownTimestamp || cursor" in helper


def test_cold_start_retry_uses_a_fresh_abort_controller_and_preserves_cache():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    load = html[html.index("async function loadNewsPage("):html.index("function applyPageCalibrationWhenActive")]
    assert "networkRetries = 0" in load
    assert "for (let attempt = 0; attempt <= networkRetries; attempt++)" in load
    assert "pageRequestController = new AbortController();" in load
    assert "if (cacheApplied)" in load
    assert "coldStartErrorHandler || renderColdStartError" in load


def test_desktop_scroll_to_top_uses_duration_controlled_animation():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    start = html.index("function scrollPageToTop(")
    end = html.index("async function showHomepageAfterScroll()", start)
    block = html[start:end]
    assert "requestAnimationFrame(step)" in block
    assert "Math.min(640, Math.max(340" in block
    assert "startY * 0.055" in block
    assert "prefers-reduced-motion" in block


def test_article_history_keeps_only_one_desktop_article_entry():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    start = html.index("function syncArticleHistory(id, date)")
    end = html.index("function openArticle(id)", start)
    block = html[start:end]
    assert "history.state && history.state.raynewsArticle" in block
    assert "const articleState = { raynewsArticle: true, articleId: Number(id) };" in block
    assert "history.replaceState(articleState, '', articleHash);" in block
    assert "history.pushState(articleState, '', articleHash);" in block


def test_list_motion_reuses_cards_and_animates_insertions():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    assert "function captureArticleRects(list)" in html
    assert "function animateArticleLayout(list, previousRects, newIds = new Set())" in html
    assert "cubic-bezier(.22,.61,.36,1)" in html
    assert "delay: Math.min(index, 6) * 40" in html
    assert "prefersReducedMotion()" in html
    assert "preserveDom: true" in html


def test_cold_start_bootstrap_has_loading_animation():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    boot_start = html.index("async function bootstrapNews(")
    boot_end = html.index("// Initial load", boot_start)
    boot_block = html[boot_start:boot_end]
    # Cold start always lands on page 1, even if the URL has ?page=N (see
    # commit 4490ff9 "reset page to 1 on browser refresh") — a stale bookmark
    # shouldn't drop the user onto a page whose content may have shifted.
    # restoreListStateFromUrl() (back/forward navigation) is unaffected.
    assert "currentPage = 1;" in boot_block
    assert "loadNewsPage(1, {" in boot_block
    assert "userInitiated: true," in boot_block
    assert "animate: true," in boot_block
    assert "useCache: true," in boot_block
    assert "networkRetries: 1," in boot_block
    assert "resetMobileColdStartScroll" in boot_block


def test_new_article_prompt_and_idle_motion_are_cancellable():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    assert 'id="newArticlesPrompt"' in html
    assert "function showNewArticlesPrompt()" in html
    assert "async function revealPendingLatest()" in html
    prompt_start = html.index("async function revealPendingLatest()")
    prompt_end = html.index("function scheduleAdjacentPagePrefetch", prompt_start)
    prompt_block = html[prompt_start:prompt_end]
    # revealPendingLatest is scoped to whatever category is active — it checks
    # pendingRelevantCount(activeFilter), not the raw global pendingNewArticleCount,
    # and reloads using that same activeFilter instead of forcing "all".
    assert "const activeFilter = filter;" in prompt_block
    assert "if (!pendingRelevantCount(activeFilter)) return;" in prompt_block
    assert "loadNewsPage(1, {" in prompt_block
    assert "forceNetwork: true" in prompt_block
    # Only the revealed category's pending items are consumed, not the whole queue.
    assert "consumePendingNewArticles(activeFilter);" in prompt_block
    assert "hideNewArticlesPrompt();" in prompt_block
    assert "function cancelActiveAutoMotion()" in html
    assert "if (!activeScrollMotion || !activeScrollMotion.auto) return;" in html
    assert "if (!mayApply() || !completed) return;" in html
    assert "const atLatestTop = currentPage === 1" in html


def test_list_filter_and_page_are_encoded_in_history_url():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    assert "function listUrlForState(activeFilter = filter, page = currentPage)" in html
    assert "url.searchParams.set('category'" in html
    assert "url.searchParams.set('source', group.label);" in html
    assert "url.searchParams.set('page'" in html
    assert "function restoreListStateFromUrl({ restoreScroll = false } = {})" in html
    assert "window.addEventListener('popstate'" in html
    assert "syncListUrl({ push: true });" in html


def test_article_return_has_one_scroll_owner_and_preserves_search_scroll():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    assert "function rememberArticleReturnState(id)" in html
    assert "cardTop: card ? card.getBoundingClientRect().top : null" in html
    assert "searchScrollTop: document.getElementById('searchBody').scrollTop" in html
    assert "function restoreArticleReturnState(onComplete)" in html
    restore_start = html.index("function restoreArticleReturnState(onComplete)")
    restore_end = html.index("function openArticle(id)", restore_start)
    restore_block = html[restore_start:restore_end]
    assert "window.scrollTo(0, state.scrollY);" in restore_block
    assert "requestAnimationFrame(() => {\n    requestAnimationFrame" not in restore_block
    assert "window.scrollBy" not in restore_block
    assert "restoreArticleReturnState(() => {" in html

    popstate_start = html.index("window.addEventListener('popstate'")
    popstate_end = html.index("window.addEventListener('keydown'", popstate_start)
    popstate_block = html[popstate_start:popstate_end]
    assert "document.getElementById('overlay').classList.contains('open')" in popstate_block
    assert "closeArticle(true);" in popstate_block
    assert "return;" in popstate_block
    assert popstate_block.index("return;") < popstate_block.index(
        "restoreListStateFromUrl({ restoreScroll: true });"
    )


def test_article_return_does_not_flush_or_animate_homepage():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    close_start = html.index("function finishArticleClose()")
    close_end = html.index("// Handle hash-based article links", close_start)
    close_block = html[close_start:close_end]

    assert "articleReturnInProgress = true;" in close_block
    assert "flushPendingListUpdate();" not in close_block
    assert "showNewArticlesPrompt();" in close_block
    assert "articleReturnInProgress = false;" in close_block


def test_mobile_article_overlay_does_not_lock_homepage_body():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    assert "function lockArticleBackground()" in html
    assert "function unlockArticleBackground()" in html
    assert "if (!usesMobileArticleNavigation()) lockBodyScroll();" in html
    assert "if (!usesMobileArticleNavigation()) unlockBodyScroll();" in html
    assert "overscroll-behavior-y:contain" in html


def test_search_login_context_and_result_progress_are_explicit():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    assert "showAuth('search');" in html
    assert "登录后才能搜索文章" in html
    assert "if (nextAction === 'search') openSearch();" in html
    assert "已显示 ${searchItems.length} / ${searchTotal} 条" in html


def test_mobile_sidebar_and_header_touch_targets_are_explicit():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    assert 'id="sidebarBackdrop"' in html
    assert "document.getElementById('sidebarBackdrop').classList.toggle('open', isOpen);" in html
    assert "window.matchMedia('(max-width: 768px)').matches" in html
    assert "@media(min-width:769px)" in html
    assert ".sidebar-backdrop{display:none}" in html
    assert ".header-right .icon-btn,.header-right .header-avatar{width:44px;height:44px" in html
    assert ".header-right .refresh-btn{padding:3px 8px;font-size:10px}" in html


def test_mobile_sidebar_keeps_open_when_expanding_source_categories():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    rebuild_start = html.index("function rebuildSourceFilterGroups()")
    rebuild_end = html.index("function toggleCategoryExpansion(cat)", rebuild_start)
    rebuild_block = html[rebuild_start:rebuild_end]
    render_start = html.index("function renderFilters()")
    render_end = html.index("async function selectFilter(value)", render_start)
    render_block = html[render_start:render_end]
    select_start = render_end
    select_end = html.index("function filteredNews()", select_start)
    select_block = html[select_start:select_end]

    assert "function toggleCategoryExpansion(cat)" in html
    assert "return groups;" in rebuild_block
    assert "const hasSourceButtons = body && body.querySelector('.fbtn');" in render_block
    assert "const isMobileSidebar = window.matchMedia && window.matchMedia('(max-width: 768px)').matches;" in render_block
    assert "if (hasSourceButtons && isMobileSidebar)" in render_block
    assert "toggleCategoryExpansion(cat);" in render_block
    assert render_block.index("toggleCategoryExpansion(cat);") < render_block.index("selectFilter('cat:' + cat);")
    assert "return;\n      }\n      if (filter === 'cat:' + cat)" in render_block
    assert "if (window.matchMedia && window.matchMedia('(max-width: 768px)').matches) closeSidebar();" in select_block


def test_source_filter_rendering_executes_group_rebuild_path():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not available for frontend runtime contract test")

    script = textwrap.dedent(
        r"""
        const fs = require('fs');
        const vm = require('vm');
        const html = fs.readFileSync('frontend/index.html', 'utf8');
        const start = html.indexOf('function rebuildSourceFilterGroups()');
        const end = html.indexOf('async function selectFilter(value)', start);
        const code = html.slice(start, end);
        const filters = { innerHTML: '', onclick: null };
        const context = {
          sourceFilterGroups: {},
          sourceToFilterGroup: {},
          sourceRows: [{
            source: '财经早餐',
            label: '财经早餐',
            category: 'Finance',
            sources: ['财经早餐'],
            raw_rows: [{ category: 'Finance' }]
          }],
          CATEGORY_ORDER: ['Finance'],
          filter: 'all',
          localStorage: { getItem(){ return null; }, setItem(){} },
          document: { getElementById(id){ return id === 'filters' ? filters : null; } },
          window: { matchMedia(){ return { matches: false }; } },
          sourceLabel(v){ return v; },
          sourceGroupKey(v){ return v; },
          chooseSourceGroupCategory(){ return 'Finance'; },
          sourceCategory(){ return 'Finance'; },
          categoryDisplayName(v){ return v; },
          esc(v){ return String(v); },
        };
        vm.createContext(context);
        vm.runInContext(code + '\nrenderFilters();', context);
        if (!filters.innerHTML.includes('财经早餐')) {
          throw new Error('renderFilters did not render grouped source button');
        }
        """
    )
    result = subprocess.run(
        [node, "-e", script],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_mobile_layout_and_article_content_cannot_create_horizontal_scroll():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    assert "html{font-size:16px;scroll-behavior:smooth;-webkit-font-smoothing:antialiased;max-width:100%;overflow-x:hidden}" in html
    assert ".app{width:100%;max-width:960px;" in html
    assert ".overlay{position:fixed;inset:0;z-index:200;background:var(--bg);overflow-x:hidden;overflow-y:auto;" in html
    assert ".article-wrap{width:100%;max-width:720px;min-width:0;" in html
    assert ".article-body{width:100%;min-width:0;max-width:100%;overflow-x:hidden;" in html
    assert ".article-body *{min-width:0;max-width:100%;overflow-wrap:anywhere;word-break:break-word}" in html
    assert ".article-body pre,.article-body code{white-space:pre-wrap;" in html
    assert ".article-body table{display:table;width:100%!important;max-width:100%!important;table-layout:fixed;" in html
    assert ".article-body iframe,.article-body embed,.article-body object,.article-body svg,.article-body canvas{max-width:100%!important}" in html
    assert ".search-body{flex:1;min-height:0;overflow-x:hidden;overflow-y:auto;" in html
    assert ".sidebar{position:fixed;left:0;top:calc(56px + var(--topcat-h));bottom:0;z-index:40;width:200px;" in html
    assert "overflow-x:hidden;overflow-y:auto;padding:16px 12px" in html


def test_mobile_header_is_fixed_and_content_respects_safe_area():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    assert ".header{position:fixed;top:0;left:0;right:0;" in html
    # The top category bar is also fixed, directly below the header (on both
    # desktop and PWA) — .app's padding-top reserves space for both the
    # header AND the bar via the shared --topcat-h custom property, so page
    # content never renders underneath either of them.
    assert "--topcat-h: 44px;" in html
    assert ".app{width:100%;max-width:960px;margin:0 auto;padding:calc(56px + var(--topcat-h) + env(safe-area-inset-top,0px))" in html
    assert ".topcat-bar{position:fixed;top:calc(56px + env(safe-area-inset-top,0px));left:0;right:0;" in html
    mobile_start = html.index("@media(max-width:768px)")
    mobile_end = html.index("@media(min-width:769px)", mobile_start)
    mobile_block = html[mobile_start:mobile_end]
    assert ".sidebar{top:calc(56px + var(--topcat-h) + env(safe-area-inset-top,0px));width:180px}" in mobile_block


def test_admin_user_tables_scroll_horizontally_instead_of_clipping_on_narrow_pwa():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    # .admin-body itself hides horizontal overflow (it's not the whole panel
    # that should scroll), so each wide table needs its own scrollable
    # wrapper with a sane min-width — otherwise on a narrow PWA viewport
    # (.ai-panel caps at max-width:94vw) the 5-column user table has no
    # escape valve and gets clipped/squeezed illegibly instead of scrolling.
    assert ".admin-body{overflow-x:hidden;" in html
    assert ".admin-table-wrap{overflow-x:auto;" in html
    assert ".admin-table{width:100%;min-width:440px;" in html
    render_start = html.index("function renderAdminUsers(data, invitationData)")
    render_end = html.index("async function revokePendingInvitation", render_start)
    render_block = html[render_start:render_end]
    assert render_block.count('<div class="admin-table-wrap"><table class="admin-table">') == 2


def test_admin_user_table_merges_role_and_time_columns_to_reduce_crowding():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    # Role badge + "modify role" dropdown used to be two separate columns showing the
    # same information twice, and registration-time + last-seen-time were two more —
    # together with the header text no longer fitting, the table forced a horizontal
    # scrollbar and its own header row wrapped inside a 580px-wide modal. Merged down
    # to 5 columns: 用户 / 角色 / 时间 / 访问 / (delete action).
    assert '<thead><tr><th>用户</th><th>角色</th><th>时间</th><th>访问</th><th></th></tr></thead>' in html
    th_rule = html[html.index(".admin-table th{"):html.index("}", html.index(".admin-table th{"))]
    assert "white-space:nowrap" in th_rule

    render_start = html.index("function renderAdminUsers(data, invitationData)")
    render_end = html.index("async function revokePendingInvitation", render_start)
    render_block = html[render_start:render_end]
    # Modifiable rows get the select (which now also carries the role color), rows
    # that can't be modified (the admin's own row) keep the read-only badge — never
    # both for the same row.
    assert "const roleCell = canModify" in render_block
    assert "admin-role-select admin-role-select-${roleClass}" in render_block
    assert '<span class="admin-role-badge ${roleClass}">${u.role}</span>' in render_block
    assert "注册 ${formatBeijingDateTime(u.created_at, false)}" in render_block
    assert "最近 ' + formatBeijingDateTime(u.last_seen_at)" in render_block


def test_refresh_button_progress_text_never_wraps_to_two_lines():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    # The pill's height is intrinsic to its content, so if the progress text ever
    # wrapped, it would stretch the pill vertically instead of just looking cramped.
    assert ".refresh-btn{" in html
    rule = html[html.index(".refresh-btn{"):html.index("}", html.index(".refresh-btn{"))]
    assert "white-space:nowrap" in rule
    # The visible label must stay short regardless of how large the count gets — the
    # full sentence is only allowed to live in the title attribute (hover/long-press).
    assert "label.textContent = count > 0 ? `+${count}` : '更新中';" in html
    assert "btn.title = count > 0 ? `已获取 ${count} 篇` : '';" in html


def test_admin_resource_usage_stats_grid_on_narrow_viewports():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    # 4 equal-weight stat boxes in a single flex row (the desktop .admin-stat
    # layout) don't fit under 640px — .admin-stat-detail's nowrap text forces each
    # box wider than min-content, overflowing .ai-body (overflow-x:hidden) instead
    # of shrinking. A 2x2 grid at that breakpoint keeps every stat visible without
    # hiding half of them behind horizontal scroll.
    mobile_start = html.index("@media(max-width:640px){\n  .header-right")
    mobile_end = html.index("</style>", mobile_start)
    mobile_block = html[mobile_start:mobile_end]
    assert ".admin-stat{display:grid;grid-template-columns:1fr 1fr" in mobile_block
    assert ".admin-stat-box{padding:" in mobile_block
    assert ".admin-stat-detail{white-space:normal" in mobile_block
    # Desktop layout (outside the media query) must be untouched.
    desktop_rule = html[html.index(".admin-stat{"):html.index("}", html.index(".admin-stat{"))]
    assert "display:flex" in desktop_rule


def test_article_back_button_does_not_pass_click_event_as_history_state():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    assert "function closeArticleFromButton()" in html
    assert "closeArticle();" in html
    assert "closeArticle(false, navigator.maxTouchPoints > 0);" not in html
    assert "addEventListener('click', closeArticleFromButton)" in html
    assert "addEventListener('click', closeArticle);" not in html


def test_purge_before_date_has_a_native_date_picker_alongside_the_text_field():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    actions_start = html.index('id="purgeBeforeDate"')
    actions_end = html.index("</div>", actions_start)
    actions_block = html[actions_start:actions_end]
    assert 'onclick="openPurgeDatePicker()"' in actions_block
    assert 'id="purgeDatePicker"' in actions_block
    assert 'type="date"' in actions_block
    assert 'onchange="applyPurgeDatePicker()"' in actions_block
    assert "function openPurgeDatePicker()" in html
    assert "function applyPurgeDatePicker()" in html
    # Picking a date must only fill the text field, not auto-run preview/delete —
    # the existing "preview then confirm" flow must still apply, and switching to a
    # different date must force a fresh preview before "确认删除" is usable again.
    apply_fn = html[html.index("function applyPurgeDatePicker()"):html.index("\n}\n", html.index("function applyPurgeDatePicker()"))]
    assert "previewPurge()" not in apply_fn
    assert "confirmPurge()" not in apply_fn
    assert "purgeConfirmBtn" in apply_fn
    assert ".disabled = true" in apply_fn
    # The picker's max must come from the server's own "today" (server_date, which
    # respects the process's TZ env var and matches what /admin/articles/purge itself
    # validates against) — not the browser's own UTC/local date, which can disagree
    # with the server's around midnight in either timezone.
    open_fn = html[html.index("function openPurgeDatePicker()"):html.index("\n}\n", html.index("function openPurgeDatePicker()"))]
    assert "serverTodayDate" in open_fn
    assert "toISOString" not in open_fn
    assert "data.server_date" in html


def test_legacy_admin_source_promotion_is_guarded_after_success():
    server = (ROOT / "web_server.py").read_text(encoding="utf-8")
    assert "_legacy_admin_source_settings_promoted = False" in server
    assert "global _legacy_admin_source_settings_promoted" in server
    assert "if _legacy_admin_source_settings_promoted:" in server


def test_sources_settings_tab_is_admin_only():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    # The 订阅源/摘要/翻译 tabs live inside the admin-only "管理员设置" overlay
    # (adminSettingsOverlay), not the regular user settings overlay, and the
    # whole overlay is gated on open/switch rather than per-tab.
    assert 'id="adminTabSources"' in html
    assert 'data-admin-tab="sources"' in html
    assert "function openAdminSettings() {\n  if (!authUser || authUser.role !== 'admin') return;" in html
    assert "function switchAdminTab(tab) {\n  if (!authUser || authUser.role !== 'admin') return;" in html
    assert "if (tab === 'sources') loadSourcesTab();" in html
    assert 'id="settingsTabSources"' not in html
    assert 'id="sourcesSettingsTab"' not in html


def test_source_delete_removes_the_group_even_when_it_has_no_articles():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    start = html.index("async function deleteSourceArticles(idx)")
    end = html.index("async function reinitializeSourceLabels()", start)
    block = html[start:end]

    assert "if (!count) return;" not in block
    assert "body: JSON.stringify({ sources })" in block
    assert "删除订阅源" in block
    assert "及其全部文章" in html


def test_header_avatar_search_and_summary_controls_are_aligned():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    assert ".header-right{display:flex;min-width:0;align-items:center;gap:10px;" in html
    assert ".daily-summary-btn{position:relative;width:32px;height:32px;" in html
    assert 'class="icon-btn daily-summary-btn"' in html
    assert ".header-right .header-avatar .user-avatar{width:30px!important;height:30px!important}" in html


def test_footer_supports_runtime_html_injection_before_fixed_suffix():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    entrypoint = (ROOT / "entrypoint.sh").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    readme_en = (ROOT / "README.en.md").read_text(encoding="utf-8")

    assert 'id="footer"' in html
    assert "<!-- {{CUSTOM_FOOTER_HTML_START}} -->" in html
    assert "<!-- {{CUSTOM_FOOTER_HTML_END}} -->" in html
    assert "· {{FULL_BUILD_VERSION}} ·" in html
    assert "https://github.com/rayyume/RayNews-Reader" in html
    assert 'os.environ.get("CUSTOM_FOOTER_HTML", "")' in entrypoint
    assert 'footer_start = "<!-- {{CUSTOM_FOOTER_HTML_START}} -->"' in entrypoint
    assert 'footer_end = "<!-- {{CUSTOM_FOOTER_HTML_END}} -->"' in entrypoint
    assert "html = before + custom_footer + after" in entrypoint
    assert "FOOTER_INJECT" not in entrypoint
    assert "`CUSTOM_FOOTER_HTML`" in readme
    assert "`CUSTOM_FOOTER_HTML`" in readme_en


def test_article_cover_body_and_translation_images_share_one_fullscreen_viewer():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    assert 'id="articleWrap"' in html
    assert 'id="lbStage"' in html
    assert "function openImageViewer(image)" in html
    assert "articleWrap.addEventListener('click'" in html
    assert html.count("img.addEventListener('click'") == 0
    assert ".lb-stage{" in html and "touch-action:none" in html
