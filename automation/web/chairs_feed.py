"""Operator-only Chairs feed: every LIVE seating lot with quantity >= N, across
every live source, ranked by price per chair. This is what the aggregator is for.

NOT public. It is the exact inverse of the public `/deals` seating exclusion:

* **GovDeals** half: `deal_lots WHERE public_deals.seating_where()` — the same
  category list + `\\y` title regex that `exclusion_where()` negates.
* **Every other source**: `deals_sources.lots(seating=True)` — built from the
  same snapshot read as /deals, kept where `public_deals.is_excluded` is True.

So a live lot is in exactly one of `/deals` or this feed (operator picks are
the one carve-out: hidden from /deals always, shown here when they are seating,
flagged `operator_pick`).

Quantity: `deals.quantity.lot_quantity` on title (+ the first 600 chars of the
description where we have one), then `chair_quantity`'s noun count ("150
Student Chairs", plural only — "8678 Phlebotomy Chair" is a model number)
when the named patterns find nothing. Unknown stays `None` — never a default 1,
which would pass a lot total off as a per-chair price.

Callers: `GET /api/deals/chairs` (admin, auth-walled) and
`python -m deals.cli chairs`. Both are read-only.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from .. import db
from . import deals_sources, public_deals
from deals.fees import FeeModel, fee_model_from_env, landed_cost
from deals.quantity import _NOUN_COUNT, DESCRIPTION_WINDOW, lot_quantity, unit_price

log = logging.getLogger(__name__)

DEFAULT_MIN_QTY = 50
MAX_MIN_QTY = 100_000
GD_ROW_CAP = 3000            # live seating lots in deal_lots is a few hundred
SORTS = ("unit_bid", "ends", "quantity", "bid")
GOVDEALS = "govdeals"

ROW_KEYS = (
    "id", "source", "source_name", "title", "quantity", "quantity_source",
    "current_bid", "unit_bid", "bid_count", "end_utc", "city", "state", "url",
    "landed_cost", "unit_landed", "viewer_url", "operator_pick",
)

_GD_PICK = (
    "(EXISTS (SELECT 1 FROM tracked_lots t "
    "  WHERE t.asset_id = deal_lots.asset_id AND t.account_id = deal_lots.account_id) "
    " OR EXISTS (SELECT 1 FROM auction_favorites f "
    "  WHERE f.asset_id = deal_lots.asset_id::text || '/' || deal_lots.account_id::text) "
    " OR EXISTS (SELECT 1 FROM deal_list_items li "
    "  WHERE li.asset_id = deal_lots.asset_id AND li.account_id = deal_lots.account_id "
    "    AND li.auction_id = deal_lots.auction_id))"
)


# ── quantity ─────────────────────────────────────────────────────────────────
def best_quantity(title: str | None, description: str | None = None) -> tuple[int | None, str]:
    """(quantity, source) for a seating lot. Named patterns first
    (`lot_quantity`: title, then description head), then the chair noun count
    (`chair_quantity`) on title, then description. (None, 'unknown') when
    nothing names a count."""
    qty, src = lot_quantity(title, description)
    if src != "default":
        return qty, src
    for text, source in ((title, "title_noun"), ((description or "")[:DESCRIPTION_WINDOW], "description_noun")):
        n = _plural_chair_count(text)
        if n:
            return n, source
    return None, "unknown"


def _plural_chair_count(text: str | None) -> int | None:
    """`chair_quantity`'s noun count, PLURAL only: "150 Student Chairs" is a
    count, "UMF Medical 8678 Power Phlebotomy Chair" is a model number (seen on
    the 2026-10-09 live smoke)."""
    m = _NOUN_COUNT.search(text or "")
    if not m or not m.group(0).lower().endswith("chairs"):
        return None
    n = int(m.group(1).replace(",", ""))
    return n if n > 1 else None


# ── readers (monkeypatched in tests) ─────────────────────────────────────────
def _govdeals_rows(*, state: str | None = None, ending_within: float | None = None) -> list[dict]:
    """Live seating lots from deal_lots. Raises on DB failure."""
    seat_sql, seat_args = public_deals.seating_where()
    where = [
        "outcome_complete IS NOT TRUE AND end_utc > now()",
        seat_sql,
    ]
    args: list = list(seat_args)
    if state:
        where.append("state = %s")
        args.append(state)
    if ending_within is not None:
        where.append("end_utc <= now() + make_interval(hours => %s)")
        args.append(float(ending_within))
    return db.fetch_all(
        "SELECT asset_id, account_id, auction_id, title, "
        f"left(description, {DESCRIPTION_WINDOW}) AS description, canonical_category, "
        f"city, state, bid_count, current_bid, end_utc, {_GD_PICK} AS operator_pick "
        f"FROM deal_lots WHERE {' AND '.join(where)} ORDER BY end_utc ASC LIMIT %s",
        (*args, GD_ROW_CAP),
    )


def _snapshot_rows(now: datetime) -> list[dict]:
    """Live seating lots from every non-GovDeals recorder source. Raises on failure."""
    return deals_sources.lots("active", now=now, seating=True)


# ── shaping ──────────────────────────────────────────────────────────────────
def _num(v) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _aware(dt):
    if isinstance(dt, datetime) and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def shape_govdeals(r: dict, fees: FeeModel) -> dict:
    qty, src = best_quantity(r.get("title"), r.get("description"))
    bid = _num(r.get("current_bid"))
    lc = landed_cost(bid or 0.0, qty=qty or 1, fees=fees)
    a, b, c = r.get("asset_id"), r.get("account_id"), r.get("auction_id")
    return {
        "id": f"{GOVDEALS}:{a}/{b}/{c}", "source": GOVDEALS, "source_name": "GovDeals",
        "title": r.get("title"), "quantity": qty, "quantity_source": src,
        "current_bid": bid, "unit_bid": unit_price(bid, qty) if qty else None,
        "bid_count": r.get("bid_count"), "end_utc": _aware(r.get("end_utc")),
        "city": r.get("city"), "state": r.get("state"),
        "url": f"https://www.govdeals.com/en/asset/{a}/{b}",
        "landed_cost": round(lc.total, 2), "unit_landed": round(lc.per_unit, 2) if qty else None,
        "viewer_url": f"/deals/{a}/{b}/{c}", "operator_pick": bool(r.get("operator_pick")),
    }


def shape_snapshot(r: dict) -> dict:
    qty, src = best_quantity(r.get("title"))
    bid = _num(r.get("current_bid"))
    return {
        "id": r.get("id"), "source": r.get("source"), "source_name": r.get("source_name"),
        "title": r.get("title"), "quantity": qty, "quantity_source": src,
        "current_bid": bid, "unit_bid": unit_price(bid, qty) if qty else None,
        "bid_count": r.get("bid_count"), "end_utc": _aware(r.get("end_utc")),
        "city": r.get("city"), "state": r.get("state"), "url": r.get("url"),
        # No landed cost off GovDeals: the fee model is GovDeals' buyer premium.
        "landed_cost": None, "unit_landed": None, "viewer_url": None,
        "operator_pick": bool(r.get("operator_pick")),
    }


def _end_key(r: dict) -> float:
    e = r.get("end_utc")
    return e.timestamp() if isinstance(e, datetime) else float("inf")


def rank(rows: list[dict], sort: str = "unit_bid") -> list[dict]:
    """unit_bid ascending NULLS LAST, ties by soonest end (default); `ends`
    soonest first; `quantity` biggest first; `bid` lowest current bid first."""
    def nulls_last(field, reverse=False):
        def key(r):
            v = r.get(field)
            if v is None:
                return (1, 0.0, _end_key(r))
            return (0, -float(v) if reverse else float(v), _end_key(r))
        return key
    if sort == "ends":
        return sorted(rows, key=lambda r: (_end_key(r), r.get("unit_bid") is None))
    if sort == "quantity":
        return sorted(rows, key=nulls_last("quantity", reverse=True))
    if sort == "bid":
        return sorted(rows, key=nulls_last("current_bid"))
    return sorted(rows, key=nulls_last("unit_bid"))


def clamp_min_qty(v) -> int:
    try:
        n = int(v)
    except (TypeError, ValueError):
        return DEFAULT_MIN_QTY
    return max(0, min(n, MAX_MIN_QTY))


# ── the read ─────────────────────────────────────────────────────────────────
def fetch(*, min_qty: int = DEFAULT_MIN_QTY, site: str | None = None, state: str | None = None,
          ending_within: float | None = None, sort: str = "unit_bid",
          now: datetime | None = None) -> dict:
    """Ranked chairs >= min_qty. `min_qty=0` also shows seating lots with no
    readable count (quantity None, ranked last). A failed half is reported in
    `errors` and the other half still returns; both failing raises."""
    now = now or datetime.now(timezone.utc)
    min_qty = clamp_min_qty(min_qty)
    sort = sort if sort in SORTS else "unit_bid"
    state = str(state).strip().upper() or None if state else None
    site = site or None
    errors: list[str] = []
    rows: list[dict] = []
    halves = 0
    if site in (None, GOVDEALS):
        halves += 1
        try:
            fees = fee_model_from_env()
            rows += [shape_govdeals(dict(r), fees)
                     for r in _govdeals_rows(state=state, ending_within=ending_within)]
        except Exception as e:  # noqa: BLE001 — reported, other half still serves
            log.warning("chairs feed: govdeals half failed: %r", e)
            errors.append(f"govdeals: {e!r}")
    if site != GOVDEALS:
        halves += 1
        try:
            snap = deals_sources.apply_filters(
                _snapshot_rows(now), state=state, ending_within=ending_within, site=site, now=now)
            rows += [shape_snapshot(r) for r in snap]
        except Exception as e:  # noqa: BLE001
            log.warning("chairs feed: sources half failed: %r", e)
            errors.append(f"sources: {e!r}")
    if halves and len(errors) == halves:
        raise RuntimeError("; ".join(errors))
    rows = [r for r in rows
            if (r["quantity"] is not None and r["quantity"] >= max(min_qty, 1))
            or (min_qty == 0 and r["quantity"] is None)]
    rows = [{k: r.get(k) for k in ROW_KEYS} for r in rank(rows, sort)]
    by_site: dict[str, int] = {}
    for r in rows:
        by_site[r["source"]] = by_site.get(r["source"], 0) + 1
    return {"rows": rows, "total": len(rows), "min_qty": min_qty, "sort": sort,
            "sites": by_site, "errors": errors}
