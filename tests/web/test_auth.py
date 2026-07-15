"""Offline tests for the "auth once" layer (BLACKWHOLE-14).

Covers: session/device token issue/verify/expiry, the login flow (password +
optional per-device TOTP), the /admin + /api middleware gate, and logout. No
DB and no network — env vars are flipped per-test and the signing-key cache is
reset around each. The one DB-free protected route we exercise over HTTP is
``/api/site-config`` (returns a config string, touches no database), so an
unauthenticated request is rejected by the middleware before any handler runs.
"""
import time

import pyotp
import pytest
from fastapi.testclient import TestClient

from automation.web import auth as auth_svc
from automation.web.app import app

PASSWORD = "hunter2-but-much-longer"
TOTP_SECRET = pyotp.random_base32()

_AUTH_ENV = ("ADMIN_PASSWORD", "SESSION_SECRET", "TOTP_SECRET")


@pytest.fixture(autouse=True)
def _clean_auth_env(monkeypatch):
    """Each test starts auth-disabled with a fresh signing-key cache."""
    for var in _AUTH_ENV:
        monkeypatch.delenv(var, raising=False)
    auth_svc.reset_caches()
    yield
    auth_svc.reset_caches()


def _configure(monkeypatch, *, password=PASSWORD, secret="unit-test-secret", totp=None):
    monkeypatch.setenv("ADMIN_PASSWORD", password)
    monkeypatch.setenv("SESSION_SECRET", secret)
    if totp is not None:
        monkeypatch.setenv("TOTP_SECRET", totp)
    auth_svc.reset_caches()


def _client():
    # https base URL so the Secure-flagged cookies round-trip in the test jar.
    # No `with` block → lifespan startup (the Telegram alert scheduler) never
    # runs, keeping these tests DB-free.
    return TestClient(app, base_url="https://testserver")


# ─────────────────────────────── token layer ────────────────────────────────

def test_session_token_roundtrip(monkeypatch):
    _configure(monkeypatch)
    token = auth_svc.issue_session_token()
    assert auth_svc.verify_session_token(token)


def test_session_token_rejects_tamper_and_garbage(monkeypatch):
    _configure(monkeypatch)
    token = auth_svc.issue_session_token()
    assert not auth_svc.verify_session_token(token + "x")
    assert not auth_svc.verify_session_token("not-a-token")
    # A device token must never pass as a session token (different salt).
    assert not auth_svc.verify_session_token(auth_svc.issue_device_token())


def test_session_token_rejects_other_key(monkeypatch):
    _configure(monkeypatch, secret="key-one")
    token = auth_svc.issue_session_token()
    _configure(monkeypatch, secret="key-two")
    assert not auth_svc.verify_session_token(token)


def test_session_token_expires_after_365_days(monkeypatch):
    """Freeze-forward: itsdangerous timestamps come from time.time()."""
    _configure(monkeypatch)
    token = auth_svc.issue_session_token()
    real_time = time.time
    monkeypatch.setattr(time, "time", lambda: real_time() + 364 * 24 * 3600)
    assert auth_svc.verify_session_token(token)  # day 364: still valid
    monkeypatch.setattr(time, "time", lambda: real_time() + 366 * 24 * 3600)
    assert not auth_svc.verify_session_token(token)  # day 366: expired


def test_device_token_roundtrip_and_expiry(monkeypatch):
    _configure(monkeypatch)
    token = auth_svc.issue_device_token()
    assert auth_svc.verify_device_token(token)
    real_time = time.time
    monkeypatch.setattr(time, "time", lambda: real_time() + 366 * 24 * 3600)
    assert not auth_svc.verify_device_token(token)


def test_per_boot_random_key_when_secret_unset(monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", PASSWORD)  # no SESSION_SECRET
    auth_svc.reset_caches()
    token = auth_svc.issue_session_token()
    assert auth_svc.verify_session_token(token)  # stable within one boot
    auth_svc.reset_caches()  # simulate a restart
    assert not auth_svc.verify_session_token(token)  # new random key


def test_path_requires_auth_matrix():
    # Admin surfaces need auth.
    for p in ("/admin", "/api/inventory", "/api/runs/state", "/screenshot/x/y.png"):
        assert auth_svc.path_requires_auth(p), p
    # Exemptions + public storefront never do.
    for p in ("/", "/listings", "/listings/abc", "/sell", "/contact",
              "/subscribe", "/image/f/x.jpg", "/static/app.css",
              "/api/health", "/admin/login", "/api/auth/login", "/api/auth/status"):
        assert not auth_svc.path_requires_auth(p), p


# ─────────────────────────── middleware + routes ────────────────────────────

def test_auth_disabled_leaves_admin_surface_open():
    """ADMIN_PASSWORD unset → the API is open exactly as before the feature."""
    client = _client()
    assert client.get("/api/health").status_code == 200
    # /api/site-config is DB-free, so a pass-through reaches the handler → 200.
    assert client.get("/api/site-config").status_code == 200
    status = client.get("/api/auth/status").json()
    assert status == {"auth_enabled": False, "authenticated": True, "totp_enabled": False}
    # Login is a no-op when disabled.
    assert client.post("/api/auth/login", json={"password": "x"}).status_code == 400


def test_middleware_blocks_api_without_session(monkeypatch):
    _configure(monkeypatch)
    client = _client()
    r = client.get("/api/site-config")
    assert r.status_code == 401
    assert r.json() == {"detail": "not_authenticated"}
    # Exempt paths still reachable.
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/auth/status").status_code == 200


def test_admin_page_redirects_to_login_when_unauthenticated(monkeypatch):
    _configure(monkeypatch)
    client = _client()
    r = client.get("/admin", headers={"accept": "text/html"}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/admin/login"
    # The login page itself is reachable without a session.
    assert client.get("/admin/login").status_code == 200


def test_public_storefront_stays_open_when_auth_enabled(monkeypatch):
    """Enabling auth must not gate the public site (robots.txt is DB-free)."""
    _configure(monkeypatch)
    client = _client()
    assert client.get("/robots.txt").status_code == 200


def test_login_wrong_password(monkeypatch):
    _configure(monkeypatch)
    client = _client()
    assert client.post("/api/auth/login", json={"password": "wrong"}).status_code == 401


def test_login_sets_year_long_session_cookie(monkeypatch):
    _configure(monkeypatch)
    client = _client()
    r = client.post("/api/auth/login", json={"password": PASSWORD})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "totp_required": False}
    set_cookie = r.headers["set-cookie"]
    assert auth_svc.SESSION_COOKIE in set_cookie
    assert f"Max-Age={365 * 24 * 3600}" in set_cookie
    assert "HttpOnly" in set_cookie
    assert "Secure" in set_cookie
    assert "samesite=lax" in set_cookie.lower()
    # Cookie now unlocks the API.
    assert client.get("/api/site-config").status_code == 200
    assert client.get("/api/auth/status").json()["authenticated"] is True


def test_logout_clears_session(monkeypatch):
    _configure(monkeypatch)
    client = _client()
    client.post("/api/auth/login", json={"password": PASSWORD})
    assert client.get("/api/site-config").status_code == 200
    client.post("/api/auth/logout")
    assert client.get("/api/site-config").status_code == 401


def test_expired_session_cookie_is_rejected(monkeypatch):
    _configure(monkeypatch)
    client = _client()
    client.post("/api/auth/login", json={"password": PASSWORD})
    real_time = time.time
    monkeypatch.setattr(time, "time", lambda: real_time() + 366 * 24 * 3600)
    assert client.get("/api/site-config").status_code == 401


# ───────────────────────────────── TOTP gate ────────────────────────────────

def test_totp_gate_first_login_requires_code(monkeypatch):
    _configure(monkeypatch, totp=TOTP_SECRET)
    client = _client()
    r = client.post("/api/auth/login", json={"password": PASSWORD})
    assert r.status_code == 200
    assert r.json() == {"ok": False, "totp_required": True}
    assert auth_svc.SESSION_COOKIE not in r.headers.get("set-cookie", "")
    # API still locked until the code is supplied.
    assert client.get("/api/site-config").status_code == 401


def test_totp_wrong_code_rejected(monkeypatch):
    _configure(monkeypatch, totp=TOTP_SECRET)
    client = _client()
    r = client.post("/api/auth/login", json={"password": PASSWORD, "totp_code": "000000"})
    assert r.status_code == 401
    assert r.json()["detail"] == "bad_totp"


def test_totp_valid_code_sets_session_and_trusts_device(monkeypatch):
    _configure(monkeypatch, totp=TOTP_SECRET)
    client = _client()
    code = pyotp.TOTP(TOTP_SECRET).now()
    r = client.post("/api/auth/login", json={"password": PASSWORD, "totp_code": code})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "totp_required": False}
    cookies = r.headers["set-cookie"]
    assert auth_svc.SESSION_COOKIE in cookies
    assert auth_svc.DEVICE_COOKIE in cookies
    assert client.get("/api/site-config").status_code == 200
    # Device is now trusted: a fresh login (session cleared) skips TOTP.
    client.post("/api/auth/logout")
    r2 = client.post("/api/auth/login", json={"password": PASSWORD})
    assert r2.status_code == 200
    assert r2.json() == {"ok": True, "totp_required": False}


def test_totp_unset_skips_second_factor(monkeypatch):
    _configure(monkeypatch)  # no TOTP_SECRET
    client = _client()
    r = client.post("/api/auth/login", json={"password": PASSWORD})
    assert r.json() == {"ok": True, "totp_required": False}
