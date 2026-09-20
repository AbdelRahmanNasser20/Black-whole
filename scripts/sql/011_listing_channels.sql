-- 011_listing_channels.sql — one row per lot × sales channel (multichannel master plan, Phase 1).
-- Status: PENDING — paste into Supabase → SQL Editor once. Idempotent.
--
-- `inventory` stays the single source of truth for WHAT we sell; this table only
-- records WHERE each lot currently is (state, far-side id/url, payload hash, last
-- error). Written by automation/channels/store.py, driven by
-- automation/channels/sync.py, read by the admin Channels tab. Bounded columns
-- only (ids, urls, a sha1, one error line) — no blobs, so no archival path needed.
--
-- The CHECK vocabularies mirror automation/channels/__init__.py (CHANNELS, STATES).
-- Adding a channel = edit both in the same commit.

CREATE TABLE IF NOT EXISTS listing_channels (
  id             bigserial PRIMARY KEY,
  lot_id         text NOT NULL REFERENCES inventory(lot_id) ON DELETE CASCADE,
  channel        text NOT NULL CHECK (channel IN ('site','fb_catalog','google','ebay','fb_marketplace','craigslist')),
  state          text NOT NULL DEFAULT 'off' CHECK (state IN ('off','queued','pending_approval','live','delisted','error')),
  external_id    text,
  url            text,
  payload_hash   text,
  last_synced_at timestamptz,
  last_error     text,
  approved_at    timestamptz,
  created_at     timestamptz NOT NULL DEFAULT now(),
  updated_at     timestamptz NOT NULL DEFAULT now(),
  UNIQUE (lot_id, channel)
);
CREATE INDEX IF NOT EXISTS listing_channels_state_idx ON listing_channels (channel, state);

-- Channel switches. 0/1 ints so site_settings' int coercion applies.
-- FB Marketplace and Craigslist ship OFF: only the operator flips them
-- (admin Channels tab). Feed channels ship ON — they are pull-based CSVs
-- that already exist today.
INSERT INTO site_settings (key, value) VALUES
  ('channels_master_enabled', '1'),
  ('channel_site_enabled', '1'),
  ('channel_fb_catalog_enabled', '1'),
  ('channel_google_enabled', '1'),
  ('channel_ebay_enabled', '0'),
  ('channel_fb_marketplace_enabled', '0'),
  ('channel_craigslist_enabled', '0'),
  ('browser_channel_daily_cap', '4'),
  ('browser_channel_spacing_s', '1200')
ON CONFLICT (key) DO NOTHING;
