"""Resend emailer + unsubscribe."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from automation import db, platform_email
from automation.web import platform_routes

LOTS = [{"title": "Chairs <b>", "current_bid": 120, "end_utc": "2026-10-20T15:00Z",
         "state": "GA", "url": "https://x.test/lot/1"}, {"title": "Bare"}]
ALERT = {"keyword": "chairs"}


class Resp:
    def __init__(self, code): self.status_code = code


def test_send_builds_resend_payload(monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "k")
    monkeypatch.setenv("PLATFORM_EMAIL_FROM", "a@b.test")
    seen = {}

    def post(url, **kw):
        seen["url"], seen["kw"] = url, kw
        return Resp(200)
    monkeypatch.setattr(platform_email.requests, "post", post)
    assert platform_email.send_alert_email("u@x.test", ALERT, LOTS, "https://s/unsub?token=t") is True
    kw = seen["kw"]
    assert seen["url"] == "https://api.resend.com/emails"
    assert kw["headers"]["Authorization"] == "Bearer k" and kw["timeout"] <= 10
    j = kw["json"]
    assert j["from"] == "a@b.test" and j["to"] == ["u@x.test"]
    assert j["subject"] == "2 new lots match “chairs”"
    assert "https://x.test/lot/1" in j["html"] and "Chairs &lt;b&gt;" in j["html"]
    assert "https://s/unsub?token=t" in j["text"] and "https://s/unsub?token=t" in j["html"]


def test_no_key_degraded_false(monkeypatch, caplog):
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    monkeypatch.setattr(platform_email.requests, "post", lambda *a, **k: pytest.fail("called"))
    assert platform_email.send_alert_email("u@x.test", ALERT, LOTS, "u") is False
    assert "platform-email: degraded" in caplog.text


def test_http_500_degraded_false(monkeypatch, caplog):
    monkeypatch.setenv("RESEND_API_KEY", "k")
    monkeypatch.setattr(platform_email.requests, "post", lambda *a, **k: Resp(500))
    assert platform_email.send_alert_email("u@x.test", ALERT, LOTS, "u") is False
    assert "platform-email: degraded" in caplog.text


def test_exception_degraded_false(monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "k")

    def boom(*a, **k): raise OSError("net")
    monkeypatch.setattr(platform_email.requests, "post", boom)
    assert platform_email.send_alert_email("u@x.test", ALERT, LOTS, "u") is False


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(platform_routes.router)
    return TestClient(app)


def test_unsub_roundtrip_deactivates(monkeypatch, client):
    calls = []
    monkeypatch.setattr(db, "execute", lambda sql, params=None: calls.append((sql, params)) or 1)
    tok = platform_email.make_unsub_token("uid-1")
    r = client.get("/platform/unsubscribe", params={"token": tok})
    assert r.status_code == 200 and "unsubscribed" in r.text.lower()
    assert "active = false" in calls[0][0] and calls[0][1] == ("uid-1",)


def test_unsub_tampered_400(monkeypatch, client):
    monkeypatch.setattr(db, "execute", lambda *a, **k: pytest.fail("db touched"))
    tok = platform_email.make_unsub_token("uid-1")
    assert client.get("/platform/unsubscribe", params={"token": tok + "x"}).status_code == 400
    assert client.get("/platform/unsubscribe").status_code == 400


def test_unsub_expired_none():
    tok = platform_email.make_unsub_token("uid-1")
    assert platform_email.verify_unsub_token(tok, max_age=-1) is None


def test_caps_at_20_lots_and_strips_newlines():
    lots = [{"title": f"Lot{i:02d}x"} for i in range(25)]
    subject, text, body = platform_email._build({"keyword": "a\r\nBcc: x"}, lots, "u")
    assert "\n" not in subject and "\r" not in subject
    for part in (text, body):
        assert "Lot19x" in part and "Lot20x" not in part and "+5 more" in part


def test_singular_subject():
    assert platform_email._build(ALERT, LOTS[:1], "https://s/u")[0] == "1 new lot matches “chairs”"
