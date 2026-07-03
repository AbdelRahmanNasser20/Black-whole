-- Schema of record for deals/store.py — mirrors the DDL string in
-- deals/store.py::DDL verbatim. Applied via deals.store.init_schema().
-- Tables: deal_lots, deal_snapshots.
-- PK on deal_lots is (asset_id, account_id, auction_id) so a relist of the
-- same asset in a new auction can't clobber a prior auction's outcome.

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
