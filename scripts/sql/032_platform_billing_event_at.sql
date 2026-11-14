-- 032_platform_billing_event_at.sql -- ordering guard for subscription webhooks.
--
-- STATUS: applied to prod by the implementer of this task (operator-approved).
-- Re-apply with `.venv/bin/python scripts/apply_sql.py scripts/sql/032_platform_billing_event_at.sql`
--
-- WHAT: platform_users.billing_event_at holds the Stripe event time of the last
-- applied subscription event. Stripe does not guarantee delivery order, so an
-- older event must never overwrite a newer plan.
--
-- RLS: column-only change, platform_users already has RLS from 030.
-- Idempotent, safe to re-run.

ALTER TABLE public.platform_users ADD COLUMN IF NOT EXISTS billing_event_at TIMESTAMPTZ;
