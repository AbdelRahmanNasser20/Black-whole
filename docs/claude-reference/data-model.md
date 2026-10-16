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
