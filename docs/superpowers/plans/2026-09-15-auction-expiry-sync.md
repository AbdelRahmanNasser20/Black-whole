# Auction expiry → fake-sold-out sync (2026-09-15)

**Summary.** A lot we are bidding on (`inventory.status = 'active_bid'`) stays on
black-whole.com after its GovDeals auction closes, so buyers ask about stock we
never won. This adds a 30-minute in-process loop that asks GovDeals whether each
such lot's auction is still live, takes the dead ones off the channels the way
`lot_channels.remove_lot` already does (`fake_sold_out = true` + a sold status),
and keeps watching them by `(asset_id, account_id)` so a relist puts the lot back
on the site and pings Telegram.

- **Operator ask (verbatim intent):** "ensure that when auctions expire these
  listings get marked fake sold out unless those auctions come live again; if we
  detect them coming back it should notify using Telegram and repost them on my
  site."
- **Where it runs:** a sibling of `_tracking_loop` inside the web process. No
  Render cron (the blueprint has not been re-applied since 2026-07-18 — see
  `docs/claude-reference/deals.md`).
- **Manual path:** `.venv/bin/python scripts/sync_auction_status.py --once
  [--dry-run]`.

---

## 1. What "expired" and "live" mean here

<details open>

- **Source of truth is `assetStatusCd`, not the clock.** `STA` = still accepting
  bids; anything else (`SOA`, `SOL`, `CLO`, `CAN`…) is over. Matching on "not
  STA" rather than enumerating closed codes means an unfamiliar code errs toward
  closed — the same call `deals/tracking.py` already makes, so this reuses
  `tracking.is_closed()` / `LIVE_STATUS` / `CLOSE_GRACE` verbatim instead of
  re-deciding it.
- **The clock alone is never enough.** GovDeals extends a close on a late bid;
  `CLOSE_GRACE` (15 min) absorbs that.
- **Two calls per lot per pass:**
  1. `GovDealsAdapter.fetch_detail(asset_id, account_id)` → the *current*
     `auctionId` for the asset. This is the only endpoint that answers "which
     auction is this asset in right now", which is exactly what relist detection
     needs.
  2. `GovDealsAdapter.fetch_bid_state(asset, account, auction)` → `assetStatusCd`
     **and** `assetAuctionEndDateUTC`.
- **Why not read the close time off `fetch_detail`?** It returns
  `assetAuctionEndDate` with no timezone, and it is US/Eastern, not UTC
  (verified live 2026-09-15: asset 420/9312 detail says `2026-09-17T17:00:05`,
  bidbox says `2026-09-17T21:00:05Z`). Reading it as UTC would put every close
  4-5 hours late. The bidbox is the only endpoint that states the zone.
- **Unknown is a third answer, and it never acts.** Maestro serves HTTP 204 with
  an empty body for an asset it has purged (verified: `53677/357`, both id
  orders). An empty detail, an empty bidbox, or an unparseable payload records a
  `poll_error` and leaves the lot exactly as it was. Flipping a live lot to SOLD
  because an endpoint blinked is the one unrecoverable failure here.

</details>

## 2. URL parsing

<details open>

- `/en/asset/{assetId}/{accountId}` — **asset FIRST**. The repo's own CLAUDE.md
  documents it backwards; swapped ids give a silent HTTP 204, not an error.
- Reuse `automation.lot_channels.parse_govdeals_url` (already unit-tested in
  `tests/test_lot_channels.py`). This module adds only `lot_ref(row)`, which
  returns `None` for a row with a missing/garbled URL instead of raising, so one
  bad row can't end a pass.

</details>

## 3. Taking an expired lot off the channels

<details open>

- **Reuse `lot_channels.remove_lot(lot_id, channels=("site", "business"))`** —
  the exact path `/list-lot` and the admin Launcher already use. It calls
  `inventory.set_fields(lot_id, status=…, fake_sold_out=True)` (the ledger, no
  raw SQL) and drops `crm_offerable`. The `fb` channel is deliberately excluded:
  Marketplace needs a browser and a `MAX_SENDS_PER_DAY`-shaped decision, so a
  background loop never touches it.
- **`sold_status_for()` picks the status**, and for an `active_bid` row that is
  `lost_sold_out` — "a lot we never owned that we still show as SOLD". That is
  precisely an auction we were bidding on and did not win.
- **Correction to the brief:** `fake_sold_out` alone does *not* remove a lot from
  the storefront. `inventory.list_public()` and `list_catalog_feed()` gate on
  `status` only (`PUBLIC_STATUSES` / `CATALOG_FEED_STATUSES`); `fake_sold_out` is
  the flag the **CRM** honors by never offering the lot. The pair is what the
  repo means by "fake sold out", which is why we go through `remove_lot` rather
  than setting the boolean by itself. A test pins both halves.
- **Untouched:** photos (`hero_image`, `hero_image_url`, `image_urls`),
  `quantity_original`, `quantity_remaining`, `price_per_chair`, and every
  platform URL. `set_fields` only writes the columns it is handed.

</details>

## 4. Keeping watch, and detecting the relist

<details open>

- **New table `inventory_auction_watch`** (migration
  `scripts/sql/011_inventory_auction_watch.sql`), one row per watched lot, keyed
  by `lot_id` with a unique `(asset_id, account_id)` — the same "key by asset,
  not by auction, so a relist keeps being followed" idea as `tracked_lots`.
- **Why a table and not a column on `inventory`:** every field is a bounded
  scalar (ids, a state word, a capped `reason`, four timestamps). Supabase is at
  the 500 MB line, and the standing rule is that a new unbounded blob column
  needs an archival path before it ships — this has no blob to archive.
- **Why not reuse `tracked_lots`:** its poller *stops* at `closed_at`. We need
  the opposite — keep polling after the close, because the close is when the
  interesting thing (a relist) starts being possible.
- **Columns:** `lot_id` PK, `asset_id`, `account_id`, `auction_id` (last seen),
  `state` (`live` | `expired`), `reason` (≤200 chars, e.g. `assetStatusCd=SOA`),
  `end_utc`, `expired_at`, `relisted_at`, `last_checked_at`, `last_seen_live_at`,
  `poll_error`, `created_at`, `updated_at`.
- **Decision table** (`decide()`, pure):

  | watch state | GovDeals now | → action |
  |---|---|---|
  | live / unknown | live | `noop` |
  | live / unknown | closed | `expire` |
  | expired | closed | `noop` |
  | expired | live, different `auction_id` | `relist` |
  | expired | live, same `auction_id` | `relist` (premature expiry, self-heals) |
  | any | unresolvable | `unknown` (record error, act on nothing) |

- **Relist action:** `lot_channels.restore_lot(lot_id, status=<the status the
  row had before we expired it, default 'active_bid'>)` → `fake_sold_out=false`,
  `crm_offerable=true`; then `inventory.set_fields(lot_id, govdeals_url=…)` with
  the canonical `/en/asset/{asset}/{account}` URL. `govdeals_url` has to be added
  to `set_fields`' whitelist — it is not there today.
- **Telegram** via `automation.telegram_alerts.send_message_sync(..., topic="deals")`
  (`TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` from `.env`, never hardcoded;
  unconfigured returns `telegram_not_configured` and the sync carries on):

  ```
  RELISTED: <title> — <city> — closes <time> — back on black-whole.com/listings/<lot_id>
  ```

  Same auction id back live reads `BACK LIVE:` instead, so the operator can tell
  a real relist from a close we called 15 minutes early.
- **Who joins the watch list:** only lots this sync expires, plus `active_bid`
  lots it checks. The four `lost_sold_out` + `fake_sold_out` rows already in prod
  were removed by hand (moved / sold), not expired — adopting them would put lots
  we deliberately pulled back on the site the next time GovDeals relists them.

</details>

## 5. Where it runs

<details open>

- `_auction_sync_loop()` in `automation/web/app.py`, a sibling task of
  `_tracking_loop`, started in the same `startup` handler. The blocking pass goes
  through `asyncio.to_thread` — `tests/web/test_event_loop_hygiene.py` is the
  standing rule and the DB pool must not be driven from the loop.
- `AUCTION_SYNC_INTERVAL_SEC` (default `1800`) and `AUCTION_SYNC_ENABLED`
  (default `1`, set `0` to keep the loop off).
- `scripts/sync_auction_status.py --once [--dry-run] [--lot <lot_id>]` for the
  terminal. `--dry-run` prints the decisions and writes nothing — not the ledger,
  not the watch table, no Telegram.
- No Render cron, by the rule in `deals.md`.

</details>

## 6. Tasks

<details open>

1. `scripts/sql/011_inventory_auction_watch.sql` + `automation/auction_watch_store.py`
   (all SQL for the table, plus a `schema_ready()` probe so a pass before the
   migration is applied degrades to a clear message instead of a stack trace).
2. `automation/auction_sync.py` — `AuctionState`, `lot_ref`, `is_live`,
   `decide`, `relist_message`, then `sync_once()` (I/O).
3. `automation/inventory.py` — add `govdeals_url` to `set_fields`' whitelist.
4. `scripts/sync_auction_status.py`.
5. `automation/web/app.py` — `_auction_sync_pass` / `_auction_sync_tick` /
   `_auction_sync_loop` + startup/shutdown wiring.
6. Tests: `tests/test_auction_sync.py` (pure functions + `sync_once` with the
   adapter, ledger and store mocked), `tests/web/test_auction_sync_loop.py`
   (wiring + off-loop), and a storefront-visibility assertion pinning that an
   expired lot leaves `list_public()` / `list_catalog_feed()` and lands on the
   sold showcase.
7. Docs: a section in `docs/claude-reference/inventory-ledger.md`, a pointer in
   `docs/claude-reference/deals.md`, and the key-commands line in `CLAUDE.md`.

</details>

## 7. Open / deferred

<details open>

- **Migration 011 is an operator gate.** It is not applied to prod; until it is,
  `sync_once()` refuses to write and says so. (`010` is taken by the in-flight
  public-map branch.)
- Public Surplus lots are skipped — `govdeals_url` is the only handle this reads.
- A lot whose asset maestro has purged stays `active_bid` forever and shows in
  the report as `unresolved`. Deciding those needs a human.

</details>
