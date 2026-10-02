-- 015_lot_archive_index.sql — index of the private GovDeals lot archive.
--
-- STATUS: APPLIED to prod 2026-09-30 (Supabase migration 015_lot_archive_index). Re-apply with:
--   .venv/bin/python scripts/apply_sql.py scripts/sql/015_lot_archive_index.sql
--
-- The archive itself is R2 (recorder/lot_archive.py): one gzip JSON document +
-- up to 6 photos per closed lot under archive/lots/govdeals/. This table is only
-- the searchable index the admin list page (/admin?tab=archive) and the
-- recorder's "not yet archived" check read. Until it exists both fall back to
-- listing R2 (`_meta/` sidecars, one LIST call per 1,000 lots), so nothing
-- breaks — the list page is just slower at thousands of lots.
--
-- Size: ~250 bytes a row, no blobs. Whole-site GovDeals closes ~125k lots a
-- month → ~30 MB/month. Supabase free tier goes read-only at 500 MB
-- (docs/claude-reference/database-size.md) and the database was 595 MB on
-- 2026-09-29 — reclaim space before applying, and prefer furniture scope until
-- there is headroom.
--
-- Rebuildable at any time from R2 (every row mirrors an _meta sidecar).

CREATE TABLE IF NOT EXISTS lot_archive (
    source              text        NOT NULL,
    lot_key             text        NOT NULL,          -- asset/account/auction
    title               text,
    canonical_category  text,
    category_name       text,
    city                text,
    state               text,
    seller              text,
    closed_at           timestamptz,
    final_price         numeric(12, 2),
    bid_count           integer,
    outcome             text,                          -- sold | reserve_not_met | no_bid | cancelled | unknown
    status_code         text,                          -- raw GovDeals assetStatusCd
    photo_count         smallint,
    completeness        text,                          -- full | partial
    archived_at         timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (source, lot_key)
);

CREATE INDEX IF NOT EXISTS ix_lot_archive_closed ON lot_archive (closed_at DESC);
CREATE INDEX IF NOT EXISTS ix_lot_archive_category ON lot_archive (canonical_category, closed_at DESC);

-- RLS is disabled workspace-wide (see workspace CLAUDE.md "RLS gate"); this
-- table holds nothing public-facing and follows the same rollout.
