-- 021 — Sales tab: freight quote follow-up, carrier price check, chair data per lot.
-- Status: APPLIED to prod 2026-10-02 (9 existing rows backfilled to status 'new'). Re-run with
--   .venv/bin/python scripts/apply_sql.py scripts/sql/021_sales_quotes.sql
-- Additive + idempotent. The code also runs on a database without it: quotes still save
-- through the old columns (phone rides in raw_response), an unquotable lane is
-- alerted but not stored, and the Sales > Quotes view says the migration is
-- missing (automation/freight_log.py::schema_ready).
--
-- `freight_quotes` is shared with the CRM (its 009_freight_quotes.sql created
-- it). Every new column is nullable or defaulted, so a CRM INSERT that does not
-- name them keeps working.
--
-- RLS: still disabled workspace-wide. This table now holds buyer phones as well
-- as emails — the open RLS gate in the workspace CLAUDE.md covers it.

-- ── 1. the buyer's phone (asked before the price, with ZIP + email) ─────────
ALTER TABLE public.freight_quotes
    ADD COLUMN IF NOT EXISTS buyer_phone TEXT;

-- ── 2. operator follow-up state ─────────────────────────────────────────────
-- DEFAULT 'new' backfills every existing row, so they all show in the inbox.
ALTER TABLE public.freight_quotes
    ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'new',
    ADD COLUMN IF NOT EXISTS status_changed_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS note TEXT;

ALTER TABLE public.freight_quotes
    DROP CONSTRAINT IF EXISTS freight_quotes_status_check;
ALTER TABLE public.freight_quotes
    ADD CONSTRAINT freight_quotes_status_check
    CHECK (status IN ('new', 'answered', 'won', 'lost', 'junk'));

-- ── 3. a request we could not price is still a request ──────────────────────
-- International / offshore / Alaska / bad ZIP / lot with no origin: the row is
-- kept with the reason and no price. Such a row has no mode and may have no
-- origin, so both stop being NOT NULL.
ALTER TABLE public.freight_quotes
    ADD COLUMN IF NOT EXISTS unquotable_reason TEXT,
    ADD COLUMN IF NOT EXISTS lot_quantity_remaining INTEGER;
ALTER TABLE public.freight_quotes ALTER COLUMN mode DROP NOT NULL;
ALTER TABLE public.freight_quotes ALTER COLUMN origin_zip DROP NOT NULL;

-- ── 4. carrier price check (operator-only, never shown to the buyer) ────────
-- A SUMMARY of Warp's multi-carrier answer: the cheapest price, who quoted it,
-- how many carriers answered, and at most 5 options. The full provider response
-- is never stored (the 500 MB rule: no unbounded provider blobs).
--   carrier_status: ok | none | too_big | error
ALTER TABLE public.freight_quotes
    ADD COLUMN IF NOT EXISTS carrier_status TEXT,
    ADD COLUMN IF NOT EXISTS carrier_low NUMERIC(10,2),
    ADD COLUMN IF NOT EXISTS carrier_name TEXT,
    ADD COLUMN IF NOT EXISTS carrier_count INTEGER,
    ADD COLUMN IF NOT EXISTS carrier_options JSONB,
    ADD COLUMN IF NOT EXISTS carrier_checked_at TIMESTAMPTZ;

-- ── 5. read path: "what have I not answered yet" ────────────────────────────
CREATE INDEX IF NOT EXISTS idx_freight_quotes_status
    ON public.freight_quotes (status, quoted_at DESC);

-- ── 6. chair data per lot ───────────────────────────────────────────────────
-- Operator-entered. NULL = use the standard (Idaho/Boise) chair in
-- automation/freight_estimate.py. `inventory.weight_lb` is left alone: its
-- meaning is not defined anywhere in the code.
ALTER TABLE public.inventory
    ADD COLUMN IF NOT EXISTS chair_weight_lb NUMERIC(6,2),
    ADD COLUMN IF NOT EXISTS chair_frame TEXT,
    ADD COLUMN IF NOT EXISTS chairs_per_pallet INTEGER,
    ADD COLUMN IF NOT EXISTS pallet_height_in INTEGER;

COMMENT ON COLUMN public.freight_quotes.status IS
    'Operator follow-up: new | answered | won | lost | junk.';
COMMENT ON COLUMN public.freight_quotes.carrier_options IS
    'At most 5 {carrier, price_usd, transit_days} entries from Warp market-options. Never the full response.';
COMMENT ON COLUMN public.inventory.chairs_per_pallet IS
    'Chairs on one LTL pallet (85 in height limit). NULL = standard chair default.';
