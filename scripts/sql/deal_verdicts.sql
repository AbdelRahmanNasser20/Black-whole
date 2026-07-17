-- scripts/sql/deal_verdicts.sql
-- Analysis verdicts + deal-browser saves/tags/searches (2026-07-17 spec).
CREATE TABLE IF NOT EXISTS deal_verdicts (
  asset_id INT, account_id INT, auction_id INT,
  analyzed_at TIMESTAMPTZ DEFAULT now(),
  identity JSONB, queries TEXT[],
  method TEXT,                    -- 'comps' | 'llm_estimate'
  comps JSONB, comp_count INT,
  per_unit REAL, recovery_tier REAL,
  est_resale REAL, piece_out_ceiling REAL,
  landed_cost REAL, margin REAL, margin_pct REAL,
  confidence TEXT,                -- 'low' | 'medium' | 'high'
  reasoning TEXT,
  rank_score REAL, rank_notes TEXT,
  alerted_at TIMESTAMPTZ,
  PRIMARY KEY (asset_id, account_id, auction_id, analyzed_at)
);
CREATE INDEX IF NOT EXISTS ix_verdicts_margin ON deal_verdicts(margin_pct DESC);

CREATE TABLE IF NOT EXISTS deal_lists (
  id BIGSERIAL PRIMARY KEY, name TEXT UNIQUE NOT NULL,
  created_at TIMESTAMPTZ DEFAULT now());
CREATE TABLE IF NOT EXISTS deal_list_items (
  list_id BIGINT REFERENCES deal_lists(id) ON DELETE CASCADE,
  asset_id INT, account_id INT, auction_id INT,
  added_at TIMESTAMPTZ DEFAULT now(),
  PRIMARY KEY (list_id, asset_id, account_id, auction_id));
CREATE TABLE IF NOT EXISTS deal_lot_tags (
  asset_id INT, account_id INT, auction_id INT, tag TEXT,
  added_at TIMESTAMPTZ DEFAULT now(),
  PRIMARY KEY (asset_id, account_id, auction_id, tag));
CREATE TABLE IF NOT EXISTS saved_searches (
  id BIGSERIAL PRIMARY KEY, name TEXT UNIQUE NOT NULL,
  params JSONB NOT NULL, alert BOOLEAN DEFAULT false,
  created_at TIMESTAMPTZ DEFAULT now(), last_run_at TIMESTAMPTZ);

ALTER TABLE deal_lots ADD COLUMN IF NOT EXISTS relist_of JSONB;
