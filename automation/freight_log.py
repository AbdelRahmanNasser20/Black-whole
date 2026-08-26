"""Best-effort logging of storefront freight quotes into `freight_quotes`.

Every ZIP a buyer types into a lot page is a signal — which lanes people want,
how big the orders are, which quotes went cold. So we log all of them, not just
the ones that turn into a lead.

**Best-effort is the whole design.** The buyer asked for a number, not for us
to write a row. If Supabase is asleep, the pooler is saturated, or migration
`006_freight_quotes_storefront.sql` hasn't been applied yet, the estimate must
still render. So every function here catches *everything* and returns
``None`` / ``False``; a caller can't tell a DB outage from a successful write
except by the returned id, and doesn't need to. (Same shape as the CRM's
`db/repositories/freight.py`, whose drafter wraps `insert_quote` best-effort
for the same reason.)

The table is shared with the CRM — it created it in `009_freight_quotes.sql`
for Messenger threads. Storefront rows are the ones with ``source='storefront'``
and a NULL ``thread_url``; see migration 006 for why that's one table and not
two.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from . import db

log = logging.getLogger(__name__)


def insert_storefront_quote(
    *,
    lot_id: str | None,
    origin_zip: str,
    dest_zip: str,
    quantity: int,
    quote: dict,
    buyer_email: str | None = None,
    client_ip: str | None = None,
) -> int | None:
    """Append one storefront quote row. Returns its id, or None if logging failed.

    ``quote`` is a `freight_estimate.get_freight_estimate` dict. Its ``raw`` key
    (calibration internals: weight, linear feet, NMFC class, carrier response)
    is stored — that's the audit trail for "why did we quote that?" — but it is
    never returned to the browser. ``client_ip`` rides inside ``raw_response``
    rather than getting its own column: it's for abuse forensics, not analytics,
    and doesn't deserve a schema change on a shared table.
    """
    try:
        raw = dict(quote.get("raw") or {})
        if client_ip:
            raw["client_ip"] = client_ip
        row = db.fetch_one(
            """
            INSERT INTO freight_quotes (
                source, thread_url, contact_id, lot_id, origin_zip, dest_zip,
                quantity, mode, ltl_low, ltl_high, partial_low, partial_high,
                miles, transit_days, provider, accessorials, raw_response,
                buyer_email, valid_until
            ) VALUES (
                'storefront', NULL, NULL, %s, %s, %s,
                %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s::jsonb, %s::jsonb,
                %s, %s
            )
            RETURNING id
            """,
            (
                (lot_id or None),
                str(origin_zip),
                str(dest_zip),
                int(quantity),
                # `mode` is NOT NULL with a CHECK of ('ltl','partial','both');
                # fall back rather than let a provider quirk kill the insert.
                quote.get("mode") or quote.get("recommended_mode") or "ltl",
                quote.get("ltl_low"),
                quote.get("ltl_high"),
                quote.get("partial_low"),
                quote.get("partial_high"),
                quote.get("miles"),
                quote.get("transit_days"),
                quote.get("provider")
                or (quote.get("raw") or {}).get("provider")
                or "estimator",
                _json_or_none(quote.get("accessorials")),
                _json_or_none(raw or None),
                (buyer_email or "").strip() or None,
                quote.get("valid_until"),
            ),
        )
        return int(row["id"]) if row else None
    except Exception:  # noqa: BLE001 — logging must never break an estimate
        log.warning("freight quote logging failed", exc_info=True)
        return None


def set_quote_email(quote_id: int, email: str) -> bool:
    """Attach an email to a previously logged quote (the optional second step).

    Scoped to ``source='storefront'`` so a guessed id can never touch a CRM row
    — the id is handed to the browser, which makes it attacker-controlled.
    """
    try:
        clean = (email or "").strip()
        if not clean:
            return False
        return db.execute(
            """
            UPDATE freight_quotes SET buyer_email = %s
            WHERE id = %s AND source = 'storefront'
            """,
            (clean, int(quote_id)),
        ) > 0
    except Exception:  # noqa: BLE001 — same contract as the insert
        log.warning("freight quote email attach failed", exc_info=True)
        return False


def _json_or_none(value: Any) -> str | None:
    return None if value is None else json.dumps(value, default=str)
