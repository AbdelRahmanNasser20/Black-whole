# deals/backfill.py
"""One-shot closer for lots that ended while the watcher wasn't running.

Uses the last snapshot when one exists, else the stored live state.
Everything closed here is outcome_complete = false — we observed the lot
before its close, not at it. Never overwrites an existing outcome.
"""
from datetime import datetime, timedelta
from automation import db
from deals.store import record_outcome

GRACE = timedelta(hours=1)   # leave truly-recent closes to the watcher
LOW_BID_THRESHOLD = 1        # mirrors deals/watcher_logic.py::detect_outcome

def backfill_plan(rows: list[dict], now: datetime) -> list[dict]:
    plan = []
    for r in rows:
        if r["end_utc"] > now - GRACE:
            continue
        bid_count = r["snap_bid_count"] if r["snap_bid_count"] is not None else r["bid_count"]
        final_bid = r["snap_current_bid"] if r["snap_current_bid"] is not None else r["current_bid"]
        if bid_count == 0:
            outcome = "no_bid"
        elif bid_count <= LOW_BID_THRESHOLD:
            outcome = "low_bid"
        else:
            outcome = "sold"
        plan.append({"key": (r["asset_id"], r["account_id"], r["auction_id"]),
                     "outcome": outcome, "final_bid": float(final_bid or 0),
                     "final_bid_count": int(bid_count or 0),
                     "closed_at": r["end_utc"], "complete": False})
    return plan

def run_backfill(now: datetime | None = None) -> int:
    now = now or datetime.now().astimezone()
    rows = db.fetch_all("""
        SELECT l.asset_id, l.account_id, l.auction_id, l.end_utc,
               l.bid_count, l.current_bid,
               s.bid_count AS snap_bid_count, s.current_bid AS snap_current_bid,
               s.observed_at AS snap_observed_at
        FROM deal_lots l
        LEFT JOIN LATERAL (
            SELECT bid_count, current_bid, observed_at FROM deal_snapshots
            WHERE asset_id=l.asset_id AND account_id=l.account_id AND auction_id=l.auction_id
            ORDER BY observed_at DESC LIMIT 1) s ON TRUE
        WHERE l.outcome IS NULL AND l.end_utc < %s""", (now,))
    plan = backfill_plan(rows, now)
    for p in plan:
        record_outcome(p["key"], p["outcome"], p["final_bid"],
                       p["final_bid_count"], p["closed_at"], p["complete"])
    return len(plan)
