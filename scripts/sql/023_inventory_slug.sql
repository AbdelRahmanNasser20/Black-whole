-- 023_inventory_slug.sql — descriptive public URL slug per lot (SEO city pages + clean URLs, 2026-10-07).
-- STATUS: APPLIED to prod 2026-10-07 (backfill run, 45 lots). (Re-)apply with:
--   .venv/bin/python scripts/apply_sql.py scripts/sql/023_inventory_slug.sql
-- then backfill:
--   .venv/bin/python scripts/backfill_slugs.py --apply
--
-- `slug` is bounded (<= 80 chars, see automation/lot_urls.SLUG_MAX) and unique. NULL = no slug yet;
-- the storefront falls back to /listings/{lot_id}. Slugs never change once set — the id route 301s
-- to the slug so every link ever sent keeps resolving. The code checks for the column once per
-- process (inventory.has_slug_column) — restart the web process after applying.

ALTER TABLE public.inventory ADD COLUMN IF NOT EXISTS slug TEXT
  CHECK (slug IS NULL OR (length(slug) BETWEEN 1 AND 80 AND slug ~ '^[a-z0-9]+(-[a-z0-9]+)*$'));

CREATE UNIQUE INDEX IF NOT EXISTS inventory_slug_uniq ON public.inventory (slug) WHERE slug IS NOT NULL;
