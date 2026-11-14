"""Platform subscription state: Stripe subscription events -> platform_users.plan.

Dark-shipped: nothing here runs unless the Stripe webhook is live. Takes plain
dicts (like deposits.apply_stripe_event) so it is testable with no SDK.
Events it does not know are no-ops, and deposits ignores these in turn.
"""
from __future__ import annotations

import os
import time

from automation import db

UPDATE_SQL = (
    "WITH old AS (SELECT user_id, plan FROM platform_users "
    "WHERE stripe_customer_id = %s FOR UPDATE) "
    "UPDATE platform_users u SET plan = %s, billing_event_at = to_timestamp(%s) "
    "FROM old WHERE u.user_id = old.user_id "
    "AND (u.billing_event_at IS NULL OR u.billing_event_at <= to_timestamp(%s)) "
    "RETURNING old.plan AS old_plan"
)
_PRO_STATUSES = {"active", "trialing", "past_due"}


def enabled() -> bool:
    return os.getenv("PLATFORM_BILLING_ENABLED", "").strip().lower() in ("1", "true", "yes", "on")


def apply_stripe_event(event: dict) -> dict:
    """Returns {"handled": bool, "changed": bool, "plan": str|None}."""
    etype = (event or {}).get("type") or ""
    obj = ((event or {}).get("data") or {}).get("object") or {}
    noop = {"handled": False, "changed": False, "plan": None}
    if etype not in ("customer.subscription.created", "customer.subscription.updated",
                     "customer.subscription.deleted"):
        return noop
    customer = obj.get("customer")
    if isinstance(customer, dict):
        customer = customer.get("id")
    if not customer:
        return noop
    if etype == "customer.subscription.deleted":
        plan = "beta"
    else:
        # past_due keeps pro on purpose: Stripe retries the card, grace period.
        plan = "pro" if obj.get("status") in _PRO_STATUSES else "beta"
    created = int((event or {}).get("created") or time.time())
    # Intentionally NOT wrapped in try/except: a DB failure must 500 so Stripe
    # retries the event. Ordering guard: an event older than the last applied
    # one matches no row (Stripe delivery order is not guaranteed). The
    # timestamp advances even when the plan is unchanged, so a stale event
    # can't slip past later.
    row = db.fetch_one(UPDATE_SQL, (customer, plan, created, created))
    if row is None:
        return {"handled": True, "changed": False, "plan": plan}
    return {"handled": True, "changed": row["old_plan"] != plan, "plan": plan}
