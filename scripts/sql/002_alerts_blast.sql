-- BLACKWHOLE-10: new-inventory alert BLAST job — supporting schema.
--
-- Follow-up to 001_subscribers.sql (the capture side). This migration adds the
-- pieces the blast job needs on top of the shipped `subscribers` table:
--   * per-subscriber match radius + an unsubscribe capability token,
--   * a `bounced` status for hard-bounce suppression,
--   * `alert_sends` — the send log + the dedupe invariant (never alert a
--     subscriber twice about the same lot),
--   * `inventory.alerted_at` (nightly-scan "already processed" marker) and
--     `ships_nationwide` (PRD §8 open-question column; harmless default false),
--   * RLS ENABLED with ZERO anon policies on the PII tables (PRD §6.2).
--
-- ⚠️  DO NOT auto-apply. Abdel applies this by hand to Supabase
--     (project blackwhole / nihgzltpjriekyqqucbd) once he's reviewed the PR,
--     e.g. Supabase MCP apply_migration as `alerts_blast` or psql against
--     BLACKWHOLE_DB_URL. Schema is never created at runtime.
--
-- Design note vs the PRD §3: the PRD sketched a richer 3-table model
-- (subscribers + subscriber_interests + alert_sends). The CAPTURE side that
-- actually shipped folded interests into flat columns on `subscribers`
-- (chair_type, quantity_wanted, city/state/zip). The blast job matches on those
-- columns, so `subscriber_interests` is intentionally NOT created here — adding
-- a dead child table the code never reads would be schema debt. Revisit if/when
-- multi-interest subscribers become real.

BEGIN;

-- ── subscribers: radius, unsubscribe token, bounced status ──────────────────
ALTER TABLE subscribers
    ADD COLUMN IF NOT EXISTS radius_miles       integer NOT NULL DEFAULT 100,
    ADD COLUMN IF NOT EXISTS unsubscribe_token  text,
    ADD COLUMN IF NOT EXISTS unsubscribed_at    timestamptz;

-- Backfill a urlsafe token for every existing row, then enforce presence going
-- forward. 24 random bytes → 32 base64 chars (the app generates these server
-- side on insert once the capture path is updated).
UPDATE subscribers
   SET unsubscribe_token = encode(gen_random_bytes(24), 'base64')
 WHERE unsubscribe_token IS NULL;

CREATE UNIQUE INDEX IF NOT EXISTS subscribers_unsub_token_idx
    ON subscribers (unsubscribe_token);

-- Widen the status CHECK to allow hard-bounce / invalid suppression states.
-- (Constraint name is Postgres' default for the 001 inline CHECK.)
ALTER TABLE subscribers DROP CONSTRAINT IF EXISTS subscribers_status_check;
ALTER TABLE subscribers
    ADD CONSTRAINT subscribers_status_check
    CHECK (status IN ('new', 'contacted', 'matched', 'unsubscribed', 'bounced', 'invalid'));

-- ── inventory: blast bookkeeping ────────────────────────────────────────────
ALTER TABLE inventory
    ADD COLUMN IF NOT EXISTS alerted_at        timestamptz,
    ADD COLUMN IF NOT EXISTS ships_nationwide  boolean NOT NULL DEFAULT false;

-- ── alert_sends: the send log + dedupe invariant ────────────────────────────
CREATE TABLE IF NOT EXISTS alert_sends (
    id                   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    blast_id             uuid   NOT NULL,               -- groups one lot's run
    subscriber_id        bigint NOT NULL REFERENCES subscribers(id) ON DELETE CASCADE,
    lot_id               text   NOT NULL REFERENCES inventory(lot_id) ON DELETE CASCADE,
    channel              text   NOT NULL CHECK (channel IN ('email', 'sms')),
    status               text   NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'sent', 'delivered', 'bounced', 'failed', 'suppressed')),
    provider_message_id  text,                          -- Resend/Twilio id
    match_reason         jsonb,                          -- {distance_miles, geo_precision, ...}
    error                text,
    sent_at              timestamptz,
    created_at           timestamptz NOT NULL DEFAULT now(),
    -- Dedupe invariant: a subscriber is alerted at most once per lot per
    -- channel, even across re-blasts / price edits. Re-alerting a restocked
    -- sold-out lot is a deliberate operator act (delete the row first).
    CONSTRAINT alert_sends_dedupe UNIQUE (subscriber_id, lot_id, channel)
);
CREATE INDEX IF NOT EXISTS alert_sends_lot_idx        ON alert_sends (lot_id);
CREATE INDEX IF NOT EXISTS alert_sends_subscriber_idx ON alert_sends (subscriber_id);
CREATE INDEX IF NOT EXISTS alert_sends_blast_idx      ON alert_sends (blast_id);

-- ── RLS: deny the public anon key any access to PII (PRD §6.2) ──────────────
-- The FastAPI server reaches these via the Postgres DSN (pooler role), which
-- BYPASSES RLS — so nothing the app does breaks. Enabling RLS with NO policies
-- means the public Supabase anon key (public by design) can never read/write
-- subscriber emails/phones or the send log via PostgREST. This is the cheapest
-- real boundary and doesn't wait on the workspace-wide RLS cleanup.
-- NOTE: ENABLE only, not FORCE. Plain ENABLE already denies the `anon` role
-- (it's neither table owner nor BYPASSRLS). FORCE would additionally apply RLS
-- to the table OWNER — a footgun if the app's DSN role turns out to own these
-- tables without BYPASSRLS. If you later confirm the app connects as a
-- BYPASSRLS role (Supabase `postgres`), FORCE is safe to add.
ALTER TABLE subscribers ENABLE ROW LEVEL SECURITY;
ALTER TABLE alert_sends ENABLE ROW LEVEL SECURITY;
-- (No CREATE POLICY statements on purpose — zero policies = deny all for any
--  role that isn't the owner or BYPASSRLS. Do NOT add anon policies here.)

COMMIT;
