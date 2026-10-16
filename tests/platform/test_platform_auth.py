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
