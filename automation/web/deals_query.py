"""Pure helpers for /api/deals: request params -> SQL fragments + row enrichment.

No DB access here so everything is unit-testable without a connection.
All values are bound via %s placeholders; sort columns come only from SORTS.
"""
from __future__ import annotations

from deals.fees import FeeModel, landed_cost
from deals.quantity import lot_quantity, unit_price

# landed cost is monotonic in current_bid for a fixed fee model, so SQL can
# sort by current_bid for both "bid" and "landed".
# "margin" sorts on the latest verdict's margin_pct — callers using it must
# join deal_verdicts as the lateral alias `v` (see app.py::list_deals).
SORTS = {
    "ends": "end_utc",
    "landed": "current_bid",
    "bid": "current_bid",
    "bids": "bid_count",
    "newest": "first_seen_at",
    "margin": "v.margin_pct",
}


def build_where(*, q: str | None = None, category: str | None = None,
                native: str | None = None,
                state: str | None = None, max_bids: int | None = None,
                ending_within: int | None = None,
                status: str = "active",
                min_margin: float | None = None,
                min_price: float | None = None,
                max_price: float | None = None,
                list_id: int | None = None,
                tag: str | None = None,
                bbox: tuple[float, float, float, float] | None = None,
                search_fields: tuple[str, ...] = ("title", "description"),
                profile_where: tuple[str, list] | None = None,
                ) -> tuple[str, list]:
    where: list[str] = []
    args: list = []
    if status == "active":
        where.append("outcome_complete IS NOT TRUE AND end_utc > now()")
    elif status == "closed":
        where.append("outcome_complete IS TRUE")
    if q:
        where.append("(" + " OR ".join(f"{f} ILIKE %s" for f in search_fields) + ")")
        args += [f"%{q}%"] * len(search_fields)
    if category:
        where.append("canonical_category = %s")
        args.append(category)
    if native:
        where.append("native_category_id = %s")
        args.append(native.upper())
    if state:
        where.append("state = %s")
        args.append(state.upper())
    if max_bids is not None:
        where.append("bid_count <= %s")
        args.append(max_bids)
    if ending_within is not None:
        where.append("end_utc <= now() + make_interval(hours => %s)")
        args.append(ending_within)
    if min_margin is not None:
        where.append("v.margin_pct >= %s")
        args.append(min_margin)
    # NULL current_bid never matches >=/<=, so unpriced lots drop out of a
    # price-bounded query on their own (same as bbox with NULL lat).
    if min_price is not None:
        where.append("current_bid >= %s")
        args.append(min_price)
    if max_price is not None:
        where.append("current_bid <= %s")
        args.append(max_price)
    if list_id is not None:
        where.append("""EXISTS (SELECT 1 FROM deal_list_items li
            WHERE li.list_id = %s AND li.asset_id = deal_lots.asset_id
              AND li.account_id = deal_lots.account_id
              AND li.auction_id = deal_lots.auction_id)""")
        args.append(list_id)
    if bbox is not None:
        # (south, west, north, east) — a NULL lat/lng never matches BETWEEN,
        # so unmapped lots drop out of a map-bounded query on their own.
        where.append("lat BETWEEN %s AND %s AND lng BETWEEN %s AND %s")
        args += [bbox[0], bbox[2], bbox[1], bbox[3]]
    if tag is not None:
        where.append("""EXISTS (SELECT 1 FROM deal_lot_tags t
            WHERE t.tag = %s AND t.asset_id = deal_lots.asset_id
              AND t.account_id = deal_lots.account_id
              AND t.auction_id = deal_lots.auction_id)""")
        args.append(tag)
    # Research-profile fragment (deals/profiles.deal_lots_where) — spliced last
    # so its bound args stay in order after the filters above.
    if profile_where is not None and profile_where[0] and profile_where[0] != "TRUE":
        where.append(f"({profile_where[0]})")
        args += list(profile_where[1])
    return (" AND ".join(where) or "TRUE", args)


def order_clause(sort: str, direction: str | None) -> str:
    col = SORTS.get(sort) or SORTS["ends"]
    if direction not in ("asc", "desc"):
        direction = "asc" if col == "end_utc" else "desc"
    return f"ORDER BY {col} {direction.upper()} NULLS LAST"


def enrich(row: dict, fees: FeeModel) -> dict:
    bid = float(row.get("current_bid") or 0)
    qty, src = lot_quantity(row.get("title"))
    lc = landed_cost(bid, qty=qty, fees=fees)
    row["landed_cost"] = round(lc.total, 2)
    row["quantity"] = qty
    row["quantity_source"] = src
    row["unit_bid"] = unit_price(row.get("current_bid"), qty)
    row["unit_landed"] = round(lc.per_unit, 2)
    row["govdeals_url"] = (
        f"https://www.govdeals.com/en/asset/{row['asset_id']}/{row['account_id']}"
    )
    row["viewer_url"] = f"/deals/{row['asset_id']}/{row['account_id']}/{row['auction_id']}"
    return row
