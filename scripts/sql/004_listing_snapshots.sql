-- BLACKWHOLE-28 Phase 0 — closing-price recorder.
-- Append-only snapshot table + derived sold_comps view. See recorder/README.md.
CREATE TABLE IF NOT EXISTS listing_snapshots (
  id            BIGSERIAL PRIMARY KEY,
  source        TEXT NOT NULL,
  source_lot_id TEXT NOT NULL,
  observed_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  status        TEXT,
  current_bid   NUMERIC(12,2),
  bid_count     INTEGER,
  end_date      TIMESTAMPTZ,
  raw           JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_listing_snapshots_lot
  ON listing_snapshots (source, source_lot_id, observed_at DESC);
-- sold_comps: DERIVED view; wrong logic later => recompute, never re-scrape
CREATE OR REPLACE VIEW sold_comps AS
WITH latest AS (
  SELECT DISTINCT ON (source, source_lot_id) *
  FROM listing_snapshots
  ORDER BY source, source_lot_id, observed_at DESC
), last_priced AS (
  SELECT DISTINCT ON (source, source_lot_id)
         source, source_lot_id, current_bid, bid_count
  FROM listing_snapshots
  WHERE current_bid IS NOT NULL
  ORDER BY source, source_lot_id, observed_at DESC
)
SELECT l.source, l.source_lot_id,
       COALESCE(l.current_bid, p.current_bid) AS final_price,
       COALESCE(l.bid_count,  p.bid_count)    AS bid_count,
       COALESCE(l.end_date, l.observed_at)    AS sold_at,
       CASE WHEN l.status = 'closed' AND l.current_bid IS NOT NULL
            THEN 'api_final' ELSE 'last_snapshot' END AS capture_method,
       CASE WHEN l.status = 'closed' AND l.current_bid IS NOT NULL
            THEN 'high' ELSE 'medium' END AS confidence
FROM latest l LEFT JOIN last_priced p USING (source, source_lot_id)
WHERE l.status IN ('closed','gone')
  AND COALESCE(l.current_bid, p.current_bid) IS NOT NULL
  AND COALESCE(l.bid_count,  p.bid_count, 0) > 0;
