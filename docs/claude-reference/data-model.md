# Data model (listing_automation additions)

## 2026-10-16 — platform tenancy (migration 030, `scripts/sql/030_platform_tenancy.sql`)

All four tables: RLS enabled + `service_role_all` policy (FOR ALL TO service_role).

```sql
CREATE TABLE public.platform_users (
  user_id UUID PRIMARY KEY,            -- Supabase auth uid
  email TEXT NOT NULL, phone TEXT, sms_consent BOOLEAN DEFAULT false,
  company TEXT, state TEXT, flips_what TEXT, biggest_pain TEXT,
  plan TEXT DEFAULT 'beta', onboarded_at TIMESTAMPTZ, last_seen_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ DEFAULT now()
);
CREATE TABLE public.platform_alerts (
  id BIGSERIAL PRIMARY KEY,
  user_id UUID NOT NULL REFERENCES platform_users(user_id),
  keyword TEXT NOT NULL, state_filter TEXT, source_filter TEXT, max_price REAL,
  active BOOLEAN DEFAULT true, last_fired_at TIMESTAMPTZ, created_at TIMESTAMPTZ DEFAULT now()
);  -- partial index platform_alerts_active_idx (active) WHERE active
CREATE TABLE public.platform_saved_lots (
  id BIGSERIAL PRIMARY KEY, user_id UUID NOT NULL, source TEXT NOT NULL, lot_key TEXT NOT NULL,
  created_at TIMESTAMPTZ DEFAULT now(), UNIQUE (user_id, source, lot_key)
);
CREATE TABLE public.platform_alert_sends (
  id BIGSERIAL PRIMARY KEY, alert_id BIGINT NOT NULL REFERENCES platform_alerts(id),
  source TEXT NOT NULL, lot_key TEXT NOT NULL, email_to TEXT NOT NULL,
  sent_at TIMESTAMPTZ DEFAULT now(), created_at TIMESTAMPTZ DEFAULT now(),
  UNIQUE (alert_id, source, lot_key)
);
```

## 2026-10-16 — platform subscriptions (migration 031, `scripts/sql/031_platform_subscriptions.sql`)

```sql
ALTER TABLE public.platform_users ADD COLUMN IF NOT EXISTS stripe_customer_id TEXT;
CREATE INDEX IF NOT EXISTS platform_users_stripe_customer_idx
  ON public.platform_users (stripe_customer_id) WHERE stripe_customer_id IS NOT NULL;
```
`customer.subscription.*` webhooks set `plan` to `pro` or `beta` by `stripe_customer_id`.

## 2026-10-16 — subscription event ordering (migration 032, `scripts/sql/032_platform_billing_event_at.sql`)

```sql
ALTER TABLE public.platform_users ADD COLUMN IF NOT EXISTS billing_event_at TIMESTAMPTZ;
```
Stripe event `created` of the last applied subscription event. Older events are ignored.

## 2026-10-16 — alert sends cascade (migration 033, `scripts/sql/033_platform_sends_cascade.sql`)

```sql
ALTER TABLE public.platform_alert_sends DROP CONSTRAINT IF EXISTS platform_alert_sends_alert_id_fkey;
ALTER TABLE public.platform_alert_sends ADD CONSTRAINT platform_alert_sends_alert_id_fkey
  FOREIGN KEY (alert_id) REFERENCES public.platform_alerts(id) ON DELETE CASCADE;
```
Deleting an alert that already sent mail now removes its dedupe rows instead of failing with a FK error.

## `chat_sessions` (migration 038, PENDING)

Storefront chat sessions. Writer and reader: `automation/site_chat_store.py` only. RLS on, `service_role_all` policy.

```sql
CREATE TABLE public.chat_sessions (
    id UUID PRIMARY KEY, created_at TIMESTAMPTZ, updated_at TIMESTAMPTZ, completed_at TIMESTAMPTZ,
    status TEXT CHECK (status IN ('open','completed')), step TEXT, answered INTEGER,
    state JSONB,                       -- script state: answers, question and answer log, picked lots (bounded)
    name TEXT, phone TEXT, email TEXT, zip_code VARCHAR(10), quantity INTEGER, lot_ids TEXT[], inquiry_id BIGINT,
    visitor_id VARCHAR(40), first_seen TIMESTAMPTZ, visit_days INTEGER, page VARCHAR(200), trail JSONB,
    device VARCHAR(20), country CHAR(2),
    attr_source VARCHAR(200), attr_medium VARCHAR(200), attr_campaign VARCHAR(200),
    attr_referrer VARCHAR(200), attr_landing VARCHAR(200)
);
ALTER TABLE public.inventory ADD COLUMN pickup_address TEXT;   -- street address the chat may show a buyer
```
