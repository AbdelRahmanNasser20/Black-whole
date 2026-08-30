-- scripts/sql/2026-08-31_site_column.sql
-- Phase 0 of the site-onboarding plan. Additive; GovDeals rows keep their key.
BEGIN;
ALTER TABLE deal_lots
  ADD COLUMN IF NOT EXISTS site TEXT NOT NULL DEFAULT 'govdeals',
  ADD COLUMN IF NOT EXISTS native_id TEXT;
UPDATE deal_lots
   SET native_id = asset_id::text || '/' || account_id::text || '/' || auction_id::text
 WHERE native_id IS NULL;
ALTER TABLE deal_lots ALTER COLUMN native_id SET NOT NULL;
ALTER TABLE deal_lots DROP CONSTRAINT deal_lots_pkey;
ALTER TABLE deal_lots ADD PRIMARY KEY (site, asset_id, account_id, auction_id);
CREATE UNIQUE INDEX IF NOT EXISTS ux_deal_lots_site_native ON deal_lots(site, native_id);
ALTER TABLE deal_snapshots ADD COLUMN IF NOT EXISTS site TEXT NOT NULL DEFAULT 'govdeals';
DROP INDEX IF EXISTS ix_deal_snap_key;
CREATE INDEX ix_deal_snap_key ON deal_snapshots(site, asset_id, account_id, auction_id, observed_at DESC);
ALTER TABLE deal_bid_observations ADD COLUMN IF NOT EXISTS site TEXT NOT NULL DEFAULT 'govdeals';
COMMIT;
