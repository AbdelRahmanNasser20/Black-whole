# deals/verdict_store.py
"""Persistence for analysis verdicts + the analyze funnel query.

Same split as deals/store.py: verdict_row is pure/tested; the rest are thin
db wrappers exercised by live smoke."""
import json
from datetime import datetime, timedelta
from automation import db

DDL_FILE = "scripts/sql/deal_verdicts.sql"

VERDICT_COLUMNS = ["asset_id","account_id","auction_id","analyzed_at",
    "identity","queries","method","comps","comp_count","per_unit",
    "recovery_tier","est_resale","piece_out_ceiling","landed_cost",
    "margin","margin_pct","confidence","reasoning",
    "rank_score","rank_notes","alerted_at"]

_JSON_COLS = {"identity", "comps"}

def verdict_row(v: dict) -> tuple:
    out = []
    for c in VERDICT_COLUMNS:
        val = v.get(c)
        if c in _JSON_COLS and val is not None:
            val = json.dumps(val, default=str)
        out.append(val)
    return tuple(out)

def init_verdict_schema() -> None:
    with open(DDL_FILE) as f:
        sql = f.read()
    for stmt in filter(str.strip, sql.split(";")):
        db.execute(stmt)

def insert_verdict(v: dict) -> None:
    cols = ",".join(VERDICT_COLUMNS)
    ph = ",".join(["%s"] * len(VERDICT_COLUMNS))
    db.execute(f"INSERT INTO deal_verdicts ({cols}) VALUES ({ph})", verdict_row(v))

def lots_for_analysis(now: datetime, *, max_bid: float, window_h: int,
                      limit: int) -> list[dict]:
    """Cheap-close funnel: open lots ending inside the window, 0 bids or bid
    <= max_bid, not analyzed in the last 12h."""
    return db.fetch_all("""
        SELECT l.* FROM deal_lots l
        WHERE l.outcome IS NULL AND l.raw IS NOT NULL
          AND l.end_utc > %s AND l.end_utc <= %s
          AND (l.bid_count = 0 OR l.current_bid <= %s)
          AND l.is_free = false AND l.currency_code = 'USD'
          AND NOT EXISTS (SELECT 1 FROM deal_verdicts v
              WHERE v.asset_id=l.asset_id AND v.account_id=l.account_id
                AND v.auction_id=l.auction_id AND v.analyzed_at > %s)
        ORDER BY l.end_utc ASC LIMIT %s""",
        (now, now + timedelta(hours=window_h), max_bid,
         now - timedelta(hours=12), limit))

def latest_verdict(key: tuple[int, int, int]) -> dict | None:
    return db.fetch_one("""SELECT * FROM deal_verdicts
        WHERE asset_id=%s AND account_id=%s AND auction_id=%s
        ORDER BY analyzed_at DESC LIMIT 1""", key)

def mark_alerted(key: tuple[int, int, int], analyzed_at: datetime) -> None:
    db.execute("""UPDATE deal_verdicts SET alerted_at=now()
        WHERE asset_id=%s AND account_id=%s AND auction_id=%s AND analyzed_at=%s""",
        (*key, analyzed_at))
