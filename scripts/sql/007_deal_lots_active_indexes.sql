-- scripts/sql/007_deal_lots_active_indexes.sql
-- Public /deals + admin Deals tab: make the active set index-driven.
-- Both are PARTIAL on `outcome_complete IS NOT TRUE` (~33k rows incl. not-yet-
-- marked closes), so they stay a few MB on a DB sitting at its 500 MB ceiling.
-- (Numbered 007: 006 is taken by research_profiles on feat/research-profiles.)
-- OPERATOR GATE — not applied to prod by the agent. Apply with autocommit
-- (CONCURRENTLY refuses to run inside a transaction):
--   .venv/bin/python scripts/apply_sql.py scripts/sql/007_deal_lots_active_indexes.sql
-- If DiskFull: run `scripts/reclaim_db_space.py --all` first (suspend Render crons).
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_deal_lots_active_end
  ON deal_lots (end_utc)
  WHERE outcome_complete IS NOT TRUE;
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_deal_lots_active_title_trgm
  ON deal_lots USING gin (title gin_trgm_ops)
  WHERE outcome_complete IS NOT TRUE;
