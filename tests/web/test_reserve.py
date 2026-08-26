"""Offline tests for Reserve-with-deposit (B2): gateway, public routes, webhook.

Same shape as ``tests/web/test_auth.py`` — ``TestClient`` built WITHOUT a
``with`` block so the lifespan (and its DB-touching alert scheduler) never runs.
Every DB call site is monkeypatched: ``inventory.get``, the ``deposits`` store
functions, and ``site_settings.get_all``. Nothing here opens a connection and
nothing here talks to Stripe over the network.

The two properties worth the most here:

  1. **The client cannot name its own price.** The checkout test POSTs a bogus
     ``amount`` and asserts the gateway was handed the server-derived cents.
  2. **A replayed webhook is silent.** Stripe retries for three days; the
     operator must not get three days of Telegram pings.

The webhook signature is built for real (v1 = HMAC-SHA256 over
``"{timestamp}.{payload}"``) so ``stripe.Webhook.construct_event`` actually runs
rather than being stubbed into always-passing.
"""
import hashlib
import hmac
import json
import sys
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from automation import config as config_mod
from automation import deposits, inventory, site_settings, stripe_gateway
from automation.web.app import app

# `automation.web.__init__` re-exports the FastAPI instance as `automation.web.app`,
# which shadows the submodule of the same name — so reach for the module itself
# through sys.modules rather than an import statement.
app_module = sys.modules["automation.web.app"]

SECRET_KEY = "sk_test_unit"
WEBHOOK_SECRET = "whsec_unit_test_secret"


# ─────────────────────────────── fixtures ───────────────────────────────────

@pytest.fixture(autouse=True)
def _dark_by_default(monkeypatch):
    """Every test starts with the feature off — lighting it is explicit."""
    monkeypatch.setattr(config_mod, "STRIPE_SECRET_KEY", "")
    monkeypatch.setattr(config_mod, "STRIPE_WEBHOOK_SECRET", "")
    monkeypatch.setattr(
        site_settings, "get_all",
        lambda: {"deposit_pct": 0.15, "deposit_min_usd": 200},
    )


def _lit(monkeypatch, *, webhook: bool = True):
    monkeypatch.setattr(config_mod, "STRIPE_SECRET_KEY", SECRET_KEY)
    monkeypatch.setattr(
        config_mod, "STRIPE_WEBHOOK_SECRET", WEBHOOK_SECRET if webhook else ""
    )


def _client():
    # https so Secure cookies round-trip; no `with` so lifespan never fires.
    return TestClient(app, base_url="https://testserver")


def make_lot(**over) -> dict:
    lot = {
        "lot_id": "31225",
        "title": "Mity-Lite folding chairs",
        "status": "listed",
        "price_per_chair": 100,
        "quantity_remaining": 100,
        "quantity_original": 945,
        "city": "Baltimore",
        "state": "MD",
        "hero_image_url": "https://cdn.example/31225.jpg",
    }
    lot.update(over)
    return lot


def make_deposit(**over) -> dict:
    row = {
        "id": 7,
        "lot_id": "31225",
        "kind": "deposit",
        "quantity": 30,
        "price_per_chair": 100,
        "subtotal_cents": 300000,
        "amount_cents": 45000,
        "currency": "usd",
        "status": "pending",
        "stripe_session_id": "cs_test_1",
        "stripe_payment_intent": None,
        "payment_method": None,
        "failure_reason": None,
        "buyer_name": "Dana Buyer",
        "buyer_email": "dana@example.com",
        "buyer_phone": None,
    }
    row.update(over)
    return row


@pytest.fixture
def lot(monkeypatch):
    """`inventory.get` returns a single in-memory lot; mutate it per test."""
    row = make_lot()
    monkeypatch.setattr(
        inventory, "get",
        lambda lot_id: dict(row) if lot_id == row["lot_id"] else None,
    )
    return row


@pytest.fixture
def gateway(monkeypatch):
    """Records what `create_checkout_session` was handed, returns a fake session."""
    seen = {}

    def fake_create(*, deposit, lot):
        seen["deposit"] = dict(deposit)
        seen["lot"] = dict(lot)
        return SimpleNamespace(id="cs_test_created", url="https://checkout.stripe/x")

    monkeypatch.setattr(stripe_gateway, "create_checkout_session", fake_create)
    return seen


@pytest.fixture
def store(monkeypatch):
    """Stub the deposits write path; keeps the real `quote_for_lot` math live."""
    calls = {"pending": [], "attached": [], "transitions": []}

    def fake_create_pending(**kw):
        calls["pending"].append(kw)
        quote = kw["quote"]
        return make_deposit(
            id=7, lot_id=kw["lot_id"], kind=quote.kind, quantity=kw["quantity"],
            price_per_chair=kw["price_per_chair"],
            subtotal_cents=quote.subtotal_cents, amount_cents=quote.amount_cents,
            buyer_name=kw.get("buyer_name"), buyer_email=kw.get("buyer_email"),
            buyer_phone=kw.get("buyer_phone"),
        )

    def fake_attach(deposit_id, session_id):
        calls["attached"].append((deposit_id, session_id))
        return make_deposit(stripe_session_id=session_id)

    def fake_transition(deposit_id, status, **kw):
        calls["transitions"].append((deposit_id, status, kw))
        return make_deposit(status=status), True

    monkeypatch.setattr(deposits, "create_pending", fake_create_pending)
    monkeypatch.setattr(deposits, "attach_session", fake_attach)
    monkeypatch.setattr(deposits, "transition", fake_transition)
    return calls


@pytest.fixture
def notified(monkeypatch):
    """Count Telegram alerts. The spy records SYNCHRONOUSLY (at create_task
    time), so the assertion doesn't depend on the task getting scheduled."""
    seen = []

    async def _noop(row, event_type):
        return None

    def spy(row, event_type):
        seen.append((row, event_type))
        return _noop(row, event_type)

    monkeypatch.setattr(app_module, "_notify_deposit", spy)
    return seen


def _post_checkout(client, body, lot_id="31225"):
    return client.post(f"/reserve/{lot_id}/checkout", json=body)


VALID_BODY = {
    "quantity": 30,
    "kind": "deposit",
    "name": "Dana Buyer",
    "email": "dana@example.com",
}


# ─────────────────────────────── dark mode ──────────────────────────────────

def test_dark_mode_hides_every_reserve_surface(lot):
    """No STRIPE_SECRET_KEY => the feature does not exist on the network."""
    client = _client()
    assert client.get("/reserve/31225", follow_redirects=False).status_code == 404
    assert client.get("/reserve/success?session_id=cs_x").status_code == 404
    assert _post_checkout(client, VALID_BODY).status_code == 404
    assert client.post("/stripe/webhook", content=b"{}").status_code == 404


def test_webhook_stays_404_without_a_signing_secret(monkeypatch, lot):
    """A key but no whsec means we cannot verify anything — stay closed."""
    _lit(monkeypatch, webhook=False)
    assert _client().post("/stripe/webhook", content=b"{}").status_code == 404


def test_public_ctx_advertises_the_gate(monkeypatch):
    assert app_module._public_ctx({})["reserve_enabled"] is False
    _lit(monkeypatch)
    assert app_module._public_ctx({})["reserve_enabled"] is True


# ────────────────────────────── reserve page ────────────────────────────────

def test_reserve_page_renders_the_rule(monkeypatch, lot):
    _lit(monkeypatch)
    r = _client().get("/reserve/31225")
    assert r.status_code == 200
    assert "31225" in r.text
    assert "15.0%" in r.text          # the live rule, not a hardcoded one
    assert "200.0" in r.text          # the floor
    assert stripe_gateway.REFUND_POLICY_SHORT in r.text


def test_reserve_page_404s_on_unknown_and_hidden_lots(monkeypatch, lot):
    _lit(monkeypatch)
    client = _client()
    assert client.get("/reserve/nope").status_code == 404
    lot["status"] = "hidden"
    assert client.get("/reserve/31225").status_code == 404


@pytest.mark.parametrize(
    "patch",
    [
        {"status": "sold_out"},
        {"status": "lost_sold_out"},
        {"quantity_remaining": 0},
        {"price_per_chair": None},
    ],
)
def test_unreservable_lots_redirect_to_the_listing(monkeypatch, lot, patch):
    _lit(monkeypatch)
    lot.update(patch)
    r = _client().get("/reserve/31225", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/listings/31225"


# ──────────────────────────────── checkout ──────────────────────────────────

def test_checkout_prices_the_order_server_side(monkeypatch, lot, store, gateway):
    """The client sends a quantity. Everything else is ours.

    30 × $100 = $3,000 → 15% = $450 = 45000¢. The payload's `amount` is noise.
    """
    _lit(monkeypatch)
    body = dict(VALID_BODY, amount=1, amount_cents=1, price_per_chair=0.01)
    r = _post_checkout(_client(), body)
    assert r.status_code == 200
    assert r.json() == {
        "ok": True, "url": "https://checkout.stripe/x", "deposit_id": 7,
    }

    quote = store["pending"][0]["quote"]
    assert (quote.subtotal_cents, quote.amount_cents) == (300000, 45000)
    # The number that actually reaches Stripe is the server's, not the client's.
    assert gateway["deposit"]["amount_cents"] == 45000
    assert store["attached"] == [(7, "cs_test_created")]


def test_checkout_pay_in_full_charges_the_subtotal(monkeypatch, lot, store, gateway):
    _lit(monkeypatch)
    r = _post_checkout(_client(), dict(VALID_BODY, kind="full"))
    assert r.status_code == 200
    assert gateway["deposit"]["amount_cents"] == 300000


@pytest.mark.parametrize("quantity", [0, -3, 101, "many", None])
def test_checkout_rejects_a_quantity_the_lot_cannot_fill(
    monkeypatch, lot, store, gateway, quantity
):
    _lit(monkeypatch)
    r = _post_checkout(_client(), dict(VALID_BODY, quantity=quantity))
    assert r.status_code == 400
    assert store["pending"] == []


def test_checkout_rejects_a_bad_kind(monkeypatch, lot, store, gateway):
    _lit(monkeypatch)
    assert _post_checkout(
        _client(), dict(VALID_BODY, kind="layaway")
    ).status_code == 400


@pytest.mark.parametrize(
    "body",
    [
        {"quantity": 5, "email": "d@example.com"},              # no name
        {"quantity": 5, "name": "  ", "email": "d@example.com"},
        {"quantity": 5, "name": "Dana"},                        # no contact
        {"quantity": 5, "name": "Dana", "email": " ", "phone": ""},
    ],
)
def test_checkout_requires_a_name_and_a_way_to_reach_them(
    monkeypatch, lot, store, gateway, body
):
    _lit(monkeypatch)
    r = _post_checkout(_client(), body)
    assert r.status_code == 400
    assert store["pending"] == []


def test_checkout_on_a_sold_lot_is_a_400(monkeypatch, lot, store, gateway):
    _lit(monkeypatch)
    lot["status"] = "sold_out"
    assert _post_checkout(_client(), VALID_BODY).status_code == 400


def test_session_create_failure_cancels_the_row_and_502s(
    monkeypatch, lot, store
):
    """A dead Stripe must not leave a pending row no webhook can ever resolve."""
    _lit(monkeypatch)

    def boom(**_kw):
        raise RuntimeError("stripe is down")

    monkeypatch.setattr(stripe_gateway, "create_checkout_session", boom)
    r = _post_checkout(_client(), VALID_BODY)
    assert r.status_code == 502
    assert r.json()["detail"] == "checkout_unavailable"
    assert store["transitions"] == [
        (7, "canceled", {"failure_reason": "session_create_failed"})
    ]


# ───────────────────────────── success page ─────────────────────────────────

def test_success_page_is_registered_before_the_lot_route(monkeypatch, lot):
    """`/reserve/{lot_id}` must not swallow "success"."""
    _lit(monkeypatch)
    monkeypatch.setattr(
        deposits, "get_by_session",
        lambda sid: make_deposit(status="paid") if sid == "cs_test_1" else None,
    )
    r = _client().get("/reserve/success?session_id=cs_test_1")
    assert r.status_code == 200
    assert "RESERVED" in r.text


def test_success_page_says_processing_while_ach_settles(monkeypatch, lot):
    _lit(monkeypatch)
    monkeypatch.setattr(
        deposits, "get_by_session", lambda sid: make_deposit(status="processing")
    )
    r = _client().get("/reserve/success?session_id=cs_test_1")
    assert r.status_code == 200
    assert "PAYMENT INITIATED" in r.text
    assert "RESERVED" not in r.text


def test_success_page_404s_on_an_unknown_session(monkeypatch, lot):
    _lit(monkeypatch)
    monkeypatch.setattr(deposits, "get_by_session", lambda sid: None)
    assert _client().get("/reserve/success?session_id=cs_nope").status_code == 404


# ──────────────────────────────── /terms ────────────────────────────────────

def test_terms_renders_dark_and_lit(monkeypatch):
    client = _client()
    dark = client.get("/terms")
    assert dark.status_code == 200
    assert "DEPOSITS" in dark.text
    _lit(monkeypatch)
    lit = client.get("/terms")
    assert lit.status_code == 200
    assert 'id="deposits"' in lit.text


# ──────────────────────────────── webhook ───────────────────────────────────

def _sign(payload: bytes, secret: str = WEBHOOK_SECRET, ts: int | None = None) -> str:
    """A real Stripe-Signature header: v1 = HMAC-SHA256 over "{ts}.{payload}"."""
    ts = ts or int(time.time())
    signed = f"{ts}.".encode() + payload
    mac = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return f"t={ts},v1={mac}"


def _event(event_type: str, obj: dict) -> bytes:
    # The top-level `"object": "event"` is NOT optional: stripe 15.x's
    # construct_event reads `event.object` to tell v1 from v2 events and raises
    # AttributeError without it. Real Stripe payloads always carry it.
    return json.dumps(
        {"id": "evt_1", "object": "event", "type": event_type,
         "data": {"object": obj}}
    ).encode()


def _post_event(client, payload: bytes, header: str | None = None):
    return client.post(
        "/stripe/webhook",
        content=payload,
        headers={"stripe-signature": header if header is not None else _sign(payload)},
    )


@pytest.fixture
def applied(monkeypatch):
    """Capture what `apply_stripe_event` was handed; control what it reports."""
    seen = []
    outcome = {"row": make_deposit(status="paid"), "changed": True}

    def fake_apply(event):
        seen.append(event)
        return outcome["row"], outcome["changed"]

    monkeypatch.setattr(deposits, "apply_stripe_event", fake_apply)
    return SimpleNamespace(events=seen, outcome=outcome)


def test_webhook_rejects_a_bad_signature(monkeypatch, applied, notified):
    _lit(monkeypatch)
    payload = _event("checkout.session.completed", {"id": "cs_test_1"})
    r = _post_event(_client(), payload, header="t=1,v1=deadbeef")
    assert r.status_code == 400
    assert applied.events == []
    assert notified == []


def test_webhook_rejects_a_signature_for_a_different_body(monkeypatch, applied):
    """Signature is over the exact bytes — a swapped body must not verify."""
    _lit(monkeypatch)
    header = _sign(_event("checkout.session.completed", {"id": "cs_test_1"}))
    other = _event("checkout.session.completed", {"id": "cs_someone_else"})
    assert _post_event(_client(), other, header=header).status_code == 400
    assert applied.events == []


@pytest.mark.parametrize(
    "event_type,obj",
    [
        ("checkout.session.completed",
         {"id": "cs_test_1", "payment_status": "paid",
          "payment_intent": "pi_1", "payment_method_types": ["card"]}),
        ("checkout.session.async_payment_succeeded",
         {"id": "cs_test_1", "payment_intent": "pi_1"}),
        ("checkout.session.async_payment_failed",
         {"id": "cs_test_1", "payment_intent": {"id": "pi_1",
                                                "last_payment_error": {"message": "R01"}}}),
        ("checkout.session.expired", {"id": "cs_test_1"}),
        ("charge.refunded", {"id": "ch_1", "payment_intent": "pi_1"}),
    ],
)
def test_webhook_forwards_each_event_type_as_a_plain_dict(
    monkeypatch, applied, notified, event_type, obj
):
    """Stripe 15.x StripeObjects aren't dicts — the store must still get one."""
    _lit(monkeypatch)
    r = _post_event(_client(), _event(event_type, obj))
    assert r.status_code == 200
    assert r.json() == {"ok": True}

    (event,) = applied.events
    assert isinstance(event, dict)
    assert event["type"] == event_type
    assert isinstance(event["data"]["object"], dict)
    assert event["data"]["object"]["id"] == obj["id"]
    assert len(notified) == 1
    assert notified[0][1] == event_type


def test_webhook_replay_alerts_once(monkeypatch, applied, notified):
    """Stripe retries for three days. The operator gets one ping, not three."""
    _lit(monkeypatch)
    client = _client()
    payload = _event(
        "checkout.session.completed",
        {"id": "cs_test_1", "payment_status": "paid", "payment_intent": "pi_1"},
    )
    assert _post_event(client, payload).status_code == 200
    assert len(notified) == 1

    # Second delivery: the state machine no-ops and reports changed=False.
    applied.outcome["changed"] = False
    assert _post_event(client, payload).status_code == 200
    assert len(notified) == 1


def test_webhook_answers_200_for_an_unmatched_event(monkeypatch, applied, notified):
    """Unknown type / row we don't own is not an error — a non-2xx means retries."""
    _lit(monkeypatch)
    applied.outcome.update(row=None, changed=False)
    r = _post_event(_client(), _event("invoice.paid", {"id": "in_1"}))
    assert r.status_code == 200
    assert notified == []


# ──────────────────────────── gateway construction ──────────────────────────

@pytest.fixture
def fake_stripe(monkeypatch):
    """Swap the lazily-imported SDK for a recorder — no network, no key needed."""
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


def test_gateway_bills_the_row_not_the_lot(fake_stripe):
    stripe_gateway.create_checkout_session(
        deposit=make_deposit(), lot=make_lot(price_per_chair=999),
    )
    line = fake_stripe["line_items"][0]
    assert line["price_data"]["unit_amount"] == 45000
    assert line["price_data"]["currency"] == "usd"
    assert fake_stripe["mode"] == "payment"


def test_gateway_rails_depend_on_the_kind(fake_stripe):
    stripe_gateway.create_checkout_session(deposit=make_deposit(), lot=make_lot())
    assert fake_stripe["payment_method_types"] == ["card", "us_bank_account"]
    # Pay-in-full is bank-only: 2.9% on a $3k order is not a rounding error.
    stripe_gateway.create_checkout_session(
        deposit=make_deposit(kind="full", amount_cents=300000), lot=make_lot()
    )
    assert fake_stripe["payment_method_types"] == ["us_bank_account"]


def test_gateway_stamps_metadata_on_the_session_and_the_intent(fake_stripe):
    """`charge.refunded` only ever sees the PaymentIntent — it needs its own copy."""
    stripe_gateway.create_checkout_session(deposit=make_deposit(), lot=make_lot())
    expected = {"deposit_id": "7", "lot_id": "31225", "qty": "30", "kind": "deposit"}
    assert fake_stripe["metadata"] == expected
    assert fake_stripe["payment_intent_data"]["metadata"] == expected


def test_gateway_urls_and_policy(monkeypatch, fake_stripe):
    monkeypatch.setattr(config_mod, "PUBLIC_BASE_URL", "https://black-whole.com")
    stripe_gateway.create_checkout_session(deposit=make_deposit(), lot=make_lot())
    assert fake_stripe["success_url"] == (
        "https://black-whole.com/reserve/success?session_id={CHECKOUT_SESSION_ID}"
    )
    assert fake_stripe["cancel_url"] == "https://black-whole.com/reserve/31225?canceled=1"
    assert fake_stripe["custom_text"]["submit"]["message"] == (
        stripe_gateway.REFUND_POLICY_SHORT
    )
    assert fake_stripe["customer_email"] == "dana@example.com"


def test_gateway_omits_customer_email_when_only_a_phone_is_known(fake_stripe):
    stripe_gateway.create_checkout_session(
        deposit=make_deposit(buyer_email=None, buyer_phone="+15555550100"),
        lot=make_lot(),
    )
    assert "customer_email" not in fake_stripe


def test_enabled_follows_the_secret_key(monkeypatch):
    assert stripe_gateway.enabled() is False
    monkeypatch.setattr(config_mod, "STRIPE_SECRET_KEY", SECRET_KEY)
    assert stripe_gateway.enabled() is True
