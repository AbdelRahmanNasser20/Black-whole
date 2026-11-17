-- 038_chat_sessions.sql -- storefront chat sessions, and the street address a buyer may see.
--
-- STATUS: PENDING -- NOT applied to prod. Operator gate.
-- Apply with `.venv/bin/python scripts/apply_sql.py scripts/sql/038_chat_sessions.sql`
-- then restart the web process (the "schema ready" answer is cached per process).
-- Until it is applied the chat widget does not render and /chat/* answers 503.
--
-- WHAT
--   1. `chat_sessions`: one row per chat a visitor opened on the storefront. `state` is the
--      question script state (answers, question and answer log, picked lots). It is bounded by
--      the code: at most 40 log entries of 300 characters each, so a row stays under about 20 kB.
--      `trail` is the visitor's last 30 page views as the browser reported them.
--   2. `inventory.pickup_address`: the street address the chat may show a buyer for a lot.
--      Operator-entered. It is the ONLY address source for the chat. `storage_note` is never
--      read, because it holds gate codes and unit numbers.
--   3. Seeds `pickup_address` for the two warehouse lots, with the addresses the operator
--      already gives buyers in the Messenger question form (2026-11-16).
--
-- RLS: the table enables Row Level Security plus one `service_role_all` policy, the rule from
-- 029_rls_service_role.sql. No FORCE, the postgres owner connection bypasses RLS.
-- Idempotent, safe to re-run. No statement here contains a semicolon inside a comment or a string.

CREATE TABLE IF NOT EXISTS public.chat_sessions (
    id            UUID PRIMARY KEY,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at  TIMESTAMPTZ,
    status        TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'completed')),
    step          TEXT NOT NULL DEFAULT 'zip',
    answered      INTEGER NOT NULL DEFAULT 0,
    state         JSONB NOT NULL DEFAULT '{}'::jsonb,
    name          TEXT,
    phone         TEXT,
    email         TEXT,
    zip_code      VARCHAR(10),
    quantity      INTEGER,
    lot_ids       TEXT[],
    inquiry_id    BIGINT,
    visitor_id    VARCHAR(40),
    first_seen    TIMESTAMPTZ,
    visit_days    INTEGER,
    page          VARCHAR(200),
    trail         JSONB,
    device        VARCHAR(20),
    country       CHAR(2),
    attr_source   VARCHAR(200),
    attr_medium   VARCHAR(200),
    attr_campaign VARCHAR(200),
    attr_referrer VARCHAR(200),
    attr_landing  VARCHAR(200)
);
ALTER TABLE public.chat_sessions ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS service_role_all ON public.chat_sessions;
CREATE POLICY service_role_all ON public.chat_sessions
    FOR ALL TO service_role USING (true) WITH CHECK (true);
CREATE INDEX IF NOT EXISTS chat_sessions_created_idx
    ON public.chat_sessions (created_at DESC);
CREATE INDEX IF NOT EXISTS chat_sessions_visitor_idx
    ON public.chat_sessions (visitor_id) WHERE visitor_id IS NOT NULL;

ALTER TABLE public.inventory ADD COLUMN IF NOT EXISTS pickup_address TEXT;

UPDATE public.inventory SET pickup_address = '675 Great SW Pkwy SW, Atlanta, GA 30336'
    WHERE lot_id = '31225-atl' AND pickup_address IS NULL;
UPDATE public.inventory SET pickup_address = '10199 W Van Buren St, Tolleson, AZ 85353'
    WHERE lot_id = '31225' AND pickup_address IS NULL;
