"""Persistence layer for the deal-tracker over Supabase Postgres.

Tables `deal_lots` + `deal_snapshots`, keyed by (site, asset_id, account_id,
auction_id) so foreign marketplaces coexist and a relist of the same asset in
a new auction can't clobber a prior auction's recorded outcome.

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
    "seller","city","state","zip","lat","lng","hero_image_url","status","is_sold","raw",
    "site","native_id"]

SNAPSHOT_COLUMNS = ["asset_id","account_id","auction_id","observed_at",
    "bid_count","current_bid","end_utc","status","site"]

DDL = """
CREATE TABLE IF NOT EXISTS deal_lots (
  site TEXT NOT NULL DEFAULT 'govdeals', native_id TEXT NOT NULL,
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
  PRIMARY KEY (site, asset_id, account_id, auction_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_deal_lots_site_native ON deal_lots(site, native_id);
CREATE INDEX IF NOT EXISTS ix_deal_lots_cat ON deal_lots(canonical_category);
CREATE INDEX IF NOT EXISTS ix_deal_lots_poll ON deal_lots(next_poll_at) WHERE outcome_complete IS NOT TRUE;
CREATE TABLE IF NOT EXISTS deal_snapshots (
  id BIGSERIAL PRIMARY KEY,
  asset_id INT, account_id INT, auction_id INT, observed_at TIMESTAMPTZ,
  bid_count INT, current_bid REAL, end_utc TIMESTAMPTZ, status TEXT,
  site TEXT NOT NULL DEFAULT 'govdeals'
);
CREATE INDEX IF NOT EXISTS ix_deal_snap_key ON deal_snapshots(site,asset_id,account_id,auction_id,observed_at DESC);
"""

BID_OBS_COLUMNS = ["asset_id","account_id","auction_id","observed_at","bid_count",
    "current_bid","currency_code","high_bidder","high_bidder_username","bid_increment",
    "visitors","hits","watcher_count","end_utc","status"]

def _bid_obs_ddl() -> str:
    """Read the bidder DDL from its file of record rather than restating it here.

    `deal_lots`/`deal_snapshots` keep a second copy of their schema in
    scripts/sql/deals_schema.sql and the two have to be hand-synced; there's no
    reason to repeat that for a new table when the file already ships with the
    repo."""
    from pathlib import Path
    p = Path(__file__).resolve().parent.parent / "scripts" / "sql" / "deal_bid_observations.sql"
    return p.read_text()

def init_schema() -> None:
    for stmt in filter(str.strip, DDL.split(";")):
        db.execute(stmt)
    for stmt in filter(str.strip, _bid_obs_ddl().split(";")):
        db.execute(stmt)
    from deals.verdict_store import init_verdict_schema
    init_verdict_schema()

def lot_row(lot: Lot) -> tuple:
    return (lot.asset_id, lot.account_id, lot.auction_id, lot.title, lot.description,
        lot.native_category_id, lot.native_category_name, lot.canonical_category,
        lot.llm_category, lot.llm_category_confidence, lot.category_agreement,
        lot.end_utc, lot.bid_count, lot.opening_bid, lot.current_bid, lot.currency_code,
        lot.high_bidder, lot.has_reserve, lot.reserve_not_met, lot.reserve_price, lot.is_free,
        lot.seller, lot.city, lot.state, lot.zip, lot.lat, lot.lng,
        lot.hero_image_url, lot.status, lot.is_sold, json.dumps(lot.raw, default=str),
        lot.site, lot.native_id)

def snapshot_row(s: Snapshot, site: str = "govdeals") -> tuple:
    return (s.asset_id, s.account_id, s.auction_id, s.observed_at,
            s.bid_count, s.current_bid, s.end_utc, s.status, site)

def upsert_lot(lot: Lot) -> None:
    cols = ",".join(LOT_COLUMNS)
    ph = ",".join(["%s"] * len(LOT_COLUMNS))
    # on conflict, refresh as-parsed columns + updated_at, but NEVER touch outcome columns
    updates = ",".join(f"{c}=EXCLUDED.{c}" for c in LOT_COLUMNS
                       if c not in ("site","asset_id","account_id","auction_id"))
    db.execute(f"""INSERT INTO deal_lots ({cols}) VALUES ({ph})
        ON CONFLICT (site,asset_id,account_id,auction_id) DO UPDATE SET {updates}, updated_at=now()""",
        lot_row(lot))

def append_snapshot(s: Snapshot, site: str = "govdeals") -> None:
    from deals.watcher_logic import is_snapshot_change
    prev = latest_snapshot((s.asset_id, s.account_id, s.auction_id))
    if not is_snapshot_change(prev, s):
        return
    db.execute(f"INSERT INTO deal_snapshots ({','.join(SNAPSHOT_COLUMNS)}) "
               f"VALUES ({','.join(['%s']*len(SNAPSHOT_COLUMNS))})", snapshot_row(s, site))

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

def unarchived_active(limit: int = 100, zero_bid_only: bool = False) -> list[dict]:
    """Active lots whose images haven't been archived yet. Zero-bid lots (the
    buy candidates) first, then soonest-ending — those pages vanish first."""
    zb = "AND bid_count = 0" if zero_bid_only else ""
    return db.fetch_all(f"""SELECT asset_id, account_id, auction_id, hero_image_url
        FROM deal_lots
        WHERE images_archived IS NOT TRUE AND end_utc > now() {zb}
        ORDER BY (bid_count = 0) DESC, end_utc ASC
        LIMIT %s""", (limit,))

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
    # `raw IS NOT NULL` guards against a cold-archived row leaking in: raw is
    # nulled once a lot closes and its blob is exported to R2, and
    # asset_to_lot(None) would raise AttributeError, killing the whole pass.
    rows = db.fetch_all("""SELECT raw FROM deal_lots
        WHERE outcome_complete IS NOT TRUE AND raw IS NOT NULL
          AND (next_poll_at IS NULL OR next_poll_at<=%s)""", (now,))
    lots = []
    for r in rows:
        try:
            lots.append(asset_to_lot(r["raw"]))
        except (ValueError, AttributeError, TypeError, KeyError) as e:
            print(f"deals.store.due_for_poll: skipping malformed stored row: {e}", file=sys.stderr)
    return lots

# ── rival-bidder observations ────────────────────────────────────────────────
# See deals/bidders.py for why these live apart from deal_snapshots.

def append_bid_observation(state) -> bool:
    """Write one observation, change-gated. Returns True if a row was written."""
    from deals.bidders import is_bid_change
    prev = latest_bid_observation((state.asset_id, state.account_id, state.auction_id))
    if not is_bid_change(prev, state):
        return False
    cols = ",".join(BID_OBS_COLUMNS)
    ph = ",".join(["%s"] * len(BID_OBS_COLUMNS))
    db.execute(f"INSERT INTO deal_bid_observations ({cols}) VALUES ({ph})",
        (state.asset_id, state.account_id, state.auction_id, state.observed_at,
         state.bid_count, state.current_bid, state.currency_code, state.high_bidder,
         state.high_bidder_username, state.bid_increment, state.visitors, state.hits,
         state.watcher_count, state.end_utc, state.status))
    return True

def latest_bid_observation(key: tuple[int, int, int]):
    from deals.bidders import BidState
    r = db.fetch_one(f"""SELECT {','.join(BID_OBS_COLUMNS)} FROM deal_bid_observations
        WHERE asset_id=%s AND account_id=%s AND auction_id=%s
        ORDER BY observed_at DESC LIMIT 1""", key)
    if not r:
        return None
    return BidState(**{c: (float(r[c]) if c in ("current_bid", "bid_increment") and r[c] is not None
                          else r[c]) for c in BID_OBS_COLUMNS})

def favorite_asset_ids() -> list[str]:
    """Raw `asset_id` keys the operator starred on the Auctions tab."""
    return [r["asset_id"] for r in db.fetch_all(
        "SELECT asset_id FROM auction_favorites ORDER BY starred_at DESC")]

def live_auction_id(asset_id: int, account_id: int) -> int | None:
    """Newest still-open auction we've stored for this asset."""
    r = db.fetch_one("""SELECT auction_id FROM deal_lots
        WHERE asset_id=%s AND account_id=%s AND end_utc > now()
        ORDER BY auction_id DESC LIMIT 1""", (asset_id, account_id))
    return r["auction_id"] if r else None

def bidder_targets(limit: int = 200, *, category: str | None = "seating_furniture",
                   title_like: str | None = None, ending_within_hours: int | None = None,
                   min_bids: int = 0) -> list[tuple[int, int, int]]:
    """Open lots worth sampling for bidder identity, soonest-closing first.

    Soonest-first is the whole ordering rationale: a lead change on a lot that
    closes tomorrow is unrecoverable, one on a lot closing in nine days can be
    caught next pass. `min_bids=1` narrows to contested lots — a lot with no
    bids has no bidder to identify, so sampling it spends a request to learn
    nothing."""
    where = ["end_utc > now()", "outcome_complete IS NOT TRUE"]
    params: list = []
    if min_bids:
        where.append("bid_count >= %s"); params.append(min_bids)
    if category:
        where.append("canonical_category = %s"); params.append(category)
    if title_like:
        where.append("title ILIKE %s"); params.append(f"%{title_like}%")
    if ending_within_hours:
        where.append("end_utc < now() + make_interval(hours => %s)")
        params.append(ending_within_hours)
    params.append(limit)
    rows = db.fetch_all(f"""SELECT asset_id, account_id, auction_id FROM deal_lots
        WHERE {' AND '.join(where)} ORDER BY end_utc ASC LIMIT %s""", tuple(params))
    return [(r["asset_id"], r["account_id"], r["auction_id"]) for r in rows]
