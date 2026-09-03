-- scripts/sql/006_research_profiles.sql
-- Research profiles: what the operator is hunting for on the auction sites.
-- One row per item family. Replaces the chair-only constants that lived in
-- auction_extractors/top_chairs.py, govdeals_chairs_extraction.py and
-- deals/cli.py. `chairs` is the default so an un-parameterised call keeps
-- today's behaviour. Apply by hand in Supabase; never at runtime.
BEGIN;

CREATE TABLE IF NOT EXISTS research_profiles (
  slug                 TEXT PRIMARY KEY,                     -- url/cli handle: [a-z0-9-]
  name                 TEXT NOT NULL,
  keywords             TEXT[] NOT NULL DEFAULT '{}',         -- title OR description ILIKE any
  exclude_terms        TEXT[] NOT NULL DEFAULT '{}',         -- title ILIKE any -> drop
  search_terms         TEXT[] NOT NULL DEFAULT '{}',         -- typed into the site search box by the scrapers
  native_category_ids  TEXT[] NOT NULL DEFAULT '{}',         -- GovDeals category codes for `deals discover`
  canonical_categories TEXT[] NOT NULL DEFAULT '{}',         -- optional narrowing on deal_lots.canonical_category
  min_quantity         INT NOT NULL DEFAULT 1,               -- auction_listings.quantity floor
  item_noun            TEXT NOT NULL DEFAULT 'units',        -- what the quantity LLM counts
  states               TEXT[] NOT NULL DEFAULT '{}',         -- USPS codes; empty = any (deal_lots only)
  min_price            NUMERIC(12,2),                        -- deal_lots.current_bid band (deal_lots only)
  max_price            NUMERIC(12,2),
  enabled              BOOLEAN NOT NULL DEFAULT true,
  is_default           BOOLEAN NOT NULL DEFAULT false,
  created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- exactly one default
CREATE UNIQUE INDEX IF NOT EXISTS ux_research_profiles_default
  ON research_profiles ((is_default)) WHERE is_default;

INSERT INTO research_profiles
  (slug, name, keywords, exclude_terms, search_terms, native_category_ids,
   canonical_categories, min_quantity, item_noun, is_default)
VALUES
  ('chairs', 'Banquet chairs',
   ARRAY['chair','banquet','stackable','seating'],
   ARRAY['scale','stool','ottoman','pouf','footrest','lumbar support','recliner',
         'filing cabinet','file cabinet','pillow','drafting chair',
         'chair cover','seat cover','chair cushion','seat cushion','chair mat',
         'dental','exam chair','treatment chair','procedure chair','phlebotomy',
         'wheelchair','wheel chair'],
   ARRAY['chairs','banquet chairs','stackable chairs','church chairs',
         'event chairs','conference chairs','folding chairs'],
   ARRAY['372','47B','47C','47A','46','47D','28E','266'],
   ARRAY[]::text[], 50, 'chairs', true),
  ('medical', 'Medical chairs & tables',
   ARRAY['dental','dentist','exam chair','examination chair','treatment chair',
         'procedure chair','phlebotomy','dialysis','geriatric','optometry',
         'ophthalmic','podiatry','tattoo','salon chair','barber chair',
         'exam table','examination table','treatment couch','stretcher','gurney',
         'dental cabinet','dental cart','midmark','ritter','pelton & crane',
         'pelton and crane','takara belmont','umf medical','clinton industries',
         'dexta','smr apex','lumex','dntlworks'],
   ARRAY[]::text[],
   ARRAY['dental chair','exam chair','treatment chair','phlebotomy chair',
         'procedure chair','exam table'],
   ARRAY['67','301'],
   ARRAY[]::text[], 1, 'chairs', false)
ON CONFLICT (slug) DO NOTHING;

COMMIT;
