"""Read model for the PUBLIC /deals/sold page ("Sold prices").

One source: `lot_archive` — the recorder's index of closed lots whose outcome
came from the site's own close record (bidbox → detail → search precedence,
`recorder/lot_archive.py`). Three rules:

1. **A sale is a confirmed sale.** Only `outcome = 'sold'` rows are priced.
   A lot that closed with a top bid under its reserve is counted, and shown,
   as NOT sold — its top bid is never a sold price.
2. **USD only.** `final_price` is never aggregated across currencies.
3. **Chair-buyer isolation**, the same policy as /deals: `public_deals` owns
   the category + title lists; the three operator tables are joined on the
   `asset/account/auction` parts of `lot_key`. `PUBLIC_COLS` is the allow-list.
"""
from __future__ import annotations

import re
import threading
import time
from datetime import date, datetime
from typing import Any

from .. import db
from . import public_deals

PUBLIC_COLS = ("source", "lot_key", "title", "canonical_category", "city", "state",
               "closed_at", "final_price", "bid_count")
SOLD = "outcome = 'sold' AND final_price IS NOT NULL"
DAY_CHOICES = (30, 90)
PER_PAGE = 50
MAX_PAGE = 200
CURRENCY = "USD"
_KEY3 = re.compile(r"^\d+/\d+/\d+$")
_A, _B, _C = (f"split_part(lot_archive.lot_key, '/', {i})" for i in (1, 2, 3))


def base_where() -> tuple[str, list]:
    """Every read starts here: the public exclusion policy + one currency."""
    where = (
        "(canonical_category IS NULL OR canonical_category <> ALL(%s)) "
        "AND COALESCE(title, '') !~* %s "
        "AND currency = %s "
        "AND NOT EXISTS (SELECT 1 FROM tracked_lots t "
        f"  WHERE t.asset_id::text = {_A} AND t.account_id::text = {_B}) "
        "AND NOT EXISTS (SELECT 1 FROM auction_favorites f "
        f"  WHERE f.asset_id = {_A} || '/' || {_B}) "
        "AND NOT EXISTS (SELECT 1 FROM deal_list_items li "
        f"  WHERE li.asset_id::text = {_A} AND li.account_id::text = {_B} "
        f"    AND li.auction_id::text = {_C})"
    )
    return where, [sorted(public_deals.EXCLUDED_CATEGORIES),
                   public_deals.EXCLUDED_TITLE_SQL_RE, CURRENCY]


def build_where(*, q: str | None = None, category: str | None = None,
                state: str | None = None, days: int | None = None) -> tuple[str, list]:
    where, args = base_where()
    if q and q.strip():
        where += " AND title ILIKE %s"
        args.append(f"%{q.strip()}%")
    if category:
        where += " AND canonical_category = %s"
        args.append(category)
    if state:
        where += " AND state = %s"
        args.append(state.strip().upper())
    if days in DAY_CHOICES:
        where += " AND closed_at >= now() - make_interval(days => %s)"
        args.append(days)
    return where, args


def viewer_url(source: str | None, lot_key: str | None) -> str | None:
    """Our own lot page exists for GovDeals `asset/account/auction` keys only."""
    if source == public_deals.GOVDEALS and lot_key and _KEY3.match(lot_key):
        return f"/deals/{lot_key}"
    return None


def share(sold: int, not_sold: int) -> float | None:
    """Percent of closes with a top bid that became a sale."""
    total = sold + not_sold
    return round(100.0 * sold / total, 1) if total else None


def _num(v: Any) -> float | None:
    return float(v) if v is not None else None


def _iso(v: Any) -> str | None:
    return v.isoformat() if isinstance(v, (datetime, date)) else v


_AGG = (f"count(*) FILTER (WHERE {SOLD}) AS sold, "
        "count(*) FILTER (WHERE outcome = 'reserve_not_met') AS not_sold, "
        f"percentile_cont(0.25) WITHIN GROUP (ORDER BY final_price) FILTER (WHERE {SOLD}) AS p25, "
        f"percentile_cont(0.5) WITHIN GROUP (ORDER BY final_price) FILTER (WHERE {SOLD}) AS median, "
        f"percentile_cont(0.75) WITHIN GROUP (ORDER BY final_price) FILTER (WHERE {SOLD}) AS p75")


# Our lot page reads `deal_lots`; link a sold row only when that row exists.
# The CASE keeps the integer casts away from a non-numeric key.
_HAS_PAGE = ("CASE WHEN p.lot_key ~ '^[0-9]+/[0-9]+/[0-9]+$' THEN EXISTS ("
             "SELECT 1 FROM deal_lots d "
             "WHERE d.asset_id = split_part(p.lot_key, '/', 1)::bigint "
             "AND d.account_id = split_part(p.lot_key, '/', 2)::bigint "
             "AND d.auction_id = split_part(p.lot_key, '/', 3)::bigint) ELSE false END")


def _stats(row: dict) -> dict:
    sold, not_sold = int(row["sold"] or 0), int(row["not_sold"] or 0)
    return {"sold": sold, "not_sold": not_sold, "sold_share": share(sold, not_sold),
            "median": _num(row["median"]), "p25": _num(row["p25"]), "p75": _num(row["p75"])}


def fetch_sold(*, q: str | None = None, category: str | None = None,
               state: str | None = None, days: int | None = None, page: int = 1) -> dict:
    """Summary + per-category table + one page of sold lots, on one connection."""
    page = max(1, min(int(page or 1), MAX_PAGE))
    where, args = build_where(q=q, category=category, state=state, days=days)
    with db.connect() as conn:
        top = conn.execute(
            f"SELECT {_AGG}, count(*) FILTER (WHERE outcome = 'no_bid') AS no_bid, "
            "min(closed_at) AS since, max(closed_at) AS until "
            f"FROM lot_archive WHERE {where}", tuple(args)).fetchone()
        cats = conn.execute(
            f"SELECT canonical_category AS value, {_AGG} FROM lot_archive "
            f"WHERE {where} AND canonical_category IS NOT NULL "
            f"GROUP BY 1 HAVING count(*) FILTER (WHERE {SOLD}) > 0 ORDER BY 2 DESC",
            tuple(args)).fetchall()
        rows = conn.execute(
            f"SELECT p.*, {_HAS_PAGE} AS has_page FROM ("
            f"SELECT {', '.join(PUBLIC_COLS)} FROM lot_archive WHERE {where} AND {SOLD} "
            "ORDER BY closed_at DESC NULLS LAST, lot_key LIMIT %s OFFSET %s) p "
            "ORDER BY closed_at DESC NULLS LAST, lot_key",
            tuple(args) + (PER_PAGE, (page - 1) * PER_PAGE)).fetchall()
    summary = _stats(top)
    summary["reserve_not_met"] = summary.pop("not_sold")
    summary["no_bid"] = int(top["no_bid"] or 0)
    summary["since"] = _iso(top["since"].date() if top["since"] else None)
    summary["until"] = _iso(top["until"].date() if top["until"] else None)
    out_rows = []
    for r in rows:
        r = dict(r)
        r["final_price"] = _num(r["final_price"])
        r["closed_at"] = _iso(r["closed_at"])
        r["viewer_url"] = viewer_url(r["source"], r["lot_key"]) if r.pop("has_page") else None
        out_rows.append(r)
    total = summary["sold"]
    return {"summary": summary,
            "categories": [dict(_stats(c), value=c["value"]) for c in cats],
            "rows": out_rows, "total": total, "page": page, "per_page": PER_PAGE,
            "pages": max(1, min(MAX_PAGE, -(-total // PER_PAGE)))}


# ── headline numbers for the /platform landing page ──────────────────────────
# Memo only on the request path: never blocks, never raises. A stale memo starts
# one background read; a failed read keeps the last good numbers.
HEAD_TTL = 6 * 3600
HEAD_RETRY = 300
_head: dict[str, Any] = {"at": 0.0, "data": None, "busy": False}
_head_lock = threading.Lock()


def _refresh_headline() -> None:
    try:
        where, args = base_where()
        data = _stats(db.fetch_one(f"SELECT {_AGG} FROM lot_archive WHERE {where}", tuple(args)))
    except Exception:  # noqa: BLE001 - the landing page renders without the numbers
        data = None
    with _head_lock:
        _head["busy"] = False
        if data is not None:
            _head["data"], _head["at"] = data, time.monotonic()
        else:
            _head["at"] = time.monotonic() - HEAD_TTL + HEAD_RETRY


def headline() -> dict | None:
    """All-time `_stats` (sold, not_sold, sold_share, median, p25, p75), or None before the first read."""
    with _head_lock:
        fresh = _head["at"] and time.monotonic() - _head["at"] < HEAD_TTL
        if fresh or _head["busy"]:
            return _head["data"]
        _head["busy"] = True
    threading.Thread(target=_refresh_headline, name="sold-headline", daemon=True).start()
    return _head["data"]
