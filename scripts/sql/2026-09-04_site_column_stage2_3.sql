-- scripts/sql/2026-09-04_site_column_stage2_3.sql
-- Remainder of 2026-08-31_site_column.sql. Stage 1 (ADD COLUMN site / native_id on
-- deal_lots, deal_snapshots, deal_bid_observations) was applied 2026-09-04 as
-- migration `site_column_stage1_add_columns`. 30,000 of 210,932 rows have native_id.
--
-- STAGE 2 — run this block in the Supabase SQL Editor (seconds; ACCESS EXCLUSIVE lock).
-- Until it runs, deals/store.py's ON CONFLICT (site, asset_id, account_id, auction_id)
-- has no matching unique index and the Render deals-discover / deals-watch crons fail.
BEGIN;
ALTER TABLE deal_lots DROP CONSTRAINT deal_lots_pkey;
ALTER TABLE deal_lots ADD PRIMARY KEY (site, asset_id, account_id, auction_id);
CREATE UNIQUE INDEX IF NOT EXISTS ux_deal_lots_site_native ON deal_lots(site, native_id);
DROP INDEX IF EXISTS ix_deal_snap_key;
CREATE INDEX ix_deal_snap_key ON deal_snapshots(site, asset_id, account_id, auction_id, observed_at DESC);
COMMIT;

-- STAGE 3 — native_id backfill, batched. The DB sits at the 500 MB free-tier line, so a
-- single UPDATE of 181k rows (+~215 MB of dead tuples) would flip it read-only.
-- Run the UPDATE, then VACUUM, repeat until `updated` = 0 (about 9 rounds of 20,000).
-- Each round reuses the space the previous VACUUM freed, so the file barely grows.
WITH b AS (SELECT asset_id, account_id, auction_id FROM deal_lots WHERE native_id IS NULL LIMIT 20000),
u AS (UPDATE deal_lots d SET native_id = d.asset_id::text||'/'||d.account_id::text||'/'||d.auction_id::text
      FROM b WHERE d.asset_id=b.asset_id AND d.account_id=b.account_id AND d.auction_id=b.auction_id RETURNING 1)
SELECT count(*) AS updated, (SELECT count(*) FROM deal_lots WHERE native_id IS NULL) AS still_null,
       (SELECT pg_database_size(current_database())/1e6) AS db_mb FROM u;
VACUUM deal_lots;
-- ... repeat the two statements above until still_null = 0, then:
ALTER TABLE deal_lots ALTER COLUMN native_id SET NOT NULL;
