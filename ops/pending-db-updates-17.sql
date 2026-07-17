-- BLACKWHOLE-17 — staged DB updates (INV-001, LA 242 orange-red banquet chairs)
-- STATUS: STAGED — NOT APPLIED. Review before running against Supabase `blackwhole`.
-- Guardrail: no live DB writes were performed by the ops-prep job.
--
-- Purpose: the row is otherwise ready (owned, 242 remaining, $25, 13 images) but
-- `description` is empty, so the lot page and the live catalog/facebook.csv feed
-- render blank prose. This backfills it with grounded copy.

BEGIN;

UPDATE inventory
SET description = 'Orange-red banquet chairs with ornate damask-pattern padded seats and backs on gold-tone metal frames. Commercial event-grade and stackable for easy storage and transport. Good, gently-used condition. Ideal for churches, event venues, banquet halls, and rental fleets.',
    updated_at  = now()
WHERE lot_id = 'folder:Orange_Red_Banquet_Chairs_Cypress_242'
  AND (description IS NULL OR description = '');

-- Verify one row affected, then:
COMMIT;
-- ROLLBACK;  -- use instead of COMMIT to test first
