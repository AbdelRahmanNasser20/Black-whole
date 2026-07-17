-- BLACKWHOLE-18 — staged DB updates (INV-002, Lockport IL 68 chairs)
-- STATUS: STAGED — NOT APPLIED. Review before running against Supabase `blackwhole`.
--
-- GUARDED REVIVE. The row is currently a sold_out stub (qty 0, no metadata).
-- Run ONLY after confirming the 68 chairs physically exist, AND after you've
-- set the true sell price + verified the chairs' color/material. Left commented
-- by default so nothing reactivates a genuinely-sold lot or publishes guesses.

BEGIN;

-- UPDATE inventory
-- SET title              = 'Stackable Event Chairs (Lot of 68)',
--     subtitle           = 'Padded stackable chairs, bulk lot.',   -- refine after eyeballing
--     city               = 'Lockport',
--     state              = 'IL',
--     zip_code           = '60441',
--     description        = 'Lot of 68 padded stackable chairs. Commercial event-grade, good gently-used condition. Great for churches, small event venues, and offices.',  -- refine color/material
--     price_per_chair    = 25.00,     -- CONFIRM: $1.79 is cost, not sell price
--     quantity_original  = 68,
--     quantity_remaining = 68,
--     status             = 'owned',
--     updated_at         = now()
-- WHERE trim(lot_id) = 'folder:Lot_of_68_Chairs_Lockport_68';

COMMIT;
-- ROLLBACK;

-- KS lot (BID-001 Manhattan KS): intentionally NO row / NO change — held pending
-- bid outcome. Do not create or list until the bid is won.
