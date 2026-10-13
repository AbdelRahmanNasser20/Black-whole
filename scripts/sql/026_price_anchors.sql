-- 026_price_anchors.sql — other sellers' banquet-chair listings priced above ours (2026-10-10).
-- STATUS: APPLIED to prod 2026-10-10 via:
--   .venv/bin/python scripts/apply_sql.py scripts/sql/026_price_anchors.sql
-- (024 is reserved for distress cases, 025 for flip-score.)
--
-- One row per outside listing URL. Filled daily by `scripts/price_anchors.py run` (Render cron
-- `price-anchors`, Claude Haiku + web search). Used as "others ask $35-$55" price anchors next to
-- our own lots — NEVER as our stock. Photos are someone else's: the copy lives in the PRIVATE R2
-- bucket (image_r2_key), never a public URL.
--
-- Every text column is bounded (Supabase size rule): no provider responses, no descriptions.

CREATE TABLE IF NOT EXISTS public.price_anchors (
  id               BIGSERIAL PRIMARY KEY,
  listing_url      TEXT NOT NULL UNIQUE CHECK (length(listing_url) <= 600),
  title            TEXT NOT NULL CHECK (length(title) <= 200),
  seller           TEXT CHECK (length(seller) <= 120),
  brand            TEXT CHECK (length(brand) <= 80),
  condition        TEXT NOT NULL CHECK (condition IN ('used', 'refurb', 'new')),
  tier             TEXT NOT NULL CHECK (tier IN ('hotel', 'event', 'new')),
  source           TEXT NOT NULL CHECK (length(source) <= 40),
  qty              INTEGER CHECK (qty IS NULL OR qty > 0),
  price_per_chair  NUMERIC(10,2) NOT NULL CHECK (price_per_chair > 0),
  currency         TEXT NOT NULL DEFAULT 'USD' CHECK (length(currency) = 3),
  location         TEXT CHECK (length(location) <= 120),
  listed_on        TEXT CHECK (length(listed_on) <= 40),
  image_source_url TEXT CHECK (length(image_source_url) <= 600),
  image_r2_key     TEXT CHECK (length(image_r2_key) <= 200),
  note             TEXT CHECK (length(note) <= 300),
  status           TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'gone', 'hidden')),
  first_seen_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_seen_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  seen_count       INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS price_anchors_active_idx
  ON public.price_anchors (tier, price_per_chair) WHERE status = 'active';
