-- Sold-out social-proof data (2026-07-21).
-- STATUS: STAGED — NOT APPLIED. Requires ops/pending-db-updates-sold-social-proof.sql first
-- (needs the sold_at column + lost_sold_out status value).
-- Review each row before running against Supabase `blackwhole`.
--
-- Operator: replace now() with the true auction-close date per lot so the
-- 90-day "recently sold" window and the sort order are accurate.

BEGIN;

-- Lost at auction — we never owned these. lost_sold_out shows "SOLD OUT"
-- publicly but is flagged internally as a loss.
UPDATE inventory SET status = 'lost_sold_out', sold_at = now(), updated_at = now()
  WHERE lot_id = '5003';    -- Fresno, CA — Burgundy Vinyl Banquet Chairs (700)
UPDATE inventory SET status = 'lost_sold_out', sold_at = now(), updated_at = now()
  WHERE lot_id = '28505';   -- Fort Sill, OK — Saffron Stacking Dining Chairs (250)
UPDATE inventory SET status = 'lost_sold_out', sold_at = now(), updated_at = now()
  WHERE lot_id = '334';     -- Wilmington, NC — Tan Banquet Chairs (500)

-- Genuinely sold — already status='sold_out' but with 360 qty_remaining, so it
-- was rendering "AVAILABLE". Stamp a sold_at so it enters the recently-sold
-- window and reads SOLD OUT. (Confirm whether the 360 is stale before running.)
UPDATE inventory SET sold_at = now(), updated_at = now()
  WHERE lot_id = '7126';    -- North Miami, FL — Red & Gold Banquet Chairs (360)

COMMIT;
-- ROLLBACK;
