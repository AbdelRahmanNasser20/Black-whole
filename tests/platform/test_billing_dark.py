"""Platform subscription: dark gate, checkout, webhook plan sync + ordering."""
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from automation import config as config_mod
from automation import db, deposits, platform_billing, stripe_gateway
from automation.web import platform_auth, platform_routes
from automation.web.app import app as real_app

app_module = sys.modules["automation.web.app"]
USER = {"user_id": "11111111-1111-1111-1111-111111111111", "email": "a@b.com",
        "onboarded_at": "2026-10-16T00:00:00Z"}


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(platform_routes.router)
    app.dependency_overrides[platform_auth.get_current_user] = lambda: dict(USER)
    return TestClient(app)


def _lit(monkeypatch):
    monkeypatch.setenv("PLATFORM_BILLING_ENABLED", "1")
    monkeypatch.setenv("PLATFORM_STRIPE_PRICE_ID", "price_x")
    monkeypatch.setenv("PLATFORM_BASE_URL", "https://p.example")


def test_subscribe_404_when_dark(client, monkeypatch):
    monkeypatch.delenv("PLATFORM_BILLING_ENABLED", raising=False)
    monkeypatch.setenv("PLATFORM_STRIPE_PRICE_ID", "price_x")
    assert client.post("/platform/api/subscribe").status_code == 404


def test_subscribe_404_when_dark_even_unauthenticated(monkeypatch):
    monkeypatch.delenv("PLATFORM_BILLING_ENABLED", raising=False)
    app = FastAPI()
    app.include_router(platform_routes.router)
    assert TestClient(app).post("/platform/api/subscribe").status_code == 404


def test_subscribe_creates_checkout_when_enabled(client, monkeypatch):
    _lit(monkeypatch)
    writes = []
    monkeypatch.setattr(db, "fetch_one", lambda sql, params=None: {"plan": "beta", "stripe_customer_id": None})
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
    assert "stripe_customer_id IS NULL" in writes[0][0]
    assert writes[0][1] == ("cus_1", USER["user_id"])


def test_subscribe_race_uses_stored_customer(client, monkeypatch):
    _lit(monkeypatch)
    rows = iter([{"plan": "beta", "stripe_customer_id": None}, {"stripe_customer_id": "cus_winner"}])
    monkeypatch.setattr(db, "fetch_one", lambda sql, params=None: next(rows))
    monkeypatch.setattr(db, "execute", lambda sql, params=None: 0)  # lost the claim
    monkeypatch.setattr(stripe_gateway, "create_platform_customer", lambda **kw: "cus_loser")
    seen = {}
    monkeypatch.setattr(stripe_gateway, "create_subscription_checkout",
                        lambda **kw: seen.update(kw) or {"url": "u"})
    assert client.post("/platform/api/subscribe").status_code == 200
    assert seen["customer_id"] == "cus_winner"


def test_subscribe_409_when_already_pro(client, monkeypatch):
    _lit(monkeypatch)
    monkeypatch.setattr(db, "fetch_one", lambda sql, params=None: {"plan": "pro", "stripe_customer_id": "c"})
    assert client.post("/platform/api/subscribe").status_code == 409


def test_subscribe_503_without_base_url(client, monkeypatch):
    _lit(monkeypatch)
    monkeypatch.delenv("PLATFORM_BASE_URL")
    assert client.post("/platform/api/subscribe").status_code == 503


def test_subscribe_requires_onboarding(monkeypatch):
    _lit(monkeypatch)
    app = FastAPI()
    app.include_router(platform_routes.router)
    app.dependency_overrides[platform_auth.get_current_user] = lambda: {**USER, "onboarded_at": None}
    assert TestClient(app).post("/platform/api/subscribe").status_code == 403


class FakeStore:
    """Emulates platform_billing.UPDATE_SQL semantics for one customer."""

    def __init__(self):
        self.plan, self.at, self.sqls = "beta", None, []

    def fetch_one(self, sql, params):
        self.sqls.append(sql)
        _cust, plan, created, _c2 = params
        if self.at is not None and self.at > created:
            return None
        old, self.plan, self.at = self.plan, plan, created
        return {"old_plan": old}


def _event(etype, status="active", customer="cus_1", created=100):
    return {"type": etype, "created": created,
            "data": {"object": {"customer": customer, "status": status}}}


@pytest.fixture
def store(monkeypatch):
    s = FakeStore()
    monkeypatch.setattr(db, "fetch_one", s.fetch_one)
    return s


def test_subscription_webhook_sets_plan(store):
    out = platform_billing.apply_stripe_event(_event("customer.subscription.created", created=1))
    assert out == {"handled": True, "changed": True, "plan": "pro"} and store.plan == "pro"
    out = platform_billing.apply_stripe_event(_event("customer.subscription.deleted", "canceled", created=2))
    assert out["plan"] == "beta" and store.plan == "beta"
    assert platform_billing.apply_stripe_event(_event("invoice.paid"))["handled"] is False


def test_replayed_webhook_idempotent(store):
    ev = _event("customer.subscription.updated", created=5)
    assert platform_billing.apply_stripe_event(ev)["changed"] is True
    assert platform_billing.apply_stripe_event(ev)["changed"] is False
    sql = store.sqls[0]
    assert "billing_event_at IS NULL OR u.billing_event_at <= to_timestamp(%s)" in sql
    assert "RETURNING old.plan" in sql and "%s" in sql


def test_stale_updated_after_deleted_is_ignored(store):
    platform_billing.apply_stripe_event(_event("customer.subscription.created", created=1))
    platform_billing.apply_stripe_event(_event("customer.subscription.deleted", "canceled", created=3))
    out = platform_billing.apply_stripe_event(_event("customer.subscription.updated", "active", created=2))
    assert out["changed"] is False and store.plan == "beta"


def test_webhook_route_runs_billing_and_deposits(monkeypatch):
    monkeypatch.setattr(config_mod, "STRIPE_SECRET_KEY", "sk_test")
    monkeypatch.setattr(config_mod, "STRIPE_WEBHOOK_SECRET", "whsec")
    ev = _event("customer.subscription.created")
    monkeypatch.setattr(stripe_gateway, "verify_webhook", lambda raw, sig: ev)
    billed, deposited = [], []
    monkeypatch.setattr(platform_billing, "apply_stripe_event", lambda e: billed.append(e) or {})
    monkeypatch.setattr(deposits, "apply_stripe_event", lambda e: deposited.append(e) or (None, False))
    r = TestClient(real_app, base_url="https://testserver").post(
        "/stripe/webhook", content=b"{}", headers={"stripe-signature": "x"})
    assert r.status_code == 200 and billed == [ev] and deposited == [ev]
