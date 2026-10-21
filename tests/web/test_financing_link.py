"""Offline tests for the Affirm financing link (operator-made, kind='financed').

The properties worth defending:

  1. **Operator-only.** The route sits under /api/, so the session middleware
     gates it; a buyer can never pick their own financed amount.
  2. **The public checkout can't create one.** /reserve/{lot}/checkout still
     rejects kind='financed'.
  3. **Affirm's band is checked before Stripe.** $50–$30,000, never above the
     order total — a typo is a 400, not a Stripe error after a row exists.
  4. **The rail is Affirm only**, with a billing address (raises approval).
  5. **A database without migration 034** answers 409 with the apply command.
"""
import sys
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from automation import config as config_mod
from automation import deposits, inventory, stripe_gateway
from automation.web import auth as auth_svc
from automation.web.app import app

app_module = sys.modules["automation.web.app"]

_AUTH_ENV = ("ADMIN_PASSWORD", "SESSION_SECRET", "TOTP_SECRET")


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for var in _AUTH_ENV:
        monkeypatch.delenv(var, raising=False)
    auth_svc.reset_caches()
    monkeypatch.setattr(config_mod, "STRIPE_SECRET_KEY", "sk_test_unit")
    yield
    auth_svc.reset_caches()


def _client():
    return TestClient(app, base_url="https://testserver")


LOT = {"lot_id": "31225", "title": "Banquet chairs", "status": "listed",
       "price_per_chair": 25, "quantity_remaining": 400}


@pytest.fixture
def lot(monkeypatch):
    monkeypatch.setattr(
        inventory, "get", lambda lot_id: dict(LOT) if lot_id == "31225" else None
    )


@pytest.fixture
def store(monkeypatch):
    """Run the real `create_financed` validation; capture the INSERT instead of a DB."""
    seen = {"attached": None, "transitions": []}

    def fake_fetch_one(sql, params):
        seen["params"] = params
        return {"id": 41, "lot_id": params[0], "kind": params[1], "quantity": params[2],
                "price_per_chair": params[3], "subtotal_cents": params[4],
                "amount_cents": params[5], "currency": "usd",
                "buyer_name": params[6], "buyer_email": params[7],
                "buyer_phone": params[8], "status": "pending"}

    monkeypatch.setattr(deposits.db, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(
        deposits, "attach_session",
        lambda did, sid: seen.__setitem__("attached", (did, sid)),
    )
    monkeypatch.setattr(
        deposits, "transition",
        lambda did, status, **kw: seen["transitions"].append((did, status, kw)),
    )
    return seen


@pytest.fixture
def gateway(monkeypatch):
    seen = {}

    def fake_create(*, deposit, lot):
        seen["deposit"], seen["lot"] = deposit, lot
        return SimpleNamespace(id="cs_fin", url="https://checkout.stripe/fin")

    monkeypatch.setattr(stripe_gateway, "create_checkout_session", fake_create)
    return seen


def _post(body):
    return _client().post("/api/deposits/financing-link", json=body)


GOOD = {"lot_id": "31225", "quantity": 360, "amount_usd": "4500",
        "order_total_usd": "9000", "name": "Sam", "phone": "4045550100"}


def test_makes_an_affirm_link_for_half_of_a_9k_deal(lot, store, gateway):
    r = _post(GOOD)
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "url": "https://checkout.stripe/fin", "deposit_id": 41}
    p = store["params"]
    assert p[1] == "financed"
    assert p[4] == 900000 and p[5] == 450000      # subtotal, amount — cents
    assert str(p[3]) == "25.00"                    # 9000 / 360
    assert store["attached"] == (41, "cs_fin")


def test_order_total_defaults_to_qty_times_lot_price(lot, store, gateway):
    body = dict(GOOD, order_total_usd=None, quantity=200)
    assert _post(body).status_code == 200
    assert store["params"][4] == 500000           # 200 × $25


@pytest.mark.parametrize("over, msg", [
    ({"amount_usd": "49.99"}, "$50 to $30,000"),
    ({"amount_usd": "30000.01", "order_total_usd": "40000"}, "$50 to $30,000"),
    ({"amount_usd": "9500"}, "more than the order total"),
    ({"amount_usd": "abc"}, "dollar amount"),
    ({"quantity": 0}, "quantity"),
    ({"phone": None}, "email or phone"),
])
def test_bad_input_is_a_400_before_stripe(lot, store, gateway, over, msg):
    r = _post(dict(GOOD, **over))
    assert r.status_code == 400
    assert msg in r.json()["detail"]
    assert "deposit" not in gateway


def test_unknown_lot_is_a_404(lot, store, gateway):
    assert _post(dict(GOOD, lot_id="nope")).status_code == 404


def test_dark_stripe_is_a_409(monkeypatch, lot, store, gateway):
    monkeypatch.setattr(config_mod, "STRIPE_SECRET_KEY", "")
    assert _post(GOOD).status_code == 409


def test_missing_migration_is_a_409_with_the_command(monkeypatch, lot, gateway):
    class CheckViolation(Exception):
        sqlstate = "23514"

    def boom(sql, params):
        raise CheckViolation("deposits_kind_check")

    monkeypatch.setattr(deposits.db, "fetch_one", boom)
    r = _post(GOOD)
    assert r.status_code == 409
    assert "034_deposits_financed.sql" in r.json()["detail"]


def test_stripe_refusal_cancels_the_row_and_says_why(monkeypatch, lot, store):
    def boom(**kw):
        raise RuntimeError("affirm is not activated on this account")

    monkeypatch.setattr(stripe_gateway, "create_checkout_session", boom)
    r = _post(GOOD)
    assert r.status_code == 502
    assert "affirm is not activated" in r.json()["detail"]
    assert store["transitions"][0][:2] == (41, "canceled")


def test_route_is_behind_the_admin_login(monkeypatch, lot, store, gateway):
    monkeypatch.setenv("ADMIN_PASSWORD", "hunter2-but-much-longer")
    monkeypatch.setenv("SESSION_SECRET", "unit-test-secret")
    auth_svc.reset_caches()
    r = _post(GOOD)
    assert r.status_code in (401, 403)
    assert "params" not in store


def test_public_checkout_still_refuses_financed(monkeypatch, lot):
    r = _client().post(
        "/reserve/31225/checkout",
        json={"quantity": 10, "kind": "financed", "name": "x", "email": "a@b.co"},
    )
    assert r.status_code == 400


# ─────────────────────────── gateway construction ───────────────────────────

@pytest.fixture
def fake_stripe(monkeypatch):
    seen = {}

    def create(**params):
        seen.update(params)
        return SimpleNamespace(id="cs_fake", url="https://checkout.stripe/fake")

    monkeypatch.setattr(
        stripe_gateway, "_stripe",
        lambda: SimpleNamespace(
            checkout=SimpleNamespace(Session=SimpleNamespace(create=create))
        ),
    )
    return seen


def test_gateway_financed_is_affirm_only_with_billing_address(monkeypatch, fake_stripe):
    monkeypatch.setattr(config_mod, "PUBLIC_BASE_URL", "https://black-whole.com")
    stripe_gateway.create_checkout_session(
        deposit={"id": 41, "lot_id": "31225", "kind": "financed", "quantity": 360,
                 "amount_cents": 450000, "currency": "usd"},
        lot=LOT,
    )
    assert fake_stripe["payment_method_types"] == ["affirm"]
    assert fake_stripe["billing_address_collection"] == "required"
    assert fake_stripe["line_items"][0]["price_data"]["unit_amount"] == 450000
    assert fake_stripe["custom_text"]["submit"]["message"] == (
        stripe_gateway.FINANCED_POLICY_SHORT
    )
    assert fake_stripe["cancel_url"] == "https://black-whole.com/listings/31225"
    assert "Financed payment" in fake_stripe["line_items"][0]["price_data"][
        "product_data"]["name"]


def test_affirm_webhook_lands_paid_with_method_affirm(monkeypatch):
    """Affirm settles at once: the existing completed-handler marks it paid."""
    monkeypatch.setattr(deposits, "get_by_session", lambda sid: {"id": 41})
    calls = []
    monkeypatch.setattr(
        deposits, "transition",
        lambda did, status, **kw: calls.append((did, status, kw)) or ({}, True),
    )
    deposits.apply_stripe_event({
        "type": "checkout.session.completed",
        "data": {"object": {"id": "cs_fin", "payment_status": "paid",
                            "payment_intent": "pi_1",
                            "payment_method_types": ["affirm"]}},
    })
    assert calls == [(41, "paid", {"payment_intent": "pi_1", "payment_method": "affirm"})]
