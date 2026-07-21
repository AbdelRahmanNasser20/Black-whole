-- Sold-out social proof (2026-07-21).
-- STATUS: STAGED — NOT APPLIED. Review before running against Supabase `blackwhole`.
--
-- Adds `sold_at` (bounds the public "recently sold" window) and a new
-- `lost_sold_out` status: lots we bid on and LOST. Publicly they read
-- identically to `sold_out`; the separate value keeps the truth on the
-- back end. `lost` (unshown) is untouched.

BEGIN;

ALTER TABLE inventory ADD COLUMN IF NOT EXISTS sold_at timestamptz;

-- Extend the status CHECK to allow lost_sold_out. The constraint name may
-- differ — inspect it first:
--   SELECT conname FROM pg_constraint
--   WHERE conrelid = 'inventory'::regclass AND contype = 'c';
-- then drop/recreate with the full allowed set:
--
-- ALTER TABLE inventory DROP CONSTRAINT <status_check_name>;
-- ALTER TABLE inventory ADD CONSTRAINT <status_check_name>
--   CHECK (status IN ('draft','listed','hidden','sold_out','owned',
--                     'won_pickup','active_bid','lost','lost_sold_out'));

COMMIT;
-- ROLLBACK;
