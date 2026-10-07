-- 022 — first-touch lead attribution on every lead table (2026-10-07).
-- Status: APPLIED to prod 2026-10-07. (Re-)apply with
--   .venv/bin/python scripts/apply_sql.py scripts/sql/022_lead_attribution.sql
-- Additive + idempotent. Safe to re-run. Nothing here is NOT NULL, so the
-- CRM's own INSERTs into freight_quotes keep working untouched.
--
-- WHY. Cloudflare Web Analytics shows where visits come from; our tables show
-- leads. Nothing joined the two, so "did the SEO work / the Facebook posts /
-- the Google feed bring buyers who actually wrote in?" had no answer that did
-- not depend on a JS beacon a blocker can drop. site.js now stores the
-- visitor's FIRST-touch source (utm tags, referrer host, landing path) and
-- posts it with every lead. The server stores it on the row.
--
-- RAW, NOT BUCKETED. The columns hold what the browser saw
-- (attr_source='google', attr_referrer='m.facebook.com'); the channel label
-- (google_feed / facebook / direct ...) is derived at report time by
-- automation/attribution.py::channel(), so a classification fix needs no
-- backfill. Every value is clipped to 200 chars in code (varchar(200) here):
-- no unbounded blobs, per the 500 MB rule.
--
-- The code runs on a database without this file: automation/attribution.py
-- probes for `attr_landing` (the last column added) once per table per
-- process and omits the columns until it is there.
--
-- RLS: still disabled workspace-wide (open gate in the workspace CLAUDE.md).

ALTER TABLE public.inquiries
    ADD COLUMN IF NOT EXISTS attr_source   VARCHAR(200),
    ADD COLUMN IF NOT EXISTS attr_medium   VARCHAR(200),
    ADD COLUMN IF NOT EXISTS attr_campaign VARCHAR(200),
    ADD COLUMN IF NOT EXISTS attr_referrer VARCHAR(200),
    ADD COLUMN IF NOT EXISTS attr_landing  VARCHAR(200);

ALTER TABLE public.subscribers
    ADD COLUMN IF NOT EXISTS attr_source   VARCHAR(200),
    ADD COLUMN IF NOT EXISTS attr_medium   VARCHAR(200),
    ADD COLUMN IF NOT EXISTS attr_campaign VARCHAR(200),
    ADD COLUMN IF NOT EXISTS attr_referrer VARCHAR(200),
    ADD COLUMN IF NOT EXISTS attr_landing  VARCHAR(200);

ALTER TABLE public.freight_quotes
    ADD COLUMN IF NOT EXISTS attr_source   VARCHAR(200),
    ADD COLUMN IF NOT EXISTS attr_medium   VARCHAR(200),
    ADD COLUMN IF NOT EXISTS attr_campaign VARCHAR(200),
    ADD COLUMN IF NOT EXISTS attr_referrer VARCHAR(200),
    ADD COLUMN IF NOT EXISTS attr_landing  VARCHAR(200);

ALTER TABLE public.deposits
    ADD COLUMN IF NOT EXISTS attr_source   VARCHAR(200),
    ADD COLUMN IF NOT EXISTS attr_medium   VARCHAR(200),
    ADD COLUMN IF NOT EXISTS attr_campaign VARCHAR(200),
    ADD COLUMN IF NOT EXISTS attr_referrer VARCHAR(200),
    ADD COLUMN IF NOT EXISTS attr_landing  VARCHAR(200);

COMMENT ON COLUMN public.inquiries.attr_source IS
    'First-touch utm_source the browser stored on its first page view (raw). Channel label derived by automation/attribution.py::channel().';
COMMENT ON COLUMN public.inquiries.attr_referrer IS
    'First-touch referrer HOST (no path, no query). NULL = typed/bookmark/direct.';
COMMENT ON COLUMN public.inquiries.attr_landing IS
    'First-touch landing PATH on black-whole.com (no query string).';
