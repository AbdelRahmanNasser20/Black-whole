"""The `freight_quotes` ledger: every storefront freight request, and its follow-up.

A buyer who types a ZIP, an email and a phone into a lot page is a lead whether
or not we could price the lane. So every request is written — quoted or
unquotable — and the admin Sales tab reads them back with a follow-up status.
(Until 2026-10 only successful, anonymous estimates were logged and nothing read
the table: a 160-chair request sat unanswered for eight days.)

**The writers never raise.** `insert_storefront_quote` returns the new id, or
``None`` when the row could not be written; the endpoint turns ``None`` into
"not saved" and still alerts the operator with the contact details, so a DB
outage cannot swallow a lead. The read side raises normally — an admin screen
should show a real error.

**Two schemas.** Migration `021_sales_quotes.sql` adds the phone, status,
unquotable and carrier columns. Until it is applied `schema_ready()` is False
and the insert falls back to the pre-021 columns (the phone rides in
``raw_response``, exactly where ``client_ip`` lives); an unquotable request
cannot be stored at all on the old schema (``mode`` is NOT NULL there).

The table is shared with the CRM — it created it in `009_freight_quotes.sql`
for Messenger threads. Storefront rows are the ones with ``source='storefront'``
and a NULL ``thread_url``; see migration 006 for why that's one table and not
two.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from . import db

log = logging.getLogger(__name__)

QUOTE_STATUSES = ("new", "answered", "won", "lost", "junk")
# What the inbox shows by default: everything still waiting on the operator.
OPEN_STATUSES = ("new", "answered")
NOTE_MAX_LEN = 500
CARRIER_OPTIONS_MAX = 5

MIGRATION_HINT = (
    "migration 021 is not applied — run "
    ".venv/bin/python scripts/apply_sql.py scripts/sql/021_sales_quotes.sql"
)


class SchemaNotReady(RuntimeError):
    """A follow-up write needs the 021 columns and they are not there yet."""


# ───────────────────────────── schema detection ─────────────────────────────

_schema_ready = False


def schema_ready() -> bool:
    """True once `021_sales_quotes.sql` is applied.

    Keyed on ``carrier_checked_at`` — the LAST `freight_quotes` column that
    file adds. `apply_sql.py` runs it statement by statement in autocommit, so
    a run that dies half-way (a lock timeout) leaves ``buyer_phone`` in place
    without ``unquotable_reason``; keying on the first column would then send
    every insert down a path that names a column that isn't there.

    Only a positive answer is cached: a schema never loses a column under a
    running process, but it can gain one (the operator applies the migration
    without a redeploy), and a DB blip must not pin us to the old path forever.
    """
    global _schema_ready
    if _schema_ready:
        return True
    try:
        row = db.fetch_one(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'freight_quotes' "
            "AND column_name = 'carrier_checked_at' LIMIT 1"
        )
    except Exception:  # noqa: BLE001 — no DB ⇒ behave like the old schema
        return False
    _schema_ready = row is not None
    return _schema_ready


def reset_schema_cache() -> None:
    """Tests only."""
    global _schema_ready
    _schema_ready = False


# ─────────────────────────────────── write ──────────────────────────────────

def insert_storefront_quote(
    *,
    lot_id: str | None,
    origin_zip: str | None,
    dest_zip: str,
    quantity: int,
    quote: dict | None = None,
    buyer_email: str | None = None,
    buyer_phone: str | None = None,
    client_ip: str | None = None,
    unquotable_reason: str | None = None,
    lot_quantity_remaining: int | None = None,
) -> int | None:
    """Append one storefront request. Returns its id, or None if it was not saved.

    ``quote`` is a `freight_estimate.get_freight_estimate` dict, or None for a
    lane we could not price (then ``unquotable_reason`` says why). Its ``raw``
    key (calibration internals: weight, linear feet, NMFC class) is stored —
    that's the audit trail for "why did we quote that?" — but it is never
    returned to a browser. ``client_ip`` rides inside ``raw_response``: it's for
    abuse forensics, not analytics.
    """
    try:
        quote = quote or {}
        raw = dict(quote.get("raw") or {})
        if client_ip:
            raw["client_ip"] = client_ip
        email = (buyer_email or "").strip() or None
        phone = (buyer_phone or "").strip() or None
        priced = bool(quote)

        if schema_ready():
            row = db.fetch_one(
                """
                INSERT INTO freight_quotes (
                    source, thread_url, contact_id, lot_id, origin_zip, dest_zip,
                    quantity, mode, ltl_low, ltl_high, partial_low, partial_high,
                    miles, transit_days, provider, accessorials, raw_response,
                    buyer_email, buyer_phone, valid_until,
                    unquotable_reason, lot_quantity_remaining
                ) VALUES (
                    'storefront', NULL, NULL, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s::jsonb, %s::jsonb,
                    %s, %s, %s,
                    %s, %s
                )
                RETURNING id
                """,
                (
                    (lot_id or None),
                    (str(origin_zip) if origin_zip else None),
                    str(dest_zip),
                    int(quantity),
                    _mode(quote) if priced else None,
                    quote.get("ltl_low"),
                    quote.get("ltl_high"),
                    quote.get("partial_low"),
                    quote.get("partial_high"),
                    quote.get("miles"),
                    quote.get("transit_days"),
                    _provider(quote),
                    _json_or_none(quote.get("accessorials")),
                    _json_or_none(raw or None),
                    email,
                    phone,
                    quote.get("valid_until"),
                    None if priced else (unquotable_reason or "unquotable"),
                    lot_quantity_remaining,
                ),
            )
            return int(row["id"]) if row else None

        # ── pre-021 schema ──
        if not priced:
            # `mode` and `origin_zip` are NOT NULL there; a fake mode on a row
            # with no price would read as a real quote. The alert carries it.
            log.warning(
                "unquotable freight request not stored (%s): lot=%s dest=%s qty=%s",
                MIGRATION_HINT, lot_id, dest_zip, quantity,
            )
            return None
        if phone:
            raw["buyer_phone"] = phone
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
                _mode(quote),
                quote.get("ltl_low"),
                quote.get("ltl_high"),
                quote.get("partial_low"),
                quote.get("partial_high"),
                quote.get("miles"),
                quote.get("transit_days"),
                _provider(quote),
                _json_or_none(quote.get("accessorials")),
                _json_or_none(raw or None),
                email,
                quote.get("valid_until"),
            ),
        )
        return int(row["id"]) if row else None
    except Exception:  # noqa: BLE001 — the caller turns None into "not saved"
        log.warning("freight quote not saved", exc_info=True)
        return None


def set_quote_email(quote_id: int, email: str) -> bool:
    """Attach an email to a logged quote — the pre-2026-10 second step.

    Kept only for lot pages still open in a browser from before the widget
    asked for contact details up front. Scoped to ``source='storefront'`` so a
    guessed id can never touch a CRM row — the id is handed to the browser,
    which makes it attacker-controlled.
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


def set_quote_status(
    quote_id: int, *, status: str | None = None, note: str | None = None
) -> dict | None:
    """Operator follow-up: move a request along and/or leave a note.

    Returns the updated row, or None when the id does not exist. Raises
    ValueError on a status outside `QUOTE_STATUSES` and SchemaNotReady before
    migration 021.
    """
    if status is not None and status not in QUOTE_STATUSES:
        raise ValueError(f"invalid status: {status}")
    if not schema_ready():
        raise SchemaNotReady(MIGRATION_HINT)
    sets, params = [], []
    if status is not None:
        sets += ["status = %s", "status_changed_at = now()"]
        params.append(status)
    if note is not None:
        sets.append("note = %s")
        params.append((str(note).strip()[:NOTE_MAX_LEN]) or None)
    if sets:
        params.append(int(quote_id))
        db.execute(
            f"UPDATE freight_quotes SET {', '.join(sets)} WHERE id = %s", params
        )
    return get_quote(quote_id)


def set_carrier_result(quote_id: int, summary: dict) -> bool:
    """Store a `warp_rates.summarize` dict on the row. Best-effort.

    Only the summary: a status, the cheapest price and carrier, the carrier
    count, and at most `CARRIER_OPTIONS_MAX` options.
    """
    try:
        if not schema_ready():
            return False
        options = list(summary.get("carrier_options") or [])[:CARRIER_OPTIONS_MAX]
        return db.execute(
            """
            UPDATE freight_quotes SET
                carrier_status = %s, carrier_low = %s, carrier_name = %s,
                carrier_count = %s, carrier_options = %s::jsonb,
                carrier_checked_at = now()
            WHERE id = %s
            """,
            (
                summary.get("carrier_status"),
                summary.get("carrier_low"),
                summary.get("carrier_name"),
                summary.get("carrier_count"),
                _json_or_none(options or None),
                int(quote_id),
            ),
        ) > 0
    except Exception:  # noqa: BLE001 — a price check must never break anything
        log.warning("carrier result not saved for quote %s", quote_id, exc_info=True)
        return False


# ─────────────────────────────────── read ───────────────────────────────────

# `raw_response` is deliberately not selected: it holds calibration internals
# and the caller's IP. The phone falls back to where the pre-021 insert put it.
_COLS_COMMON = """
    q.id, q.source, q.quoted_at, q.lot_id, q.origin_zip, q.dest_zip, q.quantity,
    q.mode, q.ltl_low, q.ltl_high, q.partial_low, q.partial_high, q.miles,
    q.transit_days, q.provider, q.buyer_email, q.thread_url, q.valid_until,
    i.title AS lot_title, i.quantity_remaining AS lot_quantity_now
"""
_COLS_021 = _COLS_COMMON + """,
    COALESCE(q.buyer_phone, q.raw_response->>'buyer_phone') AS buyer_phone,
    q.status, q.status_changed_at, q.note, q.unquotable_reason,
    q.lot_quantity_remaining, q.carrier_status, q.carrier_low, q.carrier_name,
    q.carrier_count, q.carrier_options, q.carrier_checked_at
"""
_COLS_LEGACY = _COLS_COMMON + """,
    q.raw_response->>'buyer_phone' AS buyer_phone
"""
_LEGACY_DEFAULTS = {
    "status": "new", "status_changed_at": None, "note": None,
    "unquotable_reason": None, "lot_quantity_remaining": None,
    "carrier_status": None, "carrier_low": None, "carrier_name": None,
    "carrier_count": None, "carrier_options": None, "carrier_checked_at": None,
}
_FROM = "FROM freight_quotes q LEFT JOIN inventory i ON i.lot_id = q.lot_id"


def list_quotes(status: str | None = None, limit: int = 200) -> list[dict]:
    """Requests, newest first. ``status`` is one of `QUOTE_STATUSES`, ``'open'``
    (new + answered), or None/'' for everything."""
    ready = schema_ready()
    where, params = "", []
    if ready and status:
        wanted = OPEN_STATUSES if status == "open" else (status,)
        if any(s not in QUOTE_STATUSES for s in wanted):
            raise ValueError(f"invalid status: {status}")
        where = "WHERE q.status = ANY(%s)"
        params.append(list(wanted))
    elif not ready and status not in (None, "", "open", "new"):
        return []  # nothing has been triaged on the old schema
    params.append(max(1, min(int(limit), 500)))
    rows = db.fetch_all(
        f"SELECT {_COLS_021 if ready else _COLS_LEGACY} {_FROM} {where} "
        "ORDER BY q.quoted_at DESC LIMIT %s",
        params,
    )
    return [_public(r, ready) for r in rows]


def get_quote(quote_id: int) -> dict | None:
    ready = schema_ready()
    row = db.fetch_one(
        f"SELECT {_COLS_021 if ready else _COLS_LEGACY} {_FROM} WHERE q.id = %s",
        (int(quote_id),),
    )
    return _public(row, ready) if row else None


def count_new() -> int:
    """How many storefront requests nobody has touched yet (the rail badge).

    Storefront only: a CRM row is the Messenger bot's own estimate inside a
    thread the operator is already working, not something waiting in this
    inbox. Zero before migration 021 — nothing can be marked answered there, so
    a count would be a badge that never clears.
    """
    if not schema_ready():
        return 0
    row = db.fetch_one(
        "SELECT count(*) AS n FROM freight_quotes "
        "WHERE status = 'new' AND source = 'storefront'"
    )
    return int((row or {}).get("n") or 0)


# ────────────────────────────────── helpers ─────────────────────────────────

def _mode(quote: dict) -> str:
    # `mode` carries a CHECK of ('ltl','partial','both'); fall back rather than
    # let a provider quirk kill the insert.
    return quote.get("mode") or quote.get("recommended_mode") or "ltl"


def _provider(quote: dict) -> str:
    return (
        quote.get("provider")
        or (quote.get("raw") or {}).get("provider")
        or "estimator"
    )


def _public(row: dict, ready: bool) -> dict:
    out = dict(row)
    if not ready:
        out.update(_LEGACY_DEFAULTS)
    for key, value in out.items():
        if isinstance(value, Decimal):
            out[key] = float(value)
        elif isinstance(value, (datetime, date)):
            out[key] = value.isoformat()
    return out


def _json_or_none(value: Any) -> str | None:
    return None if value is None else json.dumps(value, default=str)
