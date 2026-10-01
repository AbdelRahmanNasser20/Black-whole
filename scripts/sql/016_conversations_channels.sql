-- 016_conversations_channels.sql — multi-channel customer conversations
-- STATUS: APPLIED 2026-10-01 (scripts/apply_sql.py, operator-approved). DDL is
-- mirrored in the workspace docs/claude-reference/data-model.md §8i.
-- Comments must stay free of semicolons (apply_sql.py splits on them).
--
-- Plan: direction/tasks/2026-10-01/ebay-messaging-plan.md
--
-- The CRM tables contacts / messages / drafts already hold every conversation
-- (thread_url PK, platform column). This migration only widens them so eBay,
-- email and SMS threads can live beside Facebook, and adds a cross-channel
-- send ledger so MAX_SENDS_PER_DAY / MIN_SECONDS_BETWEEN_SENDS are enforced
-- and auditable per channel. No table is renamed, no row is rewritten.
--
-- thread_url convention per channel (the PK stays a text key):
--   facebook : https://www.facebook.com/messages/t/<tid>/   (unchanged)
--   ebay     : ebay:conv/<conversationId>                   (Message API)
--   email    : email:thread/<gmail thread id>
--   sms      : sms:+1XXXXXXXXXX
-- Idempotent: every statement is IF NOT EXISTS / guarded.

BEGIN;

-- 1. contacts: widen platform, add channel identity columns -------------------
ALTER TABLE public.contacts DROP CONSTRAINT IF EXISTS contacts_platform_check;
ALTER TABLE public.contacts
  ADD CONSTRAINT contacts_platform_check
  CHECK (platform IN ('facebook','ebay','craigslist','offerup','email','sms'));

ALTER TABLE public.contacts
  ADD COLUMN IF NOT EXISTS channel_external_id TEXT,      -- eBay conversationId / FB tid / Gmail thread id
  ADD COLUMN IF NOT EXISTS channel_handle      TEXT,      -- eBay username or immutable user id / email addr / E.164
  ADD COLUMN IF NOT EXISTS channel_listing_id  TEXT,      -- eBay item id (referenceId) / FB listing id
  ADD COLUMN IF NOT EXISTS last_synced_at      TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS contacts_platform_external_idx
  ON public.contacts (platform, channel_external_id)
  WHERE channel_external_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS contacts_channel_listing_idx
  ON public.contacts (platform, channel_listing_id)
  WHERE channel_listing_id IS NOT NULL;

-- 2. messages: attachments + direction already covered by sender ----------------
-- messages.message_id already exists (native id, partial-unique). eBay rows use
-- 'ebay:' || messageId so the Phase 1 poller dedupes on it.
ALTER TABLE public.messages
  ADD COLUMN IF NOT EXISTS media JSONB NOT NULL DEFAULT '[]'::jsonb;  -- [{name,type,url}] ≤ 5, capped by CHECK below
ALTER TABLE public.messages DROP CONSTRAINT IF EXISTS messages_media_len;
ALTER TABLE public.messages
  ADD CONSTRAINT messages_media_len CHECK (jsonb_array_length(media) <= 5);

-- 3. channel_sends: every outbound, every channel ------------------------------
-- One row per attempted send. The cap query is:
--   SELECT count(*) FROM channel_sends
--    WHERE channel=%s AND status='sent' AND sent_at >= date_trunc('day', now()) —
CREATE TABLE IF NOT EXISTS public.channel_sends (
  id            BIGSERIAL PRIMARY KEY,
  channel       TEXT NOT NULL CHECK (channel IN ('facebook','ebay','email','sms')),
  thread_url    TEXT NOT NULL REFERENCES public.contacts(thread_url)
                  ON UPDATE CASCADE ON DELETE CASCADE,
  body_sha256   TEXT NOT NULL,                              -- dedupe: same text to same thread within 24 h = refused
  sent_by       TEXT NOT NULL CHECK (sent_by IN ('operator','bot','skill')),
  approved_by   TEXT,                                       -- 'operator' for every bot/skill send (never NULL when sent_by<>'operator')
  draft_rule_id TEXT,                                       -- drafts.rule_id at approve time
  status        TEXT NOT NULL DEFAULT 'queued'
                  CHECK (status IN ('queued','sent','refused_cap','refused_spacing','failed')),
  external_message_id TEXT,                                 -- eBay messageId / Gmail message id
  error         TEXT CHECK (error IS NULL OR length(error) <= 500),
  queued_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  sent_at       TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS channel_sends_cap_idx
  ON public.channel_sends (channel, sent_at DESC) WHERE status = 'sent';
CREATE INDEX IF NOT EXISTS channel_sends_thread_idx
  ON public.channel_sends (thread_url, queued_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS channel_sends_external_idx
  ON public.channel_sends (channel, external_message_id)
  WHERE external_message_id IS NOT NULL;

-- 4. channel_sync_state: poll cursors per channel/account ----------------------
CREATE TABLE IF NOT EXISTS public.channel_sync_state (
  channel       TEXT NOT NULL CHECK (channel IN ('facebook','ebay','email','sms')),
  account       TEXT NOT NULL,                               -- eBay seller username / mailbox
  cursor        TEXT,                                        -- ISO start_time for getConversations
  last_poll_at  TIMESTAMPTZ,
  last_ok_at    TIMESTAMPTZ,
  last_error    TEXT CHECK (last_error IS NULL OR length(last_error) <= 500),
  PRIMARY KEY (channel, account)
);

-- 5. RLS mirrors 022: tables stay RLS-disabled until the workspace RLS gate ----
-- (service_role-only policies are staged in facebook_scraper_Claude/db/migrations/022_rls_policies.sql)

COMMIT;
