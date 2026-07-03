"""Persistence layer for the deal-tracker over Supabase Postgres.

Tables `deal_lots` + `deal_snapshots`, keyed by (asset_id, account_id,
auction_id) so a relist of the same asset in a new auction can't clobber a
prior auction's recorded outcome.

`lot_row`/`snapshot_row` + `LOT_COLUMNS`/`SNAPSHOT_COLUMNS` are pure and
unit-tested (tests/deals/test_store_rows.py) without touching the DB. The
rest are thin wrappers over `automation.db`, exercised later via a live
smoke task.
"""
import sys
import json
from datetime import datetime
from deals.models import Lot, Snapshot
from automation import db

LOT_COLUMNS = ["asset_id","account_id","auction_id","title","description",
    "native_category_id","native_category_name","canonical_category",
    "llm_category","llm_category_confidence","category_agreement",
    "end_utc","bid_count","opening_bid","current_bid","currency_code","high_bidder",
    "has_reserve","reserve_not_met","reserve_price","is_free",
    "seller","city","state","zip","lat","lng","hero_image_url","status","is_sold","raw"]

SNAPSHOT_COLUMNS = ["asset_id","account_id","auction_id","observed_at",
    "bid_count","current_bid","end_utc","status"]

DDL = """
CREATE TABLE IF NOT EXISTS deal_lots (
  asset_id INT, account_id INT, auction_id INT,
  title TEXT, description TEXT,
  native_category_id TEXT, native_category_name TEXT, canonical_category TEXT,
  llm_category TEXT, llm_category_confidence REAL, category_agreement BOOLEAN,
  end_utc TIMESTAMPTZ, bid_count INT, opening_bid REAL, current_bid REAL,
  currency_code TEXT, high_bidder BIGINT,
  has_reserve BOOLEAN, reserve_not_met BOOLEAN, reserve_price REAL, is_free BOOLEAN,
  seller TEXT, city TEXT, state TEXT, zip TEXT, lat REAL, lng REAL,
  hero_image_url TEXT, status TEXT, is_sold BOOLEAN, raw JSONB,
  outcome TEXT, final_bid REAL, final_bid_count INT, closed_at TIMESTAMPTZ, outcome_complete BOOLEAN,
  next_poll_at TIMESTAMPTZ, lane TEXT,
  first_seen_at TIMESTAMPTZ DEFAULT now(), updated_at TIMESTAMPTZ DEFAULT now(),
  images_archived BOOLEAN DEFAULT false,
  archived_hero_url TEXT, gallery_urls JSONB,
  PRIMARY KEY (asset_id, account_id, auction_id)
);
CREATE INDEX IF NOT EXISTS ix_deal_lots_cat ON deal_lots(canonical_category);
CREATE INDEX IF NOT EXISTS ix_deal_lots_poll ON deal_lots(next_poll_at) WHERE outcome_complete IS NOT TRUE;
CREATE TABLE IF NOT EXISTS deal_snapshots (
  id BIGSERIAL PRIMARY KEY,
  asset_id INT, account_id INT, auction_id INT, observed_at TIMESTAMPTZ,
  bid_count INT, current_bid REAL, end_utc TIMESTAMPTZ, status TEXT
);
CREATE INDEX IF NOT EXISTS ix_deal_snap_key ON deal_snapshots(asset_id,account_id,auction_id,observed_at DESC);
"""

def init_schema() -> None:
    for stmt in filter(str.strip, DDL.split(";")):
        db.execute(stmt)

def lot_row(lot: Lot) -> tuple:
    return (lot.asset_id, lot.account_id, lot.auction_id, lot.title, lot.description,
        lot.native_category_id, lot.native_category_name, lot.canonical_category,
        lot.llm_category, lot.llm_category_confidence, lot.category_agreement,
        lot.end_utc, lot.bid_count, lot.opening_bid, lot.current_bid, lot.currency_code,
        lot.high_bidder, lot.has_reserve, lot.reserve_not_met, lot.reserve_price, lot.is_free,
        lot.seller, lot.city, lot.state, lot.zip, lot.lat, lot.lng,
        lot.hero_image_url, lot.status, lot.is_sold, json.dumps(lot.raw, default=str))

def snapshot_row(s: Snapshot) -> tuple:
    return (s.asset_id, s.account_id, s.auction_id, s.observed_at,
            s.bid_count, s.current_bid, s.end_utc, s.status)

def upsert_lot(lot: Lot) -> None:
    cols = ",".join(LOT_COLUMNS)
    ph = ",".join(["%s"] * len(LOT_COLUMNS))
    # on conflict, refresh as-parsed columns + updated_at, but NEVER touch outcome columns
    updates = ",".join(f"{c}=EXCLUDED.{c}" for c in LOT_COLUMNS
                       if c not in ("asset_id","account_id","auction_id"))
    db.execute(f"""INSERT INTO deal_lots ({cols}) VALUES ({ph})
        ON CONFLICT (asset_id,account_id,auction_id) DO UPDATE SET {updates}, updated_at=now()""",
        lot_row(lot))

def append_snapshot(s: Snapshot) -> None:
    from deals.watcher_logic import is_snapshot_change
    prev = latest_snapshot((s.asset_id, s.account_id, s.auction_id))
    if not is_snapshot_change(prev, s):
        return
    db.execute(f"INSERT INTO deal_snapshots ({','.join(SNAPSHOT_COLUMNS)}) "
               f"VALUES ({','.join(['%s']*len(SNAPSHOT_COLUMNS))})", snapshot_row(s))

def latest_snapshot(key: tuple[int,int,int]) -> Snapshot | None:
    r = db.fetch_one("""SELECT asset_id,account_id,auction_id,observed_at,bid_count,current_bid,end_utc,status
        FROM deal_snapshots WHERE asset_id=%s AND account_id=%s AND auction_id=%s
        ORDER BY observed_at DESC LIMIT 1""", key)
    if not r:
        return None
    return Snapshot(r["asset_id"],r["account_id"],r["auction_id"],r["observed_at"],
                    r["bid_count"],r["current_bid"],r["end_utc"],r["status"])

def record_outcome(key, outcome, final_bid, final_bid_count, closed_at, complete) -> None:
    db.execute("""UPDATE deal_lots SET outcome=%s, final_bid=%s, final_bid_count=%s,
        closed_at=%s, outcome_complete=%s, lane='done', updated_at=now()
        WHERE asset_id=%s AND account_id=%s AND auction_id=%s""",
        (outcome, final_bid, final_bid_count, closed_at, complete, *key))

def set_poll_schedule(key, next_poll_at: datetime, lane: str) -> None:
    db.execute("""UPDATE deal_lots SET next_poll_at=%s, lane=%s, updated_at=now()
        WHERE asset_id=%s AND account_id=%s AND auction_id=%s""", (next_poll_at, lane, *key))

def set_archived_images(key, hero_url: str, gallery_urls: list[str]) -> None:
    db.execute("""UPDATE deal_lots SET archived_hero_url=%s, gallery_urls=%s,
        images_archived=true, updated_at=now()
        WHERE asset_id=%s AND account_id=%s AND auction_id=%s""",
        (hero_url, json.dumps(gallery_urls), *key))

def update_live_state(key, s: Snapshot, next_poll_at, lane: str) -> None:
    db.execute("""UPDATE deal_lots SET bid_count=%s, current_bid=%s, end_utc=%s,
        next_poll_at=%s, lane=%s, updated_at=now()
        WHERE asset_id=%s AND account_id=%s AND auction_id=%s""",
        (s.bid_count, s.current_bid, s.end_utc, next_poll_at, lane, *key))

def due_for_poll(now: datetime) -> list[Lot]:
    from deals.mapping import asset_to_lot
    rows = db.fetch_all("""SELECT raw FROM deal_lots
        WHERE outcome_complete IS NOT TRUE AND (next_poll_at IS NULL OR next_poll_at<=%s)""", (now,))
    lots = []
    for r in rows:
        try:
            lots.append(asset_to_lot(r["raw"]))
        except ValueError as e:
            print(f"deals.store.due_for_poll: skipping malformed stored row: {e}", file=sys.stderr)
    return lots
