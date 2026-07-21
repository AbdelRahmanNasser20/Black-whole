"""Alerts-signup capture (BLACKWHOLE-10).

Validation in `inventory.create_subscriber` fires before any DB connection is
opened, so those paths run without BLACKWHOLE_DB_URL. Route tests monkeypatch
the inventory layer + the Telegram sender, so no network or DB either.
"""
import importlib

import pytest
from fastapi.testclient import TestClient

from automation import inventory

# `automation.web` re-exports the FastAPI instance as `app`, shadowing the
# module — import the module explicitly so monkeypatch can reach its globals.
web_app = importlib.import_module("automation.web.app")


# ───────── validation (no DB) ─────────

def test_create_subscriber_requires_email_or_phone():
    with pytest.raises(ValueError, match="email or phone"):
        inventory.create_subscriber(name="Jane")


def test_create_subscriber_rejects_whitespace_only_contact():
    with pytest.raises(ValueError, match="email or phone"):
        inventory.create_subscriber(email="   ", phone="  ")


def test_create_subscriber_rejects_unknown_source():
    with pytest.raises(ValueError, match="invalid source"):
        inventory.create_subscriber(email="a@b.c", source="facebook_dm")


def test_set_subscriber_status_rejects_unknown_status():
    with pytest.raises(ValueError, match="invalid status"):
        inventory.set_subscriber_status(1, "blast_sent")


# ───────── unsubscribe token generation (DB stubbed) ─────────

def test_generate_unsubscribe_token_is_urlsafe_and_long():
    tok = inventory.generate_unsubscribe_token()
    assert len(tok) >= 32
    assert tok == tok.strip() and " " not in tok


class _FakeCur:
    def fetchone(self):
        return {"id": 42}


class _FakeConn:
    def __init__(self, sink):
        self._sink = sink

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params):
        self._sink["sql"] = sql
        self._sink["params"] = params
        return _FakeCur()

    def commit(self):
        pass


def test_create_subscriber_stores_token_when_column_present(monkeypatch):
    sink = {}
    monkeypatch.setattr(inventory, "connect", lambda: _FakeConn(sink))
    monkeypatch.setattr(inventory, "_subscribers_has_unsub_token", lambda: True)
    monkeypatch.setattr(inventory, "get_subscriber", lambda i: {"id": i})

    inventory.create_subscriber(email="buyer@x.com")

    assert "unsubscribe_token" in sink["sql"]
    token = sink["params"][-1]  # appended last
    assert isinstance(token, str) and len(token) >= 32


def test_create_subscriber_skips_token_pre_migration(monkeypatch):
    sink = {}
    monkeypatch.setattr(inventory, "connect", lambda: _FakeConn(sink))
    monkeypatch.setattr(inventory, "_subscribers_has_unsub_token", lambda: False)
    monkeypatch.setattr(inventory, "get_subscriber", lambda i: {"id": i})

    inventory.create_subscriber(email="buyer@x.com")

    assert "unsubscribe_token" not in sink["sql"]


# ───────── routes (inventory + telegram stubbed) ─────────

SAMPLE = {
    "id": 7, "name": "Jane", "email": "jane@x.com", "phone": None,
    "city": "Atlanta", "state": "GA", "zip_code": "30301",
    "quantity_wanted": 200, "use_case": "church", "chair_type": "banquet",
    "timeline": "asap", "budget_per_chair": "5_10", "delivery": "pickup",
    "notes": None, "source": "site_listings", "status": "new",
    "created_at": "2026-07-02T00:00:00+00:00",
}


@pytest.fixture()
def client(monkeypatch):
    sent: list[str] = []

    async def fake_send(text: str, *, topic=None):
        sent.append(text)
        return True, None

    monkeypatch.setattr(web_app.telegram_alerts, "send_message", fake_send)
    tc = TestClient(web_app.app)
    tc.sent_telegram = sent
    return tc


def test_subscribe_happy_path(client, monkeypatch):
    captured = {}

    def fake_create(**kwargs):
        captured.update(kwargs)
        return dict(SAMPLE)

    monkeypatch.setattr(web_app.inventory, "create_subscriber", fake_create)
    r = client.post("/subscribe", json={
        "name": "Jane", "email": "jane@x.com", "quantity_wanted": "200",
        "city": "Atlanta", "zip_code": "30301", "use_case": "church",
        "source": "site_listings",
    })
    assert r.status_code == 200
    assert r.json() == {"ok": True, "id": 7}
    assert captured["quantity_wanted"] == 200
    assert captured["email"] == "jane@x.com"
    # fire-and-forget telegram task ran on the loop
    assert any("#7" in t for t in client.sent_telegram)


def test_subscribe_missing_contact_is_400(client, monkeypatch):

    def fake_create(**kwargs):
        raise ValueError("email or phone required")

    monkeypatch.setattr(web_app.inventory, "create_subscriber", fake_create)
    r = client.post("/subscribe", json={"name": "Jane"})
    assert r.status_code == 400
    assert "email or phone" in r.json()["detail"]


def test_subscribe_bad_quantity_is_400(client, monkeypatch):
    r = client.post("/subscribe", json={"email": "a@b.c", "quantity_wanted": "lots"})
    assert r.status_code == 400


def test_subscribe_survives_telegram_failure(client, monkeypatch):

    async def exploding_send(text: str):
        raise RuntimeError("telegram down")

    monkeypatch.setattr(web_app.telegram_alerts, "send_message", exploding_send)
    monkeypatch.setattr(web_app.inventory, "create_subscriber", lambda **kw: dict(SAMPLE))
    r = client.post("/subscribe", json={"email": "jane@x.com"})
    assert r.status_code == 200


def test_api_subscribers_list(client, monkeypatch):
    monkeypatch.setattr(
        web_app.inventory, "list_subscribers", lambda status=None: [dict(SAMPLE)]
    )
    r = client.get("/api/subscribers")
    assert r.status_code == 200
    assert r.json()["items"][0]["id"] == 7


def test_api_subscribers_patch_status(client, monkeypatch):
    updated = dict(SAMPLE, status="contacted")
    monkeypatch.setattr(
        web_app.inventory, "set_subscriber_status", lambda i, s: dict(updated)
    )
    r = client.patch("/api/subscribers/7", json={"status": "contacted"})
    assert r.status_code == 200
    assert r.json()["status"] == "contacted"


def test_api_subscribers_patch_bad_status_is_400(client, monkeypatch):

    def raise_bad(i, s):
        raise ValueError("invalid status: nope")

    monkeypatch.setattr(web_app.inventory, "set_subscriber_status", raise_bad)
    r = client.patch("/api/subscribers/7", json={"status": "nope"})
    assert r.status_code == 400


def test_api_subscribers_patch_missing_is_404(client, monkeypatch):
    monkeypatch.setattr(web_app.inventory, "get_subscriber", lambda i: None)
    r = client.patch("/api/subscribers/999", json={})
    assert r.status_code == 404


def test_api_subscribers_delete(client, monkeypatch):
    monkeypatch.setattr(web_app.inventory, "delete_subscriber", lambda i: True)
    assert client.delete("/api/subscribers/7").status_code == 200
    monkeypatch.setattr(web_app.inventory, "delete_subscriber", lambda i: False)
    assert client.delete("/api/subscribers/999").status_code == 404


# ───────── public unsubscribe (capability URL, no DB) ─────────

def test_unsubscribe_get_flips_status_and_renders_page(client, monkeypatch):
    calls = []
    monkeypatch.setattr(
        web_app.inventory, "unsubscribe_by_token",
        lambda t: calls.append(t) or True,
    )
    r = client.get("/alerts/unsubscribe?token=cap-token-abc")
    assert r.status_code == 200
    assert "off the list" in r.text.lower()
    assert calls == ["cap-token-abc"]


def test_unsubscribe_unknown_token_renders_same_page_no_oracle(client, monkeypatch):
    monkeypatch.setattr(web_app.inventory, "unsubscribe_by_token", lambda t: False)
    r = client.get("/alerts/unsubscribe?token=nope")
    assert r.status_code == 200
    assert "off the list" in r.text.lower()


def test_unsubscribe_post_one_click_rfc8058(client, monkeypatch):
    calls = []
    monkeypatch.setattr(
        web_app.inventory, "unsubscribe_by_token",
        lambda t: calls.append(t) or True,
    )
    # RFC 8058 one-click: provider POSTs with the token in the query string.
    r = client.post("/alerts/unsubscribe?token=xyz",
                    data={"List-Unsubscribe": "One-Click"})
    assert r.status_code == 200
    assert calls == ["xyz"]


def test_unsubscribe_survives_db_error(client, monkeypatch):
    def boom(t):
        raise RuntimeError("db down")

    monkeypatch.setattr(web_app.inventory, "unsubscribe_by_token", boom)
    r = client.get("/alerts/unsubscribe?token=abc")
    assert r.status_code == 200
    assert "off the list" in r.text.lower()
