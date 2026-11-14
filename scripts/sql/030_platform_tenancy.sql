-- 030_platform_tenancy.sql -- tenancy tables for the liquidator platform
-- (signup, saved lots, keyword alerts, alert send log).
--
-- STATUS: applied to prod by the implementer of this task (operator-approved).
-- Re-apply with `.venv/bin/python scripts/apply_sql.py scripts/sql/030_platform_tenancy.sql`
--
-- WHAT: four tables. platform_users.user_id is the Supabase auth uid.
-- platform_alerts are per-user keyword watches. platform_saved_lots are a
-- user's starred lots. platform_alert_sends dedupes alert emails so one
-- (alert, lot) pair is only ever sent once.
--
-- RLS: each table enables Row Level Security plus one `service_role_all`
-- policy, the rule from 029_rls_service_role.sql. No FORCE, the postgres owner
-- connection bypasses RLS. Idempotent, safe to re-run.

CREATE TABLE IF NOT EXISTS public.platform_users (
    user_id       UUID PRIMARY KEY,
    email         TEXT NOT NULL,
    phone         TEXT,
    sms_consent   BOOLEAN DEFAULT false,
    company       TEXT,
    state         TEXT,
    flips_what    TEXT,
    biggest_pain  TEXT,
    plan          TEXT DEFAULT 'beta',
    onboarded_at  TIMESTAMPTZ,
    last_seen_at  TIMESTAMPTZ,
    created_at    TIMESTAMPTZ DEFAULT now()
);
ALTER TABLE public.platform_users ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS service_role_all ON public.platform_users;
CREATE POLICY service_role_all ON public.platform_users
    FOR ALL TO service_role USING (true) WITH CHECK (true);

CREATE TABLE IF NOT EXISTS public.platform_alerts (
    id             BIGSERIAL PRIMARY KEY,
    user_id        UUID NOT NULL REFERENCES public.platform_users(user_id),
    keyword        TEXT NOT NULL,
    state_filter   TEXT,
    source_filter  TEXT,
    max_price      REAL,
    active         BOOLEAN DEFAULT true,
    last_fired_at  TIMESTAMPTZ,
    created_at     TIMESTAMPTZ DEFAULT now()
);
ALTER TABLE public.platform_alerts ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS service_role_all ON public.platform_alerts;
CREATE POLICY service_role_all ON public.platform_alerts
    FOR ALL TO service_role USING (true) WITH CHECK (true);
CREATE INDEX IF NOT EXISTS platform_alerts_active_idx
    ON public.platform_alerts (active) WHERE active;

CREATE TABLE IF NOT EXISTS public.platform_saved_lots (
    id          BIGSERIAL PRIMARY KEY,
    user_id     UUID NOT NULL,
    source      TEXT NOT NULL,
    lot_key     TEXT NOT NULL,
    created_at  TIMESTAMPTZ DEFAULT now(),
    UNIQUE (user_id, source, lot_key)
);
ALTER TABLE public.platform_saved_lots ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS service_role_all ON public.platform_saved_lots;
CREATE POLICY service_role_all ON public.platform_saved_lots
    FOR ALL TO service_role USING (true) WITH CHECK (true);

CREATE TABLE IF NOT EXISTS public.platform_alert_sends (
    id          BIGSERIAL PRIMARY KEY,
    alert_id    BIGINT NOT NULL REFERENCES public.platform_alerts(id),
    source      TEXT NOT NULL,
    lot_key     TEXT NOT NULL,
    email_to    TEXT NOT NULL,
    sent_at     TIMESTAMPTZ DEFAULT now(),
    created_at  TIMESTAMPTZ DEFAULT now(),
    UNIQUE (alert_id, source, lot_key)
);
ALTER TABLE public.platform_alert_sends ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS service_role_all ON public.platform_alert_sends;
CREATE POLICY service_role_all ON public.platform_alert_sends
    FOR ALL TO service_role USING (true) WITH CHECK (true);
