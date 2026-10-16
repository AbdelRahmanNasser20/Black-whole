"""Platform subscription: dark gate, checkout, webhook plan sync."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from automation import db, platform_billing, stripe_gateway
from automation.web import platform_auth, platform_routes

USER = {"user_id": "11111111-1111-1111-1111-111111111111", "email": "a@b.com", "onboarded_at": None}


@pytest.fixture
def client(monkeypatch):
    app = FastAPI()
    app.include_router(platform_routes.router)
    app.dependency_overrides[platform_auth.get_current_user] = lambda: dict(USER)
    return TestClient(app)


def test_subscribe_404_when_dark(client, monkeypatch):
    monkeypatch.delenv("PLATFORM_BILLING_ENABLED", raising=False)
    monkeypatch.setenv("PLATFORM_STRIPE_PRICE_ID", "price_x")
    assert client.post("/platform/api/subscribe").status_code == 404


def test_subscribe_creates_checkout_when_enabled(client, monkeypatch):
    monkeypatch.setenv("PLATFORM_BILLING_ENABLED", "1")
    monkeypatch.setenv("PLATFORM_STRIPE_PRICE_ID", "price_x")
    monkeypatch.setenv("PLATFORM_BASE_URL", "https://p.example")
    writes = []
    monkeypatch.setattr(db, "fetch_one", lambda sql, params=None: {"stripe_customer_id": None})
    monkeypatch.setattr(db, "execute", lambda sql, params=None: writes.append((sql, params)) or 1)
    monkeypatch.setattr(stripe_gateway, "create_platform_customer", lambda **kw: "cus_1")
    seen = {}

    def fake_checkout(**kw):
        seen.update(kw)
        return {"url": "https://stripe.test/c"}

    monkeypatch.setattr(stripe_gateway, "create_subscription_checkout", fake_checkout)
    r = client.post("/platform/api/subscribe", json={"price": "price_evil", "amount": 1})
    assert r.status_code == 200 and r.json()["url"] == "https://stripe.test/c"
    assert seen == {"customer_id": "cus_1", "price_id": "price_x", "base_url": "https://p.example"}
    assert writes[0][1] == ("cus_1", USER["user_id"])


def _event(etype, status="active", customer="cus_1"):
    return {"type": etype, "data": {"object": {"customer": customer, "status": status}}}


def test_subscription_webhook_sets_plan(monkeypatch):
    calls = []
    monkeypatch.setattr(db, "execute", lambda sql, params=None: calls.append(params) or 1)
    out = platform_billing.apply_stripe_event(_event("customer.subscription.created"))
    assert out == {"handled": True, "changed": True, "plan": "pro"}
    assert calls[-1][:2] == ("pro", "cus_1")
    out = platform_billing.apply_stripe_event(_event("customer.subscription.deleted", "canceled"))
    assert out["plan"] == "beta" and calls[-1][:2] == ("beta", "cus_1")
    assert platform_billing.apply_stripe_event(_event("invoice.paid"))["handled"] is False


def test_replayed_webhook_idempotent(monkeypatch):
    # DB reports 0 rows updated (plan already equal): changed must be False.
    monkeypatch.setattr(db, "execute", lambda sql, params=None: 0)
    out = platform_billing.apply_stripe_event(_event("customer.subscription.updated"))
    assert out == {"handled": True, "changed": False, "plan": "pro"}
