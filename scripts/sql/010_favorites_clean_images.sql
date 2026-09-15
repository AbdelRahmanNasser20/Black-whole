-- 010_favorites_clean_images.sql — dewatermarked R2 copies of a favorite's GovDeals photos (2026-09-15).
-- Paste into Supabase → SQL Editor once. Idempotent. Written by automation/favorite_images.py,
-- read by automation/web/public_map.py. URLs only (bounded), no blobs.
ALTER TABLE auction_favorites
  ADD COLUMN IF NOT EXISTS clean_hero_url   text,
  ADD COLUMN IF NOT EXISTS clean_image_urls jsonb,
  ADD COLUMN IF NOT EXISTS clean_images_at  timestamptz;
COMMENT ON COLUMN auction_favorites.clean_hero_url IS
  'R2 URL of the dewatermarked cover photo. The raw image_url is never shown publicly.';
