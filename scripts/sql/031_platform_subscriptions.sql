-- 031_platform_subscriptions.sql -- Stripe customer link for the liquidator
-- platform subscription (dark-shipped behind PLATFORM_BILLING_ENABLED).
--
-- STATUS: applied to prod by the implementer of this task (operator-approved).
-- Re-apply with `.venv/bin/python scripts/apply_sql.py scripts/sql/031_platform_subscriptions.sql`
--
-- WHAT: one column on platform_users. The subscription webhook finds the user
-- by stripe_customer_id and flips plan between pro and beta.
--
-- RLS: column-only change, platform_users already has RLS and its
-- service_role_all policy from 030. Idempotent, safe to re-run.

ALTER TABLE public.platform_users ADD COLUMN IF NOT EXISTS stripe_customer_id TEXT;
CREATE INDEX IF NOT EXISTS platform_users_stripe_customer_idx
    ON public.platform_users (stripe_customer_id) WHERE stripe_customer_id IS NOT NULL;
