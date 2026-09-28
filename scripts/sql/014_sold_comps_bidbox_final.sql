-- 014_sold_comps_bidbox_final.sql — sold_comps reads the GovDeals bidbox final.
--
-- Status: PENDING — NOT applied to prod. Numbered 014 because PR #105 claims
-- 013_tracked_lots_costs.sql.
-- Apply with: .venv/bin/python scripts/apply_sql.py scripts/sql/014_sold_comps_bidbox_final.sql
--
-- View-only change (no table, no column, no data). The recorder already
-- writes GovDeals finals as status='closed' rows with the real price, so the
-- CURRENT view already ranks them above last_snapshot (as 'api_final'). This
-- makes that explicit and stops non-sales from reading as comps:
--
--   capture_method  'bidbox_final' when the latest row is a bidbox final or
--                   7-day re-check (raw->'recorder_capture'->>'method'),
--                   else unchanged ('api_final' / 'last_snapshot').
--   confidence      'high' for a bidbox final only when its outcome is 'sold'
--                   (SOA/SOL); 'medium' for 'unknown' (CLO/HFR/stuck STA).
--   outcome,        appended columns (CREATE OR REPLACE VIEW may only append):
--   status_code     NULL for every non-bidbox row.
--   WHERE           drops reserve_not_met / cancelled / no_bid finals — a
--                   lot that did not sell is not a sold comp, whatever its
--                   high bid said.
--
-- Rollback: re-run the CREATE OR REPLACE VIEW in 004_listing_snapshots.sql
-- after DROP VIEW sold_comps (dropping the appended columns needs a DROP).
CREATE OR REPLACE VIEW sold_comps AS
WITH latest AS (
  SELECT DISTINCT ON (source, source_lot_id) *
  FROM listing_snapshots
  ORDER BY source, source_lot_id, observed_at DESC, id DESC
), last_priced AS (
  SELECT DISTINCT ON (source, source_lot_id)
         source, source_lot_id, current_bid, bid_count
  FROM listing_snapshots
  WHERE current_bid IS NOT NULL
  ORDER BY source, source_lot_id, observed_at DESC, id DESC
)
SELECT l.source, l.source_lot_id,
       COALESCE(l.current_bid, p.current_bid) AS final_price,
       COALESCE(l.bid_count,  p.bid_count)    AS bid_count,
       COALESCE(l.end_date, l.observed_at)    AS sold_at,
       CASE WHEN l.status = 'closed' AND l.current_bid IS NOT NULL
             AND l.raw->'recorder_capture'->>'method' IN ('bidbox_final', 'bidbox_recheck')
            THEN 'bidbox_final'
            WHEN l.status = 'closed' AND l.current_bid IS NOT NULL
            THEN 'api_final' ELSE 'last_snapshot' END AS capture_method,
       CASE WHEN l.status = 'closed' AND l.current_bid IS NOT NULL
             AND COALESCE(l.raw->'recorder_capture'->>'outcome', 'sold') = 'sold'
            THEN 'high' ELSE 'medium' END AS confidence,
       l.raw->'recorder_capture'->>'outcome'     AS outcome,
       l.raw->'recorder_capture'->>'status_code' AS status_code
FROM latest l LEFT JOIN last_priced p USING (source, source_lot_id)
WHERE l.status IN ('closed','gone')
  AND COALESCE(l.current_bid, p.current_bid) IS NOT NULL
  AND COALESCE(l.bid_count,  p.bid_count, 0) > 0
  AND COALESCE(l.raw->'recorder_capture'->>'outcome', '')
      NOT IN ('reserve_not_met', 'cancelled', 'no_bid');
