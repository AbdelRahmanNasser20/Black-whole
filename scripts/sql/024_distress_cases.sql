-- 024_distress_cases.sql — bankruptcies + WARN closures of chair-heavy businesses (/distress, 2026-10-09).
-- STATUS: PENDING — apply with:
--   .venv/bin/python scripts/apply_sql.py scripts/sql/024_distress_cases.sql
-- then run the first sync:
--   .venv/bin/python -m deals.cli distress-sync --since 2026-09-01 --dry-run   (look first)
--   .venv/bin/python -m deals.cli distress-sync --since 2026-09-01
--
-- Key choice: `id BIGSERIAL` + UNIQUE (source, source_key), NOT docket_id as the PK. WARN rows
-- (laborcurrent record ids like "GA-2026-39c54ba7") have no docket, and a negative synthetic
-- docket_id would have made every join and every URL lie. `docket_id` stays a real nullable
-- column with a partial unique index, so a CourtListener row is still findable by it.
-- Upsert key = (source, source_key): deals/distress.py::upsert_sql.
--
-- Contact details (trustee, attorneys, parties) are OPERATOR-ONLY on the web: the public
-- /distress/api/* read model (automation/web/public_distress.py::PUBLIC_COLS) never selects
-- them; /api/distress/cases (auth-walled) does.
--
-- RLS: still disabled workspace-wide (open gate in the workspace CLAUDE.md).

CREATE TABLE IF NOT EXISTS public.distress_cases (
    id                 BIGSERIAL PRIMARY KEY,
    source             TEXT NOT NULL CHECK (source IN ('courtlistener', 'warn')),
    source_key         TEXT NOT NULL,              -- courtlistener: docket_id as text / warn: laborcurrent record id
    docket_id          BIGINT,                     -- CourtListener docket id (NULL for warn rows)
    case_name          TEXT,
    court_id           TEXT,                       -- e.g. txnb, ganb (state = first two letters)
    docket_number      TEXT,
    date_filed         DATE,                       -- warn: notice_date
    chapter            TEXT,                       -- '7' | '11' | NULL (warn)
    trustee            TEXT,
    debtor_type        TEXT CHECK (debtor_type IN ('org', 'person')),
    naics              TEXT,                       -- 2-6 digits as published (warn = 2-digit group)
    industry_tag       TEXT,                       -- hotel|resort|catering|event_venue|party_rental|church|restaurant|other
    city               TEXT,
    state              TEXT,
    zip_code           TEXT,                       -- courtlistener: parsed from the petition when readable
    lat                REAL,                       -- warn: from the feed / courtlistener: 3-digit ZIP centroid
    lng                REAL,
    parties            JSONB,                      -- ["Debtor LLC", "US Trustee", ...]
    attorneys          JSONB,                      -- [{"name": ..., "firm": ...}]
    petition_url       TEXT,                       -- courtlistener petition PDF or docket page / warn: source notice
    sale_noticed_at    DATE,                       -- earliest sale / 363 / auction / bid-procedures filing
    sale_url           TEXT,
    employees_affected INTEGER,                    -- warn only
    effective_date     DATE,                       -- warn only (closure date)
    first_seen_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at       TIMESTAMPTZ,
    raw                JSONB,
    UNIQUE (source, source_key)
);

-- raw: BOUNDED by code (deals/distress.py::compact_raw — no document snippets, <= 5 docs, <= 8 KB;
-- warn rows keep ~12 scalar fields). The 500 MB rule: a provider blob column needs an archival
-- path before it grows — this one is capped at write time, and if it ever needs more it goes
-- R2-cold like deal_lots.raw (deals/raw_archive.py), never wider in Postgres.
COMMENT ON COLUMN public.distress_cases.raw IS
    'Compact subset of the source record (deals/distress.py::compact_raw, <= 8 KB). Never the full response.';
COMMENT ON COLUMN public.distress_cases.debtor_type IS
    'Heuristic (deals/distress.py::debtor_type): org tokens in the case name, individual-only forms in the docket. A lead-list flag, not a legal determination.';

CREATE INDEX IF NOT EXISTS ix_distress_cases_filed    ON public.distress_cases (date_filed DESC);
CREATE INDEX IF NOT EXISTS ix_distress_cases_state    ON public.distress_cases (state);
CREATE INDEX IF NOT EXISTS ix_distress_cases_industry ON public.distress_cases (industry_tag);
CREATE INDEX IF NOT EXISTS ix_distress_cases_sale     ON public.distress_cases (sale_noticed_at DESC) WHERE sale_noticed_at IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS ux_distress_cases_docket ON public.distress_cases (docket_id) WHERE docket_id IS NOT NULL;

-- Sync bookkeeping (one row per source). saved_searches keeps last_run_at on its own table and
-- site_settings is a typed allowlist for business numbers, so neither fits a timestamp per source.
CREATE TABLE IF NOT EXISTS public.distress_sync_state (
    source      TEXT PRIMARY KEY,
    last_run_at TIMESTAMPTZ,
    last_since  DATE,
    note        TEXT
);
