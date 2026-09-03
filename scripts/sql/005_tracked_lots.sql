-- Tracking list — DDL of record (see deals/tracking.py for the why).
--
-- One row per GovDeals ASSET the operator wants followed through its close:
-- keyed by (asset_id, account_id), NOT by auction, because a lot that doesn't
-- sell is relisted under the same asset with a new auction_id and we want to
-- keep following it. `label` is the "list" — 'banquet chairs', 'favorites',
-- 'tables' — a free-text bucket rather than a separate lists table because a
-- lot only ever needs to be in one.
--
-- The bid HISTORY itself lives in deal_bid_observations (one row per observed
-- change of bid_count / current_bid / high_bidder). This table holds the
-- membership, the poll schedule, the latest live state, and the final result
-- once the auction closes.

CREATE TABLE IF NOT EXISTS tracked_lots (
  asset_id              INT NOT NULL,
  account_id            INT NOT NULL,
  auction_id            INT,                       -- resolved lazily, changes on relist
  label                 TEXT NOT NULL DEFAULT 'default',
  title                 TEXT,
  url                   TEXT,
  note                  TEXT,
  source                TEXT NOT NULL DEFAULT 'manual',   -- 'manual' | 'favorite'
  added_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
  -- latest live state (mirror of the newest bidbox read)
  end_utc               TIMESTAMPTZ,
  status                TEXT,
  bid_count             INT,
  current_bid           NUMERIC(12,2),
  currency_code         TEXT,
  high_bidder           BIGINT,
  high_bidder_username  TEXT,
  visitors              INT,
  hits                  INT,
  watcher_count         INT,
  -- poll schedule
  last_polled_at        TIMESTAMPTZ,
  next_poll_at          TIMESTAMPTZ,
  poll_error            TEXT,
  -- final result, stamped once when the auction closes
  closed_at             TIMESTAMPTZ,
  final_bid             NUMERIC(12,2),
  final_bid_count       INT,
  final_bidder          BIGINT,
  final_bidder_username TEXT,
  PRIMARY KEY (asset_id, account_id)
);

CREATE INDEX IF NOT EXISTS ix_tracked_lots_due
  ON tracked_lots (next_poll_at) WHERE closed_at IS NULL;
CREATE INDEX IF NOT EXISTS ix_tracked_lots_label ON tracked_lots (label);
