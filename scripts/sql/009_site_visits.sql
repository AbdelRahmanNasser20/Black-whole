-- 009_site_visits.sql — first-party storefront page-view log (2026-09-13).
-- Paste into Supabase → SQL Editor once. Idempotent.
-- Read by automation/web/visits.py (insert per public page view, 90-day
-- retention, /api/visits/summary rollup). No PII: `visitor` is a salted
-- 16-char hash of (ip, user-agent, day); no raw IP or UA is stored.

CREATE TABLE IF NOT EXISTS site_visits (
  id            bigserial PRIMARY KEY,
  ts            timestamptz NOT NULL DEFAULT now(),
  path          text NOT NULL,
  lot_id        text,
  utm_source    text,
  utm_medium    text,
  utm_campaign  text,
  referer_host  text,
  visitor       char(16),
  country       char(2)
);

CREATE INDEX IF NOT EXISTS ix_site_visits_ts ON site_visits (ts DESC);
CREATE INDEX IF NOT EXISTS ix_site_visits_campaign_ts ON site_visits (utm_campaign, ts DESC);

COMMENT ON TABLE site_visits IS
  'Public storefront page views with UTM attribution (Apollo sequences tag links utm_source=apollo&utm_campaign=<metro>-churches). 90-day retention enforced by the app.';
