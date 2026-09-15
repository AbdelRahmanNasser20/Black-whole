-- Auction expiry sync (2026-09-15) — the watch list behind
-- `automation/auction_sync.py`. Plan: docs/superpowers/plans/2026-09-15-auction-expiry-sync.md
--
-- One row per inventory lot whose GovDeals auction we are following. Keyed by
-- lot_id, with (asset_id, account_id) unique on top, because a lot that does
-- not sell RELISTS under the same asset with a NEW auction id — the same
-- reason `tracked_lots` is keyed that way. `auction_id` is therefore the
-- auction we last SAW, not part of the identity: a change in it is the relist
-- signal itself.
--
-- Deliberately all bounded scalars. Supabase went read-only at 500 MB on
-- 2026-08-28, and the standing rule is that a new column holding a provider
-- response / description / any unbounded blob needs an archival path before it
-- ships. `reason` is capped at 200 chars and `poll_error` at 500 for that
-- reason — they hold "assetStatusCd=SOA", not the payload that said so.

CREATE TABLE IF NOT EXISTS inventory_auction_watch (
    lot_id             TEXT PRIMARY KEY REFERENCES inventory(lot_id) ON DELETE CASCADE,
    asset_id           BIGINT NOT NULL,
    account_id         BIGINT NOT NULL,
    -- The auction id last observed for this asset. NULL until the first
    -- successful poll (maestro serves 204 for purged assets).
    auction_id         BIGINT,
    -- 'live'    = the auction is running and the lot is on the channels.
    -- 'expired' = we flipped it fake-sold-out; keep polling for the relist.
    state              TEXT NOT NULL DEFAULT 'live' CHECK (state IN ('live', 'expired')),
    -- Why we last changed state, short and human: "assetStatusCd=SOA".
    reason             VARCHAR(200),
    end_utc            TIMESTAMPTZ,
    expired_at         TIMESTAMPTZ,
    relisted_at        TIMESTAMPTZ,
    last_seen_live_at  TIMESTAMPTZ,
    last_checked_at    TIMESTAMPTZ,
    -- Last poll failure. An unresolvable lot is never expired on; the error is
    -- recorded so the operator can see which lots the sync cannot decide.
    poll_error         VARCHAR(500),
    -- The inventory status the lot carried before we expired it, so a relist
    -- restores what was there instead of assuming 'active_bid'.
    prior_status       TEXT,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The asset is the real-world identity; two inventory rows must never claim
-- the same GovDeals asset or a relist would restore both.
CREATE UNIQUE INDEX IF NOT EXISTS ux_inventory_auction_watch_asset
    ON inventory_auction_watch (asset_id, account_id);

-- The sync's hot query: "which expired lots am I still watching?"
CREATE INDEX IF NOT EXISTS ix_inventory_auction_watch_state
    ON inventory_auction_watch (state);

COMMENT ON TABLE inventory_auction_watch IS
    'Auction-expiry sync: per-lot GovDeals auction state. state=expired rows stay '
    'polled so a relist under a new auction id can put the lot back on the site.';
