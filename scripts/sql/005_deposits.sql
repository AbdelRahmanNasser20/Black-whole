-- Reserve with deposit (Stripe Checkout) — the money ledger + the rule that
-- sets the number + the per-lot override.
--
-- WHY A NEW TABLE AND NOT THE WORKSPACE `invoices` TABLE: `invoices` hangs off
-- `sales.thread_url`, i.e. it only exists for a deal that started as a
-- Messenger conversation. A storefront deposit has no thread — the buyer is a
-- stranger who found /listings/{lot_id} on Google. Forcing a fake thread_url to
-- satisfy that FK would corrupt the CRM's join surface. This follows the
-- precedent already set by `inquiries` and `subscribers`: the storefront owns
-- its own capture tables and the CRM joins on email/phone later if it wants to.
--
-- Shape rationale:
--   - One row per Checkout attempt, not per buyer. A buyer who abandons and
--     retries leaves two rows; `stripe_session_id UNIQUE` is what makes webhook
--     handling idempotent (replays and out-of-order events land on the same row).
--   - NO foreign key on `lot_id`. Money records outlive lots — deleting a sold
--     lot from the Inventory tab must never cascade away the payment history.
--   - Every amount is integer cents (`bigint`). Stripe speaks cents; float
--     dollars round wrong and `numeric` round-trips as Decimal in odd places.
--     `price_per_chair` stays numeric because it's a *snapshot of the quote
--     inputs*, not an amount we ever charge.
--   - `deposit_pct` / `deposit_min_cents` are a SNAPSHOT of the rule at
--     checkout time. The operator can edit the live rule any day; a deposit
--     taken in July must still explain its own arithmetic in October.
--   - `status` is a state machine enforced in `automation/deposits.py`
--     (`_TRANSITIONS`), not just a CHECK — the CHECK only bounds the vocabulary.
--
-- RLS: intentionally disabled, workspace-wide decision. Every write goes
-- through the FastAPI server on the pooler `postgres` role. Do NOT add
-- anon-key policies here.
--
-- NOTE ON THE FILE NUMBER: `004_listing_snapshots.sql` (BLACKWHOLE-28) also
-- carries the 004 prefix. These files are hand-applied in any order and the
-- number is documentation, not a sequencer — kept as 004 because the plan and
-- the sibling freight migration (005) were numbered against it.
--
-- Applied to Supabase project nihgzltpjriekyqqucbd on: PENDING

-- ── 1. the money ledger ─────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS deposits (
    id                   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    lot_id               text NOT NULL,          -- no FK: money outlives lots
    kind                 text NOT NULL DEFAULT 'deposit'
        CHECK (kind IN ('deposit', 'full')),
    -- quote inputs, snapshotted so the row explains its own arithmetic
    quantity             integer NOT NULL CHECK (quantity > 0),
    price_per_chair      numeric(10,2) NOT NULL,
    subtotal_cents       bigint NOT NULL CHECK (subtotal_cents > 0),
    amount_cents         bigint NOT NULL CHECK (amount_cents > 0),  -- charged now
    deposit_pct          numeric(5,4),           -- rule snapshot (NULL for kind='full')
    deposit_min_cents    bigint,                 -- rule snapshot
    currency             text NOT NULL DEFAULT 'usd',
    -- buyer
    buyer_name           text,
    buyer_email          text,
    buyer_phone          text,
    -- Stripe linkage
    stripe_session_id    text UNIQUE,            -- idempotency key for webhooks
    stripe_payment_intent text,                  -- how charge.refunded maps back
    payment_method       text,                   -- 'card' | 'us_bank_account'
    -- lifecycle
    status               text NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'processing', 'paid',
                          'failed', 'refunded', 'canceled')),
    failure_reason       text,
    admin_note           text,
    created_at           timestamptz NOT NULL DEFAULT now(),
    updated_at           timestamptz NOT NULL DEFAULT now(),
    paid_at              timestamptz,            -- stamped once, on first paid
    refunded_at          timestamptz             -- stamped once, on first refund
);
CREATE INDEX IF NOT EXISTS deposits_status_idx ON deposits (status);
CREATE INDEX IF NOT EXISTS deposits_lot_idx    ON deposits (lot_id);
CREATE INDEX IF NOT EXISTS deposits_pi_idx     ON deposits (stripe_payment_intent);

COMMENT ON TABLE deposits IS
  'One row per Stripe Checkout attempt for a storefront reservation. '
  'Deliberately not joined to the CRM invoices/sales tables — storefront '
  'buyers have no Messenger thread.';

-- ── 2. the editable rule ────────────────────────────────────────────────────
-- Live source of truth for the deposit rule. The env keys
-- DEPOSIT_PCT_DEFAULT / DEPOSIT_MIN_USD_DEFAULT are SEED defaults only: once a
-- row exists here, the admin Deposits tab owns the number. jsonb (not text) so
-- a future setting can hold a list/object without another migration.
CREATE TABLE IF NOT EXISTS site_settings (
    key         text PRIMARY KEY,
    value       jsonb NOT NULL,
    updated_at  timestamptz NOT NULL DEFAULT now()
);

INSERT INTO site_settings (key, value) VALUES
    ('deposit_pct',     '0.15'),   -- 15% of (qty × price/chair)
    ('deposit_min_usd', '200')     -- floor, in whole dollars
ON CONFLICT (key) DO NOTHING;

-- ── 3. per-lot override ─────────────────────────────────────────────────────
-- A big or unusually cheap lot can want a different %. The global minimum from
-- site_settings still applies on top of whatever this says.
ALTER TABLE inventory ADD COLUMN IF NOT EXISTS deposit_pct_override numeric(5,4)
    CHECK (deposit_pct_override IS NULL
           OR (deposit_pct_override > 0 AND deposit_pct_override <= 1));

COMMENT ON COLUMN inventory.deposit_pct_override IS
  'Per-lot deposit percentage (0 < x <= 1). NULL = use site_settings.deposit_pct. '
  'The site_settings deposit_min_usd floor still applies.';
