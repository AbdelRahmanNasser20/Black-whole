-- BLACKWHOLE-10: new-inventory alert signups. Capture-only; the blast job
-- (matcher + email/SMS provider) is a follow-up ticket. This table is the
-- join surface for BWCRM-26 (CRM contact match on email/phone), so email and
-- phone stay as separate clean columns.
--
-- Applied to Supabase (project blackwhole / nihgzltpjriekyqqucbd) via MCP
-- apply_migration as `subscribers_table`. This file is the DDL of record —
-- schema is never created at runtime (see automation/inventory.py docstring).
--
-- RLS: intentionally disabled workspace-wide; all writes go through the
-- FastAPI server (postgres pooler role), same as inquiries. Do not add
-- anon-key policies here.
CREATE TABLE IF NOT EXISTS subscribers (
    id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name              text,
    email             text,
    phone             text,
    city              text,
    state             text,
    zip_code          text,
    quantity_wanted   integer,
    use_case          text,   -- church|event_venue|restaurant|wedding_rental|school|reseller|other
    chair_type        text,   -- banquet|chiavari|folding|stacking|any
    timeline          text,   -- asap|month|flexible
    budget_per_chair  text,   -- under_5|5_10|10_20|20_plus
    delivery          text,   -- delivery|pickup|either
    notes             text,
    source            text NOT NULL DEFAULT 'site_listings',
        -- site_listings | site_landing | site_detail | operator
    status            text NOT NULL DEFAULT 'new'
        CHECK (status IN ('new', 'contacted', 'matched', 'unsubscribed')),
    created_at        timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT subscribers_contact_present CHECK (email IS NOT NULL OR phone IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS subscribers_status_idx ON subscribers (status);
CREATE INDEX IF NOT EXISTS subscribers_email_idx  ON subscribers (lower(email));
CREATE INDEX IF NOT EXISTS subscribers_phone_idx  ON subscribers (phone);
