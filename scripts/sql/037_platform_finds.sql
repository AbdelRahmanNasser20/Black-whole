-- 037_platform_finds.sql -- saved searches ("Finds") on platform_alerts.
--
-- STATUS: PENDING -- NOT applied to prod. Operator gate. Apply with
--   .venv/bin/python scripts/apply_sql.py scripts/sql/037_platform_finds.sql
-- then restart the web process (the code caches a positive column check per process).
--
-- WHAT: a platform alert becomes a saved search. One row = one set of /deals filters
-- (keyword, state, source, max price already exist) plus a name, a category, a minimum
-- price, a bid-count ceiling, an "ending within N hours" window, and the time the user
-- last opened it. `active` stays the alert switch: false = saved search with no email.
-- `keyword` may now be NULL (a search can be "every vehicle in GA").
--
-- Until this runs, automation/web/platform_finds.py stores the four old columns only
-- and needs a keyword (has_finds_columns()).
--
-- RLS: platform_alerts already has RLS + the service_role_all policy (030). Adding
-- columns does not change it. Idempotent, safe to re-run. No statement here contains
-- a semicolon inside a comment (apply_sql.py splits on them).

ALTER TABLE public.platform_alerts
    ADD COLUMN IF NOT EXISTS name            TEXT,
    ADD COLUMN IF NOT EXISTS category        TEXT,
    ADD COLUMN IF NOT EXISTS min_price       REAL,
    ADD COLUMN IF NOT EXISTS max_bids        INTEGER,
    ADD COLUMN IF NOT EXISTS ending_within   INTEGER,
    ADD COLUMN IF NOT EXISTS last_viewed_at  TIMESTAMPTZ;

ALTER TABLE public.platform_alerts ALTER COLUMN keyword DROP NOT NULL;
