-- Rival-bidder intelligence — DDL of record (see deals/bidders.py for the why).
--
-- One row per OBSERVED CHANGE of (bid_count, current_bid, high_bidder) on a
-- GovDeals lot. GovDeals publishes no bid history, so this table *is* the
-- history: whatever we don't sample while the auction is live is lost.
--
-- Kept separate from `deal_snapshots` on purpose. Snapshots answer "is this lot
-- still cheap?" and are written by the watcher on its own lane schedule;
-- observations answer "who keeps beating us?" and are worth polling harder on
-- the handful of lots we actually want. Merging them would force one cadence
-- on both questions.

CREATE TABLE IF NOT EXISTS deal_bid_observations (
  id                   BIGSERIAL PRIMARY KEY,
  asset_id             INT NOT NULL,
  account_id           INT NOT NULL,
  auction_id           INT NOT NULL,
  observed_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  bid_count            INT,
  current_bid          NUMERIC(12,2),
  currency_code        TEXT,
  -- GovDeals' internal bidder id. Stable across lots and across auctions, which
  -- is the whole point: it's the join key that turns scattered sightings into a
  -- rival profile. NULL when the lot has no bids (the API sends 0 there).
  high_bidder          BIGINT,
  -- Masked handle, e.g. 'ja*****'. Stored verbatim — the asterisks encode the
  -- real handle's length and are the only length signal GovDeals gives us.
  high_bidder_username TEXT,
  bid_increment        NUMERIC(12,2),
  -- Competition signals, only ever exposed here (never in the search feed).
  visitors             INT,
  hits                 INT,
  watcher_count        INT,
  end_utc              TIMESTAMPTZ,
  status               TEXT
);

CREATE INDEX IF NOT EXISTS ix_deal_bid_obs_key
  ON deal_bid_observations (asset_id, account_id, auction_id, observed_at DESC);
CREATE INDEX IF NOT EXISTS ix_deal_bid_obs_bidder
  ON deal_bid_observations (high_bidder) WHERE high_bidder IS NOT NULL;

-- Rival roll-up: one row per bidder id we've ever seen leading a lot.
-- `lots_led` counts distinct auctions, not observations, so a bidder who sat on
-- one lot for a week doesn't outrank one who fought us across ten.
CREATE OR REPLACE VIEW deal_bidder_rivals AS
SELECT
  o.high_bidder                                              AS bidder_id,
  max(o.high_bidder_username)                                AS handle,
  count(DISTINCT (o.asset_id, o.account_id, o.auction_id))    AS lots_led,
  count(*)                                                   AS observations,
  min(o.observed_at)                                         AS first_seen,
  max(o.observed_at)                                         AS last_seen,
  max(o.current_bid)                                         AS highest_bid_led,
  array_agg(DISTINCT l.state)      FILTER (WHERE l.state IS NOT NULL)               AS states,
  array_agg(DISTINCT l.canonical_category)
                                   FILTER (WHERE l.canonical_category IS NOT NULL)  AS categories,
  count(DISTINCT (o.asset_id, o.account_id, o.auction_id))
    FILTER (WHERE l.outcome_complete AND l.high_bidder = o.high_bidder)             AS lots_won
FROM deal_bid_observations o
LEFT JOIN deal_lots l
  ON l.asset_id = o.asset_id AND l.account_id = o.account_id AND l.auction_id = o.auction_id
WHERE o.high_bidder IS NOT NULL
GROUP BY o.high_bidder;
