"""Signup + onboarding: phone normalization, POST /platform/api/onboarding, pages."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from automation import db
from automation.web import platform_auth, platform_routes

USER = {"user_id": "11111111-1111-1111-1111-111111111111", "email": "a@b.com", "onboarded_at": None}
BODY = {"phone": "+1 480-555-0100", "sms_consent": True, "company": "Acme", "state": "AZ",
        "flips_what": "chairs", "biggest_pain": "finding lots"}


@pytest.fixture
def calls(monkeypatch):
    seen = []
    monkeypatch.setattr(db, "execute", lambda sql, params=None: seen.append((sql, params)) or 1)
    monkeypatch.setattr(db, "fetch_one", lambda sql, params=None: seen.append((sql, params)) or {**USER})
    return seen


@pytest.fixture
def client(calls):
    app = FastAPI()
    app.include_router(platform_routes.router)
    app.dependency_overrides[platform_auth.get_current_user] = lambda: dict(USER)
    return TestClient(app)


def test_phone_normalization():
    assert platform_routes.normalize_phone("+1 480-555-0100") == "14805550100"
    assert platform_routes.normalize_phone("(480) 555.0100") == "4805550100"
    for bad in ("abc", "123", "", "1" * 16):
        with pytest.raises(ValueError):
            platform_routes.normalize_phone(bad)


def test_onboarding_sets_fields_and_onboarded_at(client, calls):
    r = client.post("/platform/api/onboarding", json=BODY)
    assert r.status_code == 200
    assert r.json() == {"ok": True, "next": "/platform/alerts"}
    sql, params = calls[-1]
    assert "onboarded_at = now()" in sql and "%s" in sql
    assert params == ("14805550100", True, "Acme", "AZ", "chairs", "finding lots", USER["user_id"])


def test_onboarding_requires_phone(client, calls):
    for body in ({**BODY, "phone": ""}, {**BODY, "phone": "abc"}, {**BODY, "phone": "123"},
                 {k: v for k, v in BODY.items() if k != "phone"}):
        assert client.post("/platform/api/onboarding", json=body).status_code == 422
    assert calls == []


def test_sms_consent_defaults_false(client, calls):
    body = {k: v for k, v in BODY.items() if k != "sms_consent"}
    assert client.post("/platform/api/onboarding", json=body).status_code == 200
    assert calls[-1][1][1] is False


def test_onboarding_unauthed_401(monkeypatch, calls):
    monkeypatch.setenv("SUPABASE_JWT_SECRET", "s" * 40)
    app = FastAPI()
    app.include_router(platform_routes.router)
    assert TestClient(app).post("/platform/api/onboarding", json=BODY).status_code == 401
    assert calls == []


def test_signup_page_renders_without_supabase_config(monkeypatch):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_PUBLISHABLE_KEY", raising=False)
    from automation.web.app import app
    r = TestClient(app).get("/platform/signup")
    assert r.status_code == 200
    assert "not configured" in r.text.lower()


def test_signup_page_injects_public_config(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://x.supabase.co")
    monkeypatch.setenv("SUPABASE_PUBLISHABLE_KEY", "sb_publishable_abc")
    from automation.web.app import app
    r = TestClient(app).get("/platform/signup")
    assert "https://x.supabase.co" in r.text and "sb_publishable_abc" in r.text
    assert "cdn.jsdelivr.net/npm/@supabase/supabase-js@2.45.4" in r.text


def test_onboarding_page_redirects_when_unauthed(monkeypatch):
    monkeypatch.setenv("SUPABASE_JWT_SECRET", "s" * 40)
    from automation.web.app import app
    r = TestClient(app).get("/platform/onboarding", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/platform/signup"
