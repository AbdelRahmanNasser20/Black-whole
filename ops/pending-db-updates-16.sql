-- BLACKWHOLE-16 — staged DB updates (Atlanta warehouse, 2 lots)
-- STATUS: STAGED — NOT APPLIED. Review before running against Supabase `blackwhole`.

BEGIN;

-- 1) Lot A (399/100 grey-blue) — backfill the empty description so the lot page
--    + live catalog/facebook.csv feed read well. This lot is already owned with
--    stock, so it will appear in the feed automatically once description is set.
UPDATE inventory
SET description = 'Denim-blue banquet chairs with padded upholstered seats and backs on silver/chrome metal frames. Commercial event-grade and stackable for easy storage and transport. Good, gently-used condition. Ideal for churches, event venues, banquet halls, and rental companies.',
    updated_at  = now()
WHERE trim(lot_id) = 'folder:ATL_Grey_blueish_chairs_399'
  AND (description IS NULL OR description = '');

-- OPTIONAL — uncomment to correct Lot A remaining quantity if 100 is wrong:
-- UPDATE inventory SET quantity_remaining = 399, updated_at = now()
-- WHERE trim(lot_id) = 'folder:ATL_Grey_blueish_chairs_399';


-- 2) Lot B (189 blue/silver) — GUARDED REVIVE.
--    The row is currently a sold_out stub (qty 0, no metadata). ONLY run this
--    block after you have CONFIRMED the ~189 chairs physically exist in the
--    Atlanta warehouse. Note the trailing space in the lot_id — trim() handles it.
--    Leaving it commented by default so nothing reactivates a genuinely-sold lot.
--
-- UPDATE inventory
-- SET title              = 'Blue Banquet Chairs w/ Silver Frame (ATL)',
--     subtitle           = 'Blue upholstery on shiny silver/chrome frames.',
--     city               = 'Atlanta',
--     state              = 'GA',
--     zip_code           = '30318',
--     description        = 'Blue padded banquet chairs on a shiny silver/chrome metal frame. Commercial-grade stackable event seating, great for weddings, churches, and conference halls. Good used condition; some may need cleaning or new floor glides.',
--     price_per_chair    = 25.00,
--     quantity_original  = 189,
--     quantity_remaining = 189,
--     status             = 'owned',
--     updated_at         = now()
-- WHERE trim(lot_id) = 'folder:ATL_Blue_banquet_silver_frame_189';

COMMIT;
-- ROLLBACK;
