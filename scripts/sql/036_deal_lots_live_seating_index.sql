-- 036: partial index for the operator Chairs feed (admin tab 12, /api/deals/chairs).
--
-- chairs_feed._govdeals_rows reads live seating lots ordered by end_utc. Without
-- this it walks ix_deal_lots_active_end over EVERY live lot (~24,600) and runs the
-- title regex on each: 1.1-1.5 s warm on Render, 3.1 s cold (measured 2026-11-10).
-- This index holds only the ~1,600 live seating rows.
--
-- The predicate MUST stay byte-identical to public_deals.seating_where()
-- (EXCLUDED_CATEGORIES + EXCLUDED_TITLE_SQL_RE). If those constants change, the
-- planner stops using this index (the feed still works, just slow again):
-- drop and recreate it with the new constants.
-- tests/web/test_chairs_seating_index.py fails when the two drift.
--
-- Apply: .venv/bin/python scripts/apply_sql.py scripts/sql/036_deal_lots_live_seating_index.sql
-- (autocommit, and CONCURRENTLY does not block the deals crons' writes)

CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_deal_lots_live_seating_end
    ON deal_lots (end_utc)
    WHERE outcome_complete IS NOT TRUE
      AND (COALESCE(canonical_category = ANY ('{seating_furniture}'::text[]), false)
           OR COALESCE(title, '') ~* '\y(?:chair|chairs|seating|stool|stools|bench|benches|pew|pews|barstool|barstools|sofa|sofas|couch|couches|banquet)\y')
