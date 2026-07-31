"""Stripe Checkout — the ONLY module in this repo that imports `stripe`.

The import is lazy (inside the functions, like `pyotp` in `web/auth.py`) for one
reason: the whole Reserve feature ships dark. With `STRIPE_SECRET_KEY` unset the
routes 404 and nothing here is ever called, so a missing/broken SDK can't take
the storefront down. It also keeps `import automation.web.app` cheap in tests.

The hard rule this module exists to hold: **the charge amount comes off the
`deposits` row the server already wrote**, never off the request body. The
browser sends a quantity; `deposits.quote_for_lot()` turns that into cents;
`create_pending()` persists it; this module bills exactly that number. A client
that POSTs `{"amount": 1}` changes nothing.

Everything here is the sync Stripe SDK — callers wrap in `asyncio.to_thread`.

Stripe 15.x note: `StripeObject` is **not** a dict subclass and has no `.get()`.
`verify_webhook` therefore returns `event.to_dict()` (recursive by default), so
`deposits.apply_stripe_event()` keeps taking a plain dict and stays testable
with zero SDK involvement.
"""
from __future__ import annotations

from typing import Any

from . import config

# Shown on the Checkout submit button. Short by necessity — the full policy
# lives on /terms#deposits. Both must say the same thing; if you edit one, edit
# the other. Stripe caps custom_text.submit.message at 1200 chars.
REFUND_POLICY_SHORT = (
    "Fully refundable until freight is booked. After booking, the deposit "
    "covers freight if you cancel. Deposit applies to your balance at "
    "delivery — balance payable by Zelle or cash."
)


def enabled() -> bool:
    """The dark-ship gate. No secret key => no Reserve anything, anywhere."""
    return bool(config.STRIPE_SECRET_KEY)


def _stripe():
    """Lazy import + per-call key binding (the key can change under a test)."""
    import stripe

    stripe.api_key = config.STRIPE_SECRET_KEY
    return stripe


def _product_name(*, kind: str, title: str, quantity: int) -> str:
    label = "Deposit" if kind == "deposit" else "Payment in full"
    name = f"{label} — {title} × {quantity} chairs"
    return name[:250]  # Stripe rejects product names over 250 chars


def create_checkout_session(*, deposit: dict, lot: dict) -> Any:
    """Open a Checkout session for an already-persisted `deposits` row.

    `deposit` is the row (it carries the authoritative `amount_cents`); `lot` is
    the inventory row, used only for display copy.

    Rails: a deposit may go on card **or** bank — it's small, and card removes
    the 3-day ACH wait for a buyer who wants the lot held tonight. Pay-in-full
    is bank-only: 2.9% + 30¢ on a $6,000 order is $174 of margin, ACH is capped
    at $5.

    Metadata is written twice — on the session AND on the PaymentIntent. The
    session copy is what `checkout.session.*` events carry; the PI copy is the
    only handle `charge.refunded` gives us, since a dashboard refund never
    mentions the session.
    """
    stripe = _stripe()

    kind = (deposit.get("kind") or "deposit").strip()
    lot_id = str(deposit.get("lot_id") or lot.get("lot_id") or "")
    quantity = int(deposit.get("quantity") or 0)
    title = (lot.get("title") or f"Lot {lot_id}").strip()
    amount_cents = int(deposit["amount_cents"])  # SERVER value. Never the client's.

    metadata = {
        "deposit_id": str(deposit.get("id")),
        "lot_id": lot_id,
        "qty": str(quantity),
        "kind": kind,
    }

    params: dict[str, Any] = {
        "mode": "payment",
        "payment_method_types": (
            ["card", "us_bank_account"] if kind == "deposit" else ["us_bank_account"]
        ),
        "line_items": [
            {
                "quantity": 1,
                "price_data": {
                    "currency": (deposit.get("currency") or "usd"),
                    "unit_amount": amount_cents,
                    "product_data": {
                        "name": _product_name(
                            kind=kind, title=title, quantity=quantity
                        ),
                    },
                },
            }
        ],
        "metadata": metadata,
        "payment_intent_data": {"metadata": dict(metadata)},
        "custom_text": {"submit": {"message": REFUND_POLICY_SHORT}},
        "success_url": (
            f"{config.PUBLIC_BASE_URL}/reserve/success"
            "?session_id={CHECKOUT_SESSION_ID}"
        ),
        "cancel_url": f"{config.PUBLIC_BASE_URL}/reserve/{lot_id}?canceled=1",
    }

    email = (deposit.get("buyer_email") or "").strip()
    if email:
        # Pre-fills Checkout and gives Stripe someone to send the receipt to.
        params["customer_email"] = email

    return stripe.checkout.Session.create(**params)


def verify_webhook(payload: bytes, sig_header: str) -> dict:
    """Verify the Stripe-Signature header and return the event as a plain dict.

    Exceptions (`SignatureVerificationError`, bad JSON) propagate — the route
    turns them into a 400. Never trust an unverified webhook body: the endpoint
    is public by necessity, so the signature IS the authentication.
    """
    stripe = _stripe()
    event = stripe.Webhook.construct_event(
        payload, sig_header, config.STRIPE_WEBHOOK_SECRET
    )
    return event.to_dict()
