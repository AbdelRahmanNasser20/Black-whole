"""Platform subscription state: Stripe subscription events -> platform_users.plan.

Dark-shipped: nothing here runs unless the Stripe webhook is live. Takes plain
dicts (like deposits.apply_stripe_event) so it is testable with no SDK.
Events it does not know are no-ops, and deposits ignores these in turn.
"""
from __future__ import annotations

import os

from automation import db

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
        plan = "pro" if obj.get("status") in _PRO_STATUSES else "beta"
    # Only write when the plan actually changes: replays report changed=False.
    n = db.execute(
        "UPDATE platform_users SET plan = %s WHERE stripe_customer_id = %s "
        "AND plan IS DISTINCT FROM %s",
        (plan, customer, plan),
    )
    return {"handled": True, "changed": bool(n), "plan": plan}
