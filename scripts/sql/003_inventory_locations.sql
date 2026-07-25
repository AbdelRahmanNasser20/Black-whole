-- BLACKWHOLE-29 — a lot can sit in more than one place.
--
-- `city`/`state`/`zip_code` stay the PRIMARY location (every existing query,
-- the FB catalog feed and the CRM geo-router all read those columns, and none
-- of them should have to learn a new shape). `locations` is additive: the full
-- list of places the lot lives, rendered on the storefront as chips.
--
-- Shape: [{"city": "Baltimore", "state": "MD", "quantity": 1200}, ...]
--   - `city` is required, `state` and `quantity` are optional.
--   - NULL / absent means "single-location lot" — read city/state instead.
--
-- Applied to Supabase project `nihgzltpjriekyqqucbd` on 2026-07-25.

ALTER TABLE inventory ADD COLUMN IF NOT EXISTS locations JSONB;

COMMENT ON COLUMN inventory.locations IS
  'BLACKWHOLE-29: extra pickup locations for a multi-site lot. '
  '[{"city": str, "state": str?, "quantity": int?}, ...]. '
  'NULL = single-location lot (use city/state/zip_code).';
