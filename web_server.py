"""RayNews Web Server — auth, favorites, AI, settings via Flask."""

import base64
import os
import re
import sys
import json
import math
import shutil
import sqlite3
import threading
import time
import calendar
import datetime as _dt
import fcntl
import ipaddress
import uuid
import requests
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

from bs4 import BeautifulSoup
from flask import Flask, request, jsonify, g
from notifier import render_notification_email_body, send_email

# Ensure the project root is on the path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from models import (
    get_db, create_registered_user, get_user, get_user_by_email, get_user_by_username,
    update_user, set_user_role_and_rotate_token_version,
    delete_user, rotate_token_version, list_users, get_first_admin_email, count_users,
    count_active_users_since,
    prune_access_log,
    verify_password, password_within_bcrypt_limit,
    admit_login_attempt, reset_login_failures,
    admit_register_attempt, reset_register_attempts,
    claim_invite_request, complete_invite_request,
    add_favorite, remove_favorite, get_favorites, get_all_favorite_article_ids, is_favorited,
    count_article_favorites,
    get_ai_config, set_ai_config, get_user_settings, set_user_settings,
    set_user_settings_for_ai_config_revision,
    set_share_health,
    apply_share_connectivity_transition,
    record_share_revalidation_failure,
    get_users_with_share_enabled,
    get_daily_summary_inapp_user_ids,
    get_app_state, set_app_state, set_app_state_values, advance_app_state_epoch,
    claim_app_state_flag,
    claim_app_state_incident,
    complete_app_state_incident_if_stable,
    get_system_ai_config, set_system_ai_config,
    create_invitation_code,
    list_pending_invitations, delete_invitation_code, delete_invitation_code_by_code,
    add_notification, list_notifications,
    count_unread_notifications, mark_notification_read, mark_all_notifications_read,
    delete_notification, delete_all_notifications,
    publish_broadcast_atomically,
)
from auth import init_auth, create_token, require_auth, require_role
from auth_validation import is_valid_email
from image_validation import detect_image_content_type
from ai_service import AIService, _redact_api_error, validate_ai_endpoint_base_url
from digest_engine import (MAX_DIGEST_EVENTS, SIGNAL_VERSION, normalize_signals, group_events,
                           rank_events, render_digest)
from network_safety import UnsafeUrlError, assert_ai_endpoint_url, assert_public_http_url
from image_cache import (
    enqueue_article_image_prefetch, unpin_article_images,
    cache_stats, evict_article_images, evict_unreferenced_images, collect_image_urls, open_cache_connection, _url_hash,
)
from news_schema import ensure_article_schema, ensure_article_title_columns
from runtime_memory import runtime_memory_snapshot
from source_categories import (
    CATEGORY_NAMES, CATEGORY_ORDER, cleanup_stale_source_categories,
    category_definitions, delete_category_definition, save_category_definition,
    save_publisher_domain, UNCATEGORIZED, valid_category,
    clamp_weighted, ensure_article_source_columns, ensure_article_sources,
    delete_source_metadata, init_source_categories,
    find_merge_target, maintain_source_categories, merge_source,
    publisher_source_for_domain,
    promote_user_source_settings,
    recent_titles_for_source, source_aliases_for_target, source_rows,
    update_source_category, extract_domains_from_html,
)


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def _env_int(name: str, default: int, *, minimum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)).strip())
    except (AttributeError, TypeError, ValueError):
        value = default
    return max(minimum, value)


def _memory_monitor_config_from_env() -> dict[str, bool | int]:
    """Parse monitor settings without allowing invalid values or a hot loop."""
    return {
        "enabled": _env_bool("MEMORY_MONITOR_ENABLED", True),
        "interval_seconds": _env_int(
            "MEMORY_MONITOR_INTERVAL_SECONDS", 60, minimum=10
        ),
        "warn_mb": _env_int("MEMORY_WARN_MB", 768, minimum=1),
    }


_MEMORY_MONITOR_CONFIG = _memory_monitor_config_from_env()
MEMORY_MONITOR_ENABLED = bool(_MEMORY_MONITOR_CONFIG["enabled"])
MEMORY_MONITOR_INTERVAL_SECONDS = int(_MEMORY_MONITOR_CONFIG["interval_seconds"])
MEMORY_WARN_MB = int(_MEMORY_MONITOR_CONFIG["warn_mb"])
MEMORY_WARN_BYTES = MEMORY_WARN_MB * 1024 * 1024

AUTO_SUMMARY_BATCH_LIMIT = int(os.environ.get("AUTO_SUMMARY_BATCH_LIMIT", "20"))
AUTO_SUMMARY_INTERVAL_SECONDS = int(os.environ.get("AUTO_SUMMARY_INTERVAL_SECONDS", "30"))
AUTO_TRANSLATION_BATCH_LIMIT = int(os.environ.get("AUTO_TRANSLATION_BATCH_LIMIT", "5"))
AUTO_TRANSLATION_INTERVAL_SECONDS = int(os.environ.get("AUTO_TRANSLATION_INTERVAL_SECONDS", "30"))
AUTO_TRANSLATION_SCAN_LIMIT = int(os.environ.get("AUTO_TRANSLATION_SCAN_LIMIT", "1000"))
TRANSLATION_REPAIR_WINDOW_SECONDS = int(os.environ.get("TRANSLATION_REPAIR_WINDOW_SECONDS", str(6 * 3600)))
STALE_TRANSLATION_SCAN_HORIZON_DAYS = int(os.environ.get("STALE_TRANSLATION_SCAN_HORIZON_DAYS", "30"))
RECENT_FULLTEXT_UPGRADE_DAYS = int(os.environ.get("RECENT_FULLTEXT_UPGRADE_DAYS", "3"))
AUTO_TITLE_PROCESS_BATCH_LIMIT = int(os.environ.get("AUTO_TITLE_PROCESS_BATCH_LIMIT", "20"))
AUTO_TITLE_PROCESS_INTERVAL_SECONDS = int(os.environ.get("AUTO_TITLE_PROCESS_INTERVAL_SECONDS", "10"))
AUTO_TITLE_PROCESS_SCAN_LIMIT = int(os.environ.get("AUTO_TITLE_PROCESS_SCAN_LIMIT", "1000"))
AUTO_SOURCE_CLASSIFY_BATCH_LIMIT = int(os.environ.get("AUTO_SOURCE_CLASSIFY_BATCH_LIMIT", "50"))
AUTO_SOURCE_CLASSIFY_INTERVAL_SECONDS = int(os.environ.get("AUTO_SOURCE_CLASSIFY_INTERVAL_SECONDS", "60"))
def _share_revalidation_interval_seconds() -> int:
    """How often each sharing user's own AI key is re-tested.

    This loop is the only thing that notices a personal key going bad: the
    background AI jobs all run on the *system* config, and a deployment with
    those jobs on gives users almost no reason to make an on-demand call that
    would surface the failure themselves. So the interval is the detection
    delay for "共享 API 失效" — hourly rather than the original six hours, which
    could leave shared results served off a dead key most of a day.

    The probe is one max_tokens=50 ping per sharing user (see
    AIService.test_connection), spread 0.5s apart, so hourly stays cheap.
    Accepts fractions of an hour, and floors at five minutes so a typo like 0
    can't turn the loop into a hot loop against everyone's provider.
    """
    try:
        hours = float(os.environ.get("AI_SHARE_REVALIDATION_INTERVAL_HOURS", "1"))
    except (TypeError, ValueError):
        hours = 1.0
    return max(300, int(hours * 3600))


AI_SHARE_REVALIDATION_INTERVAL_SECONDS = _share_revalidation_interval_seconds()
TELEGRAM_EMBED_TIMEOUT_SECONDS = int(os.environ.get("TELEGRAM_EMBED_TIMEOUT_SECONDS", "12"))
# Daily summary is now server-generated once and broadcast to every subscribed
# user — the send time is a fixed ops-level setting, not user-configurable.
DAILY_SUMMARY_HOUR = int(os.environ.get("DAILY_SUMMARY_HOUR", "21"))
DAILY_SUMMARY_MINUTE = int(os.environ.get("DAILY_SUMMARY_MINUTE", "0"))
DAILY_SUMMARY_WINDOW_MINUTES = int(os.environ.get("DAILY_SUMMARY_WINDOW_MINUTES", "10"))
# A failed generation is retried on a fixed cadence outside the send window; after
# DAILY_SUMMARY_MAX_RETRIES further failures the scheduler stops trying for the day
# and alerts every admin (email + in-app) with the reason.
DAILY_SUMMARY_RETRY_INTERVAL_SECONDS = int(os.environ.get("DAILY_SUMMARY_RETRY_INTERVAL_SECONDS", "600"))
DAILY_SUMMARY_MAX_RETRIES = int(os.environ.get("DAILY_SUMMARY_MAX_RETRIES", "3"))
TITLE_SUMMARY_MAX_CHARS = int(os.environ.get("TITLE_SUMMARY_MAX_CHARS", "30"))
TITLE_SUMMARY_MAX_WEIGHT = TITLE_SUMMARY_MAX_CHARS * 2
# The list UI already CSS-clamps titles to 2 lines with an ellipsis
# (frontend .item-title: -webkit-line-clamp:2), so there's no display reason
# to hard-reject a summary just for running over TITLE_SUMMARY_MAX_CHARS —
# doing so only forced AI/regex gymnastics (dropped details, brittle
# punctuation-splitting fallbacks) to hit an arbitrary target. This backstop
# instead only catches genuinely broken output (e.g. the AI ignoring the
# "shorten" instruction and returning most of the article).
TITLE_SUMMARY_MAX_WEIGHT_HARD = TITLE_SUMMARY_MAX_WEIGHT * 3
# Aspirational lower bound communicated to the LLM in the shortening prompt.
TITLE_SUMMARY_PROMPT_MIN_CHARS = int(os.environ.get("TITLE_SUMMARY_PROMPT_MIN_CHARS", "18"))
# Hard floor enforced by the validator. Kept well below the prompt's target so
# legitimately short titles aren't rejected; the real defense against
# attribution-only junk (e.g. "据FT报道") is _shares_title_signal below.
TITLE_SUMMARY_MIN_CHARS = int(os.environ.get("TITLE_SUMMARY_MIN_CHARS", "6"))
TITLE_SUMMARY_MIN_WEIGHT = TITLE_SUMMARY_MIN_CHARS * 2
TITLE_SUMMARY_MAX_TOTAL_CHARS = int(os.environ.get("TITLE_SUMMARY_MAX_TOTAL_CHARS", "40"))
# Only *trigger* the extra AI shortening pass once a title runs meaningfully
# past the target length — not the moment it crosses it by a character or two.
# The target (TITLE_SUMMARY_MAX_CHARS) is still what the prompt asks the model
# to aim for; this ratio just avoids spending an AI call to trim a 31-char
# title down to 30 (the list UI CSS-clamps to 2 lines anyway).
TITLE_SUMMARY_TRIGGER_RATIO = float(os.environ.get("TITLE_SUMMARY_TRIGGER_RATIO", "1.3"))
TITLE_SUMMARY_TRIGGER_CJK = int(TITLE_SUMMARY_MAX_CHARS * TITLE_SUMMARY_TRIGGER_RATIO)
TITLE_SUMMARY_TRIGGER_TOTAL = int(TITLE_SUMMARY_MAX_TOTAL_CHARS * TITLE_SUMMARY_TRIGGER_RATIO)
# When a title is BOTH foreign and over-long, translate+shorten it in a single
# AI call rather than translate → notice it's still long → summarize (two calls,
# title visibly changes twice). Set to "0" to fall back to the two-step path.
TITLE_MERGE_TRANSLATE_CONDENSE = os.environ.get("TITLE_MERGE_TRANSLATE_CONDENSE", "1") == "1"

# ─── App Setup ────────────────────────────────────────────────

MAX_REQUEST_BODY_BYTES = 2 * 1024 * 1024
AI_RESULT_MAX_BODY_BYTES = 1024 * 1024
AI_RESULT_MAX_FIELD_CHARS = 200_000

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_REQUEST_BODY_BYTES


# ─── JSON error handler — prevent HTML responses on errors ─────
@app.errorhandler(Exception)
def handle_exception(e):
    """Return JSON for all unhandled exceptions instead of HTML."""
    from flask import jsonify as _jsonify
    from werkzeug.exceptions import HTTPException
    if isinstance(e, HTTPException):
        return _jsonify({"error": e.description}), e.code or 500
    app.logger.exception(
        "Unhandled exception during %s %s",
        request.method,
        request.path,
    )
    return _jsonify({"error": "internal server error"}), 500

DATA_DIR = os.environ.get("DATA_DIR", "/app/data")

# Secret key: from env or persisted under DATA_DIR.
SECRET_KEY = os.environ.get("RAYNEWS_SECRET")
if not SECRET_KEY:
    from pathlib import Path
    import secrets
    secret_file = Path(DATA_DIR) / "raynews_secret"
    try:
        secret_file.parent.mkdir(parents=True, exist_ok=True)
        if secret_file.exists():
            SECRET_KEY = secret_file.read_text(encoding="utf-8").strip()
        if not SECRET_KEY:
            SECRET_KEY = secrets.token_hex(32)
            secret_file.write_text(SECRET_KEY, encoding="utf-8")
        print(f"[web] Using persisted RAYNEWS_SECRET from {secret_file}")
    except Exception as e:
        SECRET_KEY = secrets.token_hex(32)
        print(f"[web] Could not persist RAYNEWS_SECRET ({e}); using ephemeral key: {SECRET_KEY[:16]}...")

init_auth(SECRET_KEY)


def _admin_email_address() -> str:
    return (os.environ.get("RAYNEWS_ADMIN_EMAIL") or get_first_admin_email() or "").strip()


def _trusted_proxy_networks():
    """Return configured direct-peer networks allowed to supply X-Real-IP."""
    raw = os.environ.get("TRUSTED_PROXY_PREFIXES") or "127.0.0.1/32,::1/128"
    networks = []
    for prefix in raw.split(","):
        try:
            networks.append(ipaddress.ip_network(prefix.strip(), strict=False))
        except ValueError:
            continue
    return networks


def _trusted_client_ip() -> str:
    """Use X-Real-IP only when the direct peer is a trusted proxy."""
    remote = (request.remote_addr or "").strip()
    try:
        remote_ip = ipaddress.ip_address(remote)
    except ValueError:
        remote_ip = None

    if remote_ip is not None and any(
        remote_ip in network for network in _trusted_proxy_networks()
    ):
        real_ip = (request.headers.get("X-Real-IP") or "").strip()
        try:
            return str(ipaddress.ip_address(real_ip))
        except ValueError:
            pass
    return str(remote_ip) if remote_ip is not None else (remote or "unknown")


def _rate_limited_response(retry_after: int):
    retry_after = max(1, int(retry_after))
    response = jsonify({
        "error": "too many requests; please retry later",
        "retry_after": retry_after,
    })
    response.status_code = 429
    response.headers["Retry-After"] = str(retry_after)
    return response


def _send_registration_notice(user: dict) -> bool:
    """Notify the admin after a user successfully registers. Never blocks registration."""
    api_key = os.environ.get("RESEND_API_KEY", "")
    admin_email = _admin_email_address()
    if not api_key or not admin_email:
        return False
    from_email = os.environ.get("RAYNEWS_FROM_EMAIL") or "onboarding@resend.dev"
    nickname = user.get("nickname") or "未设置"
    try:
        from notifier import send_email
        html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#0a0a0c;color:#e8e8ed;padding:20px;max-width:560px;margin:0 auto}}
h1{{color:#6e8efb;font-size:18px}}
.box{{background:#111114;border:1px solid rgba(255,255,255,.08);border-radius:12px;padding:16px;margin:16px 0}}
.row{{margin:8px 0;color:#c9c9d4}}
.label{{display:inline-block;width:80px;color:#8b8b9e}}
.footer{{font-size:12px;color:#55556a;margin-top:20px}}
</style></head><body>
<h1>RayNews 新用户已注册</h1>
<div class="box">
  <div class="row"><span class="label">邮箱</span>{user.get("email", "")}</div>
  <div class="row"><span class="label">昵称</span>{nickname}</div>
  <div class="row"><span class="label">角色</span>{user.get("role", "")}</div>
  <div class="row"><span class="label">时间</span>{user.get("created_at", "")}</div>
</div>
<p class="footer">此邮件仅用于管理员获知注册结果，不包含密码、验证码或令牌。</p>
</body></html>"""
        send_email(
            api_key,
            admin_email,
            f"RayNews 新用户注册 — {user.get('email', '')}",
            html,
            from_name="RayNews",
            from_email=from_email,
        )
        return True
    except Exception as exc:
        print(f"[web] Failed to send registration notice: {exc}")
        return False


# ─── Auth Routes ──────────────────────────────────────────────

@app.route("/auth/register", methods=["POST"])
def register():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    nickname = (data.get("nickname") or "").strip()
    invite_code = (data.get("invite_code") or "").strip().upper()

    if not email or not password or not nickname:
        return jsonify({"error": "email, username and password required"}), 400
    if not is_valid_email(email):
        return jsonify({"error": "invalid email format"}), 400
    if len(password) < 6:
        return jsonify({"error": "password must be at least 6 characters"}), 400
    if not password_within_bcrypt_limit(password):
        return jsonify({
            "error": "password must be at most 72 UTF-8 bytes",
        }), 400

    client_ip = _trusted_client_ip()
    admitted, retry_after = admit_register_attempt(client_ip)
    if not admitted:
        return _rate_limited_response(retry_after)

    user, is_initial_admin = create_registered_user(
        email,
        password,
        nickname,
        invite_code,
    )
    if user is None:
        if nickname and get_user_by_username(nickname):
            return jsonify({"error": "username already taken"}), 409
        if get_user_by_email(email):
            return jsonify({"error": "email already registered"}), 409
        if not invite_code:
            return jsonify({"error": "invitation code required. Go to Settings → Request Invite"}), 400
        return jsonify({"error": "invalid or expired invitation code"}), 400


    reset_register_attempts(client_ip)

    admin_notified = _send_registration_notice(user) if not is_initial_admin else False

    token = create_token(user["id"], user["role"], user["token_version"])
    return jsonify({"token": token, "user": user, "admin_notified": admin_notified}), 201


@app.route("/auth/request-invite", methods=["POST"])
def request_invite():
    """Generate an 8-char invitation code and email it to the admin."""
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    if not email:
        return jsonify({"error": "email required"}), 400
    if not is_valid_email(email):
        return jsonify({"error": "invalid email format"}), 400

    # Check if already registered
    if get_user_by_email(email):
        return jsonify({"error": "email already registered"}), 409

    # Verify the email service is actually usable *before* persisting a code,
    # so a config problem never leaves a valid-but-undelivered invitation
    # sitting in the pending-review list.
    api_key = os.environ.get("RESEND_API_KEY", "")
    if not api_key:
        return jsonify({"error": "email service not configured. Contact admin."}), 500
    admin_email = (os.environ.get("RAYNEWS_ADMIN_EMAIL") or get_first_admin_email() or "").strip()
    if not admin_email:
        return jsonify({"error": "admin email is not configured."}), 500
    from_email = os.environ.get("RAYNEWS_FROM_EMAIL") or "onboarding@resend.dev"

    reservation_token, retry_after = claim_invite_request(email)
    if reservation_token is None:
        return _rate_limited_response(retry_after)

    from notifier import EmailDeliveryRejected
    code = None
    try:
        code = create_invitation_code(email)
        from notifier import send_email
        html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>
body{{font-family:-apple-system,sans-serif;background:#0a0a0c;color:#e8e8ed;padding:20px;max-width:500px;margin:0 auto}}
h1{{color:#6e8efb;font-size:18px}}
.code{{font-size:32px;font-weight:800;letter-spacing:6px;text-align:center;padding:20px;background:#1a1a1f;border-radius:12px;margin:20px 0;color:#6e8efb}}
.footer{{font-size:12px;color:#55556a;margin-top:20px}}
</style></head><body>
<h1>🔑 新用户邀请请求</h1>
<p>邮箱：<strong>{email}</strong></p>
<p>邀请码：</p>
<div class="code">{code}</div>
<p>将此邀请码告知用户，用户在注册时输入即可完成注册。</p>
<p class="footer">RayNews · 邀请码在注册后自动失效</p>
</body></html>"""
        send_email(api_key, admin_email,
                   f"RayNews 新用户邀请 — {email}",
                   html, from_name="RayNews", from_email=from_email,
                   idempotency_key=reservation_token)
    except EmailDeliveryRejected as e:
        print(f"[web] Failed to send invite email: {e}")
        if code is not None:
            delete_invitation_code_by_code(code)
        complete_invite_request(
            email,
            reservation_token,
            succeeded=False,
        )
        return jsonify({"error": "邮件发送失败，请稍后重试"}), 500
    except Exception as e:
        # A timeout, disconnect, malformed success response, or unexpected
        # transport failure may happen after Resend accepted the message.
        # Preserve both the code and the one-minute reservation. An immediate
        # retry is therefore rate-limited; a later application gets a new
        # idempotency key and atomically invalidates this code.
        print(f"[web] Invite email delivery status is uncertain: {e}")
        return jsonify({
            "error": "邮件发送状态未确认，请稍后重试",
        }), 503

    complete_invite_request(email, reservation_token, succeeded=True)
    return jsonify({"ok": True, "message": "邀请码已发送至管理员邮箱，请等待审核"}), 201


@app.route("/auth/login", methods=["POST"])
def login():
    data = request.get_json(silent=True) or {}
    login_val = (data.get("login") or data.get("email") or "").strip()
    password = data.get("password") or ""
    client_ip = _trusted_client_ip()

    admitted, retry_after = admit_login_attempt(client_ip, login_val)
    if not admitted:
        return _rate_limited_response(retry_after)

    # Try email first (case-insensitive), then username (case-sensitive)
    user = get_user_by_email(login_val.lower())
    if not user:
        user = get_user_by_username(login_val)
    if not user or not verify_password(password, user["password"]):
        return jsonify({"error": "invalid email/username or password"}), 401

    reset_login_failures(client_ip, login_val)
    token = create_token(user["id"], user["role"], user["token_version"])
    return jsonify({
        "token": token,
        "user": {k: v for k, v in user.items() if k != "password"},
    })


@app.route("/auth/me", methods=["GET"])
@require_auth
def me():
    user = get_user(g.user_id)
    if not user:
        return jsonify({"error": "user not found"}), 404
    return jsonify(user)


@app.route("/auth/me", methods=["PUT"])
@require_role("user", "admin")
def update_me():
    data = request.get_json(silent=True) or {}
    allowed = {k: data[k] for k in ("nickname",) if k in data}
    user = update_user(g.user_id, **allowed)
    return jsonify(user) if user else (jsonify({"error": "not found"}), 404)


# ─── Avatar Upload ─────────────────────────────────────────

AVATARS_DIR = os.path.join(DATA_DIR, "avatars")
AVATAR_MAX_SIZE = 500 * 1024  # 500KB
ALLOWED_AVATAR_TYPES = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/gif": "gif",
    "image/webp": "webp",
}


@app.route("/auth/me/avatar", methods=["PUT"])
@require_role("user", "admin")
def upload_avatar():
    """Upload avatar as base64 data URL. Saves to data/avatars/{user_id}.{ext}."""
    data = request.get_json(silent=True) or {}
    image_data = data.get("image", "")

    if not image_data:
        return jsonify({"error": "image required"}), 400

    # Parse data URL: data:image/{type};base64,{data}
    if not image_data.startswith("data:"):
        return jsonify({"error": "invalid image format"}), 400

    try:
        header, raw = image_data.split(",", 1)
        mime = header.split(";")[0].split(":", 1)[1]  # e.g. image/jpeg
        ext = ALLOWED_AVATAR_TYPES.get(mime)
        if not ext:
            return jsonify({"error": "unsupported image type (jpg/png/gif/webp only)"}), 400

        raw_bytes = base64.b64decode(raw, validate=True)
        actual_mime = detect_image_content_type(raw_bytes)
        if actual_mime is None or actual_mime != mime:
            return jsonify({"error": "image content does not match declared type"}), 400
        ext = ALLOWED_AVATAR_TYPES[actual_mime]

        if len(raw_bytes) > AVATAR_MAX_SIZE:
            return jsonify({"error": "image too large (max 500KB)"}), 400

        # Ensure avatars directory exists
        os.makedirs(AVATARS_DIR, exist_ok=True)

        # Build filename: user_id with proper extension
        old_paths = [
            os.path.join(AVATARS_DIR, f"{g.user_id}.{old_ext}")
            for old_ext in ALLOWED_AVATAR_TYPES.values()
        ]
        new_path = os.path.join(AVATARS_DIR, f"{g.user_id}.{ext}")

        # Remove old avatar files with different extension
        for p in old_paths:
            if p != new_path and os.path.exists(p):
                os.remove(p)

        # Write new avatar
        with open(new_path, "wb") as f:
            f.write(raw_bytes)

        import time
        avatar_url = f"/avatars/{g.user_id}.{ext}?v={int(time.time())}"
        update_user(g.user_id, avatar_url=avatar_url)

        return jsonify({"avatar_url": avatar_url}), 200

    except (ValueError, IndexError, base64.binascii.Error) as e:
        return jsonify({"error": "invalid image data"}), 400


# ─── Admin Routes ─────────────────────────────────────────────

@app.route("/auth/users", methods=["GET"])
@require_role("admin")
def admin_list_users():
    users = list_users()
    return jsonify({
        "users": users,
        "total": len(users),
        "active_7d": count_active_users_since(7),
    })


@app.route("/auth/users/<int:user_id>", methods=["DELETE"])
@require_role("admin")
def admin_delete_user(user_id):
    if user_id == g.user_id:
        return jsonify({"error": "cannot delete yourself"}), 400
    ok = delete_user(user_id)
    if ok:
        return jsonify({"ok": True}), 200
    return jsonify({"error": "not found"}), 404


@app.route("/auth/users/<int:user_id>/role", methods=["PUT"])
@require_role("admin")
def admin_set_role(user_id):
    data = request.get_json(silent=True) or {}
    new_role = data.get("role")
    if new_role not in ("user", "admin"):
        return jsonify({"error": "invalid role"}), 400
    if user_id == g.user_id:
        return jsonify({"error": "cannot change your own role"}), 400
    user = set_user_role_and_rotate_token_version(user_id, new_role)
    if not user:
        return jsonify({"error": "not found"}), 404
    return jsonify(user)


@app.route("/auth/users/<int:user_id>/revoke-tokens", methods=["POST"])
@require_role("admin")
def admin_revoke_tokens(user_id):
    if rotate_token_version(user_id):
        return jsonify({"ok": True}), 200
    return jsonify({"error": "not found"}), 404


@app.route("/auth/pending-invitations", methods=["GET"])
@require_role("admin")
def admin_list_pending_invitations():
    """Invite-code requests that haven't been used to complete registration yet."""
    pending = list_pending_invitations()
    return jsonify({"invitations": pending, "total": len(pending)})


@app.route("/auth/pending-invitations/<int:invitation_id>", methods=["DELETE"])
@require_role("admin")
def admin_revoke_pending_invitation(invitation_id):
    ok = delete_invitation_code(invitation_id)
    if ok:
        return jsonify({"ok": True}), 200
    return jsonify({"error": "not found"}), 404


@app.route("/admin/system-ai-config", methods=["GET"])
@require_role("admin")
def admin_get_system_ai_config():
    config = get_system_ai_config()
    safe = dict(config)
    safe["has_api_key"] = bool(safe.get("api_key"))
    safe.pop("api_key", None)
    return jsonify(safe)


@app.route("/admin/system-ai-config", methods=["PUT"])
@require_role("admin")
def admin_set_system_ai_config():
    try:
        data = request.get_json(silent=True) or {}
        existing = get_system_ai_config()
        api_key = data.get("api_key", "")
        if "****" in api_key:
            if existing.get("api_key"):
                data["api_key"] = existing["api_key"]
            else:
                data.pop("api_key", None)
        try:
            effective_endpoint = data.get(
                "endpoint", existing.get("endpoint", "https://api.openai.com/v1")
            )
            assert_ai_endpoint_url(effective_endpoint)
            validate_ai_endpoint_base_url(effective_endpoint)
        except (UnsafeUrlError, ValueError):
            return jsonify({"error": "AI endpoint must be a public HTTP(S) URL"}), 400
        config = set_system_ai_config(**data)
        # A new key/endpoint deserves a clean slate: without this, the previous
        # config's `alerted` flag would mute the alert for an equally broken one.
        _reset_system_ai_health()
        safe = dict(config)
        safe["has_api_key"] = bool(safe.get("api_key"))
        safe.pop("api_key", None)
        return jsonify(safe)
    except Exception:
        app.logger.exception("Failed to update system AI configuration")
        return jsonify({"error": "internal server error"}), 500


# ─── Favorites API ─────────────────────────────────────────

NEWS_DB = os.path.join(DATA_DIR, "news.db")


def _news_db_connect() -> sqlite3.Connection:
    """Open news.db with the busy semantics every other writer already uses.

    The fetcher commits a streaming batch every couple of seconds and holds the
    write lock while it does. Without an explicit timeout these connections take
    sqlite3's 5s default and surface a hard "database is locked" mid-cycle, so
    match the timeout=30 / busy_timeout=30000 pairing used by fetcher.py,
    refresh_server.py and models.py.
    """
    conn = sqlite3.connect(NEWS_DB, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


@contextmanager
def _news_db_conn():
    conn = _news_db_connect()
    try:
        yield conn
    finally:
        conn.close()


def _ensure_news_schema(conn: sqlite3.Connection, *, force: bool = False) -> None:
    # A schema upgrade is a read-then-write sequence (PRAGMA followed by
    # ALTER TABLE), so concurrent request connections must not run it in
    # parallel.  Keep the guard here rather than only in _get_news_db():
    # _get_article_meta and maintenance routes also invoke this helper.
    #
    # ensure_article_schema() opens BEGIN IMMEDIATE — SQLite's exclusive write
    # lock — before it can even read PRAGMA table_info. Werkzeug serves every
    # request on a brand new thread, so a purely thread-local connection means a
    # fresh connection (and a fresh migration pass) per request: every request
    # would contend for the write lock just to confirm nothing changed. Latch it
    # per database path instead, the same way models.get_db() guards the app
    # database. Keying on the path rather than a bare bool keeps this correct
    # when a test repoints NEWS_DB at a fresh file.
    db_path = os.path.abspath(NEWS_DB)
    with _news_schema_lock:
        if db_path in _news_schema_ready_paths and not force:
            return
        ensure_article_schema(conn)
        # Only latch once `articles` exists. A news.db file can be present before
        # the fetcher has created the table (schema migration then has nothing to
        # upgrade), and the real column migration still has to run afterwards.
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'articles'"
        ).fetchone():
            _news_schema_ready_paths.add(db_path)


def _get_article_meta(article_id: int) -> dict | None:
    """Fetch article title/source/date/thumb from news.db by id."""
    if not os.path.exists(NEWS_DB):
        return None
    try:
        with _news_db_conn() as conn:
            _ensure_news_schema(conn)
            row = conn.execute(
                "SELECT id, title, original_title, COALESCE(NULLIF(group_source, ''), source) AS source, "
                "       feed_source, group_source, publisher_domain, origin_source, "
                "       date, time, thumb, has_full_content, timestamp "
                "FROM articles WHERE id = ?",
                (article_id,),
            ).fetchone()
        return dict(row) if row else None
    except Exception:
        return None


# One SQLite connection per request-handling thread. Werkzeug's threaded server hands
# each request its own thread; a single process-wide connection shared across them meant
# several threads driving one connection's cursors — and, worse, one connection-wide
# transaction — concurrently. An admin write on /admin/* could then interleave with
# another thread's read or commit and corrupt transaction state ("cannot commit - no
# transaction"), surface a half-written row (dirty read), or hit "database is locked".
# A thread-local connection is the sqlite3-recommended pattern: each thread owns its own
# and it's finalized when the thread ends. WAL mode (set by the fetcher) lets these
# coexist cleanly — concurrent readers never block and writers serialize at the SQLite
# level rather than clobbering a shared Python-level transaction.
_news_conn_local = threading.local()
# Schema checks can add columns for databases created by older releases.  Each
# request thread owns its connection, but SQLite's PRAGMA-then-ALTER sequence
# must still be serialized in this process: otherwise concurrent first-use
# connections can all see a missing column and race to add it.
_news_schema_lock = threading.RLock()
# Database paths whose migration has already run in this process. See
# _ensure_news_schema() for why this must not be re-run per connection.
_news_schema_ready_paths: set[str] = set()


def _get_news_db():
    """Per-thread persistent connection to news.db for batch queries."""
    conn = getattr(_news_conn_local, "conn", None)
    db_path = os.path.abspath(NEWS_DB)
    # A cached connection to a different file is stale (tests repoint NEWS_DB);
    # drop it rather than answering queries against the previous database.
    if conn is not None and getattr(_news_conn_local, "path", None) != db_path:
        try:
            conn.close()
        except sqlite3.Error:
            pass
        conn = None
        _news_conn_local.conn = None
        _news_conn_local.path = None
    if conn is None and os.path.exists(NEWS_DB):
        conn = _news_db_connect()
        _ensure_news_schema(conn)
        _news_conn_local.conn = conn
        _news_conn_local.path = db_path
    return conn


def _get_article_meta_batch(article_ids: list[int]) -> dict[int, dict]:
    """Fetch metadata for multiple articles at once."""
    if not article_ids:
        return {}
    conn = _get_news_db()
    if not conn:
        return {}
    try:
        placeholders = ",".join("?" * len(article_ids))
        rows = conn.execute(
            "SELECT id, title, original_title, COALESCE(NULLIF(group_source, ''), source) AS source, "
            "       feed_source, group_source, publisher_domain, origin_source, "
            f"      date, time, thumb, has_full_content, timestamp FROM articles WHERE id IN ({placeholders})",
            article_ids,
        ).fetchall()
        return {r["id"]: dict(r) for r in rows}
    except Exception:
        return {}


def _fetch_article_images(article_id: int) -> dict | None:
    conn = _get_news_db()
    if not conn:
        return None
    try:
        row = conn.execute(
            "SELECT id, thumb, body_html FROM articles WHERE id = ?",
            (article_id,),
        ).fetchone()
        return dict(row) if row else None
    except Exception:
        return None


def _pin_favorite_article_images(article_id: int):
    article = _fetch_article_images(article_id)
    if not article:
        return
    enqueue_article_image_prefetch(
        article_id,
        article.get("body_html"),
        article.get("thumb"),
        pinned=True,
    )


def _pin_favorite_articles_images(article_ids: list[int]):
    for article_id in article_ids:
        _pin_favorite_article_images(article_id)


def _pin_existing_favorite_images_on_startup():
    try:
        article_ids = get_all_favorite_article_ids()
        if not article_ids:
            return
        print(f"[image-cache] Pinning images for {len(article_ids)} existing favorite article(s)")
        _pin_favorite_articles_images(article_ids)
        print("[image-cache] Existing favorite image pinning finished")
    except Exception as exc:
        print(f"[image-cache] Existing favorite image pinning failed: {exc}")


import sqlite3


@app.route("/favorites", methods=["GET"])
@require_role("user", "admin")
def list_favorites():
    """List user's favorites with article metadata."""
    favs = get_favorites(g.user_id)
    article_ids = [f["article_id"] for f in favs]
    if article_ids:
        threading.Thread(target=_pin_favorite_articles_images, args=(article_ids,), daemon=True).start()
    articles = _get_article_meta_batch(article_ids)
    items = []
    for f in favs:
        meta = articles.get(f["article_id"])
        if meta:
            items.append({
                "article_id": f["article_id"],
                "created_at": f["created_at"],
                "title": meta["title"],
                "original_title": meta.get("original_title", ""),
                "source": meta["source"],
                "feed_source": meta.get("feed_source", meta["source"]),
                "origin_source": meta.get("origin_source", ""),
                "date": meta["date"],
                "time": meta["time"],
                "thumb": meta["thumb"],
                "has_full_content": meta["has_full_content"],
            })
    return jsonify({"items": items, "total": len(items)})


@app.route("/favorites", methods=["POST"])
@require_role("user", "admin")
def add_favorite_route():
    data = request.get_json(silent=True) or {}
    article_id = data.get("article_id")
    if not article_id or not isinstance(article_id, int):
        return jsonify({"error": "article_id required (int)"}), 400
    fav = add_favorite(g.user_id, article_id)
    if fav is None:
        return jsonify({"error": "already favorited"}), 409
    threading.Thread(target=_pin_favorite_article_images, args=(article_id,), daemon=True).start()
    return jsonify({"ok": True, "favorite": fav}), 201


@app.route("/favorites/<int:article_id>", methods=["DELETE"])
@require_role("user", "admin")
def remove_favorite_route(article_id):
    ok = remove_favorite(g.user_id, article_id)
    if ok:
        if count_article_favorites(article_id) == 0:
            threading.Thread(target=unpin_article_images, args=(article_id,), daemon=True).start()
        return jsonify({"ok": True}), 200
    return jsonify({"error": "not found"}), 404


@app.route("/favorites/<int:article_id>/status", methods=["GET"])
@require_role("user", "admin")
def favorite_status(article_id):
    return jsonify({"favorited": is_favorited(g.user_id, article_id)})


# ─── In-App Notifications ─────────────────────────────────────

@app.route("/notifications", methods=["GET"])
@require_role("user", "admin")
def list_notifications_route():
    """User's in-app notifications, newest first, with unread count.

    Bodies are small plain text, so the list payload carries them inline —
    no separate detail endpoint needed.
    """
    response = jsonify({
        "items": list_notifications(g.user_id),
        "unread": count_unread_notifications(g.user_id),
    })
    # Notification data is user-specific and changes immediately after a
    # broadcast. Never let a browser or intermediary reuse an older empty
    # response for the same authenticated URL.
    response.headers["Cache-Control"] = "private, no-store"
    return response


@app.route("/notifications/<int:nid>/read", methods=["POST"])
@require_role("user", "admin")
def mark_notification_read_route(nid):
    # Idempotent: marking an already-read (or unknown) row is not an error;
    # the client only cares about the resulting unread count.
    mark_notification_read(g.user_id, nid)
    return jsonify({"ok": True, "unread": count_unread_notifications(g.user_id)})


@app.route("/notifications/read-all", methods=["POST"])
@require_role("user", "admin")
def mark_all_notifications_read_route():
    mark_all_notifications_read(g.user_id)
    return jsonify({"ok": True, "unread": count_unread_notifications(g.user_id)})


@app.route("/notifications/<int:nid>", methods=["DELETE"])
@require_role("user", "admin")
def delete_notification_route(nid):
    delete_notification(g.user_id, nid)
    return jsonify({"ok": True, "unread": count_unread_notifications(g.user_id)})


@app.route("/notifications", methods=["DELETE"])
@require_role("user", "admin")
def delete_all_notifications_route():
    delete_all_notifications(g.user_id)
    return jsonify({"ok": True, "unread": 0})


NOTIF_BROADCAST_BODY_MAX = 20000


def _broadcast_notification_emails(
    broadcast_id, user_ids, title, body, fmt="plain",
):
    """Fan out an email copy of a broadcast to each user (best-effort, in a
    background thread — mass send must not block the publish request).

    Each send carries a per-(broadcast, user) Resend idempotency key as a
    second line of defense — the caller (admin_broadcast_notification) never
    launches this thread twice for the same broadcast_id, but this way even a
    duplicate call can't actually double-send."""
    for uid in user_ids:
        try:
            _send_notification_email(
                uid, title, body,
                idempotency_key=f"broadcast-{broadcast_id}-{uid}",
                fmt=fmt,
            )
        except Exception as exc:
            print(f"[broadcast] email to user {uid} failed: {exc}")


@app.route("/admin/notifications/broadcast", methods=["POST"])
@require_role("admin")
def admin_broadcast_notification():
    """Publish a site-wide notification: one in-app row per user, optionally
    with an email copy. Body is stored raw (plain or markdown) — every render
    path escapes/sanitizes.

    Idempotent on broadcast_id (client-generated, persisted across a manual
    retry — see publishBroadcast() in frontend/index.html): if the response to
    the first attempt was lost in transit and the admin retries, replaying the
    same broadcast_id returns the original result instead of re-inserting a
    notification for every user and re-sending every email.
    """
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "JSON object required"}), 400
    raw_title = data.get("title", "")
    raw_body = data.get("body", "")
    raw_format = data.get("format", "plain")
    raw_email = data.get("email", False)
    raw_broadcast_id = data.get("broadcast_id", "")
    if not isinstance(raw_title, str) or not isinstance(raw_body, str):
        return jsonify({"error": "title/body must be strings"}), 400
    if not isinstance(raw_format, str):
        return jsonify({"error": "format must be a string"}), 400
    if not isinstance(raw_email, bool):
        return jsonify({"error": "email must be a boolean"}), 400
    if not isinstance(raw_broadcast_id, str):
        return jsonify({"error": "broadcast_id must be a string"}), 400
    # Fall back to a fresh id for a caller that doesn't send one (no
    # idempotency protection in that case, but still a valid single publish).
    broadcast_id = raw_broadcast_id.strip()[:100] or uuid.uuid4().hex

    title = _sanitize_plain_text(raw_title, max_len=200)
    if not title:
        return jsonify({"error": "title required"}), 400
    # Keep the raw body (do NOT run _sanitize_plain_text — it would strip
    # markdown markup); just length-clamp. Client render paths handle safety.
    body = raw_body.strip()[:NOTIF_BROADCAST_BODY_MAX]
    if not body:
        return jsonify({"error": "body required"}), 400
    fmt = "markdown" if raw_format == "markdown" else "plain"
    do_email = raw_email

    user_ids = [u["id"] for u in list_users()]
    is_new, result = publish_broadcast_atomically(
        user_ids, broadcast_id, title, body, fmt, do_email,
    )
    if is_new and do_email and user_ids:
        threading.Thread(
            target=_broadcast_notification_emails,
            args=(broadcast_id, user_ids, title, body, fmt),
            daemon=True,
        ).start()
    response = {"ok": True, **result}
    if not is_new:
        response["replayed"] = True
    return jsonify(response)


# ─── AI Routes ─────────────────────────────────────────────

@app.route("/ai/config", methods=["GET"])
@require_role("user", "admin")
def get_ai_config_route():
    config = get_ai_config(g.user_id)
    if not config:
        return jsonify({
            "provider": "openai",
            "endpoint": "https://api.openai.com/v1",
            "model": "gpt-4o-mini",
            "provider_type": "openai",
            "enabled": False,
            "has_api_key": False,
        })
    safe = dict(config)
    has_key = bool(safe.get("api_key"))
    safe["has_api_key"] = has_key
    safe.pop("api_key", None)  # never expose the key to frontend
    safe.pop("revision", None)
    return jsonify(safe)


@app.route("/ai/config", methods=["PUT"])
@require_role("user", "admin")
def set_ai_config_route():
    try:
        data = request.get_json(silent=True) or {}
        existing = get_ai_config(g.user_id)
        # If api_key contains "****", it's the masked placeholder — preserve existing
        api_key = data.get("api_key", "")
        if "****" in api_key:
            if existing and existing.get("api_key"):
                data["api_key"] = existing["api_key"]
            else:
                data.pop("api_key", None)
        effective_endpoint = data.get(
            "endpoint",
            (existing or {}).get("endpoint", "https://api.openai.com/v1"),
        )
        try:
            assert_ai_endpoint_url(effective_endpoint)
            validate_ai_endpoint_base_url(effective_endpoint)
        except (UnsafeUrlError, ValueError):
            return jsonify({"error": "AI endpoint must be a public HTTP(S) URL"}), 400
        config = set_ai_config(g.user_id, **data)
        settings = get_user_settings(g.user_id) or {}
        share_check = None
        if _is_enabled_value(settings.get("share_ai_results")):
            test_body, test_status = _run_ai_connection_test(config)
            share_check = _share_check_after_personal_api_test(
                g.user_id, test_body, test_status, (config or {}).get("revision", 0)
            )
        safe = dict(config) if config else {}
        has_key = bool(safe.get("api_key"))
        safe["has_api_key"] = has_key
        safe.pop("api_key", None)
        safe.pop("revision", None)
        if share_check is not None:
            safe["share_check"] = share_check
        return jsonify(safe)
    except Exception:
        app.logger.exception("Failed to update personal AI configuration")
        return jsonify({"error": "internal server error"}), 500


@app.route("/ai/config/client", methods=["GET"])
@require_role("user", "admin")
def get_ai_config_client_route():
    """Return the calling user's own AI config, including the plaintext api_key.

    Used by the browser to call the LLM provider directly for manual
    summarize/translate when the server has no shared cached result yet.
    Only ever returns the requesting user's own config (g.user_id).
    """
    config = get_ai_config(g.user_id)
    if not config:
        return jsonify({
            "provider": "openai",
            "endpoint": "https://api.openai.com/v1",
            "model": "gpt-4o-mini",
            "provider_type": "openai",
            "enabled": False,
            "api_key": "",
        })
    safe = dict(config)
    safe.pop("revision", None)
    return jsonify(safe)


class _SystemAIService:
    """AIService for a server-side job, reporting into the system-AI health signal.

    Health has to be recorded per *provider call*, not per job iteration. A
    cached summary/translation, or an article that turned out to need nothing,
    finishes the iteration without touching the AI at all — counting that as
    "the AI works" cleared a real outage, and with the key suspended the admin
    got a 失败/恢复 pair every 30 seconds instead of one alert.

    Only the background jobs use this: every call it wraps runs on the server
    API, so a user's own key can never move this signal.
    """

    def __init__(self, job: str, **kwargs):
        object.__setattr__(self, "_job", job)
        object.__setattr__(self, "_svc", AIService(**kwargs))

    def __getattr__(self, name):
        attr = getattr(self._svc, name)
        if not callable(attr):
            return attr

        def call(*args, **kwargs):
            try:
                result = attr(*args, **kwargs)
            except Exception as exc:
                _note_system_ai_failure(self._job, exc)
                raise
            _note_system_ai_success()
            return result

        return call

    def __setattr__(self, name, value):
        setattr(self._svc, name, value)


def _generate_article_summary(article_id: int, config: dict,
                              use_shared_cache: bool = True,
                              save_shared_cache: bool = True) -> tuple[str, bool]:
    """Generate and cache a single-article AI summary."""
    cached = _get_ai_result(article_id) if use_shared_cache else None
    if cached and cached.get("summary"):
        return cached["summary"], True

    article = _fetch_article_body(article_id)
    if not article:
        raise KeyError("article not found")

    svc = _SystemAIService(
        "自动摘要",
        api_key=config["api_key"],
        endpoint=config["endpoint"],
        model=config["model"],
        provider_type=config.get("provider_type", "openai"),
    )
    text = article.get("body_html") or article.get("summary") or ""
    title = article.get("title", "")
    if hasattr(getattr(svc, "_svc", svc), "summarize_with_signals"):
        summary, signals = svc.summarize_with_signals(article_text=text, title=title)
    else:
        summary, signals = svc.summarize(article_text=text, title=title), None
    if save_shared_cache:
        saved = _save_ai_result(
            article_id,
            summary=summary,
            summary_provider=config.get("provider") or config.get("provider_type"),
            summary_model=config.get("model"),
            summary_by_user_id=config.get("user_id"),
        )
        if not saved:
            raise RuntimeError("Could not save article AI summary")
        if signals:
            _save_digest_signals(article_id, normalize_signals(article, signals))
    return summary, False


@app.route("/ai/result/<int:article_id>", methods=["POST"])
@require_role("user", "admin")
def ai_save_result(article_id):
    """Save a browser-generated summary/translation into the shared cache.

    Manual summarize/translate now runs in the user's own browser against
    their own AI config (see GET /ai/config/client) instead of a server-side
    proxy call. Once the browser has a result, it POSTs it here so every
    other user can reuse it via GET /ai/result/<id> without regenerating.
    Body: {"summary": "..."} or {"translation": "..."} (translation is the
    same JSON-string-encoded {"title", "html"} shape used elsewhere).
    """
    if (
        request.content_length is not None
        and request.content_length > AI_RESULT_MAX_BODY_BYTES
    ):
        return jsonify({"error": "request body too large"}), 413

    settings = get_user_settings(g.user_id) or {}
    if not is_share_active(settings):
        return jsonify({"error": "shared AI result publication is not active"}), 403

    article = _fetch_article_body(article_id)
    if not article:
        return jsonify({"error": "article not found"}), 404

    data = request.get_json(silent=True) or {}
    summary = data.get("summary")
    translation = data.get("translation")
    if summary is not None and not isinstance(summary, str):
        return jsonify({"error": "summary must be a string"}), 400
    if translation is not None and not isinstance(translation, str):
        return jsonify({"error": "translation must be a string"}), 400
    if summary is not None and len(summary) > AI_RESULT_MAX_FIELD_CHARS:
        return jsonify({"error": "summary too long"}), 400
    if translation is not None and len(translation) > AI_RESULT_MAX_FIELD_CHARS:
        return jsonify({"error": "translation too long"}), 400
    if not summary and not translation:
        return jsonify({"error": "summary or translation required"}), 400

    kwargs = {}
    config = get_ai_config(g.user_id) or {}
    provider = config.get("provider") or config.get("provider_type")
    model = config.get("model")
    if summary:
        kwargs.update({
            "summary": summary,
            "summary_by_user_id": g.user_id,
            "summary_provider": provider,
            "summary_model": model,
        })
    if translation:
        kwargs.update({
            "translation": translation,
            "translation_by_user_id": g.user_id,
            "translation_provider": provider,
            "translation_model": model,
        })
    if not _save_ai_result(article_id, **kwargs):
        return jsonify({"error": "internal server error"}), 500

    # Keep the article title in news.db in sync so the home page list shows
    # the translated title too (mirrors the old server-side translate-full behavior).
    # Restricted to admins: this endpoint accepts any logged-in user's
    # self-reported translation JSON, and the home page title is shown to
    # every visitor — a regular user must not be able to rewrite it by simply
    # submitting a crafted payload. Non-admin submissions still populate the
    # shared summary/translation cache above; only the title-sync side effect
    # is gated. (Non-admin titles still get translated via the validated
    # admin-driven auto-title-process pipeline in _process_article_title.)
    if translation and g.user_role == "admin":
        try:
            translated_title = json.loads(translation).get("title") if translation.strip().startswith("{") else ""
        except (json.JSONDecodeError, TypeError):
            translated_title = ""
        if translated_title:
            _save_article_title_update(article_id, translated_title, "translation")

    return jsonify({"ok": True})


def _run_ai_connection_test(config: dict | None) -> tuple[dict, int]:
    """Shared connectivity test used by both the personal and system AI configs."""
    if not config or not config.get("api_key"):
        return {"error": "AI not configured. Save API config first."}, 400
    try:
        svc = AIService(
            api_key=config["api_key"],
            endpoint=config["endpoint"],
            model=config["model"],
            provider_type=config.get("provider_type", "openai"),
        )
        response = svc.test_connection()
        if not response or not response.strip():
            return {"error": "Connection test returned empty response"}, 502
        return {"ok": True, "response": response}, 200
    except requests.exceptions.ConnectTimeout as exc:
        _log_ai_failure("AI connection test timed out while connecting", exc, config["api_key"])
        return {"error": "连接 AI 服务超时。请检查 API 地址是否正确，或 Docker 容器是否配置了 HTTP_PROXY 环境变量"}, 502
    except requests.exceptions.ConnectionError as exc:
        _log_ai_failure("AI connection test could not connect", exc, config["api_key"])
        return {"error": "无法连接 AI 服务。请检查网络代理配置"}, 502
    except requests.exceptions.Timeout as exc:
        _log_ai_failure("AI connection test timed out", exc, config["api_key"])
        return {"error": "AI 服务响应超时"}, 502
    except Exception as exc:
        _log_ai_failure("AI connection test failed", exc, config["api_key"])
        return {"error": "AI connection test failed"}, 502


def _log_ai_failure(message: str, exc: Exception, api_key: str = "") -> None:
    """Log a compact provider failure without traceback URLs or credentials."""
    if isinstance(exc, requests.exceptions.RequestException):
        # requests exceptions may embed the complete request URL.  Arbitrary
        # query parameter names cannot be safely redacted, so retain only the
        # useful failure class and never stringify a network exception.
        detail = type(exc).__name__
    else:
        detail = _redact_api_error(str(exc), api_key)
    app.logger.error("%s: %s", message, detail or type(exc).__name__)


def _share_check_after_personal_api_test(
    user_id: int,
    body: dict,
    status: int,
    config_revision: int | None = None,
) -> dict | None:
    """Apply a personal API probe to opted-in sharing and return safe status."""
    settings = get_user_settings(user_id) or {}
    if not _is_enabled_value(settings.get("share_ai_results")):
        return None
    error = body.get("error", "") if status != 200 else ""
    transition = _apply_share_connectivity_result(
        user_id, status == 200, error, config_revision=config_revision
    )
    result = {
        "status": transition,
        "restored": transition == "restored",
    }
    if status != 200:
        result["error"] = _compact_share_error(error)
    return result


@app.route("/ai/test-connection", methods=["POST"])
@require_role("user", "admin")
def ai_test_connection():
    """Test the user's own AI API configuration with a minimal prompt."""
    config = get_ai_config(g.user_id)
    body, status = _run_ai_connection_test(config)
    share_check = _share_check_after_personal_api_test(
        g.user_id, body, status, (config or {}).get("revision", 0)
    )
    if share_check is not None:
        body = {**body, "share_check": share_check}
    if status != 200 and "error" in body:
        body = {**body, "error": _compact_share_error(body["error"])}
    return jsonify(body), status


@app.route("/ai/chat", methods=["POST"])
@require_role("user", "admin")
def ai_chat_relay():
    """Same-origin relay for the browser's per-article summarize/translate chats.

    The browser normally calls the user's AI endpoint directly (see aiChat() in
    index.html) so each user spends their own quota with zero server load. But some
    providers (e.g. opencode.ai/zen) don't send CORS headers, so a browser-direct
    fetch is blocked by the same-origin policy. For those the client falls back to
    this endpoint, which forwards the chat from the server — server-to-server, no
    CORS — using the caller's own stored AI config. The api_key never leaves the
    server on this path. Only ever invoked for CORS-blocked endpoints, so
    CORS-friendly providers keep the original direct, server-free path.
    """
    config = get_ai_config(g.user_id)
    if not config or not config.get("api_key"):
        return jsonify({"error": "AI not configured. Save API config first."}), 400
    if not config.get("enabled"):
        return jsonify({"error": "AI is disabled in your settings."}), 400

    data = request.get_json(silent=True) or {}
    messages = data.get("messages")
    if not isinstance(messages, list) or not messages:
        return jsonify({"error": "messages required"}), 400
    for m in messages:
        if not isinstance(m, dict) or not isinstance(m.get("role"), str) \
                or not isinstance(m.get("content"), str):
            return jsonify({"error": "each message needs a string role and content"}), 400
    try:
        # Clamp to the same envelope the client uses (translate asks for 8000) so a
        # crafted payload can't request an absurd generation on the user's key.
        max_tokens = max(1, min(int(data.get("max_tokens", 2000)), 8000))
        temperature = max(0.0, min(float(data.get("temperature", 0.3)), 2.0))
    except (TypeError, ValueError):
        return jsonify({"error": "invalid max_tokens/temperature"}), 400

    try:
        svc = AIService(
            api_key=config["api_key"],
            endpoint=config["endpoint"],
            model=config["model"],
            provider_type=config.get("provider_type", "openai"),
        )
        content = svc.chat(messages, max_tokens=max_tokens, temperature=temperature)
    except TimeoutError as exc:
        _log_ai_failure("AI relay timed out", exc, config["api_key"])
        return jsonify({"error": "AI service timed out"}), 504
    except requests.exceptions.RequestException as exc:
        _log_ai_failure("AI relay request failed", exc, config["api_key"])
        return jsonify({"error": "AI service unavailable"}), 502
    except Exception as exc:
        _log_ai_failure("AI relay failed", exc, config["api_key"])
        return jsonify({"error": "AI relay failed"}), 502
    if not (content or "").strip():
        return jsonify({"error": "AI returned an empty response"}), 502
    return jsonify({"content": content})


@app.route("/admin/system-ai-config/test", methods=["POST"])
@require_role("admin")
def admin_system_ai_test_connection():
    """Test the admin-configured system AI (drives background auto summary/translate)."""
    body, status = _run_ai_connection_test(get_system_ai_config())
    # A passing test is as good as a background call succeeding — clear the
    # streak (and send the recovery notice) without waiting for the next job.
    # A failing one is not counted: a manual probe shouldn't push the streak
    # toward an alert the admin is already looking at.
    if status == 200:
        _note_system_ai_success()
    return jsonify(body), status


def _resend_to_email(config) -> str:
    """The notification address out of a notification_config, in either shape it
    travels in: a dict (request payload) or the JSON string it is stored as."""
    if isinstance(config, str):
        try:
            config = json.loads(config or "{}")
        except (json.JSONDecodeError, TypeError):
            config = {}
    if not isinstance(config, dict):
        return ""
    resend = config.get("resend")
    if not isinstance(resend, dict):
        return ""
    return str(resend.get("to_email") or "").strip()


def _notification_recipient(user_id: int) -> str:
    """Best email to reach a user at: prefer the address they set for
    notifications, fall back to their account email."""
    try:
        settings = get_user_settings(user_id) or {}
        nc = settings.get("notification_config") or "{}"
        if isinstance(nc, str):
            try:
                nc = json.loads(nc)
            except (json.JSONDecodeError, TypeError):
                nc = {}
        to_email = ((nc.get("resend") or {}).get("to_email") or "").strip()
        if to_email:
            return to_email
    except Exception:
        pass
    user = get_user(user_id) or {}
    return (user.get("email") or "").strip()


def _send_notification_email(user_id: int, title: str, body: str,
                             idempotency_key: str | None = None,
                             fmt: str = "plain") -> bool:
    """Email a user a copy of an in-app notification. Best-effort; never
    raises. Markdown bodies use the notification email sanitizer; the default
    remains literal plain text so ordinary system notifications retain their
    existing rendering.

    idempotency_key, when given, is passed to Resend so a caller that retries
    after a lost response (see admin_broadcast_notification's broadcast_id
    dedup) can't cause the same email to actually go out twice, even if our
    own DB-level dedup were ever bypassed."""
    api_key = os.environ.get("RESEND_API_KEY", "")
    to_email = _notification_recipient(user_id)
    if not api_key:
        _note_email_delivery_failure("RESEND_API_KEY 未配置")
        return False
    if not to_email:
        return False
    from_email = os.environ.get("RAYNEWS_FROM_EMAIL") or "onboarding@resend.dev"

    def _esc(text: str) -> str:
        return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    safe_title = _esc(title)
    safe_body = render_notification_email_body(body, fmt)
    try:
        html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#0a0a0c;color:#e8e8ed;padding:20px;max-width:560px;margin:0 auto}}
.email-title{{color:#6e8efb;font-size:18px}}
.box{{background:#111114;border:1px solid rgba(255,255,255,.08);border-radius:12px;padding:16px;margin:16px 0;color:#c9c9d4;line-height:1.8;word-break:break-word}}
.box h1,.box h2,.box h3,.box h4,.box h5,.box h6{{color:#e8e8ed;line-height:1.35;margin:18px 0 8px}}
.box h1{{font-size:22px}}.box h2{{font-size:20px}}.box h3{{font-size:18px}}.box h4{{font-size:16px}}.box h5{{font-size:15px}}.box h6{{font-size:14px}}
.box p{{margin:8px 0}}.box ul,.box ol{{margin:8px 0;padding-left:24px}}.box li{{margin:4px 0}}
.box a{{color:#8aa4ff;text-decoration:underline}}.box blockquote{{border-left:3px solid #6e8efb;margin:12px 0;padding:4px 14px;color:#a7a7b5}}
.box code{{background:rgba(255,255,255,.08);border-radius:4px;padding:2px 5px;font-size:13px}}.box pre{{background:#08080a;border-radius:8px;overflow-x:auto;padding:12px}}.box pre code{{background:none;padding:0}}
.box table{{border-collapse:collapse;display:block;max-width:100%;overflow-x:auto;margin:12px 0}}.box th,.box td{{border:1px solid rgba(255,255,255,.14);padding:6px 9px;text-align:left}}.box th{{background:rgba(255,255,255,.06)}}
.box img{{display:block;max-width:100%;height:auto;margin:12px auto;border-radius:8px}}
.footer{{font-size:12px;color:#55556a;margin-top:20px}}
</style></head><body>
<h1 class="email-title">🔔 {safe_title}</h1>
<div class="box">{safe_body}</div>
<p class="footer">此邮件由 RayNews 自动发送；同样内容可在 头像菜单 → 我的通知 中查看。不包含密码、验证码或令牌。</p>
</body></html>"""
        send_email(
            api_key,
            to_email,
            f"RayNews 通知 — {title}",
            html,
            from_name="RayNews",
            from_email=from_email,
            idempotency_key=idempotency_key,
        )
        _clear_email_delivery_failure_alert()
        return True
    except Exception as exc:
        _note_email_delivery_failure(str(exc))
        print(f"[notify] Failed to send notification email to user {user_id}: {exc}")
        return False


def _notify_user(user_id: int, ntype: str, title: str, body: str = "") -> bool:
    """Deliver a notification to a user on both channels: insert an in-app
    row (头像菜单 → 我的通知) and email a copy. Each leg is best-effort and
    independent, so a DB or mail hiccup never breaks the caller.

    Returns whether the durable in-app copy was written. Callers use that as
    the minimum delivery signal; email remains best-effort.
    """
    inapp_delivered = False
    try:
        add_notification(user_id, ntype, title, body)
        inapp_delivered = True
    except Exception as exc:
        print(f"[notify] Failed to add in-app notification for user {user_id}: {exc}")
    _send_notification_email(user_id, title, body)
    return inapp_delivered


def _notify_admins(ntype: str, title: str, body: str) -> int:
    """Send one notification to every admin, both channels. Returns how many
    admins were reached. Never raises: alerting is best-effort by definition —
    the caller is usually a background job that must keep running."""
    try:
        admins = [u for u in list_users() if u.get("role") == "admin"]
    except Exception as exc:
        print(f"[notify] admin lookup failed for {ntype}: {exc}")
        return 0
    notified = 0
    for admin in admins:
        try:
            if _notify_user(admin["id"], ntype, title, body):
                notified += 1
        except Exception as exc:
            print(f"[notify] {ntype} to admin {admin['id']} failed: {exc}")
    return notified


EMAIL_DELIVERY_FAILURE_ALERTED_STATE_KEY = "email_delivery_failure_alerted"
EMAIL_DELIVERY_FAILURE_TITLE = "邮件推送服务不可用"


def _note_email_delivery_failure(reason: str) -> None:
    safe_reason = str(reason or "邮件发送失败")
    configured_api_key = os.environ.get("RESEND_API_KEY", "")
    if configured_api_key:
        safe_reason = safe_reason.replace(
            configured_api_key, "[redacted credential]"
        )
    safe_reason = re.sub(
        r"""(?ix)
        ["']?\bRESEND_API_KEY\b["']?\s*[=:]\s*
        (?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^\s,;}\]]+)
        """,
        "[redacted credential]",
        safe_reason,
    )
    safe_reason = re.sub(
        r"""(?ix)["']?\bRESEND_API_KEY\b["']?""",
        "[redacted credential]",
        safe_reason,
    )
    safe_reason = re.sub(r"\s+", " ", safe_reason).strip()[:300]
    try:
        if not claim_app_state_flag(EMAIL_DELIVERY_FAILURE_ALERTED_STATE_KEY):
            return
    except Exception:
        pass

    delivered = 0
    try:
        admins = [user for user in list_users() if user.get("role") == "admin"]
    except Exception as exc:
        print(f"[notify] email delivery failure admin lookup failed: {exc}")
        admins = []

    for admin in admins:
        admin_id = admin.get("id")
        try:
            add_notification(
                admin_id, "email_delivery_failed", EMAIL_DELIVERY_FAILURE_TITLE,
                f"邮件推送服务不可用。原因：{safe_reason}\n\n"
                "请检查 RESEND_API_KEY、Resend 账户状态和 RAYNEWS_FROM_EMAIL 配置。",
            )
            delivered += 1
        except Exception as exc:
            print(
                "[notify] email delivery failure alert to admin "
                f"{admin_id} failed: {exc}"
            )

    if delivered == 0:
        _clear_email_delivery_failure_alert()


def _clear_email_delivery_failure_alert() -> None:
    try:
        set_app_state(EMAIL_DELIVERY_FAILURE_ALERTED_STATE_KEY, "0")
    except Exception as exc:
        print(f"[notify] email delivery failure alert clear failed: {exc}")


# ─── System AI health ──────────────────────────────────────
#
# Every background job (auto summary/translation/title/source classification)
# and the daily summary run on the one admin-configured system AI. Until now a
# dead key only showed up as log lines: the jobs kept retrying forever and the
# daily-summary alert was the single notification anyone got — and that one
# arrives at ~21:30, hours after the failures start. Track consecutive failures
# across all of them instead and tell the admins once the streak makes it clear
# the AI itself, not one article, is the problem.
# Three, not five: the evening daily-summary chain makes exactly four attempts
# (21:00 plus three retries), so a threshold of five could only ever be reached
# by the article jobs — which stay idle when there is nothing pending. At three
# the failure is reported around 21:20, ahead of the daily-summary alert, on a
# quiet day too.
SYSTEM_AI_FAILURE_ALERT_THRESHOLD = int(
    os.environ.get("SYSTEM_AI_FAILURE_ALERT_THRESHOLD", "3")
)
if "SYSTEM_AI_RECOVERY_SUCCESS_THRESHOLD" in os.environ:
    print(
        "[system-ai] SYSTEM_AI_RECOVERY_SUCCESS_THRESHOLD 已废弃并忽略；"
        "恢复现在由 SYSTEM_AI_RECOVERY_STABILITY_SECONDS 控制。"
    )
SYSTEM_AI_ALERT_COOLDOWN_SECONDS = max(
    0, int(os.environ.get("SYSTEM_AI_ALERT_COOLDOWN_SECONDS", "1800"))
)
SYSTEM_AI_RECOVERY_STABILITY_SECONDS = max(
    0.0, float(os.environ.get("SYSTEM_AI_RECOVERY_STABILITY_SECONDS", "3600"))
)
SYSTEM_AI_FAILURE_TITLE = "服务端 AI 调用连续失败"
SYSTEM_AI_RECOVERED_TITLE = "服务端 AI 已恢复"

_system_ai_health = {
    "failures": 0,
    "alerted": False,
    "last_error": "",
    "jobs": [],
    "last_failure_at": 0.0,
    "last_success_at": 0.0,
    "failure_timestamp_dirty": False,
}
_system_ai_health_lock = threading.Lock()


# "Admins have already been told about this outage" is persisted, not just held
# in memory: the process restarts (deploys, watchdog, OOM) and an in-memory flag
# would let one ongoing outage re-alert on every restart. One outage, one alert —
# same rule the user-side share suspension gets from its share_suspended column.
SYSTEM_AI_ALERTED_STATE_KEY = "system_ai_failure_alerted"
SYSTEM_AI_ALERT_LAST_NOTIFIED_STATE_KEY = "system_ai_alert_last_notified_at"
SYSTEM_AI_LAST_FAILURE_STATE_KEY = "system_ai_last_failure_at"
SYSTEM_AI_LAST_SUCCESS_STATE_KEY = "system_ai_last_success_at"
SYSTEM_AI_LAST_FAILURE_MARKER_FILE = Path(DATA_DIR) / "system-ai-last-failure.marker"


@contextmanager
def _system_ai_failure_fence():
    """Serialize failure markers and stable completion across processes."""
    lock_path = Path(f"{SYSTEM_AI_LAST_FAILURE_MARKER_FILE}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _read_system_ai_failure_marker() -> float:
    path = Path(SYSTEM_AI_LAST_FAILURE_MARKER_FILE)
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return 0.0
    value = float(raw)
    if not math.isfinite(value) or value < 0:
        raise ValueError("invalid system AI failure marker")
    return value


def _write_system_ai_failure_marker(epoch: float) -> None:
    value = float(epoch)
    if not math.isfinite(value) or value < 0:
        raise ValueError("invalid system AI failure marker")
    path = Path(SYSTEM_AI_LAST_FAILURE_MARKER_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as marker_file:
            marker_file.write(str(value))
            marker_file.flush()
            os.fsync(marker_file.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _clear_system_ai_failure_marker() -> None:
    path = Path(SYSTEM_AI_LAST_FAILURE_MARKER_FILE)
    try:
        path.unlink()
    except FileNotFoundError:
        return
    directory_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _system_ai_incident_is_active() -> bool:
    try:
        return get_app_state(SYSTEM_AI_ALERTED_STATE_KEY) in {"1", "2"}
    except Exception as exc:
        print(f"[system-ai] incident state read failed: {exc}")
        return False


def _system_ai_incident_is_notified() -> bool:
    """Whether admins have already received the current system-AI alert."""
    try:
        return get_app_state(SYSTEM_AI_ALERTED_STATE_KEY) == "1"
    except Exception as exc:
        # A failure here must leave the independent daily-summary alert intact.
        print(f"[daily-summary] system-AI state read failed: {exc}")
        return False


def _claim_system_ai_alert() -> str:
    """Atomically start a notified or cooldown-suppressed AI incident.

    The notification timestamp is persisted in the same ``BEGIN IMMEDIATE``
    transaction as the incident state, so a crash between claiming and recording
    the notification cannot leave ``last_notified`` at zero. ``claim_app_state_incident``
    in models.py does not accept a timestamp parameter and cannot be edited here,
    so the claim logic is mirrored in this transaction to set both keys together.
    """
    current = time.time()
    cooldown = max(0.0, float(SYSTEM_AI_ALERT_COOLDOWN_SECONDS))
    try:
        db = get_db()
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT value FROM app_state WHERE key = ?",
            (SYSTEM_AI_ALERTED_STATE_KEY,),
        ).fetchone()
        if row and row["value"] in {"1", "2"}:
            db.rollback()
            return "active"
        last_row = db.execute(
            "SELECT value FROM app_state WHERE key = ?",
            (SYSTEM_AI_ALERT_LAST_NOTIFIED_STATE_KEY,),
        ).fetchone()
        try:
            last_notified = float(last_row["value"]) if last_row else 0.0
        except (TypeError, ValueError):
            last_notified = 0.0
        in_cooldown = bool(last_notified) and current - last_notified < cooldown
        if in_cooldown:
            state, result = "2", "suppressed"
        else:
            state, result = "1", "notify"
        updated_at = datetime.now().isoformat(timespec="seconds")
        db.execute(
            "INSERT INTO app_state (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (SYSTEM_AI_ALERTED_STATE_KEY, state, updated_at),
        )
        if state == "1":
            db.execute(
                "INSERT INTO app_state (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                (SYSTEM_AI_ALERT_LAST_NOTIFIED_STATE_KEY, str(current), updated_at),
            )
        db.commit()
        return result
    except Exception as exc:
        # A DB hiccup must not turn into a silent outage; alerting twice is the
        # safer failure mode than never alerting.
        print(f"[system-ai] alert claim failed: {exc}")
        return "notify"


def _release_system_ai_alert() -> None:
    """Release an alert claim that reached no administrator."""
    with _system_ai_health_lock:
        _system_ai_health["alerted"] = False
    try:
        # Reset the notification timestamp together with the incident state so a
        # released (undelivered) claim does not arm the cooldown and mute the
        # retry that _note_system_ai_failure owes the next failure.
        set_app_state_values(
            {
                SYSTEM_AI_ALERTED_STATE_KEY: "0",
                SYSTEM_AI_ALERT_LAST_NOTIFIED_STATE_KEY: "0",
            }
        )
    except Exception as exc:
        print(f"[system-ai] alert state release failed: {exc}")


def _record_system_ai_notification_time() -> None:
    try:
        set_app_state(SYSTEM_AI_ALERT_LAST_NOTIFIED_STATE_KEY, str(time.time()))
    except Exception as exc:
        print(f"[system-ai] notification timestamp write failed: {exc}")


def _reset_system_ai_health() -> None:
    """Forget the current streak — used when the admin saves a new system AI
    config, so a fresh key starts from zero (and can alert again if it is also
    broken, instead of being muted by the previous config's flag)."""
    with _system_ai_health_lock:
        # Raise the in-memory floor before any fallible I/O. If persisting the
        # reset fence fails, a later stable check still repairs from this floor.
        reset_now = time.time()
        reset_floor = max(
            float(_system_ai_health.get("last_failure_at") or 0.0), reset_now
        )
        _system_ai_health["last_failure_at"] = reset_floor
        _system_ai_health["failure_timestamp_dirty"] = True
        db_reset_committed = False
        marker_cleanup_failed = False
        try:
            with _system_ai_failure_fence():
                reset_floor = max(_read_system_ai_failure_marker(), reset_floor)
                _system_ai_health["last_failure_at"] = reset_floor
                _write_system_ai_failure_marker(reset_floor)
                set_app_state_values(
                    {
                        SYSTEM_AI_ALERTED_STATE_KEY: "0",
                        SYSTEM_AI_ALERT_LAST_NOTIFIED_STATE_KEY: "0",
                        SYSTEM_AI_LAST_FAILURE_STATE_KEY: "0",
                        SYSTEM_AI_LAST_SUCCESS_STATE_KEY: "0",
                    }
                )
                db_reset_committed = True
                try:
                    _clear_system_ai_failure_marker()
                except Exception as exc:
                    marker_cleanup_failed = True
                    print(f"[system-ai] alert marker cleanup failed: {exc}")
        except Exception as exc:
            print(f"[system-ai] alert state reset failed: {exc}")
        if not db_reset_committed:
            return

        # The four-key DB reset is already committed. Clear the old incident
        # counters even if sidecar cleanup failed, while retaining a conservative
        # floor/dirty bit until a later failure or stable reconciliation.
        _system_ai_health.update(
            {
                "failures": 0,
                "alerted": False,
                "last_error": "",
                "jobs": [],
                "last_failure_at": reset_floor if marker_cleanup_failed else 0.0,
                "last_success_at": 0.0,
                "failure_timestamp_dirty": marker_cleanup_failed,
            }
        )


def _note_system_ai_success() -> None:
    """Record a real provider success and end the current failure streak.

    Recovery notification is deliberately left to the periodic stability
    check: call volume must never substitute for a quiet recovery interval.
    """
    with _system_ai_health_lock:
        current = time.time()
        _system_ai_health["last_success_at"] = current
        try:
            # Routed through the same cross-process fence as the failure marker
            # so both timestamps are written under the shared lock. The health
            # lock is already held here, and every other fence user acquires it
            # first too — health-lock then fence is the consistent order.
            with _system_ai_failure_fence():
                set_app_state(SYSTEM_AI_LAST_SUCCESS_STATE_KEY, str(current))
        except Exception as exc:
            print(f"[system-ai] success timestamp write failed: {exc}")
        _system_ai_health.update(
            {
                "failures": 0,
                "last_error": "",
                "jobs": [],
            }
        )


def _redact_secrets(value, *known_secrets: str) -> str:
    secrets = [secret for secret in known_secrets if secret]
    try:
        system_config = get_system_ai_config() or {}
        if system_config.get("api_key"):
            secrets.append(system_config["api_key"])
    except Exception:
        pass
    return _redact_api_error(value, *secrets)


def _note_system_ai_failure(job: str, error) -> None:
    """Count one failed system-AI call. Alerts every admin exactly once per
    outage: the flag only clears after a real success and the stability window,
    so a provider that fails every 30 seconds cannot flap notifications."""
    reason = _redact_secrets(error).strip()[:300]
    with _system_ai_health_lock:
        current = time.time()
        _system_ai_health["last_failure_at"] = current
        marker_ok = db_ok = False
        try:
            with _system_ai_failure_fence():
                _write_system_ai_failure_marker(current)
                marker_ok = True
                set_app_state(SYSTEM_AI_LAST_FAILURE_STATE_KEY, str(current))
                db_ok = True
        except Exception as exc:
            print(f"[system-ai] failure timestamp write failed: {exc}")
        _system_ai_health["failure_timestamp_dirty"] = not (marker_ok and db_ok)
        _system_ai_health["failures"] += 1
        _system_ai_health["last_error"] = reason
        if job not in _system_ai_health["jobs"]:
            _system_ai_health["jobs"].append(job)
        if (
            _system_ai_health["failures"] < SYSTEM_AI_FAILURE_ALERT_THRESHOLD
            or _system_ai_health["alerted"]
        ):
            return
        _system_ai_health["alerted"] = True
        failures = _system_ai_health["failures"]
        jobs = list(_system_ai_health["jobs"])
    # The persisted claim is what actually guarantees one alert per outage: a
    # restart empties the counter above, and without this the same outage would
    # alert again three failures later.
    claim = _claim_system_ai_alert()
    if claim != "notify":
        return
    body = (
        f"服务端 AI 已连续 {failures} 次调用失败，受影响的后台任务："
        f"{'、'.join(jobs)}。\n\n"
        f"最近一次失败原因：{reason or '未知原因'}\n\n"
        "请到 管理员设置 → 服务端 API 检查 Endpoint、API Key、模型和余额，"
        "可用「测试连接」验证。恢复后系统会再发一条通知。"
    )
    print(f"[system-ai] {failures} consecutive failures ({', '.join(jobs)}): {reason}")
    if _notify_admins("system_ai_failed", SYSTEM_AI_FAILURE_TITLE, body) == 0:
        # A zero means no durable in-app notice was created. Release rather
        # than cooldown so a later provider failure can retry delivery; an
        # email-only failure remains deduplicated by its separate alert path.
        _release_system_ai_alert()
    # The notification timestamp was persisted atomically with the claim, so no
    # separate post-delivery write is needed (see _claim_system_ai_alert).


def _maybe_recover_stale_system_ai_incident() -> bool:
    """Close a quiet incident after one success and the stability window."""
    try:
        # Keep the in-memory state transition serialized with timestamp writes.
        # The transaction helper commits before returning, so every path takes
        # the health lock before touching SQLite and no lock-order inversion is
        # introduced.
        with _system_ai_health_lock:
            with _system_ai_failure_fence():
                marker_failure = _read_system_ai_failure_marker()
                effective_failure = max(
                    marker_failure,
                    float(_system_ai_health.get("last_failure_at") or 0.0)
                    if _system_ai_health["failure_timestamp_dirty"]
                    else 0.0,
                )
                if _system_ai_health["failure_timestamp_dirty"]:
                    _write_system_ai_failure_marker(effective_failure)
                if effective_failure > 0:
                    advance_app_state_epoch(
                        SYSTEM_AI_LAST_FAILURE_STATE_KEY, effective_failure
                    )
                _system_ai_health["failure_timestamp_dirty"] = False
                prior_state = complete_app_state_incident_if_stable(
                    SYSTEM_AI_ALERTED_STATE_KEY,
                    SYSTEM_AI_ALERT_LAST_NOTIFIED_STATE_KEY,
                    SYSTEM_AI_LAST_FAILURE_STATE_KEY,
                    SYSTEM_AI_LAST_SUCCESS_STATE_KEY,
                    SYSTEM_AI_RECOVERY_STABILITY_SECONDS,
                    now=time.time(),
                )
            # Always sync in-memory state to the durable verdict. When another
            # process already closed the incident (prior_state == "0") this
            # process would otherwise keep a stale alerted=True and mute the next
            # real outage; clearing failures/last_error/jobs matches the durable
            # truth whether this helper closed the incident or found it closed.
            _system_ai_health.update(
                {
                    "failures": 0,
                    "alerted": False,
                    "last_error": "",
                    "jobs": [],
                }
            )
            if prior_state not in {"1", "2"}:
                return False
    except Exception as exc:
        print(f"[system-ai] stable recovery failed: {exc}")
        return False

    if prior_state == "1":
        _notify_admins(
            "system_ai_recovered",
            SYSTEM_AI_RECOVERED_TITLE,
            "服务端 AI 调用已恢复正常，自动摘要、翻译、标题精简和每日摘要等后台任务会继续运行。",
        )
    return True


def _compact_share_error(value: str) -> str:
    """Return a safe summary, never an arbitrary provider response body."""
    text = _redact_secrets(value or "connection test failed")

    status = re.search(r"\bAI API HTTP\s+([1-5]\d{2})\b", text, flags=re.IGNORECASE)
    if status:
        return f"AI API HTTP {status.group(1)}"
    if text == "AI not configured. Save API config first.":
        return text
    if text == "Connection test returned empty response":
        return text
    if text.startswith("无法连接 AI 服务"):
        return "无法连接 AI 服务"
    if text.startswith("连接 AI 服务超时"):
        return "连接 AI 服务超时"
    if text.startswith("AI 服务响应超时"):
        return "AI 服务响应超时"
    return "AI connectivity check failed"


def _notify_share_suspended(user_id: int, safe_error: str) -> None:
    _notify_user(
        user_id,
        "share_suspended",
        "共享 API 校验失败，共享已暂停",
        "系统对你配置的个人 AI API 做连通性校验时失败，共享访问已暂停；"
        "你的总开关和查看选项均已保留。\n\n"
        f"失败原因：{safe_error}\n\n"
        "请到 用户设置 → AI 更新配置。保存并校验成功后系统会自动恢复共享。",
    )


def _apply_share_connectivity_result(
    user_id: int,
    ok: bool,
    error: str = "",
    checked_at: str | None = None,
    config_revision: int | None = None,
) -> str:
    """Apply a probe only while the exact tested AI config is still current.

    A suspension CAS can lose to another current-revision probe. Re-read a
    bounded number of times so a later opposite result still owns the edge it
    actually applies, while opt-out and config changes remain terminal.
    """
    import datetime as _dt

    if config_revision is None:
        config_revision = (get_ai_config(user_id) or {}).get("revision", 0)
    try:
        config_revision = int(config_revision)
    except (TypeError, ValueError):
        return "validation_failed"

    checked_at = checked_at or _dt.datetime.now().isoformat(timespec="seconds")
    target_suspended = 0 if ok else 1
    safe_error = None if ok else _compact_share_error(error)
    was_suspended = False
    claimed = False
    for attempt in range(3):
        settings = get_user_settings(user_id) or {}
        if not _is_enabled_value(settings.get("share_ai_results")):
            return "not_opted_in"
        current_revision = settings.get("share_current_config_revision")
        try:
            current_revision = int(current_revision) if current_revision is not None else 0
        except (TypeError, ValueError):
            return "stale"
        if current_revision != config_revision:
            return "stale"

        was_suspended = _is_enabled_value(settings.get("share_suspended"))
        # A failed CAS followed by the same target means the competing probe
        # already applied this result. It is not another real edge.
        if attempt and int(was_suspended) == target_suspended:
            return "unchanged"
        claimed = apply_share_connectivity_transition(
            user_id,
            expected_suspended=int(was_suspended),
            expected_config_revision=config_revision,
            next_suspended=target_suspended,
            checked_at=checked_at,
            check_ok=int(ok),
            error=safe_error,
        )
        if claimed:
            break
    if not claimed:
        return "unchanged"

    if ok:
        if not was_suspended:
            return "unchanged"
        _notify_user(
            user_id,
            "share_restored",
            "共享 API 已恢复，共享状态已自动恢复",
            "系统已确认你的个人 AI API 恢复连通。\n\n"
            "「共享 AI 结果」及你此前选择的查看选项已自动恢复，无需手动重新开启。",
        )
        return "restored"

    if was_suspended:
        return "unchanged"
    _notify_share_suspended(user_id, safe_error)
    return "suspended"


def _apply_share_background_connectivity_result(
    user_id: int,
    ok: bool,
    error: str = "",
    checked_at: str | None = None,
    config_revision: int | None = None,
) -> str:
    """Apply scheduled probes with a two-consecutive-failure threshold."""
    import datetime as _dt

    if config_revision is None:
        config_revision = (get_ai_config(user_id) or {}).get("revision", 0)
    try:
        config_revision = int(config_revision)
    except (TypeError, ValueError):
        return "validation_failed"

    checked_at = checked_at or _dt.datetime.now().isoformat(timespec="seconds")
    if ok:
        return _apply_share_connectivity_result(
            user_id,
            True,
            checked_at=checked_at,
            config_revision=config_revision,
        )

    safe_error = _compact_share_error(error)
    transition = record_share_revalidation_failure(
        user_id,
        config_revision,
        checked_at,
        safe_error,
    )
    if transition == "suspended":
        _notify_share_suspended(user_id, safe_error)
    return transition


def _run_ai_share_revalidation_once():
    """Re-verify every opted-in user's own AI connectivity.

    A user's "共享 AI 结果" access is only granted after a live connectivity
    test at save time (see update_settings); this loop periodically re-tests
    it so a key that later expires/runs out of credit doesn't leave shared
    content permanently accessible. Opted-in users remain scheduled while
    suspended, so a later successful check restores their saved preferences.
    """
    user_ids = get_users_with_share_enabled()
    cycle_checked_at = datetime.now().isoformat(timespec="microseconds")
    for index, user_id in enumerate(user_ids):
        try:
            config = get_ai_config(user_id)
            body, status = _run_ai_connection_test(config)
            _apply_share_background_connectivity_result(
                user_id,
                status == 200,
                body.get("error", "") if status != 200 else "",
                checked_at=cycle_checked_at,
                config_revision=(config or {}).get("revision", 0),
            )
        except Exception as e:
            print(f"[share-revalidation] user_id={user_id} failed: {e}")
        if index + 1 < len(user_ids):
            time.sleep(0.5)  # spread requests out instead of bursting every provider at once


def _ai_share_revalidation_loop():
    time.sleep(60)
    while True:
        try:
            _run_ai_share_revalidation_once()
        except Exception as e:
            print(f"[share-revalidation] Error in loop: {e}")
        time.sleep(AI_SHARE_REVALIDATION_INTERVAL_SECONDS)


@app.route("/ai/daily-summary", methods=["POST"])
@require_role("user", "admin")
def ai_daily_summary():
    """Retired: the daily summary is generated by the server on a fixed schedule.

    It used to run an AI call against the *calling user's* own API key, which
    meant the content of a shared, site-wide summary depended on whoever
    happened to click first. Generation now happens once per day from the
    admin-configured system AI (see _broadcast_daily_summary), so there is
    nothing for a client to trigger. Kept as an explicit 403 rather than
    deleted: a browser running a cached build still posts here, and a clear
    message beats a bare 404 or, worse, a route that silently reappears.
    """
    return jsonify({
        "error": (
            f"每日摘要由服务器在北京时间每天 "
            f"{DAILY_SUMMARY_HOUR:02d}:{DAILY_SUMMARY_MINUTE:02d} 统一生成，不支持手动触发"
        ),
        "status": "scheduled",
        "generate_at": f"{DAILY_SUMMARY_HOUR:02d}:{DAILY_SUMMARY_MINUTE:02d}",
    }), 403


@app.route("/ai/daily-summary/today", methods=["GET"])
@require_role("user", "admin")
def ai_daily_summary_today():
    """Today's shared summary, or why it isn't showing yet.

    The client renders four states off `status`, so the "not yet" cases are
    distinguished here rather than in the UI: `scheduled` (today's generation
    time hasn't arrived), `generating` (it has, but the summary isn't stored
    yet), `completed`, `unavailable` (the day ended with nothing produced —
    typically no articles, or the system AI isn't configured).
    """
    today_str = _today_str()
    generate_at = f"{DAILY_SUMMARY_HOUR:02d}:{DAILY_SUMMARY_MINUTE:02d}"

    shared = _get_daily_summary_global_cache(today_str)
    if shared:
        return jsonify({
            "status": "completed",
            "date": today_str,
            "generate_at": generate_at,
            "summary": shared["summary"],
            "article_count": shared["article_count"],
            "stats": shared["stats"],
            "updated_at": shared["updated_at"],
        })

    # A day that has failed at least once reports differently to admins: they get
    # the reason and the retry control, everyone else gets the ordinary
    # "generating"/"unavailable" wording — a failing system AI key is an ops
    # detail, not something to show every reader.
    failure = _get_daily_summary_failure(today_str)
    if failure and int(failure.get("attempts") or 0) > 0:
        given_up = bool(int(failure.get("given_up") or 0))
        if getattr(g, "user_role", "") == "admin":
            return jsonify({
                "status": "failed" if given_up else "retrying",
                "date": today_str,
                "generate_at": generate_at,
                "error": failure.get("last_error") or "",
                "attempts": int(failure.get("attempts") or 0),
                "max_retries": DAILY_SUMMARY_MAX_RETRIES,
                "next_retry_at": failure.get("next_retry_at"),
                "retry_interval_minutes": DAILY_SUMMARY_RETRY_INTERVAL_SECONDS // 60,
                "can_retry": True,
            })
        return jsonify({
            "status": "unavailable" if given_up else "generating",
            "date": today_str,
            "generate_at": generate_at,
        })

    now = _beijing_now()
    target_minutes = DAILY_SUMMARY_HOUR * 60 + DAILY_SUMMARY_MINUTE
    now_minutes = now.hour * 60 + now.minute
    if now_minutes < target_minutes:
        status = "scheduled"
    elif now_minutes < target_minutes + max(DAILY_SUMMARY_WINDOW_MINUTES, 1) + 5:
        # Inside (or just past) the scheduler's send window: a tick is either
        # running or about to. Slack on the tail so a slow AI call reads as
        # "still working" rather than flipping straight to unavailable.
        status = "generating"
    else:
        status = "unavailable"
    return jsonify({"status": status, "date": today_str, "generate_at": generate_at})


@app.route("/ai/daily-summary/retry", methods=["POST"])
@require_role("admin")
def ai_daily_summary_retry():
    """Admin-triggered retry of today's failed generation.

    Not a general "generate now" button: it refuses unless the day actually has
    a failure record, which keeps the retired manual-generation path retired —
    the only way to reach the AI from a client is to re-run a run that already
    failed. A successful retry clears the failure record and delivers on both
    channels exactly as the scheduled run would; a failed one restarts the
    automatic retry chain from attempt 1.
    """
    today_str = _today_str()
    failure = _get_daily_summary_failure(today_str)
    if not failure or int(failure.get("attempts") or 0) <= 0:
        return jsonify({
            "error": "今日每日摘要没有失败记录，无需重试",
            "status": "not_failed",
        }), 409

    _clear_daily_summary_failure(today_str)
    result = _broadcast_daily_summary(force=False, bypass_window=True)
    if result.get("status") == "error":
        state = _get_daily_summary_failure(today_str) or {}
        return jsonify({
            "status": "failed" if int(state.get("given_up") or 0) else "retrying",
            "error": result.get("reason") or "",
            "attempts": int(state.get("attempts") or 0),
            "can_retry": True,
        }), 502
    if result.get("status") == "skipped" and result.get("reason") == "generation already running":
        return jsonify({"status": "retrying", "error": "", "message": "正在生成，请稍候"}), 202
    return jsonify({"status": "completed", "message": "每日摘要生成成功"})


def _beijing_now():
    """Explicit UTC+8 'now' — independent of the container's TZ env var
    (unlike SQLite's own datetime('now'), which is always UTC)."""
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=8)))


def _today_str() -> str:
    return _beijing_now().strftime("%Y-%m-%d")


def _digest_window(date_str: str) -> tuple[int, int]:
    """Half-open Beijing cutoff window; late arrivals enter the next run."""
    beijing = _dt.timezone(_dt.timedelta(hours=8))
    day = _dt.date.fromisoformat(date_str)
    cutoff = _dt.datetime.combine(day, _dt.time(DAILY_SUMMARY_HOUR, DAILY_SUMMARY_MINUTE), beijing)
    return int((cutoff - _dt.timedelta(days=1)).timestamp()), int(cutoff.timestamp())


def _digest_category_definitions() -> list[dict]:
    if not os.path.exists(NEWS_DB):
        return [{"category": UNCATEGORIZED, "label": "待分类"}]
    with _news_db_conn() as conn:
        conn.row_factory = sqlite3.Row
        return category_definitions(conn)


def _previous_digest_signals(date_str: str) -> list[dict]:
    if not os.path.exists(NEWS_DB):
        return []
    _init_daily_summary_global_table()
    previous = (_dt.date.fromisoformat(date_str) - _dt.timedelta(days=1)).isoformat()
    with _news_db_conn() as conn:
        rows = conn.execute(
            "SELECT event_json FROM daily_digest_events WHERE date = ? AND selected = 1", (previous,)
        ).fetchall()
    result = []
    for row in rows:
        try:
            result.append(json.loads(row[0]))
        except (TypeError, json.JSONDecodeError):
            continue
    return result


@app.route("/admin/daily-digest/events", methods=["GET"])
@require_role("admin")
def admin_daily_digest_events():
    """Explain why each event entered or missed the saved digest."""
    date_str = request.args.get("date") or _today_str()
    try:
        _dt.date.fromisoformat(date_str)
    except ValueError:
        return jsonify({"error": "invalid date"}), 400
    if not os.path.exists(NEWS_DB):
        return jsonify({"date": date_str, "events": []})
    _init_daily_summary_global_table()
    with _news_db_conn() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT representative_article_id, event_json, score, reason, selected "
            "FROM daily_digest_events WHERE date = ? "
            "ORDER BY selected DESC, score DESC, representative_article_id", (date_str,)
        ).fetchall()
    return jsonify({"date": date_str, "events": [
        {**dict(row), "event": json.loads(row["event_json"])} for row in rows
    ]})


def _init_daily_summary_global_table():
    if not os.path.exists(NEWS_DB):
        return
    try:
        with _news_db_conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS daily_summary_global (
                    date          TEXT PRIMARY KEY,
                    summary       TEXT NOT NULL,
                    article_count INTEGER NOT NULL DEFAULT 0,
                    stats         TEXT NOT NULL DEFAULT '{}',
                    updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
                )
            """)
            conn.execute("""CREATE TABLE IF NOT EXISTS daily_digest_events (
                date TEXT NOT NULL,
                representative_article_id INTEGER NOT NULL,
                event_json TEXT NOT NULL,
                score INTEGER NOT NULL,
                reason TEXT NOT NULL,
                selected INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(date, representative_article_id)
            )""")
            conn.commit()
    except Exception as e:
        print(f"[daily-summary] global cache table init failed: {e}")


def _get_daily_summary_global_cache(date_str: str) -> dict | None:
    if not os.path.exists(NEWS_DB):
        return None
    try:
        _init_daily_summary_global_table()
        with _news_db_conn() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT summary, article_count, stats, updated_at "
                "FROM daily_summary_global WHERE date = ?",
                (date_str,),
            ).fetchone()
        if not row:
            return None
        data = dict(row)
        try:
            data["stats"] = json.loads(data.get("stats") or "{}")
        except (json.JSONDecodeError, TypeError):
            data["stats"] = {}
        return data
    except Exception as e:
        print(f"[daily-summary] global cache read failed: {e}")
        return None


def _save_daily_summary_global_cache(date_str: str, summary: str,
                                     article_count: int, stats: dict,
                                     events: list[dict] | None = None) -> bool:
    if not os.path.exists(NEWS_DB):
        return False
    try:
        _init_daily_summary_global_table()
        with _news_db_conn() as conn:
            conn.execute(
                "INSERT INTO daily_summary_global "
                "(date, summary, article_count, stats, updated_at) "
                "VALUES (?, ?, ?, ?, datetime('now')) "
                "ON CONFLICT(date) DO UPDATE SET "
                "summary = excluded.summary, "
                "article_count = excluded.article_count, "
                "stats = excluded.stats, "
                "updated_at = datetime('now')",
                (date_str, summary, article_count, json.dumps(stats or {}, ensure_ascii=False)),
            )
            if events is not None:
                conn.execute("DELETE FROM daily_digest_events WHERE date = ?", (date_str,))
                conn.executemany(
                    "INSERT INTO daily_digest_events "
                    "(date, representative_article_id, event_json, score, reason, selected) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    [(date_str, group["representative"]["id"],
                      json.dumps({**group["signal"],
                                  "article_ids": [a["id"] for a in group["articles"]],
                                  "publisher_count": group["publisher_count"],
                                  "selection_basis": group.get("selection_basis", "threshold")},
                                 ensure_ascii=False), group["score"],
                      group["reason"], int(not group["reason"])) for group in events],
                )
            conn.commit()
        return True
    except Exception as e:
        print(f"[daily-summary] global cache write failed: {e}")
        return False


# Why the reason travels in a module global instead of the return value: the
# generator's contract (dict | None) is depended on by callers and tests, and the
# only consumer of the reason is _broadcast_daily_summary, which calls it
# synchronously on the same thread one line earlier. Generation is serialized by
# _daily_summary_generation_lock, so there is no second writer to race with.
_daily_summary_last_error = ""


def _set_daily_summary_error(reason: str) -> None:
    global _daily_summary_last_error
    # Provider errors can carry a whole response body; keep the stored/emailed
    # reason short enough to read at a glance.
    text = re.sub(r"\s+", " ", str(reason or "")).strip()
    _daily_summary_last_error = text[:400]


def _generate_daily_summary_global(date_str: str) -> dict | None:
    """Return (generating if needed) the one shared daily summary for date_str,
    using the admin-configured system AI. Returns None if it can't be produced
    yet (no system AI configured, or no articles for that date); the reason is
    left in _daily_summary_last_error for the caller to record and report."""
    _set_daily_summary_error("")
    cached = _get_daily_summary_global_cache(date_str)
    if cached:
        return cached

    sys_config = get_system_ai_config()
    if not sys_config or not sys_config.get("enabled") or not sys_config.get("api_key"):
        print("[daily-summary] system AI not configured (管理员设置→服务端API), cannot generate")
        _set_daily_summary_error("系统 AI 未配置或未启用（管理员设置 → 服务端 API）")
        return None

    articles = _fetch_articles_by_date(date_str, include_shared_summary=True)
    if not articles:
        print(f"[daily-summary] no articles for {date_str}")
        _set_daily_summary_error(f"{date_str} 当日没有可用于生成摘要的文章")
        return None

    try:
        svc = _SystemAIService(
            "每日摘要",
            api_key=sys_config["api_key"],
            endpoint=sys_config["endpoint"],
            model=sys_config["model"],
            provider_type=sys_config.get("provider_type", "openai"),
        )
        start, cutoff = _digest_window(date_str)
        definitions = _digest_category_definitions()
        missing = [article for article in articles if not article.get("digest_signals")]
        signal_failures = 0
        for offset in range(0, len(missing), 20):
            batch = missing[offset:offset + 20]
            try:
                generated = svc.batch_digest_signals(batch)
            except Exception as exc:
                app.logger.warning("Daily signal batch failed: %s", _redact_api_error(str(exc)))
                generated = {}
            updates = []
            for article in batch:
                raw = generated.get(article["id"])
                article["digest_signal_fallback"] = raw is None
                if raw is None:
                    signal_failures += 1
                article["digest_signals"] = normalize_signals(article, raw)
                if raw is not None:
                    updates.append((article["id"], article["digest_signals"]))
            _save_digest_signals_batch(updates)
        for article in articles:
            if article.get("digest_signals"):
                article["digest_signals"] = normalize_signals(article, article["digest_signals"])
            else:
                article["digest_signals"] = normalize_signals(article, None)
        # Normalized defaults can render a digest, but they do not establish
        # relative importance. Retry the run if a large share of AI signals is
        # missing; otherwise a few cached high scores can produce a 2-item
        # digest from hundreds of articles and get cached as today's result.
        if len(articles) >= 20 and signal_failures > max(5, len(articles) // 5):
            raise RuntimeError(
                f"event signal coverage too low: {len(articles) - signal_failures}/"
                f"{len(articles)} articles"
            )
        if signal_failures == len(articles):
            raise RuntimeError("AI event signal generation failed")
        groups = group_events(articles)
        selected = rank_events(
            groups, _previous_digest_signals(date_str), cutoff=cutoff,
            min_events=MAX_DIGEST_EVENTS,
        )
        written = {}
        writing_failures = 0
        for offset in range(0, len(selected), 10):
            try:
                written.update(svc.write_digest_events(selected[offset:offset + 10]))
            except Exception as exc:
                writing_failures += 1
                app.logger.warning("Daily digest writing batch failed: %s", _redact_api_error(str(exc)))
        summary = render_digest(selected, definitions, written)
        stats = {"total_articles": len(articles), "events": len(groups),
                 "articles_after_dedup": len(groups),
                 "selected_events": len(selected), "missing_signals": len(missing),
                 "signal_fallbacks": signal_failures, "writing_fallback_batches": writing_failures,
                 "selected_articles_with_summary": sum(
                     bool(group["representative"].get("summary")) for group in selected
                 ),
                 "relative_ranked_events": sum(
                     group.get("selection_basis") == "relative ranking" for group in selected
                 ),
                 "window_start": start, "window_end": cutoff, "digest_item_count": len(selected)}
        if not _save_daily_summary_global_cache(date_str, summary, len(articles), stats, groups):
            raise RuntimeError("Could not persist daily digest and event audit")
        return {
            "summary": summary,
            "article_count": len(articles),
            "stats": stats,
        }
    except Exception as exc:
        app.logger.exception("Daily summary generation failed")
        _set_daily_summary_error(
            "AI 生成失败：" + (_redact_api_error(str(exc)) or "未知错误")
        )
        return None


_auto_summary_lock = threading.Lock()
_auto_translation_lock = threading.Lock()
_translation_repair_lock = threading.Lock()
_translation_repair_cursor: tuple[int, int] | None = None
_translation_repair_cursor_db_path: str | None = None
_auto_title_process_lock = threading.Lock()
_auto_source_classify_lock = threading.Lock()
_legacy_admin_source_settings_lock = threading.Lock()
_legacy_admin_source_settings_promoted = False


def _init_daily_summary_sends_table():
    if not os.path.exists(NEWS_DB):
        return
    try:
        with _news_db_conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS daily_summary_sends (
                    date       TEXT NOT NULL,
                    user_id    INTEGER NOT NULL,
                    email      TEXT NOT NULL,
                    status     TEXT NOT NULL DEFAULT 'sent',
                    sent_at    TEXT NOT NULL DEFAULT (datetime('now')),
                    PRIMARY KEY (date, user_id)
                )
            """)
            conn.commit()
    except Exception as e:
        print(f"[daily-summary] sends table init failed: {e}")


def _get_daily_summary_sent_user_ids(date_str: str) -> set[int]:
    """user_ids already *successfully* sent this date's summary — failed
    attempts are excluded on purpose so the next run retries them."""
    if not os.path.exists(NEWS_DB):
        return set()
    try:
        _init_daily_summary_sends_table()
        with _news_db_conn() as conn:
            rows = conn.execute(
                "SELECT user_id FROM daily_summary_sends WHERE date = ? AND status = 'sent'",
                (date_str,),
            ).fetchall()
        return {int(r[0]) for r in rows}
    except Exception as e:
        print(f"[daily-summary] sends read failed: {e}")
        return set()


def _record_daily_summary_send(date_str: str, user_id: int, email: str, status: str):
    if not os.path.exists(NEWS_DB):
        return
    try:
        _init_daily_summary_sends_table()
        with _news_db_conn() as conn:
            conn.execute(
                "INSERT INTO daily_summary_sends (date, user_id, email, status, sent_at) "
                "VALUES (?, ?, ?, ?, datetime('now')) "
                "ON CONFLICT(date, user_id) DO UPDATE SET "
                "email = excluded.email, status = excluded.status, sent_at = excluded.sent_at",
                (date_str, user_id, email, status),
            )
            conn.commit()
    except Exception as e:
        print(f"[daily-summary] sends write failed: {e}")


def _init_daily_summary_failures_table():
    if not os.path.exists(NEWS_DB):
        return
    try:
        with _news_db_conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS daily_summary_failures (
                    date            TEXT PRIMARY KEY,
                    attempts        INTEGER NOT NULL DEFAULT 0,
                    last_error      TEXT NOT NULL DEFAULT '',
                    last_attempt_at INTEGER NOT NULL DEFAULT 0,
                    next_retry_at   INTEGER,
                    given_up        INTEGER NOT NULL DEFAULT 0,
                    alerted         INTEGER NOT NULL DEFAULT 0
                )
            """)
            conn.commit()
    except Exception as e:
        print(f"[daily-summary] failures table init failed: {e}")


def _epoch_now() -> int:
    """Wall-clock seconds, isolated so retry scheduling can be driven in tests."""
    return int(time.time())


def _get_daily_summary_failure(date_str: str) -> dict | None:
    """This date's failure record, or None if the day has not failed (yet)."""
    if not os.path.exists(NEWS_DB):
        return None
    try:
        _init_daily_summary_failures_table()
        with _news_db_conn() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT date, attempts, last_error, last_attempt_at, next_retry_at, "
                "given_up, alerted FROM daily_summary_failures WHERE date = ?",
                (date_str,),
            ).fetchone()
        return dict(row) if row else None
    except Exception as e:
        print(f"[daily-summary] failure read failed: {e}")
        return None


def _record_daily_summary_failure(date_str: str, reason: str) -> dict:
    """Count one failed attempt for date_str and schedule (or stop) the retry.

    Attempt 1 is the scheduled 21:00 run; each subsequent attempt is a retry
    DAILY_SUMMARY_RETRY_INTERVAL_SECONDS later. Once attempts exceed
    1 + DAILY_SUMMARY_MAX_RETRIES the day is given up on and next_retry_at is
    cleared, which is what makes the admin alert fire exactly once.

    Persisted rather than kept in memory so a restart mid-retry-chain neither
    restarts the count from zero (endless retrying) nor loses the failure state
    the ✨ panel reads to show the admin's retry button.
    """
    now = _epoch_now()
    state = {
        "date": date_str,
        "attempts": 1,
        "last_error": reason,
        "last_attempt_at": now,
        "next_retry_at": now + DAILY_SUMMARY_RETRY_INTERVAL_SECONDS,
        "given_up": 0,
        "alerted": 0,
    }
    if not os.path.exists(NEWS_DB):
        return state
    try:
        _init_daily_summary_failures_table()
        with _news_db_conn() as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT attempts, alerted FROM daily_summary_failures WHERE date = ?",
                (date_str,),
            ).fetchone()
            attempts = int(row["attempts"]) + 1 if row else 1
            alerted = int(row["alerted"]) if row else 0
            given_up = 1 if attempts >= 1 + DAILY_SUMMARY_MAX_RETRIES else 0
            next_retry_at = None if given_up else now + DAILY_SUMMARY_RETRY_INTERVAL_SECONDS
            conn.execute(
                "INSERT INTO daily_summary_failures "
                "(date, attempts, last_error, last_attempt_at, next_retry_at, given_up, alerted) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(date) DO UPDATE SET "
                "attempts = excluded.attempts, last_error = excluded.last_error, "
                "last_attempt_at = excluded.last_attempt_at, "
                "next_retry_at = excluded.next_retry_at, given_up = excluded.given_up",
                (date_str, attempts, reason, now, next_retry_at, given_up, alerted),
            )
            # Only the current day is ever consulted; keep a short tail for support
            # questions ("did last Tuesday fail?") and drop the rest.
            conn.execute("DELETE FROM daily_summary_failures WHERE date < ?",
                         ((_beijing_now() - timedelta(days=7)).strftime("%Y-%m-%d"),))
            conn.commit()
        state.update({
            "attempts": attempts,
            "next_retry_at": next_retry_at,
            "given_up": given_up,
            "alerted": alerted,
        })
    except Exception as e:
        print(f"[daily-summary] failure write failed: {e}")
    return state


def _clear_daily_summary_failure(date_str: str) -> None:
    """Drop the failure record — a later attempt succeeded, or an admin asked
    for a manual retry and the count should start over."""
    if not os.path.exists(NEWS_DB):
        return
    try:
        _init_daily_summary_failures_table()
        with _news_db_conn() as conn:
            conn.execute("DELETE FROM daily_summary_failures WHERE date = ?", (date_str,))
            conn.commit()
    except Exception as e:
        print(f"[daily-summary] failure clear failed: {e}")


def _claim_daily_summary_alert(date_str: str) -> bool:
    """Flip alerted 0→1 for this date, returning True only for the caller that
    won the flip. Guards against a second alert if two ticks (or a restart
    racing the tick that gave up) reach the notification step for one day."""
    if not os.path.exists(NEWS_DB):
        return True
    try:
        _init_daily_summary_failures_table()
        with _news_db_conn() as conn:
            cur = conn.execute(
                "UPDATE daily_summary_failures SET alerted = 1 "
                "WHERE date = ? AND alerted = 0",
                (date_str,),
            )
            conn.commit()
            claimed = cur.rowcount > 0
        return claimed
    except Exception as e:
        print(f"[daily-summary] alert claim failed: {e}")
        return False


def _release_daily_summary_alert(date_str: str) -> None:
    """Release a claim when no administrator received the in-app alert."""
    if not os.path.exists(NEWS_DB):
        return
    try:
        _init_daily_summary_failures_table()
        with _news_db_conn() as conn:
            conn.execute(
                "UPDATE daily_summary_failures SET alerted = 0 "
                "WHERE date = ? AND alerted = 1",
                (date_str,),
            )
            conn.commit()
    except Exception as e:
        print(f"[daily-summary] alert claim release failed: {e}")


def _daily_summary_retry_due(state: dict | None, now: int | None = None) -> bool:
    """True when an auto-retry for a failed day is owed right now."""
    if not state or int(state.get("given_up") or 0):
        return False
    next_retry_at = state.get("next_retry_at")
    if next_retry_at is None:
        return False
    return (now if now is not None else _epoch_now()) >= int(next_retry_at)


def _alert_admins_daily_summary_failure(date_str: str, state: dict) -> int:
    """Tell every admin the day's summary is not coming, and why.

    Both channels go through _notify_user, so the alert lands in 头像菜单 →
    我的通知 even when RESEND_API_KEY is unset or the mail send fails.
    """
    attempts = int(state.get("attempts") or 0)
    reason = state.get("last_error") or "未知原因"
    body = (
        f"{date_str} 的每日摘要生成失败，已重试 {max(attempts - 1, 0)} 次仍未成功，"
        f"今日不再自动重试。\n\n"
        f"失败原因：{reason}\n\n"
        f"共尝试 {attempts} 次（首次为北京时间 "
        f"{DAILY_SUMMARY_HOUR:02d}:{DAILY_SUMMARY_MINUTE:02d} 的定时生成，"
        f"之后每 {DAILY_SUMMARY_RETRY_INTERVAL_SECONDS // 60} 分钟重试一次）。\n\n"
        f"可在首页 ✨ 每日摘要面板点击「重试生成」手动再试一次。"
    )
    notified = _notify_admins("daily_summary_failed", DAILY_SUMMARY_FAILURE_TITLE, body)
    print(f"[daily-summary] {date_str} gave up after {attempts} attempt(s); "
          f"alerted {notified} admin(s): {reason}")
    return notified


DAILY_SUMMARY_NOTIFICATION_TITLE = "RayNews每日摘要"
DAILY_SUMMARY_FAILURE_TITLE = "每日摘要生成失败"


def _daily_summary_broadcast_id(date_str: str) -> str:
    """Claim key for one date's in-app fan-out (see publish_broadcast_atomically)."""
    return f"daily-summary-{date_str}"


def _deliver_daily_summary_inapp(date_str: str, result: dict) -> dict:
    """Drop the day's summary into every opted-in user's notification list.

    Delivery is idempotent on the date-derived broadcast id, which matters here:
    the scheduler ticks once a minute across a ten-minute window and a restart
    inside that window replays the same date. The claim row makes every tick
    after the first a no-op instead of a second copy in everyone's list.

    Independent of the email leg by design — a user with no email address
    configured, or a deployment with no RESEND_API_KEY at all, still gets the
    in-app copy.
    """
    try:
        user_ids = get_daily_summary_inapp_user_ids()
        if not user_ids:
            return {"status": "skipped", "reason": "no in-app recipients", "recipients": 0}
        published, info = publish_broadcast_atomically(
            user_ids,
            _daily_summary_broadcast_id(date_str),
            DAILY_SUMMARY_NOTIFICATION_TITLE,
            result["summary"],
            # The summary is Markdown (with in-app article links); the client
            # renders it through the same escape → markdown → sanitize pipeline
            # it uses for translated article bodies.
            "markdown",
            email=False,
            ntype="daily_summary",
        )
        if not published:
            return {"status": "skipped", "reason": "already delivered today",
                    "recipients": int(info.get("recipients") or 0)}
        print(f"[scheduler] Daily summary in-app delivery for {date_str}: {len(user_ids)} recipient(s)")
        return {"status": "ok", "recipients": len(user_ids)}
    except Exception:
        # Never let the in-app leg take the email leg down with it.
        app.logger.exception("In-app daily summary delivery failed")
        return {"status": "error", "reason": "in-app delivery failed", "recipients": 0}


_daily_summary_generation_lock = threading.Lock()


def _broadcast_daily_summary(force: bool = False, bypass_window: bool = False) -> dict:
    """Generate the one shared daily summary and deliver it on both channels.

    Runs automatically at DAILY_SUMMARY_HOUR:MINUTE Beijing time; `force=True`
    (admin manual trigger) bypasses the time window and resends to every email
    subscriber regardless of history.

    Generation happens before either channel is consulted. It used to sit behind
    the RESEND_API_KEY and "any email subscribers?" checks, which was fine when
    email was the only channel — but the in-app copy goes to everyone by
    default, so a deployment with no mail configured must still produce and
    deliver the summary.

    Email send state is persisted in daily_summary_sends (date, user_id) rather
    than kept in memory: an in-memory "already sent today" set is lost on every
    restart, which could cause a duplicate broadcast if the process restarts
    inside the same day's send window. Persisting per-recipient status also
    lets a transient per-recipient failure (e.g. one bad email) get retried
    on the next scheduler tick instead of being silently skipped for the rest
    of the day. The in-app leg uses its own all-or-nothing claim row instead —
    it fans out in a single transaction, so it has no partial state to resume.
    """
    now = _beijing_now()
    today_str = now.strftime("%Y-%m-%d")

    if not force and not bypass_window:
        target_minutes = DAILY_SUMMARY_HOUR * 60 + DAILY_SUMMARY_MINUTE
        now_minutes = now.hour * 60 + now.minute
        diff = now_minutes - target_minutes
        if diff < 0 or diff >= DAILY_SUMMARY_WINDOW_MINUTES:
            return {"status": "skipped", "reason": "outside daily send window"}

    # A retry tick, an admin's manual retry and the scheduled run can all land on
    # the same minute; only one may hold an AI call for the day at a time.
    if not _daily_summary_generation_lock.acquire(blocking=False):
        return {"status": "skipped", "reason": "generation already running"}
    try:
        result = _generate_daily_summary_global(today_str)
        if not result:
            reason = _daily_summary_last_error or "生成失败（请检查 管理员设置 → 服务端 API，或当日是否有文章）"
            if force:
                # An admin's ad-hoc resend (管理员设置 → 立即发送) is not the day's
                # scheduled run: a failed one reports back to the caller and stops
                # there. Letting it seed the retry record would start a half-hour
                # retry chain — and eventually a failure alert to every admin —
                # for what was a manual experiment at an arbitrary hour.
                return {"status": "error", "reason": reason}
            state = _record_daily_summary_failure(today_str, reason)
            if int(state.get("given_up") or 0):
                # Claim the daily alert FIRST: it is the atomic gate on news.db.
                # Checking the system-AI incident before claiming left a
                # read+claim TOCTOU across two DBs — a concurrent system-AI
                # claim between them sent both alerts to the same admins.
                if not _claim_daily_summary_alert(today_str):
                    pass  # another tick already owns today's daily alert
                elif _system_ai_incident_is_notified():
                    _release_daily_summary_alert(today_str)
                    print(
                        f"[daily-summary] {today_str} gave up after "
                        f"{int(state.get('attempts') or 0)} attempt(s); suppressed "
                        "because the system-AI incident is already notified"
                    )
                elif _alert_admins_daily_summary_failure(today_str, state) == 0:
                    _release_daily_summary_alert(today_str)
            return {"status": "error", "reason": reason,
                    "attempts": state.get("attempts"),
                    "given_up": bool(state.get("given_up"))}

        _clear_daily_summary_failure(today_str)
        inapp = _deliver_daily_summary_inapp(today_str, result)
        email = _deliver_daily_summary_email(today_str, result, force=force)
        return {**email, "inapp": inapp}
    finally:
        _daily_summary_generation_lock.release()


def _deliver_daily_summary_email(date_str: str, result: dict, force: bool = False) -> dict:
    """Email the already-generated summary to every opted-in subscriber."""
    import json as _json
    from notifier import send_daily_summary_email

    today_str = date_str
    resend_api_key = os.environ.get("RESEND_API_KEY", "")
    if not resend_api_key:
        print("[scheduler] RESEND_API_KEY not set, skipping")
        return {"status": "skipped", "reason": "RESEND_API_KEY not set"}

    try:
        db = get_db()
        rows = db.execute(
            "SELECT user_id, notification_config FROM user_settings WHERE daily_summary_enabled = 1"
        ).fetchall()
    except Exception:
        app.logger.exception("Daily summary recipient lookup failed")
        return {"status": "error", "reason": "recipient lookup failed"}

    recipients = {}  # user_id -> to_email
    for row in rows:
        settings = dict(row)
        nc = settings.get("notification_config", "{}")
        if isinstance(nc, str):
            try:
                nc = _json.loads(nc)
            except (_json.JSONDecodeError, TypeError):
                nc = {}
        to_email = _resend_to_email(nc)
        if to_email and is_valid_email(to_email):
            recipients[int(settings["user_id"])] = to_email

    print(f"[scheduler] Daily summary broadcast for {today_str}: {len(recipients)} subscriber(s)")
    if not recipients:
        return {"status": "skipped", "reason": "no subscribers"}

    already_sent = set() if force else _get_daily_summary_sent_user_ids(today_str)
    pending = {uid: email for uid, email in recipients.items() if uid not in already_sent}
    if not pending:
        return {"status": "skipped", "reason": "already sent today"}

    sent = 0
    for user_id, to_email in pending.items():
        try:
            # Idempotency key = one send per (date, user). If a prior tick's send
            # actually reached Resend but its response never got back to us (so it
            # was recorded "failed" and retried this tick), Resend replays the
            # original instead of delivering a duplicate.
            send_daily_summary_email(
                resend_api_key, to_email, result["summary"], result["stats"],
                idempotency_key=f"daily-{today_str}-{user_id}",
            )
            _record_daily_summary_send(today_str, user_id, to_email, "sent")
            sent += 1
        except Exception as e:
            print(f"[scheduler] send to user {user_id} ({to_email}) failed: {e}")
            _record_daily_summary_send(today_str, user_id, to_email, "failed")

    print(f"[scheduler] Daily summary broadcast for {today_str}: sent to {sent}/{len(pending)} pending "
          f"({len(recipients)} total subscriber(s))")
    return {"status": "ok", "sent": sent, "pending": len(pending), "subscribers": len(recipients), "date": today_str}


def _send_daily_summaries():
    """One scheduler tick: either the scheduled run, or a due retry.

    Retries have to live outside _broadcast_daily_summary's send window — the
    window is ten minutes wide while the retry chain runs for half an hour — so
    the decision of *whether* this tick may generate is made here, and the
    window check is bypassed once the day has a failure record.
    """
    today_str = _today_str()
    state = _get_daily_summary_failure(today_str)
    if state and int(state.get("attempts") or 0) > 0:
        if int(state.get("given_up") or 0):
            return {"status": "skipped", "reason": "gave up for today"}
        if not _daily_summary_retry_due(state):
            return {"status": "skipped", "reason": "waiting for next retry"}
        print(f"[scheduler] Daily summary retry #{int(state['attempts'])} for {today_str}")
        return _broadcast_daily_summary(force=False, bypass_window=True)
    return _broadcast_daily_summary(force=False)


def _daily_summary_loop():
    """Background loop: check every 60 seconds."""
    import time as _time
    _time.sleep(15)  # initial delay to let app start fully
    while True:
        try:
            _send_daily_summaries()
        except Exception as e:
            print(f"[scheduler] Error in loop: {e}")
        try:
            _maybe_recover_stale_system_ai_incident()
        except Exception as e:
            print(f"[scheduler] System AI stable recovery failed: {e}")
        try:
            prune_access_log()
        except Exception as e:
            print(f"[scheduler] Access log cleanup failed: {e}")
        _time.sleep(60)


def _system_auto_config(*flag_columns: str) -> dict | None:
    """Build a single synthetic config for background auto jobs.

    Auto-summary/auto-translate/auto-title-process are admin-only: the flags
    still live on the triggering admin's user_settings row (so the admin UI
    stays simple), but the actual AI credentials always come from the
    admin-managed system_ai_config, never from any individual user's own key.
    Returns None unless some admin has one of the given flags on AND the
    system AI config is enabled with a key.
    """
    try:
        db = get_db()
        cond = " OR ".join(f"s.{col} = 1" for col in flag_columns)
        row = db.execute(
            "SELECT s.user_id, s.auto_translate_title, s.auto_translate_content, "
            "s.auto_title_summary_enabled, s.auto_summary_enabled "
            "FROM user_settings s JOIN users u ON u.id = s.user_id "
            f"WHERE u.role = 'admin' AND ({cond}) LIMIT 1"
        ).fetchone()
        if not row:
            return None
        sys_config = get_system_ai_config()
        if not sys_config or not sys_config.get("enabled") or not sys_config.get("api_key"):
            # An admin asked for this job (the flag above matched) and there is no
            # usable system AI to run it with. Nothing will call the provider, so
            # no call can fail and report it — count the misconfiguration itself,
            # or a cleared/disabled config would stay silent until the 21:30
            # daily-summary alert. Costs nothing: this is the local check the
            # loops already run every 10–60s, not a probe.
            _note_system_ai_failure(
                "服务端 API 配置",
                "系统 AI 未配置或未启用（管理员设置 → 服务端 API），已开启的后台 AI 任务无法运行",
            )
            return None
        config = dict(row)
        config.update({
            "provider": sys_config.get("provider"),
            "endpoint": sys_config["endpoint"],
            "model": sys_config["model"],
            "api_key": sys_config["api_key"],
            "provider_type": sys_config.get("provider_type", "openai"),
            "enabled": sys_config.get("enabled", 1),
        })
        return config
    except Exception as e:
        print(f"[auto-service] system config lookup error: {e}")
        return None


def _get_auto_summary_users() -> list[dict]:
    """Admin-enabled background article summarization, using the system AI config."""
    config = _system_auto_config("auto_summary_enabled")
    return [config] if config else []


def _get_auto_translation_users() -> list[dict]:
    """Admin-enabled background translation, using the system AI config."""
    config = _system_auto_config("auto_translate_title", "auto_translate_content")
    return [config] if config else []


def _has_latin(text: str) -> bool:
    return bool(re.search(r"[A-Za-z]", text or ""))


def _plain_text(html: str) -> str:
    text = re.sub(r"<[^>]+>", " ", html or "")
    return " ".join(text.split()).strip()


def _needs_translation(text: str) -> bool:
    text = _plain_text(text)
    if not text or not _has_latin(text):
        return False
    latin_count = len(re.findall(r"[A-Za-z]", text))
    cjk_count = len(re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff]", text))
    return cjk_count == 0 or (latin_count >= 40 and latin_count > cjk_count * 2)


def _title_weight(title: str) -> int:
    """Count Chinese-like characters as 2 and ASCII characters as 1."""
    return sum(1 if ord(ch) < 128 else 2 for ch in (title or "").strip())


TITLE_ERROR_BACKOFF_HOURS = float(os.environ.get("TITLE_ERROR_BACKOFF_HOURS", "6"))


def _within_error_backoff(error_at: str | None, hours: float = TITLE_ERROR_BACKOFF_HOURS) -> bool:
    """True if a recorded failure timestamp is recent enough that we should
    skip retrying for now. Shared by the title-translation and title-summary
    paths so a title that just failed isn't hammered every 10s at low temp.

    The stored timestamp comes from SQLite datetime('now'), which is UTC, so
    it's parsed with calendar.timegm (UTC), not time.mktime (local) — the
    latter silently skews the window by the host's UTC offset."""
    if not error_at:
        return False
    try:
        err_ts = calendar.timegm(time.strptime(error_at, "%Y-%m-%d %H:%M:%S"))
    except (ValueError, TypeError):
        return False
    return (time.time() - err_ts) < hours * 3600


def _title_cjk_count(title: str) -> int:
    return len(re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff]", title or ""))


def _title_total_chars(title: str) -> int:
    return len(re.sub(r"\s+", "", title or ""))


def _needs_title_summary(title: str) -> bool:
    return (
        _title_cjk_count(title) > TITLE_SUMMARY_TRIGGER_CJK
        or _title_total_chars(title) > TITLE_SUMMARY_TRIGGER_TOTAL
    )


def _strip_outer_title_quotes(text: str) -> str:
    pairs = (
        ("\"", "\""), ("'", "'"),
        (chr(0x201c), chr(0x201d)), (chr(0x2018), chr(0x2019)),
        (chr(0x300c), chr(0x300d)), (chr(0x300e), chr(0x300f)),
    )
    changed = True
    while changed and len(text) >= 2:
        changed = False
        for left, right in pairs:
            if text.startswith(left) and text.endswith(right):
                text = text[len(left):-len(right)].strip()
                changed = True
                break
    return text


def _remove_title_summary_prefix(text: str) -> str:
    prefixes = (
        "\u4ee5\u4e0b\u662f\u7b80\u5199\u540e\u7684\u6807\u9898",
        "\u4ee5\u4e0b\u662f\u538b\u7f29\u540e\u7684\u6807\u9898",
        "\u4ee5\u4e0b\u662f\u4f18\u5316\u540e\u7684\u6807\u9898",
        "\u6211\u4e3a\u4f60\u7b80\u5199\u540e\u7684\u6807\u9898",
        "\u6211\u4e3a\u4f60\u538b\u7f29\u540e\u7684\u6807\u9898",
        "\u6211\u4e3a\u4f60\u4f18\u5316\u540e\u7684\u6807\u9898",
        "\u7b80\u5199\u540e\u7684\u6807\u9898",
        "\u538b\u7f29\u540e\u7684\u6807\u9898",
        "\u4f18\u5316\u540e\u7684\u6807\u9898",
        "\u7b80\u5199\u6807\u9898",
        "\u77ed\u6807\u9898",
        "\u603b\u7ed3\u6807\u9898",
        "\u6807\u9898",
    )
    stripped = text.lstrip()
    for prefix in prefixes:
        if stripped.lower().startswith(prefix.lower()):
            rest = stripped[len(prefix):].lstrip()
            if rest.startswith((":", chr(0xff1a))):
                return rest[1:].lstrip()
    return text


def _normalize_cjk_quotes(text: str) -> str:
    """Convert ASCII straight quotes and Unicode curly quotes to Chinese corner brackets 「」 in CJK context."""
    # Paired double quotes — ASCII and Unicode curly
    text = re.sub(r'"([^"]+)"', r'「\1」', text)
    text = re.sub('\u201c([^\u201d]+)\u201d', r'「\1」', text)
    # Paired single quotes — ASCII and Unicode curly (only in CJK context)
    text = re.sub(
        r"'([^']+)'",
        lambda m: f'「{m.group(1)}」'
        if re.search(r'[\u4e00-\u9fff]', m.group(1))
        else m.group(0),
        text,
    )
    text = re.sub(
        '\u2018([^\u2019]+)\u2019',
        lambda m: f'「{m.group(1)}」'
        if re.search(r'[\u4e00-\u9fff]', m.group(1))
        else m.group(0),
        text,
    )
    return text


def _clean_title_summary(title: str | None) -> str:
    text = " ".join((title or "").strip().split())
    text = _remove_title_summary_prefix(text)
    text = re.sub(r"^\s*(?:[-*]\s+|\d{1,2}[.\u3001]\s+)", "", text)
    quote_chars = " \t\r\n" + "".join(chr(cp) for cp in (0x201c, 0x201d, 0x2018, 0x2019, 0x300c, 0x300d, 0x300e, 0x300f))
    # Only strip quote chars when both ends have them (prevent asymmetric tearing:
    # e.g. 飞行员「患有焦虑症」 should not become 飞行员「患有焦虑症)
    if text and text[0] in quote_chars and text[-1] in quote_chars:
        text = text.strip(quote_chars)
    text = _strip_outer_title_quotes(text).strip()
    text = _normalize_cjk_quotes(text)
    return text


_AI_REFUSAL_MARKERS = (
    "\u4ee5\u4e0b\u662f", "\u65e0\u6cd5", "\u4e0d\u80fd",
    "\u8bf7\u63d0\u4f9b", "\u8bf7\u8865\u5145", "as an ai", "i cannot", "i'm unable",
)


def _is_valid_title_summary(title: str | None) -> bool:
    text = _clean_title_summary(title)
    if not text:
        return False
    lowered = text.lower()
    bad_markers = _AI_REFUSAL_MARKERS + ("\u7b80\u5199\u540e\u7684",)
    if any(marker in lowered for marker in bad_markers):
        return False
    if chr(0x2026) in text or "..." in text:
        return False
    if text.startswith("{"):
        return False
    weight = _title_weight(text)
    return TITLE_SUMMARY_MIN_WEIGHT <= weight <= TITLE_SUMMARY_MAX_WEIGHT_HARD


def _parse_title_summary_result(raw: str | None) -> dict:
    text = (raw or "").strip()
    if not text:
        return {"title": "", "valid": False, "reason": "empty AI title summary"}
    match = re.search(r"\{.*\}", text, flags=re.S)
    if match:
        try:
            data = json.loads(match.group(0))
            return {
                "title": data.get("title") or "",
                "valid": bool(data.get("valid")),
                "reason": data.get("reason") or "",
            }
        except (json.JSONDecodeError, TypeError):
            pass
    return {"title": text, "valid": True, "reason": "legacy plain title summary"}


_TITLE_PUNCTUATION_PAIRS = (
    # ASCII/西文
    ("(", ")"), ("[", "]"), ("{", "}"),
    # 全角 / CJK
    ("\uff08", "\uff09"),   # （）
    ("\uff3b", "\uff3d"),   # ［］
    ("\uff5b", "\uff5d"),   # ｛｝
    # 中文专用
    ("\u300a", "\u300b"),   # 《》
    ("\u300c", "\u300d"),   # 「」
    ("\u300e", "\u300f"),   # 『』
    ("\u3010", "\u3011"),   # 【】
    ("\u3014", "\u3015"),   # 〔〕
    ("\u3016", "\u3017"),   # 〖〗
    ("\u3008", "\u3009"),   # 〈〉
    # 弯引号
    ("\u201c", "\u201d"),   # ""
    ("\u2018", "\u2019"),   # ''
)


def _strip_unbalanced_punctuation(title: str) -> str:
    """Drop orphaned members of paired punctuation \u2014 a \u300b with no opening
    \u300a, a dangling \u300c, etc. \u2014 so a title whose only defect is a stray
    bracket/quote is cleaned up in place instead of being shown (or clipped)
    with the orphan. Each pair type is matched independently with a depth
    counter: unmatched closers and leftover openers are removed."""
    remove: set[int] = set()
    for left, right in _TITLE_PUNCTUATION_PAIRS:
        if left == right:
            continue
        open_stack: list[int] = []
        for i, ch in enumerate(title):
            if ch == left:
                open_stack.append(i)
            elif ch == right:
                if open_stack:
                    open_stack.pop()
                else:
                    remove.add(i)
        remove.update(open_stack)
    if not remove:
        return title
    return "".join(ch for i, ch in enumerate(title) if i not in remove)


def _balanced_title_punctuation(title: str) -> bool:
    for left, right in _TITLE_PUNCTUATION_PAIRS:
        if title.count(left) != title.count(right):
            return False
        if title.find(right) != -1 and (title.find(left) == -1 or title.find(right) < title.find(left)):
            return False
    return True


def _looks_like_code_only_title(title: str) -> bool:
    compact = re.sub(r"[\s\-_.:\u3001\uff1a/]+", "", title or "")
    if not compact:
        return True
    if re.fullmatch(r"[\dA-Za-z]+", compact):
        digits = len(re.findall(r"\d", compact))
        letters = len(re.findall(r"[A-Za-z]", compact))
        return digits >= 3 or (digits > 0 and letters <= 4)
    if re.fullmatch(r"[\dA-Za-z.\-_/]+", title or ""):
        return True
    return False


# Attribution/sourcing words and wire-service names. These are excluded when
# checking whether a shortened title still carries the original's subject \u2014
# a title that only echoes "who reported it" (e.g. "\u636eFT\u62a5\u9053") without any of
# the actual subject/action tokens must not be treated as on-topic.
TITLE_ATTRIBUTION_STOPWORDS = {
    "\u62a5\u9053", "\u636e\u62a5\u9053", "\u636e\u6089", "\u6d88\u606f", "\u6d88\u606f\u4eba\u58eb", "\u63f4\u5f15", "\u77e5\u60c5\u4eba\u58eb", "\u62a5\u9053\u79f0", "\u62a5\u9053\u8bf4",
    "reuters", "bloomberg", "afp", "ap", "cnn", "bbc", "wsj", "ft",
    "\u8def\u900f", "\u5f6d\u535a", "\u7f8e\u8054\u793e", "\u6cd5\u65b0\u793e", "\u534e\u5c14\u8857\u65e5\u62a5", "\u91d1\u878d\u65f6\u62a5", "\u7ebd\u7ea6\u65f6\u62a5", "nyt",
    "sources", "reports", "reported", "reporting", "said", "according", "says",
}


def _title_keyword_tokens(title: str) -> set[str]:
    text = _clean_title_summary(title)
    cjk_tokens = set(re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff]{2,}", text))
    latin_tokens = {t.lower() for t in re.findall(r"[A-Za-z][A-Za-z0-9.+#-]{1,}", text)}
    return cjk_tokens | latin_tokens


def _shares_title_signal(candidate: str, original_title: str) -> bool:
    original_tokens = _title_keyword_tokens(original_title) - TITLE_ATTRIBUTION_STOPWORDS
    if not original_tokens:
        return True
    candidate_tokens = _title_keyword_tokens(candidate) - TITLE_ATTRIBUTION_STOPWORDS
    if not candidate_tokens:
        return False
    if candidate_tokens & original_tokens:
        return True
    candidate_text = _clean_title_summary(candidate).lower()
    original_text = _clean_title_summary(original_title).lower()
    for token in original_tokens:
        if token.lower() in candidate_text:
            return True
    for token in candidate_tokens:
        if token.lower() in original_text:
            return True
    return False


def _validate_ai_title_summary_result(result: dict | str | None, original_title: str = "") -> dict:
    """Hard-reject only when the output structurally can't be a title summary
    at all (AI says so itself, or it doesn't fit the length/format contract
    the whole "shorten the title" feature exists to deliver). Everything else
    below (code-only text, unbalanced punctuation, missing keyword/number
    overlap with the original) is a probabilistic clue, not proof the result
    is wrong — a false positive on any one of them used to discard an
    otherwise-fine title forever, since the same input reliably re-triggers
    the same rule on every retry. Those are logged but kept instead: a
    slightly-off title beats no title. (_repair_title_summary, upstream,
    checks bracket/quote balance itself before picking a fallback clause, so
    a mid-bracket punctuation warning here should be rare in practice.)
    """
    data = _parse_title_summary_result(result) if isinstance(result, str) or result is None else dict(result)
    title = _repair_title_summary(data.get("title") or "")
    reason = data.get("reason") or ""
    if data.get("valid") is False:
        return {"title": title, "valid": False, "reason": reason or "AI marked title summary invalid"}
    if not _is_valid_title_summary(title):
        return {"title": title, "valid": False, "reason": "invalid title summary length or format"}
    warnings = []
    if _looks_like_code_only_title(title):
        warnings.append("code-only or numeric title")
    if not _balanced_title_punctuation(title):
        warnings.append("unbalanced punctuation")
    if original_title:
        # _shares_title_signal is a literal-keyword-overlap check that only
        # makes sense when both titles are in the same language (same-language
        # shortening). When the "summary" step is actually also translating
        # (original_title is still untranslated English), fall back to the
        # cross-language-safe numeric check instead — see _numbers_match.
        if _needs_translation(original_title):
            if not _numbers_match(original_title, title):
                warnings.append("missing numbers from original title")
        elif not _shares_title_signal(title, original_title):
            warnings.append("missing original title signal")
    if warnings:
        print(f"[auto-title] title summary kept despite soft warning(s) "
              f"({'; '.join(warnings)}): {title[:120]!r}")
    return {"title": title, "valid": True, "reason": reason}


def _title_numeric_values(text: str) -> set[float]:
    """Numbers in a title, including Chinese 万/亿-scaled numerals (e.g. "1万"
    -> 10000.0), normalized to their numeric value. Unlike words, a number's
    *value* should survive a correct translation even though its literal
    digits/formatting often don't (English "10,000" is routinely rendered as
    Chinese "1万"), so this is a safer cross-language sanity check than
    literal-token overlap (see _shares_title_signal).
    """
    text = text or ""
    values: set[float] = set()
    for m in re.finditer(r"\d[\d,]*\.?\d*", text):
        try:
            values.add(float(m.group(0).replace(",", "")))
        except ValueError:
            pass
    for m in re.finditer(r"(\d+(?:\.\d+)?)\s*([万亿])", text):
        try:
            values.add(float(m.group(1)) * (10000 if m.group(2) == "万" else 100000000))
        except ValueError:
            pass
    return values


def _numbers_match(original_title: str, candidate: str) -> bool:
    original_values = _title_numeric_values(original_title)
    if not original_values:
        return True
    return bool(original_values & _title_numeric_values(candidate))


def _validate_title_translation(translated: str | None, original_title: str) -> dict:
    """Sanity-check a translated title before it's saved as the site-wide title.

    Hard-rejects only truly unusable output: empty, an AI refusal/explanation
    instead of a translation, or an unparsed JSON payload — cases where
    there's no plausible title to fall back on at all. Everything else below
    (ellipsis/truncation look, code-only text, unbalanced punctuation, being
    shorter than expected, missing numbers from the original — see
    _numbers_match) is a probabilistic heuristic, not proof of a bad
    translation, and discarding on those used to permanently strand titles
    that legitimately tripped one rule or another on every retry (the same
    input reliably re-triggers the same rule, so it never self-heals). Those
    are logged as a warning but the translation is kept: a slightly-off
    translation is still more useful to readers than none at all.
    """
    title = _clean_title_summary(translated)
    if not title:
        return {"title": title, "valid": False, "reason": "empty translation"}
    if any(marker in title.lower() for marker in _AI_REFUSAL_MARKERS):
        return {"title": title, "valid": False, "reason": "AI refusal or explanation, not a translation"}
    if title.startswith("{"):
        return {"title": title, "valid": False, "reason": "unparsed JSON payload"}
    warnings = []
    if chr(0x2026) in title or "..." in title:
        warnings.append("looks truncated (ellipsis)")
    if _looks_like_code_only_title(title):
        warnings.append("code-only or numeric title")
    if not _balanced_title_punctuation(title):
        warnings.append("unbalanced punctuation")
    if _title_weight(title) < TITLE_SUMMARY_MIN_WEIGHT:
        warnings.append("shorter than expected")
    if not _numbers_match(original_title, title):
        warnings.append("missing numbers from original title")
    if warnings:
        print(f"[auto-title] translation kept despite soft warning(s) "
              f"({'; '.join(warnings)}): {title[:120]!r}")
    return {"title": title, "valid": True, "reason": ""}


def _clip_title_by_weight(title: str, max_weight: int = TITLE_SUMMARY_MAX_WEIGHT) -> str:
    title = _clean_title_summary(title)
    if _title_weight(title) <= max_weight:
        return title
    out = []
    weight = 0
    for ch in title:
        ch_weight = 1 if ord(ch) < 128 else 2
        if weight + ch_weight > max_weight:
            break
        out.append(ch)
        weight += ch_weight
    clipped = "".join(out).rstrip(" ，,、：:；;。.!！?？-_/")
    if " " in clipped and re.search(r"[A-Za-z0-9]$", clipped):
        clipped = clipped.rsplit(" ", 1)[0].rstrip(" ，,、：:；;。.!！?？-_/") or clipped
    return clipped


def _repair_title_summary(title: str | None) -> str:
    """Turn a slightly non-compliant AI title into a valid short title when safe.

    The sentence/clause splits below are punctuation-based and don't know
    about bracket/quote pairs — a naturally-written title can have a comma or
    colon nested inside a 「」/《》 quote (e.g. "「甲：从乙到丙」是丁"), and
    slicing there leaves one candidate with a dangling open bracket. Such a
    candidate can still be short enough to pass _is_valid_title_summary's
    length check, so it must also pass _balanced_title_punctuation before
    being accepted — otherwise a fragment like "「甲" gets picked as the
    "repaired" title instead of a genuinely complete clause.
    """
    text = _clean_title_summary(title)
    if not text:
        return ""
    text = text.replace("……", " ").replace("...", " ").replace("…", " ")
    text = " ".join(text.split())
    if _is_valid_title_summary(text) and _balanced_title_punctuation(text):
        return text

    # If the only defect is a dangling bracket/quote, drop the orphan(s) and
    # keep the full title rather than slicing off content — a stray 》 must
    # never survive into what the reader sees.
    if not _balanced_title_punctuation(text):
        stripped = _clean_title_summary(_strip_unbalanced_punctuation(text))
        if _is_valid_title_summary(stripped) and _balanced_title_punctuation(stripped):
            return stripped

    sentence_parts = [p.strip() for p in re.split(r"[。！？!?；;]\s*", text) if p.strip()]
    for part in sentence_parts:
        part = _clean_title_summary(part)
        if _is_valid_title_summary(part) and _balanced_title_punctuation(part):
            return part

    clause_source = sentence_parts[0] if sentence_parts else text
    clause_parts = [p.strip() for p in re.split(r"[，,、：:]\s*", clause_source) if p.strip()]
    for part in clause_parts:
        part = _clean_title_summary(part)
        if _is_valid_title_summary(part) and _balanced_title_punctuation(part):
            return part

    return _clip_title_by_weight(clause_source)


def _ensure_article_title_columns(conn) -> None:
    """Compatibility wrapper for shared, cross-process-safe title migration."""
    ensure_article_title_columns(conn)


def _invalidate_refresh_server_cache(article_id: int) -> None:
    """Best-effort: tell refresh_server.py to drop its in-memory cached
    /api/news/<id> response for this article, so the next read reflects the
    title/body update we just wrote to news.db instead of stale cached JSON.
    refresh_server is a separate process with no other way to learn about
    writes made here; without this, staleness only self-heals on the next
    ~15min fetcher cycle (which clears the whole cache) or when some client
    happens to poll /api/news/title-updates (which only handles title
    changes, not body_html).
    """
    try:
        requests.get(
            "http://127.0.0.1:8081/internal/cache-evict",
            params={"id": article_id},
            timeout=3,
        )
    except requests.exceptions.RequestException:
        pass  # non-fatal — cache will self-heal on the next fetcher cycle


def _save_article_title_update(article_id: int, title: str | None,
                               title_source: str = "ai") -> bool:
    title = _clean_title_summary(_sanitize_plain_text(title or "", max_len=300))
    if not title or not os.path.exists(NEWS_DB):
        return False
    conn = sqlite3.connect(NEWS_DB, timeout=30)
    try:
        _ensure_article_title_columns(conn)
        row = conn.execute("SELECT title FROM articles WHERE id = ?", (article_id,)).fetchone()
        if not row:
            return False
        current_title = row[0] or ""
        if current_title == title:
            return False
        conn.execute(
            "UPDATE articles SET "
            "original_title = CASE WHEN original_title IS NULL OR original_title = '' THEN title ELSE original_title END, "
            "title = ?, title_updated_at = strftime('%Y-%m-%d %H:%M:%f', 'now'), title_source = ? "
            "WHERE id = ?",
            (title, title_source, article_id),
        )
        conn.commit()
        _invalidate_refresh_server_cache(article_id)
        return True
    finally:
        conn.close()


def _article_age_days(article: dict) -> float:
    ts = article.get("timestamp")
    if ts is None:
        return 0.0
    try:
        return max(0.0, (time.time() - float(ts)) / 86400.0)
    except (TypeError, ValueError):
        return 0.0


def _translation_pending_fulltext(article: dict) -> bool:
    """Whether a Telegraph article still has only its Telegram excerpt.

    After the backfill window elapses, a still-excerpt-only article is allowed
    to translate its excerpt rather than waiting indefinitely for full text
    that may never arrive.
    """
    if not bool(article.get("telegraph_url")) or bool(article.get("has_full_content")):
        return False
    return _article_age_days(article) < RECENT_FULLTEXT_UPGRADE_DAYS


def _translation_looks_truncated(
    article: dict, cached_html: str, source_html: str
) -> bool:
    """Conservatively identify a cached translation made from an excerpt."""
    if not bool(article.get("has_full_content")) or not cached_html:
        return False
    source = _plain_text(source_html or "")
    translated = _plain_text(cached_html)
    if not (len(source) >= 400 and _has_latin(source)):
        return False
    if len(translated) < len(source) * 0.30:
        return True
    original = _plain_text(article.get("original_body_html") or "")
    return len(original) >= 400 and len(translated) < len(original) * 0.30


def _fetch_stale_translation_articles(
    limit: int = AUTO_TRANSLATION_BATCH_LIMIT,
) -> list[dict]:
    """Scan one historical keyset page for conservatively stale translations.

    ``limit`` bounds rows inspected, not stale rows returned. Advancing by the
    last inspected row ensures that full pages without candidates cannot pin
    every future scan to the newest history forever.
    """
    global _translation_repair_cursor, _translation_repair_cursor_db_path

    if limit <= 0:
        return []

    db_path = os.path.abspath(NEWS_DB)
    with _translation_repair_lock:
        if _translation_repair_cursor_db_path != db_path:
            _translation_repair_cursor = None
            _translation_repair_cursor_db_path = db_path
        if not os.path.exists(db_path):
            _translation_repair_cursor = None
            return []

        try:
            if not _init_ai_results_table():
                _translation_repair_cursor = None
                return []

            cursor = _translation_repair_cursor
            where_cursor = ""
            params: list = []
            if cursor is not None:
                cursor_timestamp, cursor_id = cursor
                where_cursor = (
                    "AND (a.timestamp < ? OR "
                    "(a.timestamp = ? AND a.id < ?)) "
                )
                params.extend((cursor_timestamp, cursor_timestamp, cursor_id))
            horizon_cutoff = time.time() - STALE_TRANSLATION_SCAN_HORIZON_DAYS * 86400
            repair_cutoff = time.time() - TRANSLATION_REPAIR_WINDOW_SECONDS
            params.extend((horizon_cutoff, repair_cutoff, limit))

            with _news_db_conn() as conn:
                ensure_article_source_columns(conn)
                rows = conn.execute(
                    "SELECT a.id, a.title, "
                    "       COALESCE(NULLIF(a.group_source, ''), a.source) AS source, "
                    "       a.origin_source, a.summary, a.body_html, a.timestamp, "
                    "       a.has_full_content, a.telegraph_url, "
                    "       a.original_body_html, r.translation, "
                    "       r.translation_updated_at "
                    "FROM articles a "
                    "JOIN ai_results r ON r.article_id = a.id "
                    "WHERE a.has_full_content = 1 "
                    "  AND r.translation IS NOT NULL "
                    "  AND TRIM(r.translation) <> '' "
                    f"{where_cursor}"
                    "  AND a.timestamp >= ? "
                    "  AND (r.translation_updated_at IS NULL "
                    "       OR r.translation_updated_at <= ?) "
                    "ORDER BY a.timestamp DESC, a.id DESC "
                    "LIMIT ?",
                    params,
                ).fetchall()
        except Exception as exc:
            print(f"[auto-translate] stale scan failed: {exc}")
            return []

        if rows and len(rows) == limit:
            last_row = rows[-1]
            _translation_repair_cursor = (
                int(last_row["timestamp"]),
                int(last_row["id"]),
            )
        else:
            _translation_repair_cursor = None

        selected = []
        for row in rows:
            article = dict(row)
            _, cached_html = _cached_full_translation(article.get("translation"))
            if not _translation_looks_truncated(
                article, cached_html, article.get("body_html") or ""
            ):
                continue
            article["translation_stale"] = True
            article["translate_content_needed"] = True
            article["translate_title_needed"] = _needs_translation(
                article.get("title", "")
            )
            selected.append(article)
        return selected


def _fetch_untranslated_articles(config: dict, limit: int = AUTO_TRANSLATION_BATCH_LIMIT) -> list[dict]:
    """Fetch recent today articles that need background title/body translation."""
    import datetime as _dt
    import sqlite3
    if not os.path.exists(NEWS_DB):
        return []
    translate_title = bool(config.get("auto_translate_title"))
    translate_content = bool(config.get("auto_translate_content"))
    if not translate_title and not translate_content:
        return []
    try:
        _init_ai_results_table()
        today_str = _dt.datetime.now().strftime("%Y-%m-%d")
        upgrade_cutoff = time.time() - RECENT_FULLTEXT_UPGRADE_DAYS * 86400
        with _news_db_conn() as conn:
            conn.row_factory = sqlite3.Row
            ensure_article_source_columns(conn)
            rows = conn.execute(
                "SELECT a.id, a.title, COALESCE(NULLIF(a.group_source, ''), a.source) AS source, "
                "       a.origin_source, a.summary, a.body_html, a.has_full_content, "
                "       a.telegraph_url, r.translation "
                "FROM articles a "
                "LEFT JOIN ai_results r ON r.article_id = a.id "
                "WHERE a.date = ? "
                # Newest-first, and scan far more than one batch: the untranslated
                # rows are filtered in Python (the latin/CJK heuristic can't run in
                # SQL), so a tight oldest-first LIMIT would only ever see the oldest
                # articles of the day. Once the day exceeds that window, freshly
                # fetched English articles would never enter the candidate set and
                # stay untranslated forever. Mirrors the title-process scan.
                "ORDER BY a.timestamp DESC LIMIT ?",
                (today_str, max(limit * 8, AUTO_TRANSLATION_SCAN_LIMIT)),
            ).fetchall()
            # P2.9: also catch articles upgraded to full content after their
            # ``date`` left the today window (backfill succeeded on day >= 2).
            upgraded_rows = conn.execute(
                "SELECT a.id, a.title, COALESCE(NULLIF(a.group_source, ''), a.source) AS source, "
                "       a.origin_source, a.summary, a.body_html, a.has_full_content, "
                "       a.telegraph_url, r.translation "
                "FROM articles a "
                "LEFT JOIN ai_results r ON r.article_id = a.id "
                "WHERE a.has_full_content = 1 "
                "  AND (r.translation IS NULL OR TRIM(r.translation) = '') "
                "  AND a.timestamp >= ? "
                "ORDER BY a.timestamp DESC LIMIT ?",
                (upgrade_cutoff, max(limit * 4, AUTO_TRANSLATION_SCAN_LIMIT // 2)),
            ).fetchall()
    except Exception as e:
        print(f"[auto-translate] fetch failed: {e}")
        return []

    selected = []
    seen_ids = set()
    for row in rows:
        article = dict(row)
        _, cached_html = _cached_full_translation(article.get("translation"))
        title_needed = translate_title and _needs_translation(article.get("title", ""))
        content_needed = (
            translate_content
            and bool(article.get("body_html"))
            and not cached_html
            and not _translation_pending_fulltext(article)
            and _needs_translation(article.get("body_html") or article.get("summary") or "")
        )
        if title_needed or content_needed:
            article["translate_title_needed"] = title_needed
            article["translate_content_needed"] = content_needed
            selected.append(article)
            seen_ids.add(article["id"])
        if len(selected) >= limit:
            break
    # P2.9: backfill-upgraded articles that lost their "today" date.
    if len(selected) < limit:
        for row in upgraded_rows:
            article = dict(row)
            if article["id"] in seen_ids:
                continue
            _, cached_html = _cached_full_translation(article.get("translation"))
            if cached_html:
                continue
            title_needed = translate_title and _needs_translation(article.get("title", ""))
            content_needed = (
                translate_content
                and bool(article.get("body_html"))
                and not _translation_pending_fulltext(article)
                and _needs_translation(article.get("body_html") or article.get("summary") or "")
            )
            if title_needed or content_needed:
                article["translate_title_needed"] = title_needed
                article["translate_content_needed"] = content_needed
                selected.append(article)
                seen_ids.add(article["id"])
                if len(selected) >= limit:
                    break
    return selected


def _save_article_translation(article_id: int, title: str | None = None,
                              body_html: str | None = None) -> bool:
    """Persist only title metadata; translated bodies live in ai_results.

    ``body_html`` remains accepted for compatibility with callers/tests but is
    deliberately never written to the canonical unauthenticated article row.
    """
    if not os.path.exists(NEWS_DB):
        return False
    return bool(title and _save_article_title_update(article_id, title, "translation"))


def _cached_full_translation(translation: str | None) -> tuple[str, str]:
    text = (translation or "").strip()
    if not text:
        return "", ""
    if text.startswith("{"):
        try:
            data = json.loads(text)
            return data.get("title", "") or "", data.get("html", "") or ""
        except (json.JSONDecodeError, TypeError):
            return "", ""
    if text.startswith("<"):
        return "", text
    return "", ""


def _translate_article_background(article: dict, config: dict) -> bool:
    svc = _SystemAIService(
        "自动翻译",
        api_key=config["api_key"],
        endpoint=config["endpoint"],
        model=config["model"],
        provider_type=config.get("provider_type", "openai"),
    )
    article_id = article["id"]
    translated_title = None
    translated_html = None
    cached_title = ""
    translation_cache_data = None

    if article.get("translate_content_needed"):
        cached_title, cached_html = _cached_full_translation(article.get("translation"))
        if cached_html and not article.get("translation_stale"):
            translated_html = cached_html
            if article.get("translate_title_needed"):
                translated_title = cached_title or None
        else:
            title_for_translation = article.get("title", "") if article.get("translate_title_needed") else ""
            result = svc.translate_full(
                article.get("body_html") or "",
                "zh-CN",
                title=title_for_translation,
            )
            translated_html = result.get("html") or ""
            if article.get("translate_title_needed"):
                translated_title = result.get("title") or None
        if translated_html:
            translation_cache_data = json.dumps({
                "title": translated_title if translated_title is not None else cached_title,
                "html": translated_html,
            }, ensure_ascii=False)
        if article.get("translate_title_needed") and not translated_title:
            translated_title = svc.translate_title(article.get("title", ""), "zh-CN")

    elif article.get("translate_title_needed"):
        translated_title = svc.translate_title(article.get("title", ""), "zh-CN")

    _save_article_translation(article_id, title=translated_title)
    # Publish only after the authenticated shared cache commit. The canonical
    # detail body remains original and is safe for unauthenticated readers.
    if translation_cache_data is not None:
        _save_ai_result(
            article_id,
            translation=translation_cache_data,
            translation_provider=config.get("provider") or config.get("provider_type"),
            translation_model=config.get("model"),
            translation_by_user_id=config.get("user_id"),
        )
        _publish_translation_update(article_id)
    return bool(translated_title or translated_html)


def _run_auto_translation_once():
    """Translate article titles/bodies in small batches for opted-in users."""
    if not _auto_translation_lock.acquire(blocking=False):
        return
    try:
        users = _get_auto_translation_users()
        if not users:
            return
        for config in users:
            today = _fetch_untranslated_articles(config, AUTO_TRANSLATION_BATCH_LIMIT)
            articles = []
            seen_ids = set()
            articles_by_id = {}
            for article in today:
                if article["id"] in seen_ids:
                    continue
                seen_ids.add(article["id"])
                articles_by_id[article["id"]] = article
                articles.append(article)
                if len(articles) >= AUTO_TRANSLATION_BATCH_LIMIT:
                    break

            remaining = AUTO_TRANSLATION_BATCH_LIMIT - len(articles)
            if remaining and config.get("auto_translate_content"):
                try:
                    historical = _fetch_stale_translation_articles(remaining)
                except Exception as exc:
                    print(f"[auto-translate] stale scan failed: {exc}")
                    historical = []
                for article in historical:
                    article = dict(article)
                    article["translate_title_needed"] = bool(
                        config.get("auto_translate_title")
                        and article.get("translate_title_needed")
                    )
                    if article["id"] in seen_ids:
                        existing = articles_by_id[article["id"]]
                        title_needed = bool(
                            config.get("auto_translate_title")
                            and (
                                existing.get("translate_title_needed")
                                or article.get("translate_title_needed")
                            )
                        )
                        content_needed = bool(
                            existing.get("translate_content_needed")
                            or article.get("translate_content_needed")
                        )
                        translation_stale = bool(
                            existing.get("translation_stale")
                            or article.get("translation_stale")
                        )
                        for key, value in article.items():
                            existing.setdefault(key, value)
                        existing["translate_title_needed"] = title_needed
                        existing["translate_content_needed"] = content_needed
                        if translation_stale:
                            existing["translation_stale"] = True
                        continue
                    seen_ids.add(article["id"])
                    articles_by_id[article["id"]] = article
                    articles.append(article)
                    if len(articles) >= AUTO_TRANSLATION_BATCH_LIMIT:
                        break
            if not articles:
                continue
            print(f"[auto-translate] User {config['user_id']}: translating {len(articles)} article(s)")
            for article in articles:
                try:
                    if _translate_article_background(article, config):
                        print(f"[auto-translate] Translated article {article['id']}: {article.get('title', '')[:50]}")
                except Exception as e:
                    print(f"[auto-translate] Article {article.get('id')}: failed: {e}")
    finally:
        _auto_translation_lock.release()


def _auto_translation_loop():
    """Background loop for opt-in automatic title/content translation."""
    import time as _time
    _time.sleep(60)
    while True:
        try:
            _run_auto_translation_once()
        except Exception as e:
            print(f"[auto-translate] Error in loop: {e}")
        _time.sleep(AUTO_TRANSLATION_INTERVAL_SECONDS)


def _get_auto_title_process_users() -> list[dict]:
    """Admin-enabled title translation/shortening, using the system AI config."""
    config = _system_auto_config("auto_translate_title", "auto_title_summary_enabled")
    return [config] if config else []


def _fetch_title_process_articles(config: dict, limit: int = AUTO_TITLE_PROCESS_BATCH_LIMIT) -> list[dict]:
    """Fetch today's articles needing title translation or AI title shortening."""
    import datetime as _dt
    if not os.path.exists(NEWS_DB):
        return []
    translate_title = bool(config.get("auto_translate_title"))
    summarize_title = bool(config.get("auto_title_summary_enabled"))
    if not translate_title and not summarize_title:
        return []
    conn = None
    try:
        _init_ai_results_table()
        today_str = _dt.datetime.now().strftime("%Y-%m-%d")
        conn = sqlite3.connect(NEWS_DB, timeout=30)
        conn.row_factory = sqlite3.Row
        ensure_article_source_columns(conn)
        _ensure_article_title_columns(conn)
        conn.commit()
        rows = conn.execute(
            "SELECT a.id, a.title, a.original_title, a.title_source, a.title_updated_at, "
            "       r.title_summary, r.title_summary_error, r.title_summary_error_at, "
            "       r.title_translation_error_at "
            "FROM articles a "
            "LEFT JOIN ai_results r ON r.article_id = a.id "
            "WHERE a.date = ? "
            "ORDER BY a.timestamp DESC LIMIT ?",
            (today_str, max(limit * 10, AUTO_TITLE_PROCESS_SCAN_LIMIT)),
        ).fetchall()
    except Exception as e:
        print(f"[auto-title] fetch failed: {e}")
        return []
    finally:
        if conn:
            conn.close()

    selected = []
    for row in rows:
        article = dict(row)
        title = article.get("title") or ""
        cached_summary = _clean_title_summary(article.get("title_summary"))
        translate_needed = translate_title and _needs_translation(title)
        summary_needed = summarize_title and (
            (cached_summary and title != cached_summary)
            or (not cached_summary and _needs_title_summary(title))
        )
        if not translate_needed and not summary_needed:
            continue
        # Back off recently-failed translations so a title that reliably fails
        # validation isn't re-attempted every 10s at low temperature.
        if translate_needed and _within_error_backoff(article.get("title_translation_error_at")):
            translate_needed = False
        previous_error = (article.get("title_summary_error") or "").lower()
        retryable_invalid_output = "invalid title summary" in previous_error
        if (summary_needed and not cached_summary and not retryable_invalid_output
                and _within_error_backoff(article.get("title_summary_error_at"))):
            summary_needed = False
        if translate_needed or summary_needed:
            article["translate_title_needed"] = translate_needed
            article["title_summary_needed"] = summary_needed
            selected.append(article)
        if len(selected) >= limit:
            break
    return selected


def _title_service(config: dict) -> "_SystemAIService":
    return _SystemAIService(
        "标题精简",
        api_key=config["api_key"],
        endpoint=config["endpoint"],
        model=config["model"],
        provider_type=config.get("provider_type", "openai"),
    )


# On a rejected result we re-ask once with the failure reason as feedback and
# a higher temperature — a low-temp model tends to re-emit the same bad output
# verbatim, so a plain retry is wasted. One corrective retry is enough; beyond
# that the article backs off (error timestamp) instead of hammering every 10s.
TITLE_RETRY_TEMPERATURE = float(os.environ.get("TITLE_RETRY_TEMPERATURE", "0.5"))


def _translate_title_with_retry(svc: "AIService", title: str) -> dict:
    """Translate `title`, and if validation rejects the output, re-ask once
    with the failure reason as feedback. Returns the _validate_title_translation
    dict of whichever attempt succeeded, else the last failure."""
    raw = svc.translate_title(title, "zh-CN")
    result = _validate_title_translation(raw, title)
    if result["valid"]:
        return result
    raw2 = svc.translate_title(
        title, "zh-CN",
        feedback=result.get("reason") or "输出不合规",
        temperature=TITLE_RETRY_TEMPERATURE,
    )
    return _validate_title_translation(raw2, title)


def _summarize_title_with_retry(svc: "AIService", title: str) -> dict:
    """Shorten `title`, re-asking once with feedback if the first result is
    rejected. Returns the _validate_ai_title_summary_result dict."""
    raw = svc.summarize_title(title, TITLE_SUMMARY_MAX_CHARS, TITLE_SUMMARY_PROMPT_MIN_CHARS)
    result = _validate_ai_title_summary_result(_parse_title_summary_result(raw), original_title=title)
    if result["valid"]:
        return result
    raw2 = svc.summarize_title(
        title, TITLE_SUMMARY_MAX_CHARS, TITLE_SUMMARY_PROMPT_MIN_CHARS,
        feedback=result.get("reason") or "输出不合规",
        temperature=TITLE_RETRY_TEMPERATURE,
    )
    return _validate_ai_title_summary_result(_parse_title_summary_result(raw2), original_title=title)


def _translate_condense_with_retry(svc: "AIService", title: str) -> dict:
    """One-shot translate+shorten with one corrective retry. Validated as a
    title summary (the output is both translated and shortened), against the
    still-foreign original."""
    raw = svc.translate_and_condense_title(
        title, "zh-CN", TITLE_SUMMARY_MAX_CHARS, TITLE_SUMMARY_PROMPT_MIN_CHARS)
    result = _validate_ai_title_summary_result(_parse_title_summary_result(raw), original_title=title)
    if result["valid"]:
        return result
    raw2 = svc.translate_and_condense_title(
        title, "zh-CN", TITLE_SUMMARY_MAX_CHARS, TITLE_SUMMARY_PROMPT_MIN_CHARS,
        feedback=result.get("reason") or "输出不合规",
        temperature=TITLE_RETRY_TEMPERATURE,
    )
    return _validate_ai_title_summary_result(_parse_title_summary_result(raw2), original_title=title)


def _process_article_title(article: dict, config: dict) -> bool:
    article_id = article["id"]
    title = article.get("title") or ""
    changed = False
    svc = None
    summarize_enabled = bool(config.get("auto_title_summary_enabled"))

    if article.get("translate_title_needed") and _needs_translation(title):
        svc = _title_service(config)

        # Merged path: the title is BOTH foreign and over-long, so translate
        # and shorten it in a single call — one AI round-trip, and the title
        # only changes once on screen instead of translate-then-shorten.
        if (TITLE_MERGE_TRANSLATE_CONDENSE and summarize_enabled
                and _needs_title_summary(title)):
            result = _translate_condense_with_retry(svc, title)
            if result["valid"]:
                new_title = result["title"]
                if _save_article_title_update(article_id, new_title, "title_summary"):
                    changed = True
                _save_ai_result(
                    article_id,
                    title_summary=new_title,
                    title_summary_provider=config.get("provider_type") or "openai",
                    title_summary_model=config.get("model") or "",
                    title_summary_by_user_id=config.get("user_id"),
                    clear_title_translation_error=True,
                )
            else:
                print(f"[auto-title] Article {article_id}: discarded invalid "
                      f"translate+condense ({result['reason']}): {result['title'][:120]!r}")
                _save_ai_result(
                    article_id,
                    title_translation_error=f"{result['reason']}: {result['title'][:120]}",
                )
            return changed

        result = _translate_title_with_retry(svc, title)
        if result["valid"]:
            translated = result["title"]
            if _save_article_title_update(article_id, translated, "translation"):
                changed = True
            title = translated
            _save_ai_result(article_id, clear_title_translation_error=True)
        else:
            print(f"[auto-title] Article {article_id}: discarded invalid title translation "
                  f"({result['reason']}): {result['title'][:120]!r}")
            _save_ai_result(
                article_id,
                title_translation_error=f"{result['reason']}: {result['title'][:120]}",
            )

    if not summarize_enabled:
        return changed
    if not _needs_title_summary(title):
        return changed

    cached = _get_ai_result(article_id) or {}
    cached_summary = cached.get("title_summary")
    if cached_summary:
        cached_result = _validate_ai_title_summary_result(
            {"title": cached_summary, "valid": True, "reason": "cached title summary"},
            original_title=title,
        )
        if cached_result["valid"]:
            return _save_article_title_update(article_id, cached_result["title"], "title_summary") or changed
        _save_ai_result(article_id, title_summary_error=f"invalid cached title summary: {cached_result['reason']}")
        return changed

    try:
        svc = svc or _title_service(config)
        result = _summarize_title_with_retry(svc, title)
        if not result["valid"]:
            snippet = _clean_title_summary(result.get("title") or "")[:120] or "<empty>"
            reason = result.get("reason") or "invalid title summary"
            raise ValueError(f"AI returned invalid title summary: {reason}: {snippet}")
        short_title = result["title"]
        _save_ai_result(
            article_id,
            title_summary=short_title,
            title_summary_provider=config.get("provider_type") or "openai",
            title_summary_model=config.get("model") or "",
            title_summary_by_user_id=config.get("user_id"),
        )
        if _save_article_title_update(article_id, short_title, "title_summary"):
            changed = True
    except Exception as e:
        _save_ai_result(article_id, title_summary_error=_redact_secrets(e))
        raise
    return changed

def _run_auto_title_process_once():
    """Translate and shorten today's titles for opted-in users."""
    if not _auto_title_process_lock.acquire(blocking=False):
        return
    try:
        users = _get_auto_title_process_users()
        if not users:
            return
        for config in users:
            articles = _fetch_title_process_articles(config, AUTO_TITLE_PROCESS_BATCH_LIMIT)
            if not articles:
                continue
            print(f"[auto-title] User {config['user_id']}: processing {len(articles)} title(s)")
            for article in articles:
                try:
                    if _process_article_title(article, config):
                        print(f"[auto-title] Updated article {article['id']}: {article.get('title', '')[:50]}")
                except Exception as e:
                    safe_error = _redact_secrets(e)
                    print(f"[auto-title] Article {article.get('id')}: failed: {safe_error}")
    finally:
        _auto_title_process_lock.release()


def _auto_title_process_loop():
    """Background loop for fast title translation/shortening."""
    import time as _time
    _time.sleep(20)
    while True:
        try:
            _run_auto_title_process_once()
        except Exception as e:
            print(f"[auto-title] Error in loop: {e}")
        _time.sleep(AUTO_TITLE_PROCESS_INTERVAL_SECONDS)


def _get_source_classification_users() -> list[dict]:
    """The server API config for shared source classification.

    Source classification is a server-side job producing shared, site-wide
    labels, so it runs on the admin-managed server API (管理员设置 → 服务端 API)
    like every other background job. It used to read an admin's personal
    ai_configs row instead, which meant two different keys drove server work:
    with the server API suspended and that personal key still valid, this job
    kept succeeding and the health signal could not tell the two apart.
    """
    try:
        sys_config = get_system_ai_config()
        if not sys_config or not sys_config.get("enabled") or not sys_config.get("api_key"):
            return []
        return [{
            "user_id": 0,
            "endpoint": sys_config["endpoint"],
            "model": sys_config["model"],
            "api_key": sys_config["api_key"],
            "provider_type": sys_config.get("provider_type", "openai"),
            "enabled": sys_config.get("enabled", 1),
        }]
    except Exception as e:
        print(f"[source-classify] server API config error: {e}")
        return []


def _classify_source_batch(config: dict, limit: int = AUTO_SOURCE_CLASSIFY_BATCH_LIMIT,
                           force: bool = False) -> dict:
    """Classify sources with the server API, like every other server-side job.

    `config` comes from _get_source_classification_users(), i.e. from
    system_ai_config. It used to come from an admin's own ai_configs row, which
    meant two different keys drove server-side work: with the server API
    suspended and that key still valid, this job kept succeeding and cancelled
    the failure streak the summary/translation/title jobs were building, so the
    outage never reached the alert threshold.
    """
    conn = _get_news_db()
    if not conn:
        return {"processed": [], "failed": [], "remaining": 0}
    ensure_article_sources(conn)
    rows = source_rows(conn)
    candidates = [
        row for row in rows
        if row.get("source")
        and not row.get("alias_target")
        and not row.get("user_override")
        and row.get("status") != "manual"
        and (
            force
            or row.get("status") in ("pending", "failed")
            or (
                row.get("status") == "classified"
                and (row.get("confidence") or 0) < 0.6
            )
        )
    ][:limit]

    categories = [row for row in category_definitions(conn) if not row.get("is_system")]
    category_keys = {row["category"] for row in categories}

    svc = _SystemAIService(
        "订阅源分类",
        api_key=config["api_key"],
        endpoint=config["endpoint"],
        model=config["model"],
        provider_type=config.get("provider_type", "openai"),
    )
    processed = []
    failed = []
    for row in candidates:
        source = row["source"]
        titles = recent_titles_for_source(conn, source, limit=8)
        # Extract domains from recent article bodies for stronger AI signal
        domains = _extract_domains_for_source(conn, source)
        try:
            result = svc.classify_source(source, titles, domains=domains, categories=categories)
            confidence = float(result.get("confidence") or 0)
            category = result.get("category") or ""
            label = result.get("label") or row.get("label") or source
            if category in category_keys and confidence >= 0.85:
                saved = update_source_category(
                    conn, source, category, label, status="classified",
                    confidence=confidence, reason=result.get("reason") or "AI classified",
                    sample_titles=titles,
                )
                processed.append(saved)
            else:
                conn.execute(
                    "UPDATE source_categories SET suggested_category = ?, suggested_label = ?, "
                    "suggested_confidence = ?, reason = ?, sample_titles = ?, status = 'review' "
                    "WHERE source = ? AND status != 'manual'",
                    (category if category in category_keys else None, label, confidence,
                     result.get("reason") or "AI suggestion requires review",
                     json.dumps(titles, ensure_ascii=False), source),
                )
                conn.commit()
                processed.append({"source": source, "suggestion": result, "status": "review"})
        except Exception:
            app.logger.exception("Source classification failed for %s", source)
            try:
                update_source_category(
                    conn,
                    source,
                    row.get("category") or "Info",
                    row.get("label") or source,
                    status="failed",
                    reason="classification failed",
                    sample_titles=titles,
                )
            except Exception:
                pass
            failed.append({"source": source, "error": "classification failed"})

    remaining = [
        row for row in source_rows(conn)
        if not row.get("alias_target")
        and not row.get("user_override")
        and row.get("status") in ("pending", "failed")
    ]
    return {
        "processed": processed,
        "failed": failed,
        "remaining": len(remaining),
    }


def _extract_domains_for_source(conn, source: str) -> list[str]:
    """Extract unique domain names from recent articles of a given source."""
    try:
        rows = conn.execute(
            "SELECT body_html, telegraph_url FROM articles "
            "WHERE COALESCE(NULLIF(group_source, ''), source) = ? "
            "AND (body_html != '' OR telegraph_url != '') "
            "ORDER BY timestamp DESC LIMIT 5",
            (source,),
        ).fetchall()
    except Exception:
        return []
    all_domains = []
    seen = set()
    for row in rows:
        html = (row["body_html"] or "") + " " + (row["telegraph_url"] or "")
        for domain in extract_domains_from_html(html):
            if domain not in seen:
                seen.add(domain)
                all_domains.append(domain)
    return all_domains[:10]


_source_classify_jobs = {}
_source_classify_jobs_lock = threading.Lock()


def _update_source_classify_job(job_id: str, **updates):
    updates["updated_at"] = time.time()
    with _source_classify_jobs_lock:
        if job_id in _source_classify_jobs:
            _source_classify_jobs[job_id].update(updates)


def _run_source_classify_job(job_id: str, user_id: int, config: dict, force: bool):
    _update_source_classify_job(job_id, status="running")
    processed_total = 0
    failed_total = 0
    remaining = 0
    try:
        config = dict(config)
        config["user_id"] = user_id
        for _ in range(200):
            result = _classify_source_batch(config, limit=50, force=force)
            processed = len(result.get("processed") or [])
            failed = len(result.get("failed") or [])
            remaining = int(result.get("remaining") or 0)
            processed_total += processed
            failed_total += failed
            _update_source_classify_job(
                job_id,
                processed=processed_total,
                failed=failed_total,
                remaining=remaining,
            )
            if remaining <= 0:
                break
            if processed == 0:
                break
            # With force=True, one pass over non-manual sources is enough.
            if force:
                break
        _update_source_classify_job(
            job_id,
            status="completed",
            processed=processed_total,
            failed=failed_total,
            remaining=remaining,
        )
    except Exception:
        app.logger.exception("Source classification job failed")
        _update_source_classify_job(job_id, status="failed", error="classification failed")


def _run_auto_source_classification_once():
    if not _auto_source_classify_lock.acquire(blocking=False):
        return
    try:
        configs = _get_source_classification_users()
        if not configs:
            return
        for config in configs:
            result = _classify_source_batch(config, AUTO_SOURCE_CLASSIFY_BATCH_LIMIT)
            if result["processed"] or result["failed"]:
                print(
                    f"[source-classify] user={config['user_id']} processed="
                    f"{len(result['processed'])} failed={len(result['failed'])} "
                    f"remaining={result['remaining']}"
                )
            if result["remaining"] == 0:
                break
    finally:
        _auto_source_classify_lock.release()


def _auto_source_classification_loop():
    import time as _time
    _time.sleep(90)
    cleanup_counter = 0
    while True:
        try:
            _run_auto_source_classification_once()
        except Exception as e:
            print(f"[source-classify] Error in loop: {e}")
        # Periodically discover new sources and clean up empty ones (every 10 cycles)
        cleanup_counter += 1
        if cleanup_counter % 10 == 0:
            try:
                conn = _get_news_db()
                if conn:
                    result = maintain_source_categories(conn, force=True)
                    conn.commit()
                    if result.get("discovered") or result.get("deleted"):
                        print(
                            f"[source-maintenance] discovered {result.get('discovered', 0)}, "
                            f"removed {result.get('deleted', 0)} stale source(s)"
                        )
            except Exception as e:
                print(f"[source-maintenance] Error: {e}")
        _time.sleep(AUTO_SOURCE_CLASSIFY_INTERVAL_SECONDS)


def _run_auto_summary_once():
    """Fill cached summaries in small batches so daily summaries can reuse them."""
    if not _auto_summary_lock.acquire(blocking=False):
        return
    try:
        users = _get_auto_summary_users()
        if not users:
            return

        for config in users:
            articles = _fetch_unsummarized_articles(AUTO_SUMMARY_BATCH_LIMIT)
            if not articles:
                continue
            print(f"[auto-summary] User {config['user_id']}: summarizing {len(articles)} article(s)")
            for article in articles:
                try:
                    summary, cached = _generate_article_summary(article["id"], config)
                    if not cached:
                        print(f"[auto-summary] Cached summary for article {article['id']}: {article.get('title', '')[:50]}")
                except Exception as e:
                    safe_error = _redact_secrets(e)
                    _save_ai_result(article["id"], summary_error=safe_error)
                    print(f"[auto-summary] Article {article.get('id')}: failed: {safe_error}")
    finally:
        _auto_summary_lock.release()


def _auto_summary_loop():
    """Background loop for opt-in automatic article summaries."""
    import time as _time
    _time.sleep(45)
    while True:
        try:
            _run_auto_summary_once()
        except Exception as e:
            print(f"[auto-summary] Error in loop: {e}")
        _time.sleep(AUTO_SUMMARY_INTERVAL_SECONDS)


@app.route("/ai/daily-summary/send", methods=["POST"])
@require_role("admin")
def ai_daily_summary_send():
    """Manually force-send today's shared daily summary now (bypasses the fixed send window)."""
    result = _broadcast_daily_summary(force=True)
    return jsonify(result)


def _fetch_recent_articles(limit: int = 20) -> list[dict]:
    """Fetch most recent articles from news.db."""
    import sqlite3
    if not os.path.exists(NEWS_DB):
        return []
    try:
        with _news_db_conn() as conn:
            conn.row_factory = sqlite3.Row
            ensure_article_source_columns(conn)
            rows = conn.execute(
                "SELECT id, title, COALESCE(NULLIF(group_source, ''), source) AS source, "
                "       feed_source, group_source, publisher_domain, origin_source, "
                "       date, time FROM articles ORDER BY timestamp DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


# ─── Dedup helper ──────────────────────────────────────────


def _dedup_articles(articles: list[dict]) -> list[dict]:
    """Remove duplicate/similar articles. Prefers articles with body content.

    Heuristic: same normalized title → same event → keep first with body.
    """
    best_by_title = {}
    untitled = []
    for a in articles:
        title = (a.get("title") or "").strip().lower()
        # Normalize whitespace and common quote characters.
        normalized = re.sub("[\\s\"'“”‘’「」『』]+", " ", title).strip()
        if not normalized:
            untitled.append(a)
            continue
        existing = best_by_title.get(normalized)
        if existing is None or (a.get("body_html") and not existing.get("body_html")):
            best_by_title[normalized] = a
    return untitled + list(best_by_title.values())


def _fetch_articles_by_date(date_str: str, include_shared_summary: bool = True) -> list[dict]:
    """Fetch all arrivals in the digest cutoff window with cached event signals."""
    import sqlite3
    if not os.path.exists(NEWS_DB):
        return []
    try:
        _init_ai_results_table()
        with _news_db_conn() as conn:
            conn.row_factory = sqlite3.Row
            ensure_article_source_columns(conn)
            init_source_categories(conn)
            start, cutoff = _digest_window(date_str)
            summary_expr = (
                "COALESCE(NULLIF(r.summary, ''), a.summary)"
                if include_shared_summary else "a.summary"
            )
            rows = conn.execute(
                "SELECT a.id, a.title, COALESCE(NULLIF(a.group_source, ''), a.source) AS source, "
                "a.origin_source, a.date, a.time, a.body_html, a.ingested_at, "
                f"{summary_expr} AS summary, "
                "a.telegraph_url, r.digest_signals_json, r.digest_signals_version, "
                "COALESCE(d.category, ?) AS category "
                "FROM articles a "
                "LEFT JOIN ai_results r ON r.article_id = a.id "
                "LEFT JOIN source_categories sc ON sc.source = COALESCE(NULLIF(a.group_source, ''), a.source) "
                "LEFT JOIN source_category_definitions d ON d.category = sc.category AND d.enabled = 1 "
                "WHERE a.ingested_at >= ? AND a.ingested_at < ? ORDER BY a.ingested_at ASC, a.id ASC",
                (UNCATEGORIZED, start, cutoff),
            ).fetchall()
        result = []
        for row in rows:
            article = dict(row)
            try:
                article["digest_signals"] = (json.loads(article["digest_signals_json"])
                    if article["digest_signals_version"] == SIGNAL_VERSION else None)
            except (TypeError, json.JSONDecodeError):
                article["digest_signals"] = None
            article["url"] = (f"https://news.rayyu.me/#/article/"
                              f"{(article.get('date') or '')[2:]}-{article['id']}")
            result.append(article)
        return result
    except Exception:
        return []


def _save_digest_signals(article_id: int, signals: dict) -> bool:
    return _save_digest_signals_batch([(article_id, signals)])


def _save_digest_signals_batch(items: list[tuple[int, dict]]) -> bool:
    if not items:
        return True
    if not os.path.exists(NEWS_DB) or not _init_ai_results_table():
        return False
    try:
        with _news_db_conn() as conn:
            conn.executemany(
                "INSERT INTO ai_results "
                "(article_id, digest_signals_json, digest_signals_version, digest_signals_generated_at) "
                "VALUES (?, ?, ?, datetime('now')) "
                "ON CONFLICT(article_id) DO UPDATE SET "
                "digest_signals_json = excluded.digest_signals_json, "
                "digest_signals_version = excluded.digest_signals_version, "
                "digest_signals_generated_at = excluded.digest_signals_generated_at",
                [(article_id, json.dumps(signals, ensure_ascii=False), SIGNAL_VERSION)
                 for article_id, signals in items],
            )
            conn.commit()
        return True
    except Exception:
        app.logger.exception("Could not save digest signals")
        return False


def _fetch_unsummarized_articles(limit: int = AUTO_SUMMARY_BATCH_LIMIT) -> list[dict]:
    """Fetch recent arrivals, retrying failures only within the same 24-hour window."""
    import sqlite3
    if not os.path.exists(NEWS_DB):
        return []
    try:
        _init_ai_results_table()
        recent_start = int(time.time()) - 86400
        with _news_db_conn() as conn:
            conn.row_factory = sqlite3.Row
            ensure_article_source_columns(conn)
            rows = conn.execute(
                "SELECT a.id, a.title, COALESCE(NULLIF(a.group_source, ''), a.source) AS source, "
                "a.origin_source, a.summary, a.body_html "
                "FROM articles a "
                "LEFT JOIN ai_results r ON r.article_id = a.id "
                "WHERE a.ingested_at >= ? "
                "AND (r.summary IS NULL OR r.summary = '') "
                "AND (r.summary_error_at IS NULL OR datetime(r.summary_error_at, '+6 hours') < datetime('now')) "
                "AND (a.body_html != '' OR a.summary != '') "
                "ORDER BY a.ingested_at ASC LIMIT ?",
                (recent_start, limit),
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def _fetch_article_body(article_id: int) -> dict | None:
    """Fetch full article body from news.db."""
    import sqlite3
    if not os.path.exists(NEWS_DB):
        return None
    try:
        with _news_db_conn() as conn:
            conn.row_factory = sqlite3.Row
            ensure_article_source_columns(conn)
            row = conn.execute(
                "SELECT id, title, COALESCE(NULLIF(group_source, ''), source) AS source, "
                "origin_source, summary, body_html FROM articles WHERE id = ?",
                (article_id,),
            ).fetchone()
        return dict(row) if row else None
    except Exception:
        return None


# ─── AI Results Cache (prevent duplicate generation) ────────

# ai_results migrations can issue CREATE/ALTER statements.  They need the
# same single-flight protection as the article-schema migration, but callers
# can point NEWS_DB at a different file during tests or runtime maintenance.
_ai_results_schema_lock = threading.RLock()
_ai_results_schema_ready_paths: set[str] = set()


def _add_ai_results_column_if_missing(conn, cols: set[str], name: str, col_type: str) -> None:
    if name not in cols:
        conn.execute(f"ALTER TABLE ai_results ADD COLUMN {name} {col_type}")
        cols.add(name)


def _init_ai_results_table() -> bool:
    """Create and latch the ai_results schema for the current news database."""
    import sqlite3
    db_path = os.path.abspath(NEWS_DB)
    if not os.path.exists(db_path):
        return False
    with _ai_results_schema_lock:
        # Recheck under the lock: a database can disappear between the initial
        # fast-path check and acquiring the process-wide initializer lock.
        if not os.path.exists(db_path):
            return False
        if db_path in _ai_results_schema_ready_paths:
            return True
        try:
            with _news_db_conn() as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS ai_results (
                        article_id   INTEGER PRIMARY KEY,
                        summary      TEXT,
                        translation  TEXT,
                        translation_updated_at TEXT,
                        title_summary TEXT,
                        title_summary_error TEXT,
                        title_summary_error_at TEXT,
                        title_translation_error TEXT,
                        title_translation_error_at TEXT,
                        title_summary_provider TEXT,
                        title_summary_model TEXT,
                        title_summary_by_user_id INTEGER,
                        summary_provider TEXT,
                        summary_model TEXT,
                        summary_by_user_id INTEGER,
                        summary_generated_at TEXT,
                        translation_provider TEXT,
                        translation_model TEXT,
                        translation_by_user_id INTEGER,
                        translation_generated_at TEXT,
                        summary_error TEXT,
                        summary_error_at TEXT,
                        digest_signals_json TEXT,
                        digest_signals_version INTEGER,
                        digest_signals_generated_at TEXT,
                        updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
                    )
                """)
                cols = {
                    row[1]
                    for row in conn.execute("PRAGMA table_info(ai_results)").fetchall()
                }
                _add_ai_results_column_if_missing(conn, cols, "summary_error", "TEXT")
                _add_ai_results_column_if_missing(conn, cols, "summary_error_at", "TEXT")
                _add_ai_results_column_if_missing(conn, cols, "digest_signals_json", "TEXT")
                _add_ai_results_column_if_missing(conn, cols, "digest_signals_version", "INTEGER")
                _add_ai_results_column_if_missing(conn, cols, "digest_signals_generated_at", "TEXT")
                _add_ai_results_column_if_missing(conn, cols, "translation_updated_at", "TEXT")
                _add_ai_results_column_if_missing(conn, cols, "title_summary", "TEXT")
                _add_ai_results_column_if_missing(conn, cols, "title_summary_error", "TEXT")
                _add_ai_results_column_if_missing(conn, cols, "title_summary_error_at", "TEXT")
                _add_ai_results_column_if_missing(conn, cols, "title_translation_error", "TEXT")
                _add_ai_results_column_if_missing(conn, cols, "title_translation_error_at", "TEXT")
                _add_ai_results_column_if_missing(conn, cols, "title_summary_provider", "TEXT")
                _add_ai_results_column_if_missing(conn, cols, "title_summary_model", "TEXT")
                _add_ai_results_column_if_missing(conn, cols, "title_summary_by_user_id", "INTEGER")
                _add_ai_results_column_if_missing(conn, cols, "summary_provider", "TEXT")
                _add_ai_results_column_if_missing(conn, cols, "summary_model", "TEXT")
                _add_ai_results_column_if_missing(conn, cols, "summary_by_user_id", "INTEGER")
                _add_ai_results_column_if_missing(conn, cols, "summary_generated_at", "TEXT")
                _add_ai_results_column_if_missing(conn, cols, "translation_provider", "TEXT")
                _add_ai_results_column_if_missing(conn, cols, "translation_model", "TEXT")
                _add_ai_results_column_if_missing(conn, cols, "translation_by_user_id", "INTEGER")
                _add_ai_results_column_if_missing(conn, cols, "translation_generated_at", "TEXT")
                if "updated_at" not in cols:
                    conn.execute("ALTER TABLE ai_results ADD COLUMN updated_at TEXT")
                    conn.execute("UPDATE ai_results SET updated_at = datetime('now') WHERE updated_at IS NULL")
                conn.commit()
            # Do not latch until commit completed and the connection context
            # closed successfully. A failed initializer must remain retryable.
            _ai_results_schema_ready_paths.add(db_path)
            return True
        except sqlite3.OperationalError as exc:
            if "duplicate column name" in str(exc).lower():
                # Another process added the column while we waited for the
                # write lock. The schema is now current; latch so we skip
                # the PRAGMA+ALTER on the next call.
                with _news_db_conn() as conn:
                    current = {
                        row[1]
                        for row in conn.execute("PRAGMA table_info(ai_results)").fetchall()
                    }
                expected = {
                    "summary_error", "summary_error_at", "translation_updated_at",
                    "title_summary", "title_summary_error", "title_summary_error_at",
                    "title_translation_error", "title_translation_error_at",
                    "title_summary_provider", "title_summary_model",
                    "title_summary_by_user_id", "summary_provider", "summary_model",
                    "summary_by_user_id", "summary_generated_at",
                    "translation_provider", "translation_model",
                    "translation_by_user_id", "translation_generated_at", "updated_at",
                    "digest_signals_json", "digest_signals_version", "digest_signals_generated_at",
                }
                if expected.issubset(current):
                    _ai_results_schema_ready_paths.add(db_path)
                    return True
                app.logger.warning("[ai-results] schema init raced but still incomplete: %s", exc)
                return False
            app.logger.exception("[ai-results] schema initialization failed: %s", exc)
            return False
        except Exception as exc:
            app.logger.exception("[ai-results] schema initialization failed: %s", exc)
            return False


def _get_ai_result(article_id: int) -> dict | None:
    """Get cached AI result (summary/translation) for an article."""
    import sqlite3
    if not os.path.exists(NEWS_DB):
        return None
    try:
        with _news_db_conn() as conn:
            conn.row_factory = sqlite3.Row
            _init_ai_results_table()
            row = conn.execute(
                "SELECT summary, translation, summary_error, summary_error_at, "
                "title_summary, title_summary_error, title_summary_error_at, "
                "title_summary_provider, title_summary_model, title_summary_by_user_id, "
                "summary_provider, summary_model, summary_by_user_id, summary_generated_at, "
                "translation_provider, translation_model, translation_by_user_id, "
                "translation_generated_at "
                "FROM ai_results WHERE article_id = ?",
                (article_id,),
            ).fetchone()
        return dict(row) if row else None
    except Exception:
        return None


_SANITIZE_ALLOWED_TAGS = {
    "p", "br", "b", "strong", "i", "em", "u", "s", "a", "ul", "ol", "li",
    "blockquote", "h1", "h2", "h3", "h4", "h5", "h6", "dd", "dt", "dl",
    "figure", "figcaption", "img", "span", "div", "table", "thead", "tbody",
    "tr", "td", "th", "code", "pre", "sub", "sup", "hr",
}
_SANITIZE_ALLOWED_ATTRS = {"a": {"href"}, "img": {"src", "alt"}}


def _sanitize_translated_html(html: str) -> str:
    """Whitelist-sanitize AI-translated article HTML before it enters the
    shared cache.

    Manual translate/summarize results come from whichever user happened to
    trigger them (browser-generated with their own API key), and even
    admin-driven auto-translation results come from an LLM reading
    attacker-influenced article text — never trust returned HTML as safe to
    innerHTML-render for every other user without stripping scripts, event
    handlers, and javascript:/data: URLs first.
    """
    if not html:
        return html
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all(True):
        if tag.name not in _SANITIZE_ALLOWED_TAGS:
            tag.unwrap()
            continue
        allowed_attrs = _SANITIZE_ALLOWED_ATTRS.get(tag.name, set())
        for attr in list(tag.attrs.keys()):
            if attr not in allowed_attrs:
                del tag[attr]
        if tag.name == "a" and tag.get("href"):
            href = tag["href"].strip()
            if not re.match(r"^https?://|^mailto:", href, re.I):
                del tag["href"]
            else:
                tag["rel"] = "noopener noreferrer"
                tag["target"] = "_blank"
        if tag.name == "img" and tag.get("src"):
            src = tag["src"].strip()
            # Allow absolute http(s) URLs and our own same-origin image proxy
            # endpoints. The manual-translate flow feeds already-proxied body
            # HTML (img src="/img-cache?url=...") through here, so an https-only
            # rule silently strips *every* image from the translated result.
            # These relative URLs only ever load an image via our own cache; the
            # real risk this guards against is javascript:/data: srcs.
            if not re.match(r"^https?://|^/img-(?:cache|proxy)\?", src, re.I):
                tag.decompose()
    return str(soup)


def _sanitize_plain_text(text: str, max_len: int = 4000) -> str:
    """Strip any HTML markup for values that must always be plain text."""
    if not text:
        return text
    cleaned = BeautifulSoup(text, "html.parser").get_text()
    return cleaned.strip()[:max_len]


def _sanitize_translation_payload(translation: str) -> str:
    stripped = translation.strip()
    if stripped.startswith("{"):
        try:
            data = json.loads(stripped)
        except (json.JSONDecodeError, TypeError):
            return _sanitize_translated_html(translation)
        data["html"] = _sanitize_translated_html(data.get("html") or "")
        data["title"] = _sanitize_plain_text(data.get("title") or "", max_len=300)
        return json.dumps(data, ensure_ascii=False)
    if stripped.startswith("["):
        # Legacy per-segment array shape ([{"id":0,"text":"<b>..</b>"}, ...]) —
        # no live route writes this anymore, but sanitize each item's HTML
        # defensively in case old data ever gets re-saved through here.
        try:
            items = json.loads(stripped)
        except (json.JSONDecodeError, TypeError):
            return _sanitize_translated_html(translation)
        if not isinstance(items, list):
            return _sanitize_translated_html(translation)
        for item in items:
            if isinstance(item, dict) and "text" in item:
                item["text"] = _sanitize_translated_html(item.get("text") or "")
        return json.dumps(items, ensure_ascii=False)
    return _sanitize_translated_html(translation)


def _save_ai_result(article_id: int, summary: str | None = None,
                    translation: str | None = None,
                    summary_error: str | None = None,
                    title_summary: str | None = None,
                    title_summary_error: str | None = None,
                    title_summary_provider: str | None = None,
                    title_summary_model: str | None = None,
                    title_summary_by_user_id: int | None = None,
                    title_translation_error: str | None = None,
                    clear_title_translation_error: bool = False,
                    summary_provider: str | None = None,
                    summary_model: str | None = None,
                    summary_by_user_id: int | None = None,
                    summary_generated_at: str | None = None,
                    translation_provider: str | None = None,
                    translation_model: str | None = None,
                    translation_by_user_id: int | None = None,
                    translation_generated_at: str | None = None) -> bool:
    """Save or update AI result for an article."""
    import sqlite3
    if summary is not None:
        summary = _sanitize_plain_text(summary)
    if translation is not None:
        translation = _sanitize_translation_payload(translation)
    if summary_error is not None:
        summary_error = _redact_secrets(summary_error)
    if title_summary_error is not None:
        title_summary_error = _redact_secrets(title_summary_error)
    if title_translation_error is not None:
        title_translation_error = _redact_secrets(title_translation_error)
    if not os.path.exists(NEWS_DB):
        return False
    conn = None
    try:
        conn = _news_db_connect()
        _init_ai_results_table()
        conn.execute(
            """
            INSERT INTO ai_results
            (article_id, summary, translation, translation_updated_at, summary_error, summary_error_at,
             title_summary, title_summary_error, title_summary_error_at,
             title_translation_error, title_translation_error_at,
             title_summary_provider, title_summary_model, title_summary_by_user_id)
            VALUES (?, ?, ?, NULL, ?, CASE WHEN ? IS NULL THEN NULL ELSE datetime('now') END,
                    ?, ?, CASE WHEN ? IS NULL THEN NULL ELSE datetime('now') END,
                    ?, CASE WHEN ? IS NULL THEN NULL ELSE datetime('now') END,
                    ?, ?, ?)
            ON CONFLICT(article_id) DO UPDATE SET
                summary = COALESCE(excluded.summary, summary),
                translation = COALESCE(excluded.translation, translation),
                translation_updated_at = CASE
                    WHEN excluded.translation IS NOT NULL THEN datetime('now')
                    ELSE translation_updated_at
                END,
                summary_error = CASE
                    WHEN excluded.summary IS NOT NULL THEN NULL
                    WHEN excluded.summary_error IS NOT NULL THEN excluded.summary_error
                    ELSE summary_error
                END,
                summary_error_at = CASE
                    WHEN excluded.summary IS NOT NULL THEN NULL
                    WHEN excluded.summary_error IS NOT NULL THEN datetime('now')
                    ELSE summary_error_at
                END,
                title_summary = COALESCE(excluded.title_summary, title_summary),
                title_summary_error = CASE
                    WHEN excluded.title_summary IS NOT NULL THEN NULL
                    WHEN excluded.title_summary_error IS NOT NULL THEN excluded.title_summary_error
                    ELSE title_summary_error
                END,
                title_summary_error_at = CASE
                    WHEN excluded.title_summary IS NOT NULL THEN NULL
                    WHEN excluded.title_summary_error IS NOT NULL THEN datetime('now')
                    ELSE title_summary_error_at
                END,
                title_translation_error = CASE
                    WHEN ? = 1 THEN NULL
                    WHEN excluded.title_translation_error IS NOT NULL THEN excluded.title_translation_error
                    ELSE title_translation_error
                END,
                title_translation_error_at = CASE
                    WHEN ? = 1 THEN NULL
                    WHEN excluded.title_translation_error IS NOT NULL THEN datetime('now')
                    ELSE title_translation_error_at
                END,
                title_summary_provider = COALESCE(excluded.title_summary_provider, title_summary_provider),
                title_summary_model = COALESCE(excluded.title_summary_model, title_summary_model),
                title_summary_by_user_id = COALESCE(excluded.title_summary_by_user_id, title_summary_by_user_id),
                updated_at = datetime('now')
            """,
            (
                article_id,
                summary,
                translation,
                summary_error[:500] if summary_error else None,
                summary_error,
                title_summary,
                title_summary_error[:500] if title_summary_error else None,
                title_summary_error,
                title_translation_error[:500] if title_translation_error else None,
                title_translation_error,
                title_summary_provider,
                title_summary_model,
                title_summary_by_user_id,
                1 if clear_title_translation_error else 0,
                1 if clear_title_translation_error else 0,
            ),
        )
        if summary is not None:
            conn.execute(
                """
                UPDATE ai_results SET
                    summary_provider = ?,
                    summary_model = ?,
                    summary_by_user_id = ?,
                    summary_generated_at = COALESCE(?, datetime('now'))
                WHERE article_id = ?
                """,
                (
                    summary_provider,
                    summary_model,
                    summary_by_user_id,
                    summary_generated_at,
                    article_id,
                ),
            )
        if translation is not None:
            conn.execute(
                """
                UPDATE ai_results SET
                    translation_provider = ?,
                    translation_model = ?,
                    translation_by_user_id = ?,
                    translation_generated_at = COALESCE(?, datetime('now'))
                WHERE article_id = ?
                """,
                (
                    translation_provider,
                    translation_model,
                    translation_by_user_id,
                    translation_generated_at,
                    article_id,
                ),
            )
        conn.commit()
        return True
    except Exception:
        app.logger.exception("Failed to save shared AI result for article %s", article_id)
        return False
    finally:
        if conn:
            conn.close()


# ─── AI Result Cache (read-only) ──────────────────────────


def _publish_translation_update(article_id: int):
    """Publish a completed automatic full-body translation writeback.

    This is intentionally separate from ``_save_ai_result`` because manual
    browser translations are cache-only and must not invalidate article detail
    caches before their underlying ``articles.body_html`` has changed.
    """
    if not os.path.exists(NEWS_DB):
        return
    conn = None
    try:
        conn = _news_db_connect()
        _init_ai_results_table()
        conn.execute(
            """
            INSERT INTO ai_results (article_id, translation_updated_at)
            VALUES (?, strftime('%Y-%m-%d %H:%M:%f', 'now'))
            ON CONFLICT(article_id) DO UPDATE SET
                translation_updated_at = excluded.translation_updated_at,
                updated_at = datetime('now')
            """,
            (article_id,),
        )
        conn.commit()
    finally:
        if conn:
            conn.close()


@app.route("/ai/translation-updates", methods=["GET"])
@require_role("user", "admin")
def ai_translation_updates():
    """Return IDs whose shared translation cache changed after ``since``.

    This deliberately exposes only article IDs and a cursor: the normal result
    endpoint remains responsible for applying each viewer's sharing settings.
    """
    since = (request.args.get("since") or "").strip()
    since_ts = since
    since_id = 0
    if "|" in since:
        since_ts, since_id_text = since.rsplit("|", 1)
        try:
            since_id = int(since_id_text)
        except ValueError:
            return jsonify({"error": "invalid cursor"}), 400
        if since_id < 0:
            return jsonify({"error": "invalid cursor"}), 400
    if since:
        try:
            datetime.strptime(since_ts, "%Y-%m-%d %H:%M:%S.%f")
        except ValueError:
            try:
                datetime.strptime(since_ts, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                return jsonify({"error": "invalid cursor"}), 400

    if not os.path.exists(NEWS_DB):
        return jsonify({"items": [], "cursor": since})

    conn = None
    try:
        conn = _news_db_connect()
        conn.row_factory = sqlite3.Row
        _init_ai_results_table()
        if not since:
            cursor = conn.execute(
                "SELECT strftime('%Y-%m-%d %H:%M:%f', 'now')"
            ).fetchone()[0]
            return jsonify({"items": [], "cursor": cursor})
        rows = conn.execute(
            "SELECT article_id, translation_updated_at FROM ai_results "
            "WHERE translation_updated_at IS NOT NULL "
            "AND (translation_updated_at > ? "
            "OR (translation_updated_at = ? AND article_id > ?)) "
            "ORDER BY translation_updated_at ASC, article_id ASC LIMIT 500",
            (since_ts, since_ts, since_id),
        ).fetchall()
        items = [{"id": row["article_id"]} for row in rows]
        cursor = (
            f"{rows[-1]['translation_updated_at']}|{rows[-1]['article_id']}"
            if rows else since
        )
        return jsonify({"items": items, "cursor": cursor})
    except Exception:
        return jsonify({"items": [], "cursor": since}), 500
    finally:
        if conn:
            conn.close()


@app.route("/ai/result/<int:article_id>", methods=["GET"])
@require_role("user", "admin")
def ai_get_result(article_id):
    """Return cached AI result (summary/translation) without generating.

    Shared cache, but each field is only returned to viewers who opted into
    seeing it (Settings → AI → 共享) — see share_view_summary/share_view_translation.
    """
    cached = dict(_get_ai_result(article_id) or {})
    for private_key in (
        "summary_provider",
        "summary_model",
        "summary_by_user_id",
        "summary_generated_at",
        "translation_provider",
        "translation_model",
        "translation_by_user_id",
        "translation_generated_at",
    ):
        cached.pop(private_key, None)
    settings = get_user_settings(g.user_id) or {}
    share_active = is_share_active(settings)
    if not share_active or not settings.get("share_view_summary"):
        cached.pop("summary", None)
        cached.pop("summary_error", None)
        cached.pop("summary_error_at", None)
    if not share_active or not settings.get("share_view_translation"):
        cached.pop("translation", None)
    elif not settings.get("share_view_title") and cached.get("translation"):
        translation = str(cached["translation"]).strip()
        if translation.startswith("{"):
            try:
                payload = json.loads(translation)
            except (json.JSONDecodeError, TypeError):
                payload = None
            if isinstance(payload, dict):
                payload.pop("title", None)
                cached["translation"] = json.dumps(payload, ensure_ascii=False)
    return jsonify(cached)


# ─── Source Categories ─────────────────────────────────────

def _promote_legacy_admin_source_settings(conn: sqlite3.Connection) -> None:
    """Migrate the first administrator's old private source overrides once."""
    global _legacy_admin_source_settings_promoted
    if _legacy_admin_source_settings_promoted:
        return
    try:
        with _legacy_admin_source_settings_lock:
            if _legacy_admin_source_settings_promoted:
                return
            admin = get_db().execute(
                "SELECT id FROM users WHERE role = 'admin' ORDER BY id LIMIT 1"
            ).fetchone()
            if not admin:
                return
            promote_user_source_settings(conn, int(admin["id"]))
            _legacy_admin_source_settings_promoted = True
    except Exception as exc:
        print(f"[sources] Failed to promote legacy administrator settings: {exc}")


@app.route("/sources", methods=["GET"])
@require_role("user", "admin")
def list_sources():
    conn = _get_news_db()
    if not conn:
        return jsonify({"error": "news db not found"}), 404
    _promote_legacy_admin_source_settings(conn)
    definitions = category_definitions(conn)
    all_definitions = category_definitions(conn, include_disabled=True)
    return jsonify({
        "categories": [row["category"] for row in definitions],
        "category_names": {row["category"]: row["label"] for row in definitions},
        "category_definitions": all_definitions,
        "sources": source_rows(conn),
    })


@app.route("/sources/categories", methods=["PUT", "DELETE"])
@require_role("admin")
def manage_source_categories():
    conn = _get_news_db()
    if not conn:
        return jsonify({"error": "news db not found"}), 404
    data = request.get_json(silent=True) or {}
    try:
        if request.method == "PUT":
            result = save_category_definition(
                conn,
                data.get("category", ""),
                data.get("label", ""),
                data.get("sort_order", 0),
                data.get("enabled", True),
                data.get("migration_target"),
            )
            return jsonify({"ok": True, "category": result, "categories": category_definitions(conn)})
        deleted = delete_category_definition(
            conn, str(data.get("category", "")), str(data.get("target", ""))
        )
        return jsonify({"ok": True, "changed": deleted, "categories": category_definitions(conn)})
    except (ValueError, TypeError) as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/sources/domains", methods=["GET", "PUT"])
@require_role("admin")
def manage_publisher_domains():
    conn = _get_news_db()
    if not conn:
        return jsonify({"error": "news db not found"}), 404
    if request.method == "GET":
        rows = conn.execute(
            "SELECT domain, group_source, enabled, is_builtin, updated_at "
            "FROM publisher_domains ORDER BY domain"
        ).fetchall()
        return jsonify({"domains": [dict(row) for row in rows]})
    data = request.get_json(silent=True) or {}
    try:
        row = save_publisher_domain(
            conn, str(data.get("domain", "")), str(data.get("group_source", "")),
            bool(data.get("enabled", True)),
        )
        return jsonify({"ok": True, "domain": row})
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/sources/articles", methods=["GET"])
@require_role("user", "admin")
def list_source_articles():
    conn = _get_news_db()
    if not conn:
        return jsonify({"error": "news db not found"}), 404
    sources_json = (request.args.get("sources") or "").strip()
    if sources_json:
        try:
            base_sources = [
                str(item).strip()
                for item in json.loads(sources_json)
                if str(item).strip()
            ][:100]
        except (json.JSONDecodeError, TypeError, ValueError):
            return jsonify({"error": "invalid sources"}), 400
    else:
        source = (request.args.get("source") or "").strip()
        base_sources = [source] if source else []
    if not base_sources:
        return jsonify({"error": "source required"}), 400
    try:
        limit = min(max(int(request.args.get("limit", "80")), 1), 200)
    except ValueError:
        limit = 80

    sources = []
    for source in base_sources:
        sources.append(source)
        sources.extend(source_aliases_for_target(conn, source))
    sources = list(dict.fromkeys(sources))
    placeholders = ",".join("?" * len(sources))
    rows = conn.execute(
        "SELECT id, title, original_title, COALESCE(NULLIF(group_source, ''), source) AS source, "
        "       feed_source, group_source, publisher_domain, origin_source, "
        "       date, time, timestamp, thumb, has_full_content "
        f"FROM articles WHERE COALESCE(NULLIF(group_source, ''), source) IN ({placeholders}) "
        "ORDER BY timestamp DESC LIMIT ?",
        (*sources, limit),
    ).fetchall()
    return jsonify({
        "source": base_sources[0],
        "sources": sources,
        "items": [dict(row) for row in rows],
    })


def _resolve_shared_source_alias_closure(
    conn: sqlite3.Connection,
    base_sources: list[str],
) -> list[str]:
    """Resolve every shared alias that transitively targets the source group."""
    normalized = list(dict.fromkeys(
        str(source).strip() for source in base_sources if str(source).strip()
    ))
    if not normalized:
        return []
    seed_values = ", ".join("(?)" for _source in normalized)
    rows = conn.execute(
        f"""
        WITH RECURSIVE source_group(source) AS (
            VALUES {seed_values}
            UNION
            SELECT aliases.alias_source
            FROM source_aliases AS aliases
            JOIN source_group AS resolved
              ON aliases.target_source = resolved.source
        )
        SELECT source FROM source_group
        """,
        normalized,
    ).fetchall()
    resolved = [
        row["source"] if isinstance(row, sqlite3.Row) else row[0]
        for row in rows
    ]
    return list(dict.fromkeys((*normalized, *resolved)))


def _delete_source_article_ids_in_transaction(
    conn: sqlite3.Connection,
    article_ids: list[int],
    *,
    deleted_by: int | None = None,
    strict_ai_results: bool = False,
) -> tuple[dict, list[int]]:
    """Delete article rows inside a caller-owned news database transaction."""
    ids = sorted({int(article_id) for article_id in article_ids if int(article_id) > 0})
    if not ids:
        return {"deleted": 0, "deleted_sources": 0}, []
    placeholders = ",".join("?" * len(ids))
    existing = conn.execute(
        f"SELECT id, title, COALESCE(NULLIF(group_source, ''), source) AS source "
        f"FROM articles WHERE id IN ({placeholders})",
        ids,
    ).fetchall()
    if not existing:
        return {"deleted": 0, "deleted_sources": 0}, []
    for row in existing:
        conn.execute(
            """
            INSERT INTO deleted_articles (article_id, title, source, deleted_by, deleted_at)
            VALUES (?, ?, ?, ?, datetime('now'))
            ON CONFLICT(article_id) DO UPDATE SET
                title = excluded.title,
                source = excluded.source,
                deleted_by = excluded.deleted_by,
                deleted_at = excluded.deleted_at
            """,
            (row["id"], row["title"] or "", row["source"] or "", deleted_by),
        )
    existing_ids = [int(row["id"]) for row in existing]
    existing_placeholders = ",".join("?" * len(existing_ids))
    cur = conn.execute(f"DELETE FROM articles WHERE id IN ({existing_placeholders})", existing_ids)
    if strict_ai_results:
        ai_results_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'ai_results'"
        ).fetchone()
        if ai_results_exists:
            conn.execute(
                f"DELETE FROM ai_results WHERE article_id IN ({existing_placeholders})",
                existing_ids,
            )
    else:
        try:
            conn.execute(
                f"DELETE FROM ai_results WHERE article_id IN ({existing_placeholders})",
                existing_ids,
            )
        except sqlite3.OperationalError:
            # Preserve the public deletion helper's legacy best-effort cleanup of
            # optional AI rows. Article/tombstone deletion remains authoritative.
            pass
    return {"deleted": cur.rowcount, "deleted_sources": 0}, existing_ids


def _cleanup_deleted_article_side_effects(
    article_ids: list[int], *, maintain_image_cache: bool,
) -> None:
    """Run cross-database/cache cleanup only after news deletion commits."""
    if not article_ids:
        return
    placeholders = ",".join("?" * len(article_ids))
    app_db = get_db()
    app_db.execute(f"DELETE FROM favorites WHERE article_id IN ({placeholders})", article_ids)
    app_db.commit()
    if maintain_image_cache:
        threading.Thread(target=unpin_article_images, args=(article_ids,), daemon=True).start()


def _delete_article_ids(article_ids: list[int], deleted_by: int | None = None,
                        *, maintain_image_cache: bool = True,
                        cleanup_sources: bool = True) -> dict:
    """Delete articles with the existing public commit and cleanup behavior."""
    ids = sorted({int(article_id) for article_id in article_ids if int(article_id) > 0})
    if not ids:
        return {"deleted": 0, "deleted_sources": 0}
    conn = _get_news_db()
    if not conn:
        raise FileNotFoundError("news db not found")
    _ensure_news_schema(conn)
    try:
        result, existing_ids = _delete_source_article_ids_in_transaction(
            conn, ids, deleted_by=deleted_by,
        )
        if not existing_ids:
            # Honour cleanup_sources here too. A batch whose articles were all already
            # gone would otherwise still trigger the full-table stale-source scan that
            # the batched purge passes cleanup_sources=False precisely to avoid.
            return {
                "deleted": 0,
                "deleted_sources": cleanup_stale_source_categories(conn) if cleanup_sources else 0,
            }
        conn.commit()
        result["deleted_sources"] = cleanup_stale_source_categories(conn) if cleanup_sources else 0
        _cleanup_deleted_article_side_effects(
            existing_ids, maintain_image_cache=maintain_image_cache,
        )
        return result
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise


@app.route("/articles", methods=["DELETE"])
@require_role("admin")
def delete_articles():
    data = request.get_json(silent=True) or {}
    raw_ids = data.get("ids") or data.get("article_ids") or []
    if not isinstance(raw_ids, list):
        return jsonify({"error": "ids must be a list"}), 400
    try:
        result = _delete_article_ids([int(item) for item in raw_ids], deleted_by=g.user_id)
    except FileNotFoundError:
        return jsonify({"error": "news db not found"}), 404
    except (TypeError, ValueError):
        return jsonify({"error": "invalid article id"}), 400
    return jsonify({"ok": True, **result})


@app.route("/articles/<int:article_id>", methods=["DELETE"])
@require_role("admin")
def delete_article(article_id):
    try:
        result = _delete_article_ids([article_id], deleted_by=g.user_id)
    except FileNotFoundError:
        return jsonify({"error": "news db not found"}), 404
    return jsonify({"ok": True, **result})


@app.route("/sources/articles", methods=["DELETE"])
@require_role("admin")
def delete_source_articles():
    conn = _get_news_db()
    if not conn:
        return jsonify({"error": "news db not found"}), 404
    data = request.get_json(silent=True) or {}
    base_sources = []
    if isinstance(data.get("sources"), list):
        base_sources = [str(item).strip() for item in data.get("sources") if str(item).strip()]
    else:
        source = (data.get("source") or "").strip()
        if source:
            base_sources = [source]
    base_sources = base_sources[:100]
    if not base_sources:
        return jsonify({"error": "source required"}), 400
    try:
        # Source/news schema bootstrap may commit migrations or seeding, so both
        # must finish before the group deletion transaction begins.
        init_source_categories(conn)
        _ensure_news_schema(conn)
    except Exception as exc:
        print(f"[sources] Failed to initialize source metadata: {exc}")
        return jsonify({"error": "failed to delete source metadata"}), 500
    try:
        conn.execute("BEGIN IMMEDIATE")
        sources = _resolve_shared_source_alias_closure(conn, base_sources)
        placeholders = ",".join("?" * len(sources))
        rows = conn.execute(
            f"SELECT id FROM articles WHERE COALESCE(NULLIF(group_source, ''), source) IN ({placeholders})",
            sources,
        ).fetchall()
        result, deleted_ids = _delete_source_article_ids_in_transaction(
            conn,
            [int(row["id"]) for row in rows],
            deleted_by=g.user_id,
            strict_ai_results=True,
        )
        result["deleted_sources"] = delete_source_metadata(conn, sources)
        conn.commit()
    except Exception as exc:
        if conn.in_transaction:
            conn.rollback()
        print(f"[sources] Failed to delete source metadata: {exc}")
        return jsonify({"error": "failed to delete source metadata"}), 500
    _cleanup_deleted_article_side_effects(deleted_ids, maintain_image_cache=True)
    return jsonify({"ok": True, "sources": sources, **result})


# ─── Server statistics & storage maintenance (admin) ───────────

def _path_size(path: str) -> int:
    """Size of a single file plus its SQLite -wal/-shm sidecars, if present."""
    total = 0
    for suffix in ("", "-wal", "-shm"):
        try:
            total += os.path.getsize(path + suffix)
        except OSError:
            pass
    return total


def _dir_size(path: str) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def _read_int_file(path: str) -> int | None:
    try:
        with open(path) as fh:
            value = fh.read().strip()
        return int(value)
    except (OSError, ValueError):
        return None


def _host_memory_total_bytes() -> int | None:
    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def _container_resource_stats() -> dict:
    """Container memory/CPU with cgroup breakdown and per-process RSS.

    Fields that can't be read are returned as None so the UI shows N/A rather
    than erroring; there is no psutil dependency."""
    stats = {"mem_used_bytes": None, "mem_limit_bytes": None,
             "cpu_percent": 0.0, "cpu_count": None,
             "memory_breakdown": {}, "processes": []}
    try:
        memory = runtime_memory_snapshot()
        cgroup = memory.get("cgroup") or {}
        processes = memory.get("processes") or []
    except Exception:
        app.logger.exception("Failed to read runtime memory snapshot")
        cgroup = {}
        processes = []
    stats["memory_breakdown"] = cgroup
    stats["processes"] = processes
    stats["mem_used_bytes"] = cgroup.get("current_bytes")
    limit = cgroup.get("max_bytes")
    if limit is not None and not (
        cgroup.get("version") == "v1" and limit >= (1 << 62)
    ):
        stats["mem_limit_bytes"] = limit
    elif stats["mem_used_bytes"] is not None:
        stats["mem_limit_bytes"] = _host_memory_total_bytes()

    try:
        stats["cpu_count"] = len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        stats["cpu_count"] = os.cpu_count()

    cgroup_v2 = os.path.exists("/sys/fs/cgroup/cgroup.controllers")

    def cpu_usage_usec() -> int | None:
        if cgroup_v2:
            try:
                with open("/sys/fs/cgroup/cpu.stat") as fh:
                    for line in fh:
                        if line.startswith("usage_usec"):
                            return int(line.split()[1])
            except (OSError, ValueError, IndexError):
                return None
            return None
        ns = _read_int_file("/sys/fs/cgroup/cpuacct/cpuacct.usage")
        return ns // 1000 if ns is not None else None

    usage = cpu_usage_usec()
    now = time.monotonic()
    if usage is not None:
        with _container_cpu_sample_lock:
            previous = _container_cpu_sample[0]
            _container_cpu_sample[0] = (usage, now)
        if previous is not None:
            previous_usage, previous_at = previous
            elapsed = now - previous_at
            if elapsed > 0:
                cpus = stats["cpu_count"] or 1
                busy = (usage - previous_usage) / (elapsed * 1_000_000 * cpus) * 100
                stats["cpu_percent"] = round(max(0.0, busy), 1)
    return stats


def _refresh_runtime_stats(http_get=None) -> dict:
    """Read refresh-process metrics without coupling tests to a real socket."""
    get = http_get or requests.get
    try:
        response = get(
            "http://127.0.0.1:8081/internal/runtime-stats",
            timeout=1,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("refresh runtime stats must be an object")
        metrics = {}
        for key in (
            "article_cache_items",
            "article_cache_bytes",
            "article_cache_inflight",
        ):
            value = payload.get(key)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"invalid refresh runtime metric: {key}")
            metrics[key] = value
        return metrics
    except (requests.RequestException, AttributeError, TypeError, ValueError):
        return {"status": "unavailable"}


def _memory_sample_once() -> dict:
    """Collect one bounded memory sample without propagating reader failures."""
    sampled_at = int(time.time())
    try:
        snapshot = runtime_memory_snapshot()
        cgroup = snapshot["cgroup"]
        processes = sorted(
            snapshot["processes"],
            key=lambda process: -(process.get("rss_bytes") or 0),
        )[:5]
        current_bytes = cgroup.get("current_bytes")
        warning = (
            isinstance(current_bytes, int)
            and not isinstance(current_bytes, bool)
            and current_bytes >= MEMORY_WARN_BYTES
        )
    except Exception as exc:
        return {
            "sampled_at": sampled_at,
            "warning": False,
            "error": f"snapshot unavailable: {exc}",
        }

    sample = {
        "sampled_at": sampled_at,
        "warning": warning,
        "cgroup": cgroup,
        "processes": processes,
    }
    try:
        refresh_stats = _refresh_runtime_stats()
        sample["application"] = {"refresh": refresh_stats}
        if refresh_stats.get("status") == "unavailable":
            sample["error"] = "refresh unavailable"
    except Exception as exc:
        sample["application"] = {"refresh": {"status": "unavailable"}}
        sample["error"] = f"refresh unavailable: {exc}"
    return sample


def _memory_monitor_loop(
    *,
    sample_once=None,
    sleep_fn=None,
    interval_seconds: int | None = None,
) -> None:
    """Log compact memory samples forever from this single worker thread."""
    sampler = sample_once or _memory_sample_once
    sleeper = sleep_fn or time.sleep
    interval = (
        MEMORY_MONITOR_INTERVAL_SECONDS
        if interval_seconds is None
        else max(10, interval_seconds)
    )
    while True:
        try:
            sample = sampler()
        except Exception as exc:
            sample = {
                "sampled_at": int(time.time()),
                "warning": False,
                "error": f"memory sample failed: {exc}",
            }
        print(
            "[memory] "
            + json.dumps(sample, separators=(",", ":"), ensure_ascii=False),
            flush=True,
        )
        sleeper(interval)


_memory_monitor_thread: threading.Thread | None = None
_memory_monitor_thread_lock = threading.Lock()


def _announce_memory_monitor_started() -> None:
    """Keep lifecycle notices outside the reserved JSON sample prefix."""
    print("[memory-monitor] Background memory monitor thread started")


def _start_memory_monitor_thread(*, thread_factory=None):
    """Start at most one monitor daemon; importing this module never calls it."""
    global _memory_monitor_thread
    if not MEMORY_MONITOR_ENABLED:
        return None
    factory = thread_factory or threading.Thread
    with _memory_monitor_thread_lock:
        if (
            _memory_monitor_thread is not None
            and _memory_monitor_thread.is_alive()
        ):
            return _memory_monitor_thread

        candidate = factory(
            target=_memory_monitor_loop,
            daemon=True,
            name="memory-monitor",
        )
        # Publish only a successfully started thread. If start() raises, the
        # previous None/dead registration remains retryable.
        candidate.start()
        _memory_monitor_thread = candidate
        return candidate


def _count_scalar(conn, sql: str) -> int:
    try:
        row = conn.execute(sql).fetchone()
        return int(row[0]) if row and row[0] is not None else 0
    except sqlite3.OperationalError:
        return 0


@app.route("/admin/server-stats", methods=["GET"])
@require_role("admin")
def admin_server_stats():
    # Sample the sibling process before opening/using any SQLite connection in
    # this view; a one-second local network timeout must never extend a DB lock.
    refresh_stats = _refresh_runtime_stats()
    news_db = _path_size(NEWS_DB)
    app_db = _path_size(os.path.join(DATA_DIR, "raynews.db"))
    image_cache_db = _path_size(os.path.join(DATA_DIR, "image_cache", "cache.db"))
    avatars = _dir_size(os.path.join(DATA_DIR, "avatars"))
    misc = sum(_path_size(os.path.join(DATA_DIR, name))
               for name in ("news.json", "fetcher_state.json", "raynews_secret"))
    cache = cache_stats()
    image_cache_files = int(cache.get("used_bytes") or 0)
    data_dir_total = news_db + app_db + image_cache_db + image_cache_files + avatars + misc

    try:
        du = shutil.disk_usage(DATA_DIR)
        disk = {"total": du.total, "used": du.used, "free": du.free}
    except OSError:
        disk = {"total": None, "used": None, "free": None}

    articles = cached_images = 0
    news_conn = _get_news_db()
    if news_conn is not None:
        articles = _count_scalar(news_conn, "SELECT COUNT(*) FROM articles")
    cached_images = int(cache.get("count") or 0)

    try:
        favorites = _count_scalar(get_db(), "SELECT COUNT(*) FROM favorites")
    except Exception:
        favorites = 0
    try:
        users = count_users()
    except Exception:
        users = 0

    return jsonify({
        "storage": {
            "news_db": news_db,
            "app_db": app_db,
            "image_cache_db": image_cache_db,
            "image_cache_files": image_cache_files,
            "avatars": avatars,
            "misc": misc,
            "data_dir_total": data_dir_total,
        },
        "disk": disk,
        "image_cache": cache,
        "counts": {
            "articles": articles,
            "users": users,
            "favorites": favorites,
            "cached_images": cached_images,
        },
        "container": _container_resource_stats(),
        "application": {"refresh": refresh_stats},
        # Same date.today() the purge endpoint's "not after today" validation uses
        # (_parse_purge_before_date), which respects the process's TZ env var. The
        # admin UI's date picker uses this instead of the browser's own UTC/local
        # date so the two never disagree about what "today" means.
        "server_date": date.today().isoformat(),
    })


_PURGE_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


_container_cpu_sample_lock = threading.Lock()
_container_cpu_sample: list[tuple[int, float] | None] = [None]
PURGE_BATCH_SIZE = 200
_purge_tasks: dict[str, dict] = {}
_purge_tasks_lock = threading.Lock()
# Terminal purge tasks stay queryable so the admin UI can still poll for a result
# after the run finishes, but the dict must not grow for the life of the process.
# Mirrors refresh_server.py's REFRESH_JOB_HISTORY_LIMIT.
PURGE_TASK_HISTORY_LIMIT = 16


def _trim_purge_tasks_locked() -> None:
    """Drop the oldest finished tasks once the history exceeds its limit.

    Callers must already hold _purge_tasks_lock. Running tasks are never evicted:
    a long purge must stay pollable no matter how many short ones ran after it.
    """
    finished = [
        (task.get("finished_at") or 0, task_id)
        for task_id, task in _purge_tasks.items()
        if task.get("status") != "running"
    ]
    excess = len(_purge_tasks) - PURGE_TASK_HISTORY_LIMIT
    if excess <= 0:
        return
    for _finished_at, task_id in sorted(finished)[:excess]:
        _purge_tasks.pop(task_id, None)


def _parse_purge_before_date(value: str) -> date | None:
    """Accept only real calendar dates up to today for destructive purges."""
    if not _PURGE_DATE_RE.fullmatch(value):
        return None
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None
    return parsed if parsed <= date.today() else None


def _purge_articles_before(before_date: str, dry_run: bool,
                           deleted_by: int | None = None) -> dict:
    """Select articles dated on/before before_date, excluding any article
    favorited by any user, and (unless dry_run) delete them plus their
    non-shared cached images."""
    conn = _get_news_db()
    if not conn:
        raise FileNotFoundError("news db not found")
    favorited = set(get_all_favorite_article_ids())
    rows = []
    skipped_invalid_dates = 0
    for row in conn.execute("SELECT id, body_html, thumb, date FROM articles WHERE date != ''").fetchall():
        try:
            article_date = datetime.strptime(str(row["date"]), "%Y-%m-%d").date()
        except ValueError:
            skipped_invalid_dates += 1
            continue
        if article_date <= datetime.strptime(before_date, "%Y-%m-%d").date():
            rows.append(row)
    matched = len(rows)
    candidates = [r for r in rows if int(r["id"]) not in favorited]
    excluded = matched - len(candidates)
    if dry_run or not candidates:
        return {"matched": matched, "to_delete": len(candidates),
                "favorites_excluded": excluded, "skipped_invalid_dates": skipped_invalid_dates, "deleted": 0}

    payloads = [(int(r["id"]), r["body_html"], r["thumb"]) for r in candidates]
    candidate_ids = {article_id for article_id, _body, _thumb in payloads}
    protected_hashes: set[str] = set()
    for row in conn.execute("SELECT id, body_html, thumb FROM articles").fetchall():
        if int(row["id"]) in candidate_ids:
            continue
        protected_hashes.update(
            _url_hash(url)
            for url, _is_cover in collect_image_urls(row["body_html"], row["thumb"], body_limit=None)
        )
    task_id = uuid.uuid4().hex
    task = {
        "task_id": task_id, "status": "running", "total": len(payloads), "processed": 0,
        "deleted": 0, "images_deleted": 0, "errors": 0, "started_at": int(time.time()),
        "favorites_excluded": excluded,
    }
    with _purge_tasks_lock:
        _purge_tasks[task_id] = task
        _trim_purge_tasks_locked()

    def _notify(final_task: dict) -> None:
        try:
            user = get_user(deleted_by) if deleted_by else None
            to_email = (user or {}).get("email") if user else _admin_email_address()
            api_key = os.environ.get("RESEND_API_KEY", "")
            if not api_key or not to_email:
                print("[purge] completion email not configured")
                return
            elapsed = max(0, int(final_task["finished_at"] - final_task["started_at"]))
            send_email(api_key, to_email, f"RayNews 清理任务{final_task['status']}",
                       f"<h2>RayNews 清理任务{final_task['status']}</h2>"
                       f"<p>已删除文章：{final_task['deleted']} / {final_task['total']}</p>"
                       f"<p>已删除图片缓存：{final_task['images_deleted']}<br>耗时：{elapsed} 秒<br>错误：{final_task['errors']}</p>",
                       idempotency_key=f"purge-{task_id}")
        except Exception as exc:
            print(f"[purge] completion email failed: {exc}")

    def _run_purge():
        cache_conn = None
        try:
            cache_conn = open_cache_connection()
            cache_conn.execute("PRAGMA busy_timeout = 5000")
            for start in range(0, len(payloads), PURGE_BATCH_SIZE):
                batch = payloads[start:start + PURGE_BATCH_SIZE]
                result = _delete_article_ids(
                    [article_id for article_id, _body, _thumb in batch], deleted_by=deleted_by,
                    maintain_image_cache=False, cleanup_sources=False,
                )
                deleted_images = 0
                for article_id, body_html, thumb in batch:
                    try:
                        deleted_images += evict_article_images(
                            body_html, thumb, article_id,
                            protected_hashes=protected_hashes, conn=cache_conn,
                        )
                    except Exception as exc:
                        with _purge_tasks_lock:
                            task["errors"] += 1
                        print(f"[purge] image eviction failed for {article_id}: {exc}")
                cache_conn.commit()
                with _purge_tasks_lock:
                    task["processed"] += len(batch)
                    task["deleted"] += result.get("deleted", 0)
                    task["images_deleted"] += deleted_images
                time.sleep(0.02)
            orphaned = evict_unreferenced_images(protected_hashes, conn=cache_conn)
            cache_conn.commit()
            # Every per-batch _delete_article_ids() call above ran with
            # cleanup_sources=False, so the stale-source sweep the batches skipped
            # happens once here. _get_news_db() must be called from *this* thread:
            # it returns a thread-local connection, and reusing the requesting
            # thread's connection would drive one sqlite3 connection from two
            # threads at once — the exact hazard the per-thread design removes.
            purge_conn = _get_news_db()
            if purge_conn is not None:
                cleanup_stale_source_categories(purge_conn)
                purge_conn.commit()
            with _purge_tasks_lock:
                task["images_deleted"] += orphaned
                task["status"] = "completed" if not task["errors"] else "completed_with_errors"
        except Exception:
            app.logger.exception("Image purge failed")
            with _purge_tasks_lock:
                task["status"] = "failed"
                task["errors"] += 1
                task["error"] = "purge failed"
        finally:
            if cache_conn:
                cache_conn.close()
            with _purge_tasks_lock:
                task["finished_at"] = int(time.time())
                final_task = dict(task)
            _notify(final_task)

    threading.Thread(target=_run_purge, daemon=True, name=f"article-purge-{task_id[:8]}").start()

    return {"matched": matched, "favorites_excluded": excluded, "to_delete": len(payloads),
            "deleted": 0, "task_id": task_id, "status": "running"}


@app.route("/admin/articles/purge", methods=["POST"])
@require_role("admin")
def admin_purge_articles():
    data = request.get_json(silent=True) or {}
    before_date = str(data.get("before_date") or "").strip()
    if _parse_purge_before_date(before_date) is None:
        return jsonify({"error": "before_date must be a real YYYY-MM-DD date not later than today"}), 400
    dry_run = bool(data.get("dry_run"))
    if not dry_run:
        with _purge_tasks_lock:
            if any(task.get("status") == "running" for task in _purge_tasks.values()):
                return jsonify({"error": "another purge task is already running"}), 409
    try:
        result = _purge_articles_before(before_date, dry_run, deleted_by=g.user_id)
    except FileNotFoundError:
        return jsonify({"error": "news db not found"}), 404
    return jsonify({"ok": True, "before_date": before_date, "dry_run": dry_run, **result})


@app.route("/admin/articles/purge/<task_id>", methods=["GET"])
@require_role("admin")
def admin_purge_status(task_id):
    with _purge_tasks_lock:
        task = _purge_tasks.get(task_id)
        return jsonify(dict(task)) if task else (jsonify({"error": "purge task not found"}), 404)


@app.route("/sources", methods=["PUT"])
@require_role("admin")
def save_source():
    conn = _get_news_db()
    if not conn:
        return jsonify({"error": "news db not found"}), 404
    data = request.get_json(silent=True) or {}
    source = (data.get("source") or "").strip()
    category = (data.get("category") or "").strip()
    label = (data.get("label") or "").strip()
    if not source:
        return jsonify({"error": "source required"}), 400
    if not valid_category(conn, category):
        return jsonify({"error": "invalid category"}), 400
    if not label:
        return jsonify({"error": "label required"}), 400
    label = clamp_weighted(label, 20)
    try:
        target_source = find_merge_target(conn, source, label)
        if target_source:
            target = merge_source(conn, source, target_source)
            return jsonify({
                **target,
                "merged": True,
                "merged_from": source,
                "target_source": target_source,
            })
        row = update_source_category(
            conn, source, category, label, status="manual", reason="user edited",
        )
        return jsonify(row)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@app.route("/sources/classify", methods=["POST"])
@require_role("admin")
def classify_sources():
    conn = _get_news_db()
    if not conn:
        return jsonify({"error": "news db not found"}), 404
    # Source labels are site-wide, so they are produced with the server API —
    # never with whichever admin happened to click. See
    # _get_source_classification_users().
    configs = _get_source_classification_users()
    if not configs:
        return jsonify({"error": "请先在 管理员设置 → 服务端 API 配置并启用服务端 API"}), 400

    data = request.get_json(silent=True) or {}
    limit = min(max(int(data.get("limit", 50) or 50), 1), 100)
    force = bool(data.get("force"))
    return jsonify(_classify_source_batch(configs[0], limit, force))


@app.route("/sources/reinitialize", methods=["POST"])
@require_role("admin")
def reinitialize_sources():
    conn = _get_news_db()
    if not conn:
        return jsonify({"error": "news db not found"}), 404
    data = request.get_json(silent=True) or {}
    preserve_manual = data.get("preserve_manual", True) is not False
    if preserve_manual:
        conn.execute("DELETE FROM source_categories WHERE status != 'manual'")
    else:
        conn.execute("DELETE FROM source_categories")
        conn.execute("DELETE FROM source_aliases")
        conn.execute("DELETE FROM user_source_categories")
        conn.execute("DELETE FROM user_source_aliases")
    ensure_article_sources(conn)
    cleanup_stale_source_categories(conn)
    conn.commit()
    count = conn.execute("SELECT COUNT(*) AS c FROM source_categories").fetchone()["c"]
    return jsonify({"ok": True, "sources": count, "preserve_manual": preserve_manual})


@app.route("/sources/classify-job", methods=["POST"])
@require_role("admin")
def classify_sources_job():
    conn = _get_news_db()
    if not conn:
        return jsonify({"error": "news db not found"}), 404
    configs = _get_source_classification_users()
    if not configs:
        return jsonify({"error": "请先在 管理员设置 → 服务端 API 配置并启用服务端 API"}), 400
    config = configs[0]

    data = request.get_json(silent=True) or {}
    force = bool(data.get("force"))
    with _source_classify_jobs_lock:
        for job in _source_classify_jobs.values():
            if job.get("user_id") == g.user_id and job.get("status") in ("queued", "running"):
                return jsonify({"job_id": job["job_id"], "status": job["status"]}), 202
        job_id = uuid.uuid4().hex
        _source_classify_jobs[job_id] = {
            "job_id": job_id,
            "user_id": g.user_id,
            "status": "queued",
            "force": force,
            "created_at": time.time(),
            "updated_at": time.time(),
            "processed": 0,
            "failed": 0,
            "remaining": 0,
            "error": "",
        }
    threading.Thread(
        target=_run_source_classify_job,
        args=(job_id, g.user_id, config, force),
        daemon=True,
    ).start()
    return jsonify({"job_id": job_id, "status": "queued"}), 202


@app.route("/sources/classify-job/<job_id>", methods=["GET"])
@require_role("admin")
def classify_sources_job_status(job_id):
    with _source_classify_jobs_lock:
        job = _source_classify_jobs.get(job_id)
        if not job or job.get("user_id") != g.user_id:
            return jsonify({"error": "job not found"}), 404
        safe = {k: v for k, v in job.items() if k != "user_id"}
    return jsonify(safe)


def _telegram_channel_and_base() -> tuple[str, str]:
    """Resolve (channel, base_url) from TELEGRAM_CHANNEL_URL if set,
    otherwise fall back to legacy TELEGRAM_CHANNEL + hardcoded t.me domain."""
    channel_url = (os.environ.get("TELEGRAM_CHANNEL_URL") or "").strip()
    if channel_url:
        parsed = urlsplit(channel_url)
        path_parts = [p for p in parsed.path.split("/") if p]
        channel = path_parts[-1] if path_parts else ""
        return channel, f"{parsed.scheme}://{parsed.netloc}"
    channel = (os.environ.get("TELEGRAM_CHANNEL") or "").strip().lstrip("@")
    return channel, "https://t.me"


def _fetch_telegram_message_content(article_id: int) -> str:
    """Fetch the original Telegram message body for historical source repair."""
    channel, base_url = _telegram_channel_and_base()
    if not channel or channel == "your_channel":
        return ""
    url = f"{base_url}/{channel}/{article_id}?embed=1&mode=tme"
    try:
        resp = requests.get(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36"
                )
            },
            timeout=TELEGRAM_EMBED_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
    except Exception as exc:
        print(f"telegram source lookup failed for {article_id}: {exc}")
        return ""

    soup = BeautifulSoup(resp.text, "html.parser")
    text_el = soup.select_one(".tgme_widget_message_text.js-message_text")
    if not text_el:
        text_el = soup.select_one(".tgme_widget_message_text")
    return text_el.decode_contents() if text_el else ""


_source_redetect_jobs = {}
_source_redetect_jobs_lock = threading.Lock()
SOURCE_REDETECT_RETRY_SECONDS = 900


def _parse_source_redetect_options(data: dict) -> tuple[int, int, bool]:
    try:
        limit = min(max(int(data.get("limit", 2000) or 2000), 1), 10000)
    except (TypeError, ValueError):
        limit = 2000
    try:
        network_limit = min(max(int(data.get("network_limit", 1000) or 1000), 0), limit)
    except (TypeError, ValueError):
        network_limit = min(1000, limit)
    force_telegram = bool(data.get("force_telegram"))
    return limit, network_limit, force_telegram


def _cleanup_source_redetect_jobs():
    cutoff = time.time() - 7200
    with _source_redetect_jobs_lock:
        old_ids = [
            job_id for job_id, job in _source_redetect_jobs.items()
            if job.get("updated_at", job.get("created_at", 0)) < cutoff
        ]
        for job_id in old_ids:
            _source_redetect_jobs.pop(job_id, None)


def _update_source_redetect_job(job_id: str, **updates):
    updates["updated_at"] = time.time()
    with _source_redetect_jobs_lock:
        if job_id in _source_redetect_jobs:
            _source_redetect_jobs[job_id].update(updates)


def _reliable_source_detection(group: str | None, domain: str | None,
                               feed_source: str) -> tuple[str | None, str]:
    """Ignore the channel fallback returned when no publisher was identified."""
    from fetcher import detect_feed_source
    group = (group or "").strip()
    domain = (domain or "").strip()
    if domain or (group and group != detect_feed_source(channel=feed_source)):
        return group or None, domain
    return None, ""


def _classified_source_lookup(conn: sqlite3.Connection) -> tuple[dict[str, str], dict[str, str]]:
    """Reuse the same historical evidence as new article ingestion."""
    from fetcher import classified_source_history
    try:
        return classified_source_history(conn)
    except sqlite3.OperationalError:
        # A standalone legacy database may not have category metadata yet.
        return {}, {}


def _classified_source_keys(conn: sqlite3.Connection) -> set[str]:
    """Exact classified source identities; display labels are not identities."""
    try:
        return {
            row[0].casefold() for row in conn.execute(
                "SELECT source FROM source_categories WHERE status IN ('manual', 'classified')"
            )
        }
    except sqlite3.OperationalError:
        return set()


def _detect_source_for_redetect(conn: sqlite3.Connection, content: str,
                                preview_url: str, feed_source: str,
                                lookup: tuple[dict[str, str], dict[str, str]] | None = None,
                                ) -> tuple[str | None, str, bool]:
    """Prefer an already classified footer link over positional link guesses."""
    from fetcher import detect_group_source, source_from_classified_history

    learned = source_from_classified_history(
        content, lookup if lookup is not None else _classified_source_lookup(conn)
    )
    if learned:
        return learned[0], learned[1], True
    group, domain = detect_group_source(content, preview_url, feed_source)
    group, domain = _reliable_source_detection(group, domain, feed_source)
    return group, domain, False


def _redetect_article_sources_work(limit: int, network_limit: int, force_telegram: bool,
                                   job_id: str | None = None,
                                   pending_only: bool = False) -> dict:
    if not os.path.exists(NEWS_DB):
        raise FileNotFoundError("news db not found")
    conn = sqlite3.connect(NEWS_DB, timeout=30)
    conn.row_factory = sqlite3.Row
    ensure_article_source_columns(conn)
    classified_lookup = _classified_source_lookup(conn)
    classified_keys = _classified_source_keys(conn)
    rows = conn.execute(
        """
        SELECT id, title, source, feed_source, group_source, publisher_domain,
               source_detection_version, origin_source, body_html, summary, telegraph_url
        FROM articles
        WHERE (? = 0 OR (source_detection_version < 1
               AND source_detection_last_attempt_at <= ?
               AND NOT EXISTS (SELECT 1 FROM source_categories sc
                   WHERE sc.source = COALESCE(NULLIF(articles.group_source, ''), articles.source)
                   AND sc.status IN ('manual', 'classified'))))
        ORDER BY source_detection_last_attempt_at ASC, timestamp DESC
        LIMIT ?
        """,
        (1 if pending_only else 0, int(time.time()) - SOURCE_REDETECT_RETRY_SECONDS, limit),
    ).fetchall()
    changed = []
    skipped = 0
    telegram_checked = 0
    telegram_hits = 0
    checked = 0
    def update_progress():
        if job_id and (checked % 20 == 0 or checked == len(rows)):
            _update_source_redetect_job(
                job_id,
                checked=checked,
                updated=len(changed),
                skipped=skipped,
                telegram_checked=telegram_checked,
                telegram_hits=telegram_hits,
            )

    for row in rows:
        checked += 1
        current_group = (row["group_source"] or row["source"] or "").strip()
        # Existing manual/automatic classifications are training data, not
        # candidates for migration. Do not touch their article identity.
        if current_group.casefold() in classified_keys:
            skipped += 1
            update_progress()
            continue
        content = row["body_html"] or row["summary"] or row["title"] or ""
        detected_group, detected_domain, detected_classified = _detect_source_for_redetect(
            conn, content, row["telegraph_url"], row["feed_source"] or "", classified_lookup
        )
        fetched_telegram = False
        is_telegraph = bool(row["telegraph_url"])
        should_fetch_telegram = (
            telegram_checked < network_limit
            and (force_telegram or (not detected_classified and (
                is_telegraph or not detected_domain or not detected_group
            )))
        )
        if should_fetch_telegram:
            fetched_telegram = True
            telegram_checked += 1
            telegram_content = _fetch_telegram_message_content(row["id"])
            if telegram_content:
                fetched_group, fetched_domain, fetched_classified = _detect_source_for_redetect(
                    conn, telegram_content, row["telegraph_url"], row["feed_source"] or "",
                    classified_lookup,
                )
                if fetched_group and (
                    (fetched_classified and not detected_classified)
                    or (not detected_classified and (fetched_domain or not detected_group))
                ):
                    detected_group, detected_domain, detected_classified = (
                        fetched_group, fetched_domain, fetched_classified
                    )
                if fetched_group:
                    telegram_hits += 1
        current_feed = (row["feed_source"] or row["source"] or "").strip()
        current_origin = (row["origin_source"] or "").strip()
        if not detected_group and not detected_domain:
            # Keep the row pending: a later fetch may provide the missing via line.
            # A quota skip is not a failed attempt. Leave its timestamp at zero
            # so it gets the next run's network slots before attempted rows.
            if pending_only and fetched_telegram:
                conn.execute(
                    "UPDATE articles SET source_detection_last_attempt_at = ? WHERE id = ?",
                    (int(time.time()), row["id"]),
                )
                conn.commit()
            skipped += 1
            update_progress()
            continue
        next_group = detected_group or current_group or current_feed
        stored_domain = (row["publisher_domain"] or "").strip()
        # Older WeChat detections stored qq.com after collapsing mp.weixin.qq.com.
        # An explicit attribution is stronger evidence than that legacy domain.
        effective_domain = detected_domain if detected_classified else detected_domain or (
            "" if stored_domain == "qq.com" and detected_group else stored_domain
        )
        if effective_domain and not detected_classified:
            next_group = publisher_source_for_domain(conn, effective_domain) or next_group
        next_origin = next_group or current_origin
        conn.execute(
            "UPDATE articles SET source = ?, group_source = ?, publisher_domain = ?, "
            "origin_source = ?, source_detection_version = 1 WHERE id = ?",
            (next_group, next_group, effective_domain,
             next_origin, row["id"]),
        )
        conn.commit()
        if next_group != current_group or effective_domain != row["publisher_domain"]:
            changed.append({
                "id": row["id"], "title": row["title"],
                "from": current_group, "to": next_group,
                "origin_from": current_origin, "origin_to": next_origin,
            })
        else:
            skipped += 1
        update_progress()
    ensure_article_sources(conn)
    deleted_sources = cleanup_stale_source_categories(conn)
    conn.commit()
    conn.close()
    return {
        "checked": len(rows),
        "updated": len(changed),
        "skipped": skipped,
        "telegram_checked": telegram_checked,
        "telegram_hits": telegram_hits,
        "deleted_sources": deleted_sources,
        "changes": changed[:100],
    }


def _run_source_redetect_job(job_id: str, limit: int, network_limit: int, force_telegram: bool):
    _update_source_redetect_job(job_id, status="running")
    try:
        result = _redetect_article_sources_work(limit, network_limit, force_telegram, job_id)
        _update_source_redetect_job(job_id, status="completed", **result)
    except Exception:
        app.logger.exception("Source redetection job failed")
        _update_source_redetect_job(job_id, status="failed", error="redetection failed")


_source_identity_backfill_lock = threading.Lock()


def _migrate_unambiguous_manual_source_settings(conn: sqlite3.Connection) -> int:
    """Move legacy channel settings only when that channel resolves to one publisher."""
    rows = conn.execute(
        "SELECT source, category, label, status FROM source_categories "
        "WHERE status = 'manual'"
    ).fetchall()
    migrated = 0
    for row in rows:
        # A manual setting can move as soon as this feed has finished detection;
        # unrelated feeds may legitimately retain retryable articles indefinitely.
        unfinished = conn.execute(
            "SELECT 1 FROM articles WHERE feed_source = ? "
            "AND source_detection_version < 1 "
            "AND NOT EXISTS (SELECT 1 FROM source_categories sc "
            "WHERE sc.source = COALESCE(NULLIF(articles.group_source, ''), articles.source) "
            "AND sc.status IN ('manual', 'classified')) LIMIT 1", (row["source"],)
        ).fetchone()
        if unfinished:
            continue
        targets = conn.execute(
            "SELECT group_source, COUNT(*) AS n FROM articles "
            "WHERE feed_source = ? AND TRIM(group_source) != '' "
            "GROUP BY group_source ORDER BY n DESC",
            (row["source"],),
        ).fetchall()
        if len(targets) != 1 or targets[0]["group_source"] == row["source"]:
            continue
        target = targets[0]["group_source"]
        current = conn.execute(
            "SELECT status FROM source_categories WHERE source = ?", (target,)
        ).fetchone()
        if current and current["status"] == "manual":
            continue
        conn.execute(
            "INSERT INTO source_categories (source, category, label, status, reason) "
            "VALUES (?, ?, ?, 'manual', 'migrated from legacy feed label') "
            "ON CONFLICT(source) DO UPDATE SET category=excluded.category, "
            "label=excluded.label, status='manual', reason=excluded.reason",
            (target, row["category"], row["label"]),
        )
        migrated += 1
    conn.commit()
    return migrated


def _run_source_identity_backfill_once() -> tuple[dict, int, int]:
    result = _redetect_article_sources_work(
        limit=100, network_limit=10, force_telegram=False, pending_only=True
    )
    conn = _get_news_db()
    if not conn:
        return result, 0, 0
    remaining = conn.execute(
        "SELECT COUNT(*) FROM articles WHERE source_detection_version < 1 "
        "AND NOT EXISTS (SELECT 1 FROM source_categories sc "
        "WHERE sc.source = COALESCE(NULLIF(articles.group_source, ''), articles.source) "
        "AND sc.status IN ('manual', 'classified'))"
    ).fetchone()[0]
    migrated = _migrate_unambiguous_manual_source_settings(conn)
    return result, remaining, migrated


def _source_identity_backfill_loop():
    """Automatically finish old article source migration in short, resumable batches."""
    time.sleep(15)
    while True:
        if not os.path.exists(NEWS_DB):
            time.sleep(30)
            continue
        if not _source_identity_backfill_lock.acquire(blocking=False):
            time.sleep(2)
            continue
        try:
            result, remaining, migrated = _run_source_identity_backfill_once()
            if migrated:
                print(f"[source-migration] preserved {migrated} unambiguous manual label(s)")
            if result.get("checked") or result.get("updated"):
                print(
                    f"[source-migration] checked {result['checked']}, updated {result['updated']}, "
                    f"remaining {remaining}"
                )
            time.sleep(2 if remaining and result.get("checked") else 60)
        except Exception:
            app.logger.exception("Automatic source identity migration failed")
            time.sleep(30)
        finally:
            _source_identity_backfill_lock.release()


@app.route("/sources/redetect", methods=["POST"])
@require_role("admin")
def redetect_article_sources():
    if not os.path.exists(NEWS_DB):
        return jsonify({"error": "news db not found"}), 404
    data = request.get_json(silent=True) or {}
    limit, network_limit, force_telegram = _parse_source_redetect_options(data)
    _cleanup_source_redetect_jobs()
    with _source_redetect_jobs_lock:
        for job in _source_redetect_jobs.values():
            if job.get("user_id") == g.user_id and job.get("status") in ("queued", "running"):
                return jsonify({"job_id": job["job_id"], "status": job["status"]}), 202
        job_id = uuid.uuid4().hex
        _source_redetect_jobs[job_id] = {
            "job_id": job_id,
            "user_id": g.user_id,
            "status": "queued",
            "created_at": time.time(),
            "updated_at": time.time(),
            "limit": limit,
            "network_limit": network_limit,
            "checked": 0,
            "updated": 0,
            "skipped": 0,
            "telegram_checked": 0,
            "telegram_hits": 0,
            "deleted_sources": 0,
            "error": "",
        }
    threading.Thread(
        target=_run_source_redetect_job,
        args=(job_id, limit, network_limit, force_telegram),
        daemon=True,
    ).start()
    return jsonify({"job_id": job_id, "status": "queued"}), 202


@app.route("/sources/redetect/<job_id>", methods=["GET"])
@require_role("admin")
def source_redetect_status(job_id):
    with _source_redetect_jobs_lock:
        job = _source_redetect_jobs.get(job_id)
        if not job or job.get("user_id") != g.user_id:
            return jsonify({"error": "job not found"}), 404
        safe = {k: v for k, v in job.items() if k != "user_id"}
    return jsonify(safe)


@app.route("/sources/redetect-single", methods=["POST"])
@require_role("admin")
def redetect_single_source():
    """Re-detect source for all articles matching a given source name."""
    conn = _get_news_db()
    if not conn:
        return jsonify({"error": "news db not found"}), 404
    data = request.get_json(silent=True) or {}
    source = (data.get("source") or "").strip()
    if not source:
        return jsonify({"error": "source required"}), 400

    rows = conn.execute(
        "SELECT id, title, source, feed_source, group_source, publisher_domain, origin_source, body_html, summary, telegraph_url "
        "FROM articles "
        "WHERE COALESCE(NULLIF(group_source, ''), source) = ? "
        "ORDER BY timestamp DESC",
        (source,),
    ).fetchall()

    classified_lookup = _classified_source_lookup(conn)
    classified_keys = _classified_source_keys(conn)
    changed = []
    for row in rows:
        current_group = (row["group_source"] or row["source"] or "").strip()
        if current_group.casefold() in classified_keys:
            continue
        content = row["body_html"] or row["summary"] or row["title"] or ""
        detected_group, detected_domain, detected_classified = _detect_source_for_redetect(
            conn, content, row["telegraph_url"], row["feed_source"] or "", classified_lookup
        )
        if not detected_classified and not detected_domain:
            tg_content = _fetch_telegram_message_content(row["id"])
            if tg_content:
                fetched_group, fetched_domain, fetched_classified = _detect_source_for_redetect(
                    conn, tg_content, row["telegraph_url"], row["feed_source"] or "",
                    classified_lookup,
                )
                if fetched_group and (fetched_classified or fetched_domain or not detected_group):
                    detected_group, detected_domain, detected_classified = (
                        fetched_group, fetched_domain, fetched_classified
                    )
        current_origin = (row["origin_source"] or "").strip()
        if (not detected_group and not detected_domain
                and (not row["publisher_domain"] or row["publisher_domain"] == "qq.com")):
            continue
        next_group = detected_group or current_group
        stored_domain = (row["publisher_domain"] or "").strip()
        effective_domain = detected_domain if detected_classified else detected_domain or (
            "" if stored_domain == "qq.com" and detected_group else stored_domain
        )
        if effective_domain and not detected_classified:
            next_group = publisher_source_for_domain(conn, effective_domain) or next_group
        next_origin = next_group or current_origin
        if next_group == current_group and effective_domain == (row["publisher_domain"] or ""):
            continue
        conn.execute(
            "UPDATE articles SET source = ?, group_source = ?, publisher_domain = ?, "
            "origin_source = ?, source_detection_version = 1 WHERE id = ?",
            (next_group, next_group, effective_domain, next_origin, row["id"]),
        )
        changed.append({
            "id": row["id"],
            "title": row["title"],
            "from": current_group,
            "to": next_group,
            "origin_from": current_origin,
            "origin_to": next_origin,
        })

    ensure_article_sources(conn)
    cleanup_stale_source_categories(conn)
    conn.commit()
    return jsonify({"ok": True, "checked": len(rows), "updated": len(changed), "changes": changed[:50]})


# ─── Settings Routes ────────────────────────────────────────

def _settings_response(settings: dict | None) -> dict:
    """Serialize settings consistently for both settings endpoints."""
    safe = dict(settings or {})
    safe.setdefault("share_ai_results", 0)
    safe.setdefault("share_view_title", 0)
    safe.setdefault("share_view_translation", 0)
    safe.setdefault("share_view_summary", 0)
    safe.setdefault("share_suspended", 0)
    safe.setdefault("share_revalidation_failure_streak", 0)
    safe.setdefault("share_revalidation_last_failure_at", None)
    safe.setdefault("share_revalidation_last_failure_error", None)
    try:
        current_revision = int(safe.get("share_current_config_revision") or 0)
    except (TypeError, ValueError):
        current_revision = 0
    try:
        failure_revision = (
            int(safe["share_revalidation_failure_revision"])
            if safe.get("share_revalidation_failure_revision") is not None
            else None
        )
    except (TypeError, ValueError):
        failure_revision = None
    try:
        failure_streak = int(safe.get("share_revalidation_failure_streak") or 0)
    except (TypeError, ValueError):
        failure_streak = 0
    if failure_streak > 0 and failure_revision != current_revision:
        safe["share_revalidation_failure_streak"] = 0
        safe["share_revalidation_last_failure_at"] = None
        safe["share_revalidation_last_failure_error"] = None
    # On by default, including for accounts with no settings row yet — this must
    # agree with models.get_daily_summary_inapp_user_ids(), which is what
    # actually decides who receives the in-app copy.
    safe.setdefault("daily_summary_inapp_enabled", 1)
    safe["share_active"] = is_share_active(safe)
    nc = safe.get("notification_config", "{}")
    if isinstance(nc, str):
        try:
            nc = json.loads(nc)
        except (json.JSONDecodeError, TypeError):
            nc = {}
    safe["notification_config"] = nc
    safe.pop("share_last_check_revision", None)
    safe.pop("share_current_config_revision", None)
    safe.pop("share_intent_revision", None)
    safe.pop("share_revalidation_failure_revision", None)
    return safe

@app.route("/settings", methods=["GET"])
@require_role("user", "admin")
def get_settings():
    settings = get_user_settings(g.user_id)
    if not settings:
        settings = {
            "auto_translate_title": False,
            "auto_translate_content": False,
            "auto_title_summary_enabled": False,
            "auto_summary_enabled": False,
            "daily_summary_enabled": False,
            "daily_summary_inapp_enabled": True,
            "theme_preference": "system",
            "notification_config": {},
            "share_ai_results": False,
            "share_view_title": False,
            "share_view_translation": False,
            "share_view_summary": False,
            "share_suspended": False,
            "share_last_check_at": None,
            "share_last_check_ok": None,
            "share_last_check_error": None,
            "share_active": False,
        }
    return jsonify(_settings_response(settings))


@app.route("/settings", methods=["PUT"])
@require_role("user", "admin")
def update_settings():
    data = request.get_json(silent=True) or {}
    # Connectivity health and derived access are server-owned. Ignore forged
    # values on every settings request, including requests that do not carry the
    # sharing master switch.
    for key in (
        "share_suspended",
        "share_last_check_at",
        "share_last_check_ok",
        "share_last_check_error",
        "share_last_check_revision",
        "share_current_config_revision",
        "share_intent_revision",
        "share_active",
    ):
        data.pop(key, None)
    # Auto-summary/auto-translate are admin-only; silently drop these fields
    # for non-admin requests instead of erroring, so the rest of the payload
    # (theme, notifications, daily summary) still saves normally.
    if g.user_role != "admin":
        for key in (
            "auto_summary_enabled",
            "auto_translate_title",
            "auto_translate_content",
            "auto_title_summary_enabled",
        ):
            data.pop(key, None)
    # 邮件推送 needs an address to push to. Refuse the save rather than storing a
    # subscription that can never deliver: _deliver_daily_summary_email() only
    # collects recipients that have a to_email, so the user would be left looking
    # at an enabled toggle that silently sends nothing. Evaluated against the
    # merged state (payload over stored), so clearing the address while the
    # toggle is already on is rejected too, not just enabling without one.
    if "daily_summary_enabled" in data or "notification_config" in data:
        stored_settings = get_user_settings(g.user_id) or {}
        email_push_on = _is_enabled_value(
            data["daily_summary_enabled"] if "daily_summary_enabled" in data
            else stored_settings.get("daily_summary_enabled")
        )
        to_email = _resend_to_email(
            data["notification_config"] if "notification_config" in data
            else stored_settings.get("notification_config")
        )
        if email_push_on and not to_email:
            return jsonify({"error": "开启邮件推送前请先填写接收邮箱"}), 400
        if to_email and not is_valid_email(to_email):
            return jsonify({"error": "接收邮箱格式不正确"}), 400

    # Normalize notification_config to JSON string for storage
    if "notification_config" in data:
        nc = data["notification_config"]
        data["notification_config"] = json.dumps(nc) if isinstance(nc, dict) else nc
    if "theme_preference" in data:
        theme = str(data.get("theme_preference") or "system").strip().lower()
        data["theme_preference"] = theme if theme in {"system", "light", "dark"} else "system"

    # Sharing ("共享AI结果"): the master switch requires the user's own AI
    # config to pass a live connectivity test at save time — this isn't the
    # admin-only system AI, so every role (including plain "user") goes
    # through this check. Sub-toggles (title/translation/summary) may only be
    # turned on once the master switch is verified on; turning the master
    # switch off cascades off all sub-toggles regardless of what the request
    # body says, so we never persist a state where a sub-toggle is on while
    # the master is off.
    share_sub_keys = ("share_view_title", "share_view_translation", "share_view_summary")
    share_check_ok = False
    clear_share_suspension = False
    share_check_revision = 0
    observed_share_intent_revision = int(
        (get_user_settings(g.user_id) or {}).get("share_intent_revision") or 0
    )
    if "share_ai_results" in data:
        if _is_enabled_value(data.get("share_ai_results")):
            user_ai_config = get_ai_config(g.user_id)
            share_check_revision = (user_ai_config or {}).get("revision", 0)
            test_body, test_status = _run_ai_connection_test(user_ai_config)
            if test_status != 200:
                error = test_body.get("error", "connection test failed")
                transition = _apply_share_connectivity_result(
                    g.user_id, False, error, config_revision=share_check_revision
                )
                latest = get_user_settings(g.user_id) or {}
                paused = (
                    _is_enabled_value(latest.get("share_ai_results"))
                    and _is_enabled_value(latest.get("share_suspended"))
                    and transition in {"suspended", "unchanged"}
                )
                return jsonify({
                    "error": "personal AI API connection test failed",
                    "share_check": {
                        "ok": False,
                        "status": "paused" if paused else transition,
                        "error": _compact_share_error(error),
                    },
                }), 400

            # Do not accept health fields from the request. Keeping the
            # previous suspension until after persistence lets the atomic
            # transition own a restored notification exactly once.
            data["share_ai_results"] = 1
            for key in (
                "share_suspended",
                "share_last_check_ok",
                "share_last_check_at",
                "share_last_check_error",
            ):
                data.pop(key, None)
            share_check_ok = True
        else:
            data["share_ai_results"] = 0
            clear_share_suspension = True
            for key in share_sub_keys:
                data[key] = 0
    resulting_share_master = _is_enabled_value(
        data["share_ai_results"] if "share_ai_results" in data
        else (get_user_settings(g.user_id) or {}).get("share_ai_results")
    )
    if not resulting_share_master and any(_is_enabled_value(data.get(key)) for key in share_sub_keys):
        return jsonify({"error": "请先开启并通过校验“共享AI结果”"}), 400

    needs_ai_config = any(
        _is_enabled_value(data.get(key))
        for key in (
            "auto_summary_enabled",
            "auto_translate_title",
            "auto_translate_content",
            "auto_title_summary_enabled",
        )
    )
    if needs_ai_config:
        config = get_system_ai_config()
        if not config or not config.get("enabled") or not config.get("api_key"):
            return jsonify({
                "error": "请先在管理员设置→服务端API中配置系统AI"
            }), 400
    if share_check_ok:
        settings = set_user_settings_for_ai_config_revision(
            g.user_id,
            share_check_revision,
            observed_share_intent_revision,
            **data,
        )
        if settings is None:
            share_intent_changed = int(
                (get_user_settings(g.user_id) or {}).get("share_intent_revision")
                or 0
            ) != observed_share_intent_revision
            stale_error = (
                "Sharing settings changed during validation; retry enabling sharing"
                if share_intent_changed
                else "AI config changed during validation; retry enabling sharing"
            )
            return jsonify({
                "error": stale_error,
                "share_check": {
                    "ok": False,
                    "status": "stale_settings" if share_intent_changed else "stale_validation",
                    "error": stale_error,
                },
            }), 409
    else:
        settings = set_user_settings(g.user_id, **data)
    if clear_share_suspension:
        settings = set_share_health(
            g.user_id,
            share_suspended=0,
            share_revalidation_failure_streak=0,
            share_revalidation_failure_revision=None,
            share_revalidation_last_failure_at=None,
            share_revalidation_last_failure_error=None,
        )
    if not settings:
        return jsonify({"error": "update failed"}), 400
    if share_check_ok:
        _apply_share_connectivity_result(
            g.user_id, True, config_revision=share_check_revision
        )
        settings = get_user_settings(g.user_id) or {}
    if _is_enabled_value(data.get("auto_summary_enabled")):
        threading.Thread(target=_run_auto_summary_once, daemon=True).start()
    if _is_enabled_value(data.get("auto_translate_title")) or _is_enabled_value(data.get("auto_translate_content")):
        threading.Thread(target=_run_auto_translation_once, daemon=True).start()
    if _is_enabled_value(data.get("auto_title_summary_enabled")) or _is_enabled_value(data.get("auto_translate_title")):
        threading.Thread(target=_run_auto_title_process_once, daemon=True).start()
    return jsonify(_settings_response(settings))


def _is_enabled_value(value) -> bool:
    return value is True or value == 1 or str(value).strip().lower() in {"1", "true", "yes", "on"}


def is_share_active(settings: dict | None) -> bool:
    """Whether the caller may currently read user-enabled shared AI results."""
    settings = settings or {}
    try:
        revision_is_current = (
            settings.get("share_last_check_revision") is not None
            and settings.get("share_current_config_revision") is not None
            and int(settings["share_last_check_revision"])
            == int(settings["share_current_config_revision"])
        )
    except (TypeError, ValueError):
        revision_is_current = False
    return (
        _is_enabled_value(settings.get("share_ai_results"))
        and not _is_enabled_value(settings.get("share_suspended"))
        and _is_enabled_value(settings.get("share_last_check_ok"))
        and revision_is_current
    )


@app.route("/settings/test-notification", methods=["POST"])
@require_role("user", "admin")
def test_notification():
    """Send a test email via Resend API using configured server email settings."""
    settings = get_user_settings(g.user_id) or {}

    nc = settings.get("notification_config", "{}")
    if isinstance(nc, str):
        try:
            nc = json.loads(nc)
        except (json.JSONDecodeError, TypeError):
            nc = {}

    # Always use RESEND_API_KEY from environment
    api_key = os.environ.get("RESEND_API_KEY", "")
    to_email = _resend_to_email(nc)

    if not api_key:
        return jsonify({"error": "RESEND_API_KEY not set in server environment. Contact admin."}), 400
    if not to_email:
        return jsonify({"error": "notification not configured. Set recipient email in Settings."}), 400
    if not is_valid_email(to_email):
        return jsonify({"error": "接收邮箱格式不正确"}), 400

    try:
        from notifier import send_email
        from_email = os.environ.get("RAYNEWS_FROM_EMAIL") or "onboarding@resend.dev"
        result = send_email(api_key, to_email,
                            "RayNews 测试通知",
                            "<h2>✅ 配置成功</h2><p>这是一封来自 RayNews 的测试邮件，通知功能正常工作。</p>",
                            from_email=from_email)
        return jsonify({"ok": True, "id": result.get("id", "")})
    except Exception:
        app.logger.exception("Notification test send failed")
        return jsonify({"error": "notification send failed"}), 502


# ─── Health (unused section divider) ────────────────────────

# ─── Authenticated Refresh ────────────────────────────────

@app.route("/auth/refresh", methods=["POST"])
@require_role("user", "admin")
def protected_refresh():
    """Start a fetcher refresh job. Requires an authenticated user or admin."""
    import requests as http_req
    try:
        resp = http_req.post("http://127.0.0.1:8081/refresh", timeout=5)
        payload = resp.json()
        return jsonify(payload), resp.status_code
    except (http_req.RequestException, ValueError):
        app.logger.exception("Refresh service request failed")
        return jsonify({"error": "refresh service unavailable"}), 502
    except Exception:
        app.logger.exception("Unexpected refresh request failure")
        return jsonify({"error": "internal server error"}), 500


@app.route("/auth/refresh/status", methods=["GET"])
@require_role("user", "admin")
def protected_refresh_status():
    """Return the current fetcher refresh job status."""
    import requests as http_req
    try:
        job_id = (request.args.get("job_id") or "").strip()
        if job_id:
            resp = http_req.get(
                "http://127.0.0.1:8081/refresh/status",
                params={"job_id": job_id},
                timeout=5,
            )
        else:
            resp = http_req.get("http://127.0.0.1:8081/refresh/status", timeout=5)
        payload = resp.json()
        return jsonify(payload), resp.status_code
    except (http_req.RequestException, ValueError):
        app.logger.exception("Refresh status request failed")
        return jsonify({"error": "refresh service unavailable"}), 502
    except Exception:
        app.logger.exception("Unexpected refresh status failure")
        return jsonify({"error": "internal server error"}), 500


# ─── Health ───────────────────────────────────────────────────

@app.route("/auth/health", methods=["GET"])
def health():
    return jsonify({"ok": True})


@app.route("/scheduler/status", methods=["GET"])
@app.route("/admin/scheduler/status", methods=["GET"])
@require_role("admin")
def scheduler_status():
    """Return scheduler status for debugging."""
    import datetime as _dt
    now = _dt.datetime.now()
    beijing_now = _beijing_now()
    today_str = beijing_now.strftime("%Y-%m-%d")
    today_sent = sorted(_get_daily_summary_sent_user_ids(today_str))
    return jsonify({
        "running": True,
        "server_time": now.strftime("%Y-%m-%d %H:%M:%S"),
        "beijing_time": beijing_now.strftime("%Y-%m-%d %H:%M:%S"),
        "today": today_str,
        "timezone_hint": str(_dt.datetime.now().astimezone().tzinfo),
        "daily_summary_send_time": f"{DAILY_SUMMARY_HOUR:02d}:{DAILY_SUMMARY_MINUTE:02d}",
        "daily_summary_sent_user_ids_today": today_sent,
    })


# ─── Main ─────────────────────────────────────────────────────

if __name__ == "__main__":
    _init_ai_results_table()
    _init_daily_summary_global_table()
    _init_daily_summary_sends_table()
    _init_daily_summary_failures_table()
    import threading as _th
    _th.Thread(target=_daily_summary_loop, daemon=True).start()
    _th.Thread(target=_auto_summary_loop, daemon=True).start()
    _th.Thread(target=_auto_translation_loop, daemon=True).start()
    _th.Thread(target=_auto_title_process_loop, daemon=True).start()
    _th.Thread(target=_auto_source_classification_loop, daemon=True).start()
    _th.Thread(target=_source_identity_backfill_loop, daemon=True).start()
    _th.Thread(target=_ai_share_revalidation_loop, daemon=True).start()
    _th.Thread(target=_pin_existing_favorite_images_on_startup, daemon=True).start()
    _start_memory_monitor_thread()
    print("[scheduler] Daily summary background thread started")
    print("[auto-summary] Background summary thread started")
    print("[auto-translate] Background translation thread started")
    print("[auto-title] Background title processing thread started")
    print("[source-classify] Background source classification thread started")
    print("[image-cache] Existing favorite image pinning thread started")
    if MEMORY_MONITOR_ENABLED:
        _announce_memory_monitor_started()
    port = int(os.environ.get("WEB_PORT", 8082))
    print(f"[web] RayNews Web Server listening on {port}")
    # threaded=True is passed explicitly for clarity, but it's already Flask's default
    # (Flask.run() does options.setdefault("threaded", True)), so it does NOT by itself
    # change concurrency — this process has always served /auth/, /ai/, /admin/ etc. on a
    # thread per request. The real cross-request stall wasn't single-threaded serving; it
    # was the process-wide shared news.db connection several of those threads drove at
    # once (a slow /ai/ or admin write interleaving with a /auth/refresh/status read on
    # the same connection/transaction). That is fixed by the per-thread connection in
    # _get_news_db() above; this flag just documents the intended serving model.
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)
