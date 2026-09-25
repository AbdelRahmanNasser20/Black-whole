<!-- Moved verbatim from ../../CLAUDE.md on 2026-08-28 (trim to <=8 KB). Original kept as ../../CLAUDE.md.pre-trim-2026-08-28 -->

## Inventory ledger — READ BEFORE TOUCHING run.py OR APP.PY

**Why it exists:** FB/eBay draft URLs used to be emitted as progress events and thrown away. Re-running the pipeline on the same lot would burn API budget a second time. The ledger is now the single source of truth for "what we've parsed, what's up where, how many are left to sell."

**Storage:** `~/.listing_automation/inventory.db` (SQLite). Two tables:
- `inventory` — one row per GovDeals lot, PK = `lot_id`. Columns include `folder_name`, `folder_path`, `sku`, `title`, `city`, `chair_type`, `quantity_original`, `quantity_remaining` (user-editable), `price_per_chair`, `hero_image`, `status`, `facebook_url` / `facebook_published_at`, `ebay_url` / `ebay_published_at`, `parsed_at`, `updated_at`. `status` lifecycle values: `draft` / `listed` / `hidden` / `sold_out` (the original automation set) plus `active_bid` / `lost` / `owned` / `won_pickup` (added so manually-tracked govt-auction lots can live in the same ledger; the SQLite schema doesn't enforce the set — the parallel Supabase mirror does, see workspace `CLAUDE.md §12`).
- `inquiries` — customer contact-form submissions. `kind` = `buy` | `sell`, nullable `lot_id`, `status` = `new` | `contacted` | `closed`.

**Dedup flow in `run.py`:** before each marketplace phase, `inventory.get(lot_id)` is consulted. If `facebook_url` (or `ebay_url`) is already set and `--force-republish` is NOT passed, that phase emits `phase:facebook` `status=skipped_duplicate url=<existing>` and does not touch the browser. After the phase, `inventory.set_platform_url()` stamps the URL + timestamp and promotes `status` from `draft` → `listed`. `inventory.upsert_from_run()` runs unconditionally at the end so a row exists even if both platforms were skipped.

**Preserved on re-upsert:** user edits never get stomped by a re-run. The upsert keeps existing `quantity_remaining`, `status`, `price_per_chair` (if set), `hero_image` (if set), and both platform URLs. It only refreshes the "as-parsed" metadata columns.

**Auto-sold-out rule:** editing `quantity_remaining` to 0 via the admin tab auto-flips `status` to `sold_out` (unless the same PATCH also sets `status` explicitly).

**Sold archive + multi-location (BLACKWHOLE-29, 2026-07-25):** sold-out rows no longer vanish from the site — `inventory.list_sold_showcase()` feeds an `ALREADY MOVED` strip under the live grid on `/listings`, stamped SOLD, showing the lot size and the price it sold at. Its job is social proof: a buyer who sees ~4,900 chairs already moved trusts the ones on the floor, and a sold lot's detail page swaps its CTA to "FIND ME ONE LIKE THIS". Rules worth keeping:
- Showcase set = `status IN ('sold_out','lost_sold_out')` AND `quantity_original > 0` AND the row has a photo. Half-imported folder stubs are filtered out by that last clause — don't loosen it without a plan for the junk rows.
- `lost_sold_out` = a lot we never owned but present as sold; it's in the DB CHECK constraint and now in `ALL_STATUSES` too (it wasn't, so the admin couldn't set it). Pairs with `fake_sold_out`, which the CRM honors by refusing to offer the lot to a buyer.
- `status` → sold stamps `sold_at` once (first transition only).
- **The FB catalog feed and CRM recommendations are unaffected** — both are status-gated (`CATALOG_FEED_STATUSES` / the CRM's `ROUTABLE_STATUSES`), so an archived lot can never be offered as stock. Keep it that way.
- `inventory.locations` (JSONB, migration `scripts/sql/003_inventory_locations.sql`) lets one lot list several pickup cities: `[{"city","state","quantity"}]`. `city`/`state` stay the PRIMARY location — every existing query reads those. The admin edits it as one text cell (`Baltimore, MD x1200; Orlando, FL`); `inventory.parse_locations()` accepts that, a JSON string, or the list shape.
- Seeded by `scripts/seed_sold_showcase.py` (idempotent).

**Backfill path for pre-tracking listings:** `POST /api/inventory/backfill` walks `~/Desktop/Banquet chiars Pictures/`, imports any folder missing from the ledger as a `draft` row with best-effort metadata from the matching `llm_compare_logs` row (Supabase). FB/eBay URLs stay NULL — admin uses the "paste URL" cell on the Inventory tab for each row.

**Things NOT to do:**
- Don't add columns to `auction_extractors/state/listings.db` for publish state. That DB is the upstream scrape cache — keep the read-only separation.
- Don't re-introduce "emit URL, forget URL" in `facebook.py` / `ebay.py`. The URL must flow back into `inventory.set_platform_url()` or the dedup check stops working.
- Don't bypass the ledger to work around a "stuck" row. Delete/edit via the admin Inventory tab or `DELETE /api/inventory/{lot_id}`, don't hack the DB directly.

**Dashboard URL migration (breaking):** the admin console moved from `/` → `/admin` to make room for the public site. JS/API paths under `/api/*`, `/image/*`, `/screenshot/*`, `/static/*` are unchanged. Bookmarks and any external scripts hitting `/` now land on the customer-facing landing page instead.

## Auction expiry / relist sync (2026-09-15)

**Summary.** A lot we're bidding on (`status = 'active_bid'`) used to stay on the
storefront after its GovDeals auction closed. A 30-minute loop in the web process
now takes the dead ones off as fake-sold-out, keeps watching them, and puts them
back — with a Telegram ping — when the auction relists.

- **Code:** `automation/auction_sync.py` (decisions + the pass),
  `automation/auction_watch_store.py` (SQL), `scripts/sync_auction_status.py` (CLI),
  `_auction_sync_loop` in `automation/web/app.py`.
  Plan: `docs/superpowers/plans/2026-09-15-auction-expiry-sync.md`.
- **Scope:** `inventory` rows carrying a `govdeals_url` that are either
  `status = 'active_bid'` or already on the watch list as `expired`. Public
  Surplus is out — there's no `govdeals_url` to parse.
- **Expire = `lot_channels.remove_lot(lot_id, channels=("site","business"))`**,
  the same path `/list-lot` and the admin Launcher use: `status` → `lost_sold_out`
  (via `sold_status_for`), `fake_sold_out = true`, `crm_offerable = false`. The
  `fb` channel is never touched — Marketplace needs a browser and an operator.
  Photos, `quantity_original`, prices and platform URLs are untouched, so the lot
  lands on the ALREADY MOVED strip with a real card.
  - **`fake_sold_out` alone does not hide a lot.** `list_public` / `list_catalog_feed`
    gate on `status`; `fake_sold_out` is the flag the CRM honors by never offering
    it. The pair is what "fake sold out" means here — pinned by
    `tests/test_auction_sync.py::TestExpiredLotLeavesEveryPublicSurface`.
- **Relist = `lot_channels.restore_lot(lot_id, status=<prior status>)`** +
  `inventory.set_fields(lot_id, govdeals_url=…)` (that column joined the
  `set_fields` whitelist for this) + a Telegram message on the `deals` topic:
  `RELISTED: <title> — <city> — closes <time> — back on black-whole.com/listings/<lot_id>`.
  Same auction id back live reads `BACK LIVE:` — a close we called ~15 min early,
  self-healing rather than a second listing.
- **Live / closed is decided by `assetStatusCd`** (`STA` = live), reusing
  `deals.tracking.is_closed` so this and the bid-history poller can't disagree.
  Close *times* come from the bidbox's `assetAuctionEndDateUTC` only — the detail
  endpoint's `assetAuctionEndDate` has no timezone and is US/Eastern.
- **Silence is never an answer.** Maestro serves HTTP 204 with an empty body for
  a purged asset (verified on `53677/357`, both id orders). An unreadable lot is
  counted as `unresolved`, gets a `poll_error`, and is left exactly as it was.
- **Watch list = `inventory_auction_watch`** (migration
  `scripts/sql/012_inventory_auction_watch.sql`, **operator gate — not applied**),
  keyed by `lot_id` with `(asset_id, account_id)` unique on top: an unsold lot
  relists under the same asset with a new auction id, the same reason
  `tracked_lots` is keyed that way. Bounded scalar columns only (`state`,
  `reason` ≤200 chars, timestamps) — nothing that grows, because Supabase is at
  the 500 MB line. Until the migration is applied, a real run refuses with the
  exact command to run; `--dry-run` still reports.
- **Where it runs:** `_auction_sync_loop` in the web process, `AUCTION_SYNC_INTERVAL_SEC`
  (default 1800) / `AUCTION_SYNC_ENABLED` (default on). No Render cron, same
  reason as `_tracking_loop`. Manual: `scripts/sync_auction_status.py --once [--dry-run] [--lot ID]`.
- **Not adopted:** the `lost_sold_out` + `fake_sold_out` rows already in prod.
  Those were pulled by hand (moved / sold), not expired — putting them on the
  watch list would relist lots we deliberately took down.
