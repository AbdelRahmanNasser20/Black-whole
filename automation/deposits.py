"""Deposits ledger — one row per Stripe Checkout attempt, plus the money math.

Why this exists: a remote buyer won't wire $5k to a Facebook stranger and the
operator won't book a $400 freight lane on a stranger's word. A refundable
deposit breaks that standoff. This module owns three things:

  1. **The rule.** `deposit_rules()` resolves the live percentage + floor
     (per-lot override → `site_settings` → seeded env default).
  2. **The arithmetic.** `quote()` is pure and integer-cents throughout. It is
     the ONLY place a charge amount is computed — the browser never sends an
     amount, it sends a quantity, and the server re-derives everything.
  3. **The lifecycle.** `transition()` enforces a status state machine. Stripe
     webhooks arrive out of order and get replayed on retry; without the
     machine, a replayed `checkout.session.completed` would re-fire the
     operator's Telegram alert and a late `processing` could un-pay a paid row.
     Every mutation returns a `changed` flag so alerting keys off real changes.

No `stripe` import lives here — `apply_stripe_event()` takes the plain dict the
SDK already parsed, which keeps this module testable with zero network and lets
the whole feature stay dark when `STRIPE_SECRET_KEY` is unset.

Storage goes through `automation.db` (psycopg over Supabase), same as the
inquiries/subscribers stores. Schema is never created at runtime — DDL of
record is `scripts/sql/004_deposits.sql`.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from . import db, site_settings

DEPOSIT_KINDS = ("deposit", "full")
DEPOSIT_STATUSES = (
    "pending", "processing", "paid", "failed", "refunded", "canceled",
)

# The state machine. Anything not listed is illegal and becomes a no-op.
#   pending    — row created, Checkout session may not exist yet
#   processing — ACH debit initiated, settles in 1–4 business days
#   paid       — money is ours
#   failed / refunded / canceled — terminal. A refund reversal or a retry is a
#   NEW Checkout attempt (a new row), never a resurrection of this one.
_TRANSITIONS: dict[str, frozenset[str]] = {
    "pending": frozenset({"processing", "paid", "failed", "canceled"}),
    "processing": frozenset({"paid", "failed", "canceled"}),
    "paid": frozenset({"refunded"}),
    "failed": frozenset(),
    "refunded": frozenset(),
    "canceled": frozenset(),
}


# ───────────────────────────── the rule + the math ──────────────────────────

@dataclass(frozen=True)
class DepositQuote:
    """What the buyer owes now, and the rule that produced it."""
    subtotal_cents: int      # full order value: quantity × price_per_chair
    amount_cents: int        # charged at Checkout
    pct: float               # rule snapshot
    min_cents: int           # rule snapshot
    kind: str                # 'deposit' | 'full'

    @property
    def balance_cents(self) -> int:
        """Left to pay at delivery (Zelle/cash)."""
        return self.subtotal_cents - self.amount_cents


def deposit_rules(lot: dict | None = None) -> tuple[float, int]:
    """(pct, min_cents) for a lot — per-lot override wins, floor is global."""
    settings = site_settings.get_all()
    pct = float(settings["deposit_pct"])
    override = (lot or {}).get("deposit_pct_override")
    if override is not None:
        try:
            candidate = float(override)
        except (TypeError, ValueError):
            candidate = 0.0
        if 0 < candidate <= 1:
            pct = candidate
    min_cents = int(Decimal(str(settings["deposit_min_usd"])) * 100)
    return pct, min_cents


def _to_cents(quantity: int, price_per_chair: Any) -> int:
    """quantity × price → integer cents, via Decimal (floats round wrong)."""
    total = Decimal(int(quantity)) * Decimal(str(price_per_chair)) * 100
    return int(total.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def quote(
    *,
    quantity: int,
    price_per_chair: Any,
    kind: str = "deposit",
    pct: float,
    min_cents: int,
) -> DepositQuote:
    """Pure. No DB, no clock, no Stripe. The single source of the charge amount.

    deposit = min(subtotal, max(ceil(subtotal × pct), min_cents))
      - `ceil` so we never round a fraction of a cent in the buyer's favour and
        end up with an amount that doesn't reconcile against the rule.
      - `max(..., min_cents)` is the $200 floor: a 15% deposit on a $600 order
        isn't worth the Stripe fee or the operator's time.
      - the outer `min(subtotal, ...)` is the guard that matters — without it a
        $150 order would ask for a $200 deposit, i.e. more than the goods cost.
    """
    if kind not in DEPOSIT_KINDS:
        raise ValueError(f"invalid kind: {kind}")
    quantity = int(quantity)
    if quantity <= 0:
        raise ValueError("quantity must be positive")
    subtotal_cents = _to_cents(quantity, price_per_chair)
    if subtotal_cents <= 0:
        raise ValueError("subtotal must be positive")

    if kind == "full":
        amount_cents = subtotal_cents
    else:
        pct = float(pct)
        if not 0 < pct <= 1:
            raise ValueError("pct must be in (0, 1]")
        min_cents = max(0, int(min_cents))
        raw = math.ceil(Decimal(subtotal_cents) * Decimal(str(pct)))
        amount_cents = min(subtotal_cents, max(raw, min_cents))

    return DepositQuote(
        subtotal_cents=subtotal_cents,
        amount_cents=amount_cents,
        pct=float(pct),
        min_cents=int(min_cents),
        kind=kind,
    )


def quote_for_lot(lot: dict, *, quantity: int, kind: str = "deposit") -> DepositQuote:
    """Convenience: resolve the rule for a lot row, then quote it."""
    pct, min_cents = deposit_rules(lot)
    return quote(
        quantity=quantity,
        price_per_chair=lot.get("price_per_chair"),
        kind=kind,
        pct=pct,
        min_cents=min_cents,
    )


# ───────────────────────────────── CRUD ─────────────────────────────────────

def create_pending(
    *,
    lot_id: str,
    quantity: int,
    price_per_chair: Any,
    quote: DepositQuote,
    buyer_name: str | None = None,
    buyer_email: str | None = None,
    buyer_phone: str | None = None,
    currency: str = "usd",
) -> dict:
    """Row first, Stripe session second — so a session we never see still has a
    record to reconcile against."""
    if not lot_id or not str(lot_id).strip():
        raise ValueError("lot_id required")
    if not buyer_email and not buyer_phone:
        raise ValueError("email or phone required")
    return db.fetch_one(
        """
        INSERT INTO deposits (
            lot_id, kind, quantity, price_per_chair,
            subtotal_cents, amount_cents, deposit_pct, deposit_min_cents,
            currency, buyer_name, buyer_email, buyer_phone, status
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending')
        RETURNING *
        """,
        (
            str(lot_id).strip(),
            quote.kind,
            int(quantity),
            price_per_chair,
            quote.subtotal_cents,
            quote.amount_cents,
            quote.pct if quote.kind == "deposit" else None,
            quote.min_cents if quote.kind == "deposit" else None,
            currency,
            (buyer_name or "").strip() or None,
            (buyer_email or "").strip() or None,
            (buyer_phone or "").strip() or None,
        ),
    )


def attach_session(deposit_id: int, session_id: str) -> dict | None:
    """Stamp the Checkout session id — the key every webhook looks us up by."""
    return db.fetch_one(
        """
        UPDATE deposits SET stripe_session_id = %s, updated_at = now()
        WHERE id = %s RETURNING *
        """,
        (session_id, int(deposit_id)),
    )


def get_deposit(deposit_id: int) -> dict | None:
    return db.fetch_one("SELECT * FROM deposits WHERE id = %s", (int(deposit_id),))


def get_by_session(session_id: str | None) -> dict | None:
    if not session_id:
        return None
    return db.fetch_one(
        "SELECT * FROM deposits WHERE stripe_session_id = %s", (session_id,)
    )


def get_by_payment_intent(payment_intent: str | None) -> dict | None:
    if not payment_intent:
        return None
    return db.fetch_one(
        """
        SELECT * FROM deposits WHERE stripe_payment_intent = %s
        ORDER BY created_at DESC LIMIT 1
        """,
        (payment_intent,),
    )


def list_deposits(status: str | None = None) -> list[dict]:
    if status:
        return db.fetch_all(
            "SELECT * FROM deposits WHERE status = %s ORDER BY created_at DESC",
            (status,),
        )
    return db.fetch_all("SELECT * FROM deposits ORDER BY created_at DESC")


def set_admin_fields(
    deposit_id: int,
    *,
    status: str | None = None,
    admin_note: str | None = None,
) -> dict | None:
    """Operator edits from the admin tab. Deliberately NOT state-machine gated —
    this is the manual override for when reality and Stripe disagree."""
    sets, params = [], []
    if status is not None:
        if status not in DEPOSIT_STATUSES:
            raise ValueError(f"invalid status: {status}")
        sets.append("status = %s")
        params.append(status)
    if admin_note is not None:
        sets.append("admin_note = %s")
        params.append(admin_note.strip() or None)
    if not sets:
        return get_deposit(deposit_id)
    sets.append("updated_at = now()")
    params.append(int(deposit_id))
    return db.fetch_one(
        f"UPDATE deposits SET {', '.join(sets)} WHERE id = %s RETURNING *", params
    )


def delete_deposit(deposit_id: int) -> bool:
    return db.execute("DELETE FROM deposits WHERE id = %s", (int(deposit_id),)) > 0


# ─────────────────────────── the state machine ──────────────────────────────

def transition(
    deposit_id: int,
    new_status: str,
    *,
    payment_intent: str | None = None,
    payment_method: str | None = None,
    failure_reason: str | None = None,
) -> tuple[dict | None, bool]:
    """Move a deposit's status. Returns `(row, changed)`.

    An illegal move — including the same status arriving twice, which is what a
    Stripe retry looks like — is a NO-OP that returns `changed=False`. Callers
    gate operator alerts on `changed`, so replays stay silent.
    """
    if new_status not in DEPOSIT_STATUSES:
        raise ValueError(f"invalid status: {new_status}")
    row = get_deposit(deposit_id)
    if row is None:
        return None, False
    if new_status not in _TRANSITIONS.get(row["status"], frozenset()):
        return row, False

    # COALESCE: a later event that omits a field must not blank what we learned
    # from an earlier one. paid_at/refunded_at are stamped once, never moved.
    updated = db.fetch_one(
        """
        UPDATE deposits SET
            status                = %s,
            stripe_payment_intent = COALESCE(%s, stripe_payment_intent),
            payment_method        = COALESCE(%s, payment_method),
            failure_reason        = COALESCE(%s, failure_reason),
            paid_at     = CASE WHEN %s = 'paid'     AND paid_at     IS NULL
                               THEN now() ELSE paid_at     END,
            refunded_at = CASE WHEN %s = 'refunded' AND refunded_at IS NULL
                               THEN now() ELSE refunded_at END,
            updated_at  = now()
        WHERE id = %s
        RETURNING *
        """,
        (
            new_status, payment_intent, payment_method, failure_reason,
            new_status, new_status, int(deposit_id),
        ),
    )
    return updated, True


# ─────────────────────────── Stripe event mapping ───────────────────────────

def _id_of(value: Any) -> str | None:
    """Stripe fields are either an id string or an expanded object."""
    if isinstance(value, dict):
        return value.get("id")
    return value or None


def _payment_method_of(session: dict) -> str | None:
    """Best-effort 'which rail did they use'.

    Stripe doesn't put the chosen method on the session unless the PaymentIntent
    is expanded, so we infer: one allowed type is unambiguous; otherwise only
    the bank rail settles asynchronously and only the card rail settles instantly.
    """
    method = session.get("payment_method")
    if isinstance(method, dict):
        return method.get("type")
    types = session.get("payment_method_types") or []
    if len(types) == 1:
        return types[0]
    status = session.get("payment_status")
    if status == "paid" and "card" in types:
        return "card"
    if status and status != "paid" and "us_bank_account" in types:
        return "us_bank_account"
    return None


def _failure_reason_of(session: dict) -> str:
    intent = session.get("payment_intent")
    if isinstance(intent, dict):
        message = (intent.get("last_payment_error") or {}).get("message")
        if message:
            return str(message)
    return "bank payment failed"


def apply_stripe_event(event: dict) -> tuple[dict | None, bool]:
    """Map a Stripe webhook event onto a `transition()`. Returns `(row, changed)`.

    Unknown event types are `(None, False)` — the route still answers 2xx so
    Stripe stops retrying. Takes a plain dict, never a `stripe.Event`, so this
    is testable offline and importable while the feature is dark.
    """
    event_type = (event or {}).get("type")
    obj = ((event or {}).get("data") or {}).get("object") or {}

    if event_type == "charge.refunded":
        # Refunds are executed in the Stripe dashboard, so the only handle back
        # to our row is the PaymentIntent we stored at checkout.
        row = get_by_payment_intent(_id_of(obj.get("payment_intent")))
        if row is None:
            return None, False
        return transition(row["id"], "refunded")

    row = get_by_session(obj.get("id"))
    if row is None:
        return None, False

    if event_type == "checkout.session.completed":
        # Card => already 'paid'. ACH => 'unpaid' here and settles days later
        # via async_payment_succeeded/failed.
        status = "paid" if obj.get("payment_status") == "paid" else "processing"
        return transition(
            row["id"], status,
            payment_intent=_id_of(obj.get("payment_intent")),
            payment_method=_payment_method_of(obj),
        )

    if event_type == "checkout.session.async_payment_succeeded":
        return transition(
            row["id"], "paid",
            payment_intent=_id_of(obj.get("payment_intent")),
            payment_method=_payment_method_of(obj),
        )

    if event_type == "checkout.session.async_payment_failed":
        return transition(
            row["id"], "failed",
            payment_intent=_id_of(obj.get("payment_intent")),
            failure_reason=_failure_reason_of(obj),
        )

    if event_type == "checkout.session.expired":
        return transition(row["id"], "canceled")

    return None, False
