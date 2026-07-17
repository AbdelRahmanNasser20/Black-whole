-- BLACKWHOLE-15 — staged DB updates (PHX lot 9006, Mauve Banquet Chairs)
-- STATUS: STAGED — NOT APPLIED. Review before running against Supabase `blackwhole`.
--
-- 1) Add the Phoenix zip so the lot page JSON-LD + SEO title carry city/zip
--    (Offer.availableAtOrFrom) — this is what ranks "banquet chairs phoenix az".
--    85004 = downtown Phoenix placeholder; replace with the real storage zip.
-- 2) Quantity reconcile is COMMENTED OUT — only you know whether 442 (DB) or
--    790 (ticket) is correct, and whether BID-003 is this lot or a new one.

BEGIN;

UPDATE inventory
SET zip_code   = '85004',   -- TODO: replace with actual Phoenix storage zip
    updated_at = now()
WHERE lot_id = '9006'
  AND (zip_code IS NULL OR zip_code = '');

-- OPTIONAL — uncomment ONLY after confirming the true remaining quantity:
-- UPDATE inventory
-- SET quantity_remaining = 790,     -- or the real figure
--     updated_at = now()
-- WHERE lot_id = '9006';

COMMIT;
-- ROLLBACK;
