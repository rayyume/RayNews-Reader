"""Security regressions for registration, login, and invite throttling."""

from concurrent.futures import ThreadPoolExecutor
import sqlite3
import threading
import time

import jwt
import bcrypt
import pytest

import models
import notifier
import web_server


@pytest.fixture
def auth_env(tmp_path, monkeypatch):
    old_db_file = models.DB_FILE
    models.close_db()
    models.DB_FILE = tmp_path / "auth-security.db"
    models.get_db()
    monkeypatch.setenv("RESEND_API_KEY", "test-key")
    monkeypatch.setenv("RAYNEWS_ADMIN_EMAIL", "admin@example.com")
    client = web_server.app.test_client()
    try:
        yield client
    finally:
        models.close_db()
        models.DB_FILE = old_db_file


def _create_login_user():
    user = models.create_user(
        "reader@example.com",
        "correct-password",
        "reader",
        role="user",
    )
    assert user is not None
    return user


def _seed_admin(email="admin@example.com", nickname="admin"):
    db = models.get_db()
    cur = db.execute(
        "INSERT INTO users (email, password, nickname, role) VALUES (?, ?, ?, ?)",
        (email, "unused-test-hash", nickname, "admin"),
    )
    db.commit()
    return cur.lastrowid


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


def _legacy_token(user_id, role):
    return jwt.encode(
        {
            "user_id": user_id,
            "role": role,
            "iat": int(time.time()),
            "exp": int(time.time()) + 3600,
        },
        web_server.SECRET_KEY,
        algorithm="HS256",
    )


def test_token_version_schema_migrates_existing_users(tmp_path):
    old_db_file = models.DB_FILE
    models.close_db()
    models.DB_FILE = tmp_path / "legacy-users.db"
    legacy = sqlite3.connect(models.DB_FILE)
    legacy.execute(
        "CREATE TABLE users ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "email TEXT NOT NULL UNIQUE, password TEXT NOT NULL, "
        "nickname TEXT NOT NULL DEFAULT '', role TEXT NOT NULL DEFAULT 'user', "
        "avatar_url TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL DEFAULT '')"
    )
    legacy.execute(
        "INSERT INTO users (email, password, nickname, role) VALUES (?, ?, ?, ?)",
        ("legacy@example.com", "unused", "legacy", "user"),
    )
    legacy.commit()
    legacy.close()
    try:
        migrated = models.get_user(1)
        assert migrated["token_version"] == 0
        assert models.list_users()[0]["token_version"] == 0
    finally:
        models.close_db()
        models.DB_FILE = old_db_file


def test_legacy_token_without_token_version_matches_default_database_version(auth_env):
    user = _create_login_user()

    response = auth_env.get("/auth/me", headers=_bearer(_legacy_token(user["id"], "user")))

    assert response.status_code == 200
    assert response.get_json()["token_version"] == 0


def test_admin_revoke_tokens_rejects_old_token_and_new_login_works(auth_env):
    user = _create_login_user()
    admin_id = _seed_admin()
    old_token = web_server.create_token(user["id"], "user")
    admin_token = web_server.create_token(admin_id, "admin")

    revoked = auth_env.post(
        f"/auth/users/{user['id']}/revoke-tokens",
        headers=_bearer(admin_token),
    )

    assert revoked.status_code == 200
    assert revoked.get_json() == {"ok": True}
    assert models.get_user(user["id"])["token_version"] == 1
    rejected = auth_env.get("/auth/me", headers=_bearer(old_token))
    assert rejected.status_code == 401
    assert rejected.get_json() == {"error": "invalid or expired token"}

    logged_in = auth_env.post(
        "/auth/login",
        json={"login": user["email"], "password": "correct-password"},
        environ_base={"REMOTE_ADDR": "198.51.100.70"},
    )
    assert logged_in.status_code == 200
    new_token = logged_in.get_json()["token"]
    assert jwt.decode(new_token, web_server.SECRET_KEY, algorithms=["HS256"])["ver"] == 1
    assert auth_env.get("/auth/me", headers=_bearer(new_token)).status_code == 200


def test_admin_role_change_rotates_token_version(auth_env):
    user = _create_login_user()
    admin_id = _seed_admin()
    old_token = web_server.create_token(user["id"], "user")

    changed = auth_env.put(
        f"/auth/users/{user['id']}/role",
        json={"role": "admin"},
        headers=_bearer(web_server.create_token(admin_id, "admin")),
    )

    assert changed.status_code == 200
    current = models.get_user(user["id"])
    assert current["role"] == "admin"
    assert current["token_version"] == 1
    assert auth_env.get("/auth/me", headers=_bearer(old_token)).status_code == 401


def test_role_update_and_token_rotation_are_one_database_update(auth_env):
    user = _create_login_user()
    db = models.get_db()
    db.execute(
        "CREATE TRIGGER require_atomic_role_token_rotation "
        "BEFORE UPDATE OF role ON users "
        "WHEN NEW.role <> OLD.role "
        "AND NEW.token_version <> OLD.token_version + 1 "
        "BEGIN SELECT RAISE(ABORT, 'role and token version must change together'); END"
    )
    db.commit()

    changed = models.set_user_role_and_rotate_token_version(user["id"], "admin")

    assert changed["role"] == "admin"
    assert changed["token_version"] == 1


def test_generic_user_update_rejects_role_without_mutating_security_state(auth_env):
    user = _create_login_user()

    with pytest.raises(ValueError, match="set_user_role_and_rotate_token_version"):
        models.update_user(user["id"], role="admin")

    unchanged = models.get_user(user["id"])
    assert unchanged["role"] == "user"
    assert unchanged["token_version"] == 0


def test_admin_role_route_does_not_use_split_update_helpers(auth_env, monkeypatch):
    user = _create_login_user()
    admin_id = _seed_admin()

    def split_update_called(*_args, **_kwargs):
        raise AssertionError("role changes must use the atomic model helper")

    monkeypatch.setattr(web_server, "get_user", split_update_called)
    monkeypatch.setattr(web_server, "update_user", split_update_called)
    monkeypatch.setattr(web_server, "rotate_token_version", split_update_called)

    changed = auth_env.put(
        f"/auth/users/{user['id']}/role",
        json={"role": "admin"},
        headers=_bearer(web_server.create_token(admin_id, "admin")),
    )

    assert changed.status_code == 200
    assert changed.get_json()["role"] == "admin"
    assert changed.get_json()["token_version"] == 1


def test_admin_setting_same_role_keeps_token_version_and_existing_token(auth_env):
    user = _create_login_user()
    admin_id = _seed_admin()
    old_token = web_server.create_token(user["id"], "user")

    unchanged = auth_env.put(
        f"/auth/users/{user['id']}/role",
        json={"role": "user"},
        headers=_bearer(web_server.create_token(admin_id, "admin")),
    )

    assert unchanged.status_code == 200
    assert models.get_user(user["id"])["token_version"] == 0
    assert auth_env.get("/auth/me", headers=_bearer(old_token)).status_code == 200


def test_admin_role_change_for_missing_user_returns_not_found(auth_env):
    admin_id = _seed_admin()

    response = auth_env.put(
        "/auth/users/999/role",
        json={"role": "user"},
        headers=_bearer(web_server.create_token(admin_id, "admin")),
    )

    assert response.status_code == 404
    assert response.get_json() == {"error": "not found"}


def test_revoke_tokens_for_missing_user_returns_not_found(auth_env):
    admin_id = _seed_admin()

    response = auth_env.post(
        "/auth/users/999/revoke-tokens",
        headers=_bearer(web_server.create_token(admin_id, "admin")),
    )

    assert response.status_code == 404
    assert response.get_json() == {"error": "not found"}


def test_revoked_token_is_rejected_before_recording_access(auth_env):
    user = _create_login_user()
    old_token = web_server.create_token(user["id"], "user")
    assert models.rotate_token_version(user["id"])

    response = auth_env.get("/auth/me", headers=_bearer(old_token))

    assert response.status_code == 401
    access_count = models.get_db().execute(
        "SELECT COUNT(*) FROM user_access_log WHERE user_id = ?",
        (user["id"],),
    ).fetchone()[0]
    assert access_count == 0


def test_register_token_version_matches_returned_user_and_database(
    auth_env,
    monkeypatch,
):
    monkeypatch.setattr(models, "hash_password", lambda _password: "test-hash")

    registered = auth_env.post(
        "/auth/register",
        json={
            "email": "first@example.com",
            "password": "correct-password",
            "nickname": "first",
        },
        environ_base={"REMOTE_ADDR": "198.51.100.71"},
    )

    assert registered.status_code == 201
    payload = registered.get_json()
    database_user = models.get_user(payload["user"]["id"])
    token_payload = jwt.decode(
        payload["token"],
        web_server.SECRET_KEY,
        algorithms=["HS256"],
    )
    assert token_payload["ver"] == payload["user"]["token_version"]
    assert token_payload["ver"] == database_user["token_version"]


def test_regular_user_cannot_revoke_tokens_for_another_user(auth_env):
    user = _create_login_user()
    other = models.create_user(
        "other@example.com",
        "correct-password",
        "other",
        role="user",
    )

    response = auth_env.post(
        f"/auth/users/{other['id']}/revoke-tokens",
        headers=_bearer(web_server.create_token(user["id"], "user")),
    )

    assert response.status_code == 403
    assert models.get_user(other["id"])["token_version"] == 0


def _register(
    client,
    *,
    email,
    nickname,
    invite_code="INVALID",
    remote_addr="198.51.100.40",
):
    return client.post(
        "/auth/register",
        json={
            "email": email,
            "password": "correct-password",
            "nickname": nickname,
            "invite_code": invite_code,
        },
        environ_base={"REMOTE_ADDR": remote_addr},
    )


@pytest.mark.parametrize("password", ["a" * 73, "密" * 25])
def test_register_rejects_passwords_over_bcrypt_byte_limit(auth_env, password):
    response = auth_env.post(
        "/auth/register",
        json={
            "email": "first@example.com",
            "password": password,
            "nickname": "first",
        },
        environ_base={"REMOTE_ADDR": "198.51.100.72"},
    )

    assert response.status_code == 400
    assert response.get_json() == {
        "error": "password must be at most 72 UTF-8 bytes",
    }


@pytest.mark.parametrize("password", ["a" * 72, "密" * 24])
def test_hash_password_accepts_exactly_72_utf8_bytes(password):
    hashed = models.hash_password(password)

    assert models.verify_password(password, hashed) is True


@pytest.mark.parametrize("password", ["a" * 73, "密" * 25])
def test_hash_password_rejects_more_than_72_utf8_bytes(password):
    with pytest.raises(ValueError, match="at most 72 UTF-8 bytes"):
        models.hash_password(password)


@pytest.mark.parametrize(
    "password",
    [
        "a" * 73,
        "a" * 71 + "é",
    ],
)
def test_login_accepts_legacy_bcrypt_hashes_created_from_72_byte_prefix(
    auth_env,
    password,
):
    password_bytes = password.encode("utf-8")
    assert len(password_bytes) > 72
    legacy_hash = bcrypt.hashpw(password_bytes[:72], bcrypt.gensalt()).decode()
    db = models.get_db()
    db.execute(
        "INSERT INTO users (email, password, nickname, role) VALUES (?, ?, ?, ?)",
        ("legacy-long@example.com", legacy_hash, "legacy-long", "user"),
    )
    db.commit()

    response = auth_env.post(
        "/auth/login",
        json={"login": "legacy-long@example.com", "password": password},
        environ_base={"REMOTE_ADDR": "198.51.100.73"},
    )

    assert response.status_code == 200


def test_register_rate_limit_rejects_eleventh_attempt(auth_env):
    client = auth_env
    _seed_admin()

    for index in range(10):
        response = _register(
            client,
            email=f"failed-{index}@example.com",
            nickname=f"failed-{index}",
        )
        assert response.status_code == 400

    rejected = _register(
        client,
        email="failed-10@example.com",
        nickname="failed-10",
    )
    assert rejected.status_code == 429
    retry_after = rejected.get_json()["retry_after"]
    assert isinstance(retry_after, int)
    assert retry_after > 0
    assert rejected.headers["Retry-After"] == str(retry_after)


def test_invalid_invite_skips_bcrypt(auth_env, monkeypatch):
    _seed_admin()

    def unexpected_hash(_password):
        raise AssertionError("bcrypt must not run for an invalid invitation")

    monkeypatch.setattr(models, "hash_password", unexpected_hash)
    user, is_initial_admin = models.create_registered_user(
        "new@example.com",
        "correct-password",
        "new-reader",
        "INVALID",
    )

    assert user is None
    assert is_initial_admin is False


def test_duplicate_email_skips_bcrypt(auth_env, monkeypatch):
    _seed_admin(email="duplicate@example.com")

    def unexpected_hash(_password):
        raise AssertionError("bcrypt must not run for a duplicate email")

    monkeypatch.setattr(models, "hash_password", unexpected_hash)
    user, is_initial_admin = models.create_registered_user(
        "duplicate@example.com",
        "correct-password",
        "different-reader",
        "",
    )

    assert user is None
    assert is_initial_admin is False


def test_duplicate_nickname_skips_bcrypt(auth_env, monkeypatch):
    _seed_admin(nickname="duplicate-reader")

    def unexpected_hash(_password):
        raise AssertionError("bcrypt must not run for a duplicate nickname")

    monkeypatch.setattr(models, "hash_password", unexpected_hash)
    user, is_initial_admin = models.create_registered_user(
        "different@example.com",
        "correct-password",
        "duplicate-reader",
        "",
    )

    assert user is None
    assert is_initial_admin is False


def test_register_rate_limit_success_resets_attempts(auth_env, monkeypatch):
    client = auth_env
    client_ip = "198.51.100.41"
    _seed_admin()

    for index in range(9):
        assert _register(
            client,
            email=f"before-success-{index}@example.com",
            nickname=f"before-success-{index}",
            remote_addr=client_ip,
        ).status_code == 400

    attempt = models.get_db().execute(
        "SELECT failure_count FROM register_attempts WHERE client_ip = ?",
        (client_ip,),
    ).fetchone()
    assert attempt["failure_count"] == 9

    invite_code = models.create_invitation_code("success@example.com")
    monkeypatch.setattr(models, "hash_password", lambda _password: "test-hash")
    monkeypatch.setattr(web_server, "_send_registration_notice", lambda _user: False)
    successful = _register(
        client,
        email="success@example.com",
        nickname="successful-reader",
        invite_code=invite_code,
        remote_addr=client_ip,
    )
    assert successful.status_code == 201
    assert models.get_db().execute(
        "SELECT 1 FROM register_attempts WHERE client_ip = ?",
        (client_ip,),
    ).fetchone() is None

    after_reset = _register(
        client,
        email="after-reset@example.com",
        nickname="after-reset",
        remote_addr=client_ip,
    )
    assert after_reset.status_code == 400


def test_register_attempt_cleanup_removes_only_stale_rows(auth_env):
    now = time.time()
    db = models.get_db()
    db.executemany(
        "INSERT INTO register_attempts "
        "(client_ip, failure_count, locked_until, updated_at) "
        "VALUES (?, ?, ?, ?)",
        [
            ("198.51.100.42", 3, 0, now - 7200),
            ("198.51.100.43", 3, 0, now),
        ],
    )
    db.commit()

    allowed, retry_after = models.admit_register_attempt(
        "198.51.100.44",
        now=now,
    )

    assert allowed is True
    assert retry_after == 0
    rows = db.execute(
        "SELECT client_ip FROM register_attempts ORDER BY client_ip"
    ).fetchall()
    assert [row["client_ip"] for row in rows] == [
        "198.51.100.43",
        "198.51.100.44",
    ]


def _failed_login(client, *, remote_addr="198.51.100.10", real_ip=None):
    headers = {"X-Real-IP": real_ip} if real_ip else {}
    return client.post(
        "/auth/login",
        json={"login": "reader@example.com", "password": "wrong-password"},
        headers=headers,
        environ_base={"REMOTE_ADDR": remote_addr},
    )


def test_concurrent_initial_registration_creates_only_one_admin(auth_env):
    barrier = threading.Barrier(2)

    def register(index):
        try:
            barrier.wait(timeout=5)
            return models.create_registered_user(
                f"first{index}@example.com",
                "correct-password",
                f"first{index}",
                "",
            )
        finally:
            models.close_db()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(register, (1, 2)))

    successful = [user for user, _is_initial_admin in results if user]
    assert [user["role"] for user in successful] == ["admin"]
    assert models.count_users() == 1


def test_login_locks_after_five_failed_attempts(auth_env):
    client = auth_env
    _create_login_user()

    for _ in range(5):
        assert _failed_login(client).status_code == 401

    locked = _failed_login(client)
    assert locked.status_code == 429
    assert locked.get_json()["retry_after"] > 0
    assert locked.headers["Retry-After"]


def test_successful_login_resets_failure_state(auth_env):
    client = auth_env
    _create_login_user()

    for _ in range(4):
        assert _failed_login(client).status_code == 401
    success = client.post(
        "/auth/login",
        json={"login": "reader@example.com", "password": "correct-password"},
        environ_base={"REMOTE_ADDR": "198.51.100.10"},
    )
    assert success.status_code == 200

    for _ in range(5):
        assert _failed_login(client).status_code == 401
    assert _failed_login(client).status_code == 429


def test_untrusted_peer_cannot_spoof_x_real_ip_to_evade_login_lock(auth_env):
    client = auth_env
    _create_login_user()

    for index in range(5):
        response = _failed_login(
            client,
            remote_addr="198.51.100.20",
            real_ip=f"203.0.113.{index + 1}",
        )
        assert response.status_code == 401

    locked = _failed_login(
        client,
        remote_addr="198.51.100.20",
        real_ip="203.0.113.99",
    )
    assert locked.status_code == 429


def test_loopback_proxy_x_real_ip_separates_login_limits(auth_env):
    client = auth_env
    _create_login_user()

    for _ in range(5):
        assert _failed_login(
            client,
            remote_addr="127.0.0.1",
            real_ip="198.51.100.30",
        ).status_code == 401

    other_client = _failed_login(
        client,
        remote_addr="127.0.0.1",
        real_ip="198.51.100.31",
    )
    assert other_client.status_code == 401
    assert _failed_login(
        client,
        remote_addr="127.0.0.1",
        real_ip="198.51.100.30",
    ).status_code == 429


def test_parallel_login_failures_admit_only_five_password_verifications(
    auth_env,
    monkeypatch,
):
    _create_login_user()
    start = threading.Barrier(6)
    verification_calls = 0
    call_lock = threading.Lock()

    def slow_wrong_password(*args):
        nonlocal verification_calls
        with call_lock:
            verification_calls += 1
        # Keep every admitted request in password verification long enough for
        # all contenders to exercise the rate-limit admission boundary.
        time.sleep(0.15)
        return False

    monkeypatch.setattr(web_server, "verify_password", slow_wrong_password)

    def attempt(_index):
        client = web_server.app.test_client()
        try:
            start.wait(timeout=5)
            return _failed_login(client).status_code
        finally:
            models.close_db()

    with ThreadPoolExecutor(max_workers=6) as pool:
        statuses = list(pool.map(attempt, range(6)))

    assert sorted(statuses) == [401, 401, 401, 401, 401, 429]
    assert verification_calls == 5


def test_successful_invite_request_starts_email_cooldown(auth_env, monkeypatch):
    client = auth_env
    sent = []

    def fake_send(*args, **kwargs):
        sent.append((args, kwargs))

    monkeypatch.setattr(notifier, "send_email", fake_send)

    first = client.post(
        "/auth/request-invite",
        json={"email": "new-reader@example.com"},
    )
    second = client.post(
        "/auth/request-invite",
        json={"email": "NEW-READER@example.com"},
    )

    assert first.status_code == 201
    assert second.status_code == 429
    assert 0 < second.get_json()["retry_after"] <= 60
    assert second.headers["Retry-After"]
    assert len(sent) == 1


def test_failed_invite_delivery_does_not_consume_allowance(auth_env, monkeypatch):
    client = auth_env
    attempts = 0

    def flaky_send(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise notifier.EmailDeliveryRejected("mail provider rejected request")

    monkeypatch.setattr(notifier, "send_email", flaky_send)

    failed = client.post(
        "/auth/request-invite",
        json={"email": "retry-reader@example.com"},
    )
    retried = client.post(
        "/auth/request-invite",
        json={"email": "retry-reader@example.com"},
    )

    assert failed.status_code == 500
    assert retried.status_code == 201
    assert attempts == 2
    assert models.count_pending_invitations() == 1


def test_ambiguous_invite_delivery_preserves_code_and_idempotency_reservation(
    auth_env,
    monkeypatch,
):
    client = auth_env
    idempotency_keys = []

    def ambiguous_send(*args, **kwargs):
        idempotency_keys.append(kwargs.get("idempotency_key"))
        raise notifier.EmailDeliveryUncertain("response lost after upload")

    monkeypatch.setattr(notifier, "send_email", ambiguous_send)

    first = client.post(
        "/auth/request-invite",
        json={"email": "uncertain-reader@example.com"},
    )
    immediate_retry = client.post(
        "/auth/request-invite",
        json={"email": "uncertain-reader@example.com"},
    )

    assert first.status_code == 503
    assert immediate_retry.status_code == 429
    assert 0 < immediate_retry.get_json()["retry_after"] <= 60
    assert len(idempotency_keys) == 1
    assert idempotency_keys[0]
    assert models.count_pending_invitations() == 1
    persisted = models.get_db().execute(
        "SELECT reservation_token FROM invite_request_limits WHERE email = ?",
        ("uncertain-reader@example.com",),
    ).fetchone()
    assert persisted["reservation_token"] == idempotency_keys[0]

    old_code = models.get_db().execute(
        "SELECT code FROM invitation_codes "
        "WHERE email = ? AND used = 0",
        ("uncertain-reader@example.com",),
    ).fetchone()["code"]
    models.get_db().execute(
        "UPDATE invite_request_limits SET reserved_at = ? WHERE email = ?",
        (time.time() - 61, "uncertain-reader@example.com"),
    )
    models.get_db().commit()

    def delivered_on_new_application(*args, **kwargs):
        idempotency_keys.append(kwargs.get("idempotency_key"))

    monkeypatch.setattr(notifier, "send_email", delivered_on_new_application)
    after_one_minute = client.post(
        "/auth/request-invite",
        json={"email": "uncertain-reader@example.com"},
    )

    assert after_one_minute.status_code == 201
    assert idempotency_keys[1] and idempotency_keys[1] != idempotency_keys[0]
    codes = models.get_db().execute(
        "SELECT code, used FROM invitation_codes "
        "WHERE email = ? ORDER BY id",
        ("uncertain-reader@example.com",),
    ).fetchall()
    assert codes[0]["code"] == old_code and codes[0]["used"] == 1
    assert codes[1]["code"] != old_code and codes[1]["used"] == 0


def test_new_invite_after_one_minute_invalidates_previous_code(
    auth_env,
    monkeypatch,
):
    client = auth_env
    monkeypatch.setattr(notifier, "send_email", lambda *args, **kwargs: None)

    first = client.post(
        "/auth/request-invite",
        json={"email": "renew-reader@example.com"},
    )
    assert first.status_code == 201
    old_code = models.get_db().execute(
        "SELECT code FROM invitation_codes "
        "WHERE email = ? AND used = 0",
        ("renew-reader@example.com",),
    ).fetchone()["code"]
    models.get_db().execute(
        "UPDATE invite_request_limits SET last_success_at = ? WHERE email = ?",
        (time.time() - 61, "renew-reader@example.com"),
    )
    models.get_db().commit()

    renewed = client.post(
        "/auth/request-invite",
        json={"email": "renew-reader@example.com"},
    )

    assert renewed.status_code == 201
    rows = models.get_db().execute(
        "SELECT code, used FROM invitation_codes WHERE email = ? ORDER BY id",
        ("renew-reader@example.com",),
    ).fetchall()
    assert len(rows) == 2
    assert rows[0]["code"] == old_code
    assert rows[0]["used"] == 1
    assert rows[1]["used"] == 0
    assert rows[1]["code"] != old_code


def test_invite_cooldown_expires_after_one_minute(auth_env):
    token, retry_after = models.claim_invite_request(
        "minute-reader@example.com",
        now=1_000,
    )
    assert token and retry_after == 0
    assert models.complete_invite_request(
        "minute-reader@example.com",
        token,
        succeeded=True,
        now=1_000,
    )

    blocked_token, retry_after = models.claim_invite_request(
        "minute-reader@example.com",
        now=1_059,
    )
    assert blocked_token is None
    assert retry_after == 1

    renewed_token, retry_after = models.claim_invite_request(
        "minute-reader@example.com",
        now=1_060,
    )
    assert renewed_token and renewed_token != token
    assert retry_after == 0


def test_notifier_marks_provider_rejection_as_definitive(monkeypatch):
    class RejectedResponse:
        status_code = 401

        @staticmethod
        def json():
            return {"message": "invalid API key"}

    monkeypatch.setattr(
        notifier.requests,
        "post",
        lambda *args, **kwargs: RejectedResponse(),
    )

    with pytest.raises(notifier.EmailDeliveryRejected):
        notifier.send_email("bad-key", "admin@example.com", "subject", "body")


def test_notifier_marks_lost_response_as_uncertain(monkeypatch):
    def timeout(*args, **kwargs):
        raise notifier.requests.Timeout("response timed out")

    monkeypatch.setattr(notifier.requests, "post", timeout)

    with pytest.raises(notifier.EmailDeliveryUncertain):
        notifier.send_email("key", "admin@example.com", "subject", "body")


def test_scheduler_status_requires_admin_and_alias_preserves_payload_shape(auth_env):
    """Removing admin protection or the nginx-reachable alias must fail this test."""
    client = auth_env
    user = _create_login_user()
    admin_id = _seed_admin()
    user_headers = {
        "Authorization": f"Bearer {web_server.create_token(user['id'], 'user')}"
    }
    admin_headers = {
        "Authorization": f"Bearer {web_server.create_token(admin_id, 'admin')}"
    }

    for path in ("/scheduler/status", "/admin/scheduler/status"):
        assert client.get(path).status_code == 401
        assert client.get(path, headers=user_headers).status_code == 403

    legacy = client.get("/scheduler/status", headers=admin_headers)
    alias = client.get("/admin/scheduler/status", headers=admin_headers)

    assert legacy.status_code == 200
    assert alias.status_code == 200
    expected_fields = {
        "running",
        "server_time",
        "beijing_time",
        "today",
        "timezone_hint",
        "daily_summary_send_time",
        "daily_summary_enabled",
        "daily_summary_delivery_method",
        "daily_summary_sent_user_ids_today",
    }
    assert set(legacy.get_json()) == expected_fields
    assert set(alias.get_json()) == expected_fields

@pytest.mark.parametrize(
    ("remote_addr", "headers", "expected"),
    [
        (
            "127.0.0.1",
            {"X-Real-IP": "198.51.100.50"},
            "198.51.100.50",
        ),
        (
            "127.0.0.1",
            {"X-Forwarded-For": "198.51.100.51"},
            "127.0.0.1",
        ),
    ],
)
def test_trusted_client_ip_defaults_to_loopback_and_only_x_real_ip(
    monkeypatch,
    remote_addr,
    headers,
    expected,
):
    """Would fail if the default trust range expands or XFF is accepted."""
    monkeypatch.delenv("TRUSTED_PROXY_PREFIXES", raising=False)

    with web_server.app.test_request_context(
        "/", headers=headers, environ_base={"REMOTE_ADDR": remote_addr}
    ):
        assert web_server._trusted_client_ip() == expected


def test_trusted_client_ip_accepts_x_real_ip_from_configured_ipv4_proxy(monkeypatch):
    """Would fail if configured IPv4 proxy CIDRs are not honored."""
    monkeypatch.setenv("TRUSTED_PROXY_PREFIXES", "192.168.0.0/24")

    with web_server.app.test_request_context(
        "/",
        headers={"X-Real-IP": "198.51.100.52"},
        environ_base={"REMOTE_ADDR": "192.168.0.8"},
    ):
        assert web_server._trusted_client_ip() == "198.51.100.52"


def test_trusted_client_ip_rejects_spoofing_from_untrusted_configured_peer(monkeypatch):
    """Would fail if any peer, rather than only the configured CIDR, is trusted."""
    monkeypatch.setenv("TRUSTED_PROXY_PREFIXES", "192.168.0.0/24")

    with web_server.app.test_request_context(
        "/",
        headers={"X-Real-IP": "198.51.100.53"},
        environ_base={"REMOTE_ADDR": "192.168.1.8"},
    ):
        assert web_server._trusted_client_ip() == "192.168.1.8"


def test_trusted_client_ip_fails_closed_for_invalid_proxy_prefix(monkeypatch):
    """Would fail if malformed prefixes accidentally retain or broaden trust."""
    monkeypatch.setenv("TRUSTED_PROXY_PREFIXES", "not-a-network")

    with web_server.app.test_request_context(
        "/",
        headers={"X-Real-IP": "198.51.100.54"},
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    ):
        assert web_server._trusted_client_ip() == "127.0.0.1"


def test_trusted_client_ip_accepts_x_real_ip_from_configured_ipv6_proxy(monkeypatch):
    """Would fail if IPv6 proxy CIDRs cannot establish trusted proxy status."""
    monkeypatch.setenv("TRUSTED_PROXY_PREFIXES", "2001:db8:1::/64")

    with web_server.app.test_request_context(
        "/",
        headers={"X-Real-IP": "2001:db8:2::99"},
        environ_base={"REMOTE_ADDR": "2001:db8:1::8"},
    ):
        assert web_server._trusted_client_ip() == "2001:db8:2::99"
