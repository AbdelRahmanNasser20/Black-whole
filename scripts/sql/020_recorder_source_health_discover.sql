-- 020_recorder_source_health_discover.sql — discover staleness for the recorder breaker.
--
-- STATUS: PENDING — NOT applied to prod. Requires 018. Apply in the Supabase
-- SQL Editor (or `.venv/bin/python scripts/apply_sql.py scripts/sql/020_recorder_source_health_discover.sql`).
--
-- `run` decides a source is stale from max(newest listing_snapshots row,
-- last clean DISCOVER). Poll successes also refresh last_success_at, so that
-- column cannot stand in for it. Until this is applied the recorder neither
-- reads nor writes the column (store._health_cols checks information_schema),
-- and staleness falls back to the newest row alone.

ALTER TABLE recorder_source_health ADD COLUMN IF NOT EXISTS last_discover_at timestamptz;
