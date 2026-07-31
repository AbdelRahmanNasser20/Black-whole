-- Let the storefront log freight quotes into the CRM's `freight_quotes` table.
--
-- BACKGROUND. `public.freight_quotes` was created by the CRM repo's migration
-- `db/migrations/009_freight_quotes.sql` (branch BWCRM-19). It was written for
-- one caller: the Messenger bot, quoting a lane inside a conversation. So
-- `thread_url` is `NOT NULL` and FKs to `contacts(thread_url)`.
--
-- The storefront has no thread. A buyer typing a ZIP into `/listings/{lot_id}`
-- is anonymous — there is no contact row to point at, and inventing one to
-- satisfy the FK would poison the CRM's join surface (same reasoning as the
-- `deposits` table in 005). Hence: one shared append-only quote log, with a
-- `source` column saying who asked.
--
-- WHY ONE TABLE AND NOT TWO. Every interesting question about freight is a
-- lane question — "which lanes do people ask about", "what did we quote
-- Worcester last month", "does the estimator drift from what Warp says". Those
-- queries want one table. Splitting by caller would mean UNION-ing forever.
--
-- ADDITIVE + IDEMPOTENT. Every statement is safe to re-run and nothing here
-- changes what the CRM already writes: existing rows get `source='crm'` from
-- the DEFAULT, and the CRM's own INSERT (which never names `source`) keeps
-- landing as 'crm' without a code change. Dropping NOT NULL only widens what
-- is accepted — the CRM still always supplies a thread_url.
--
-- ORDER-INDEPENDENT vs. THE APP. Storefront quote logging is best-effort
-- (`automation/freight_log.py` swallows every DB error), so shipping the code
-- before this migration degrades to "estimates work, nothing is logged" rather
-- than breaking the endpoint. Deploy order does not matter.
--
-- NOTE ON THE TABLE'S EXISTENCE. This file only ALTERs. `public.freight_quotes`
-- was confirmed live in project nihgzltpjriekyqqucbd on 2026-07-31 (4 rows,
-- column set identical to the CRM's 009) — the CRM applied its migration to the
-- shared DB even though PR #43 never merged. If you're ever pointing this at a
-- fresh project, run the CRM's 009 first.
--
-- RLS: intentionally disabled, workspace-wide decision. All writes go through
-- a server holding the pooler `postgres` role. Do not add anon-key policies.
--
-- Applied to Supabase project nihgzltpjriekyqqucbd on: PENDING

-- ── 1. a storefront quote has no Messenger thread ───────────────────────────
ALTER TABLE public.freight_quotes ALTER COLUMN thread_url DROP NOT NULL;

-- ── 2. who asked for this quote ─────────────────────────────────────────────
-- DEFAULT 'crm' backfills every pre-existing row and keeps the CRM's INSERT
-- (which doesn't name the column) working untouched.
ALTER TABLE public.freight_quotes
    ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'crm';

-- Re-created rather than added, so re-running this file after the vocabulary
-- changes doesn't leave a stale constraint behind. (There is no
-- ADD CONSTRAINT IF NOT EXISTS in Postgres.)
ALTER TABLE public.freight_quotes
    DROP CONSTRAINT IF EXISTS freight_quotes_source_check;
ALTER TABLE public.freight_quotes
    ADD CONSTRAINT freight_quotes_source_check
    CHECK (source IN ('crm', 'storefront'));

-- ── 3. the storefront's only handle on the buyer ────────────────────────────
-- Two-step capture: the ZIP form logs the quote immediately (anonymous), and
-- an optional "email me this estimate" second step fills this in. A row with a
-- buyer_email is a hot lead; a row without one is still lane analytics.
ALTER TABLE public.freight_quotes
    ADD COLUMN IF NOT EXISTS buyer_email TEXT;

-- ── 4. read path ────────────────────────────────────────────────────────────
-- "the last N storefront quotes" is the query the admin/lead review runs; the
-- CRM's existing idx_freight_quotes_thread can't serve it (thread_url is NULL).
CREATE INDEX IF NOT EXISTS idx_freight_quotes_source
    ON public.freight_quotes (source, quoted_at DESC);

COMMENT ON COLUMN public.freight_quotes.source IS
    'Which surface produced this quote: ''crm'' = Messenger bot/agent (has a '
    'thread_url), ''storefront'' = anonymous ZIP form on /listings/{lot_id} '
    '(thread_url NULL, buyer_email optionally captured after the fact).';
COMMENT ON COLUMN public.freight_quotes.buyer_email IS
    'Storefront only: email captured by the optional post-estimate '
    '"email me this estimate" step. NULL = the buyer never gave one.';
