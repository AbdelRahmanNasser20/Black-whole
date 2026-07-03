"""Pure helpers for /api/deals: request params -> SQL fragments + row enrichment.

No DB access here so everything is unit-testable without a connection.
All values are bound via %s placeholders; sort columns come only from SORTS.
"""
from __future__ import annotations

from deals.fees import FeeModel, landed_cost

# landed cost is monotonic in current_bid for a fixed fee model, so SQL can
# sort by current_bid for both "bid" and "landed".
SORTS = {
    "ends": "end_utc",
    "landed": "current_bid",
    "bid": "current_bid",
    "bids": "bid_count",
    "newest": "first_seen_at",
}


def build_where(*, q: str | None = None, category: str | None = None,
                state: str | None = None, max_bids: int | None = None,
                ending_within: int | None = None,
                status: str = "active") -> tuple[str, list]:
    where: list[str] = []
    args: list = []
    if status == "active":
        where.append("outcome_complete IS NOT TRUE AND end_utc > now()")
    elif status == "closed":
        where.append("outcome_complete IS TRUE")
    if q:
        where.append("(title ILIKE %s OR description ILIKE %s)")
        args += [f"%{q}%", f"%{q}%"]
    if category:
        where.append("canonical_category = %s")
        args.append(category)
    if state:
        where.append("state = %s")
        args.append(state.upper())
    if max_bids is not None:
        where.append("bid_count <= %s")
        args.append(max_bids)
    if ending_within is not None:
        where.append("end_utc <= now() + make_interval(hours => %s)")
        args.append(ending_within)
    return (" AND ".join(where) or "TRUE", args)


def order_clause(sort: str, direction: str | None) -> str:
    col = SORTS.get(sort) or SORTS["ends"]
    if direction not in ("asc", "desc"):
        direction = "asc" if col == "end_utc" else "desc"
    return f"ORDER BY {col} {direction.upper()} NULLS LAST"


def enrich(row: dict, fees: FeeModel) -> dict:
    bid = float(row.get("current_bid") or 0)
    row["landed_cost"] = round(landed_cost(bid, qty=1, fees=fees).total, 2)
    row["govdeals_url"] = (
        f"https://www.govdeals.com/en/asset/{row['asset_id']}/{row['account_id']}"
    )
    row["viewer_url"] = f"/deals/{row['asset_id']}/{row['account_id']}/{row['auction_id']}"
    return row
