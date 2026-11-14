-- 033_platform_sends_cascade.sql -- deleting a fired alert must not fail.
--
-- STATUS: applied to prod by the implementer of this task (operator-approved).
-- Re-apply with `.venv/bin/python scripts/apply_sql.py scripts/sql/033_platform_sends_cascade.sql`
--
-- WHAT: platform_alert_sends.alert_id referenced platform_alerts(id) with no
-- ON DELETE, so DELETE on an alert that had already sent mail raised a FK
-- violation (HTTP 500). Re-add the constraint with ON DELETE CASCADE so the
-- dedupe rows go with their alert.
--
-- RLS: constraint-only change, the table already has RLS from 030.
-- Idempotent, safe to re-run.

ALTER TABLE public.platform_alert_sends
    DROP CONSTRAINT IF EXISTS platform_alert_sends_alert_id_fkey;
ALTER TABLE public.platform_alert_sends
    ADD CONSTRAINT platform_alert_sends_alert_id_fkey
    FOREIGN KEY (alert_id) REFERENCES public.platform_alerts(id) ON DELETE CASCADE;
