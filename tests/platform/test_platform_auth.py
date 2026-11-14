"""Supabase JWT verification + current-user dependency."""
import time
import uuid

import jwt
import pytest
from fastapi import HTTPException
from starlette.requests import Request

from automation import db
from automation.web import platform_auth

SECRET = "test-secret-" + "x" * 32
UID = str(uuid.uuid4())


def _token(secret=SECRET, aud="authenticated", exp_in=3600, sub=UID, email="a@b.com"):
    return jwt.encode(
        {"sub": sub, "email": email, "aud": aud, "exp": int(time.time()) + exp_in},
        secret,
        algorithm="HS256",
    )


def _req(bearer=None, cookie=None):
    headers = []
    if bearer:
        headers.append((b"authorization", f"Bearer {bearer}".encode()))
    if cookie:
        headers.append((b"cookie", f"sb-access-token={cookie}".encode()))
    return Request({"type": "http", "headers": headers})


@pytest.fixture(autouse=True)
def fake_db(monkeypatch):
    monkeypatch.setenv("SUPABASE_JWT_SECRET", SECRET)
    calls = []

    def fetch_one(sql, params=None):
        calls.append((sql, params))
        return {"user_id": params[0], "email": params[1], "onboarded_at": None}

    monkeypatch.setattr(db, "fetch_one", fetch_one)
    return calls


def test_valid_jwt_creates_user_row(fake_db):
    user = platform_auth.get_current_user(_req(bearer=_token()))
    assert user["user_id"] == UID and user["email"] == "a@b.com"
    sql, params = fake_db[0]
    assert "INSERT INTO platform_users" in sql and "ON CONFLICT" in sql
    assert params == (UID, "a@b.com")


def test_cookie_token_accepted():
    assert platform_auth.get_current_user(_req(cookie=_token()))["user_id"] == UID


def _status(req):
    with pytest.raises(HTTPException) as e:
        platform_auth.get_current_user(req)
    return e.value


def test_expired_jwt_401():
    assert _status(_req(bearer=_token(exp_in=-60))).status_code == 401


def test_wrong_secret_401():
    assert _status(_req(bearer=_token(secret="other-" + "y" * 32))).status_code == 401


def test_wrong_aud_401():
    assert _status(_req(bearer=_token(aud="anon"))).status_code == 401


def test_no_token_401():
    assert _status(_req()).status_code == 401


def test_secret_unset_401(monkeypatch):
    monkeypatch.delenv("SUPABASE_JWT_SECRET")
    e = _status(_req(bearer=_token()))
    assert e.status_code == 401 and e.detail == "platform auth not configured"


def test_maybe_user_none(monkeypatch):
    assert platform_auth.maybe_user(_req()) is None
    assert platform_auth.maybe_user(_req(bearer=_token(secret="z" * 40))) is None
    assert platform_auth.maybe_user(_req(bearer=_token()))["user_id"] == UID


def test_require_onboarded():
    with pytest.raises(HTTPException) as e:
        platform_auth.require_onboarded({"onboarded_at": None})
    assert e.value.status_code == 403 and e.value.detail == {"error": "onboarding_required"}
    u = {"onboarded_at": "2026-10-16"}
    assert platform_auth.require_onboarded(u) is u


def test_missing_exp_401():
    tok = jwt.encode({"sub": UID, "email": "a@b.com", "aud": "authenticated"}, SECRET, algorithm="HS256")
    assert _status(_req(bearer=tok)).status_code == 401


def test_missing_sub_401():
    tok = jwt.encode({"email": "a@b.com", "aud": "authenticated", "exp": int(time.time()) + 60}, SECRET, algorithm="HS256")
    assert _status(_req(bearer=tok)).status_code == 401


def test_non_uuid_sub_401(fake_db):
    assert _status(_req(bearer=_token(sub="not-a-uuid"))).status_code == 401
    assert fake_db == []


def test_upsert_sets_last_seen_on_insert():
    from automation.web import platform_auth as pa
    assert "(user_id, email, last_seen_at) VALUES (%s, %s, now())" in pa._UPSERT_SQL


# --- asymmetric signing keys (the live project signs ES256; public keys come from JWKS) ---

URL = "https://proj.supabase.co"


@pytest.fixture
def es256(monkeypatch):
    from cryptography.hazmat.primitives.asymmetric import ec

    key = ec.generate_private_key(ec.SECP256R1())
    monkeypatch.delenv("SUPABASE_JWT_SECRET")
    monkeypatch.setenv("SUPABASE_URL", URL)
    asked = []

    class _Client:
        def get_signing_key_from_jwt(self, token):
            return type("K", (), {"key": key.public_key()})()

    monkeypatch.setattr(platform_auth, "_jwks", lambda url: asked.append(url) or _Client())

    def make(signer=key, iss=URL + "/auth/v1", aud="authenticated"):
        return jwt.encode({"sub": UID, "email": "a@b.com", "aud": aud, "iss": iss,
                           "exp": int(time.time()) + 3600}, signer, algorithm="ES256")

    make.asked = asked
    return make


def test_es256_token_verifies_against_the_project_jwks_without_a_secret(es256):
    assert platform_auth.get_current_user(_req(bearer=es256()))["user_id"] == UID
    assert es256.asked == [URL]


def test_es256_token_signed_by_another_key_401(es256):
    from cryptography.hazmat.primitives.asymmetric import ec
    assert _status(_req(bearer=es256(signer=ec.generate_private_key(ec.SECP256R1())))).status_code == 401


def test_es256_token_from_another_project_401(es256):
    assert _status(_req(bearer=es256(iss="https://evil.supabase.co/auth/v1"))).status_code == 401


def test_hs256_token_is_refused_when_only_jwks_is_configured(es256):
    e = _status(_req(bearer=_token()))
    assert e.status_code == 401 and e.detail == "platform auth not configured"


def test_unsigned_token_401(es256):
    tok = jwt.encode({"sub": UID, "email": "a@b.com", "aud": "authenticated",
                      "exp": int(time.time()) + 60}, None, algorithm="none")
    assert _status(_req(bearer=tok)).status_code == 401


def test_jwks_fetch_failure_reads_as_invalid_token(es256, monkeypatch):
    def boom(url):
        raise jwt.PyJWKClientError("network down")
    monkeypatch.setattr(platform_auth, "_jwks", boom)
    assert _status(_req(bearer=es256())).status_code == 401
