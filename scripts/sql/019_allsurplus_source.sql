-- 019_allsurplus_source.sql — AllSurplus becomes its own recorder source.
--
-- STATUS: PENDING — NOT applied to prod. Review, then apply in the Supabase
-- SQL Editor (or `.venv/bin/python scripts/apply_sql.py scripts/sql/019_allsurplus_source.sql`).
--
-- Why: GovDeals and AllSurplus share one maestro API. Before this change the
-- GovDeals whole-site sweep stored AllSurplus ("GI") lots under
-- source='govdeals' — and AllSurplus prices in EUR/GBP/ZAR/CNY/..., so those
-- rows leaked foreign-currency finals into USD comps. From this release the
-- recorder files any asset with raw.businessId='GI' under source='allsurplus'
-- (recorder/sources/govdeals.py::_lot_to_observation).
--
-- Measured 2026-10-02 (read-only): 36 lots carry a GI row under govdeals —
-- 82 rows with raw.businessId='GI', 212 rows in total for those 36 lots
-- (poll/bidbox rows carry no businessId). ALL rows of those lots move, so each
-- lot's timeline stays whole. This is the one sanctioned data fix to the
-- append-only listing_snapshots (recorder code itself never UPDATEs).
--
-- Expected: UPDATE 212 (± lots that closed since). Re-running is a no-op.

BEGIN;

UPDATE listing_snapshots s
SET source = 'allsurplus'
WHERE s.source = 'govdeals'
  AND s.source_lot_id IN (
      SELECT DISTINCT source_lot_id FROM listing_snapshots
      WHERE source = 'govdeals' AND raw->>'businessId' = 'GI');

-- The lot_archive index (015) keys on (source, lot_key); none of the 30
-- archived lots is GI today, but keep the two tables consistent anyway.
UPDATE lot_archive a
SET source = 'allsurplus'
WHERE a.source = 'govdeals'
  AND a.lot_key IN (SELECT DISTINCT source_lot_id FROM listing_snapshots
                    WHERE source = 'allsurplus');

-- sold_comps: 014's definition unchanged, plus two appended columns
-- (CREATE OR REPLACE VIEW may only append):
--   currency  the lot's maestro currencyCode (latest row that has one);
--             'USD' for the US-only non-maestro sources; NULL = unknown.
--   country   the maestro `country` code (NULL for non-maestro sources).
-- Consumers must never aggregate final_price across currencies.
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
), money AS (
  SELECT DISTINCT ON (source, source_lot_id)
         source, source_lot_id, raw->>'currencyCode' AS currency, raw->>'country' AS country
  FROM listing_snapshots
  WHERE raw ? 'currencyCode'
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
       l.raw->'recorder_capture'->>'status_code' AS status_code,
       COALESCE(m.currency,
                CASE WHEN l.source IN ('govdeals', 'allsurplus') THEN NULL ELSE 'USD' END) AS currency,
       m.country
FROM latest l
LEFT JOIN last_priced p USING (source, source_lot_id)
LEFT JOIN money m USING (source, source_lot_id)
WHERE l.status IN ('closed','gone')
  AND COALESCE(l.current_bid, p.current_bid) IS NOT NULL
  AND COALESCE(l.bid_count,  p.bid_count, 0) > 0
  AND COALESCE(l.raw->'recorder_capture'->>'outcome', '')
      NOT IN ('reserve_not_met', 'cancelled', 'no_bid');

COMMIT;

-- Rollback: UPDATE listing_snapshots SET source='govdeals' WHERE source='allsurplus'
-- AND observed_at < '<apply time>'; re-run 014's CREATE OR REPLACE VIEW after
-- DROP VIEW sold_comps (dropping appended columns needs a DROP).
