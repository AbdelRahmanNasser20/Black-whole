# Channels — `listing_channels`, the sync loop, the Channels tab

Plan of record: `docs/superpowers/plans/2026-09-20-multichannel-listing-master.md` (Phase 1 built 2026-09-20).

**One line:** `inventory` says WHAT we sell; `listing_channels` records WHERE each lot currently is, one row per lot × channel; a loop in the web process moves the far side toward what inventory says.

## Model (`automation/channels/`)

| Piece | File | Job |
|---|---|---|
| Vocabulary | `channels/__init__.py` | `CHANNELS`, `FEED_CHANNELS`, `PUSH_CHANNELS`, `APPROVAL_CHANNELS`, `BROWSER_CHANNELS`, `STATES` |
| Store | `channels/store.py` | CRUD over `listing_channels` (`get`, `upsert`, `set_state`, `approve`, `reject`, `matrix`, `queue`). Validates names before SQL. `%s` only. Never returns `storage_note`. |
| Engine | `channels/sync.py` | `desired_state`, `payload_hash`, `plan`, `run_once` |
| DDL | `scripts/sql/011_listing_channels.sql` | table + switch seed rows. **Operator applies in Supabase SQL Editor.** Never created at runtime. |
| Backfill | `scripts/backfill_listing_channels.py` | seeds rows from `inventory` (dry-run default, `--apply`) |

Channels: `site`, `fb_catalog`, `google` (feed — pull-based CSV, bookkeeping only) · `ebay`, `fb_marketplace`, `craigslist` (push — an adapter posts).

`google` — Merchant Center scheduled fetch of `/catalog/google.csv` (`automation/google_feed.py`); runbook `docs/google_merchant_runbook.md`; switch only affects bookkeeping, pause the feed in MC to actually pull products.

States: `off` (never went out) → `queued` (approved, next pass posts) / `pending_approval` (waiting for the operator) → `live` → `delisted`; `error` carries `last_error`.

Row columns: `lot_id, channel, state, external_id, url, payload_hash, last_synced_at, last_error, approved_at`. Bounded — no blobs, so no archival path needed.

## Rules the engine encodes

- **Desired `live`** iff `status in inventory.CATALOG_FEED_STATUSES` and `quantity_remaining > 0` and not `fake_sold_out`. Same gate as the FB catalog feed, so an archived lot is never offered as stock anywhere. Else `delisted`.
- **Hash** = sha1 over `title, price_per_chair, quantity_remaining, city, state, hero_image, chair_type, description` (+ channel). `updated_at` alone never triggers an update.
- **Feed channels:** `list`/`delist` write state, `update` writes the new hash. Nothing is pushed.
- **Approval channels (`fb_marketplace`):** a `list` parks the row in `pending_approval` — no adapter call. Only Approve (→ `queued`) lets the next pass post, and only if the switch is on.
- **Push channels:** adapter `publish_lot` → `live` + url + external_id; exception → `error` + `last_error`. No `publish_lot` → `error` "adapter has no publish_lot" (visible, never silent). Phase 1 ships one built-in publisher: `fb_marketplace` runs `lot_channels.post_to_facebook` (same as `/list-lot`). `craigslist` has a real adapter (below, 2026-10-03). eBay arrives in Phase 3.
- **Delist** on a push channel: adapter `unpublish_lot` if present, else state `delisted` with `last_error="manual delist required"`. A row that never went out (`queued`/`pending_approval`) is simply dropped to `delisted`.
- **Switches:** a channel with `site_settings.channel_enabled(c) is False` (master off OR its own switch off) is skipped entirely and counted in `skipped.disabled`.
- **Browser pacing** (`fb_marketplace`, `craigslist`): at most `browser_channel_daily_cap` list actions per UTC day, `browser_channel_spacing_s` apart, counted from `last_synced_at`. Delists are never paced.
- `storage_note` is stripped before a row reaches an adapter or a hash.

## Switches (`site_settings`)

| Key | Ships | Meaning |
|---|---|---|
| `channels_master_enabled` | 1 | off = no channel syncs at all |
| `channel_site_enabled`, `channel_fb_catalog_enabled`, `channel_google_enabled` | 1 | the feeds that already exist |
| `channel_ebay_enabled` | 0 | flip when the eBay adapter lands |
| `channel_craigslist_enabled` | 0 | adapter is built; flip to let the loop post (see Craigslist below) |
| `channel_fb_marketplace_enabled` | **0** | **HARD RULE: ships disabled; only the operator flips it.** Renew/Relist are the spam triggers that shadowbanned the family account (D2). |
| `browser_channel_daily_cap` / `browser_channel_spacing_s` | 4 / 1200 | pacing for the browser channels |

Edited through the admin Channels tab (`PATCH /api/channels/switches`, keys must start `channel`/`browser_channel` — the deposit rule cannot be touched from there).

## Loop + API (`automation/web/app.py`)

- `_channel_sync_loop` every `CHANNEL_SYNC_SEC` (env, default 300; `0` disables) → `asyncio.to_thread(sync.run_once)`. Never raises out of the loop. Skipped with the same guard the other pollers use.
- `GET /api/channels` → `{switches, matrix, queue, channels}` (`@readcache.cached()`, plain `def`).
- `PATCH /api/channels/switches` · `POST /api/channels/queue/{id}/approve` (409 `channel disabled` when the switch is off — the queue is never a back door past the switch) · `POST /api/channels/queue/{id}/reject` · `POST /api/channels/sync` (manual "Sync now").
- All under `/api/` → auth-walled.

## Admin tab (12 · Channels)

`static/admin/channels.js` (`mount()`/`load()`) + `channels.css`, registered in `shell.js`, pane in `templates/index.html`. Switch row (one `<input data-key=…>` per channel, rendered server-side from `CHANNELS`), FB Marketplace red pill **"OFF — waiting for operator"** until its switch is on, approval queue (Approve/Reject), lot × channel matrix (`.ch-cell--<state>`, red cells carry `last_error` as `title`), `Sync now`.

## Existing writers mirror into the store (Phase 1.7)

- `lot_channels.post_to_facebook` → `upsert(fb_marketplace, live, url, external_id=item id)`.
- `lot_channels.remove_lot(channels=…)` → `set_state(delisted)` for each (`site→site`, `business→fb_catalog`, `fb→fb_marketplace`).
- `lot_channels.restore_lot` → `set_state(fb_marketplace, pending_approval)` — the relist waits in the queue, never auto-posts.
- `inventory.set_platform_url("facebook"|"ebay", url)` → `upsert(live)`; url cleared → `off`. `fb_business`/`ad` are not channels.
- Every store call is wrapped: a store failure is logged and swallowed. Bookkeeping never breaks a post, a remove, or a ledger write.

## How to turn FB Marketplace on (operator only)

1. Migration `011` applied; backfill run with `--apply`.
2. Admin → Channels → click the `FB MARKETPLACE` switch. The red pill disappears.
3. Rows in the approval queue: Approve the ones you want. The next pass (≤ 5 min, or `Sync now`) posts them through `post_to_facebook` on the family-account profile, paced by `browser_channel_daily_cap` / `_spacing_s`.
4. To stop: flip the switch off. Nothing else is needed — approved rows stay `queued` and are skipped as `disabled`.

## Runbook

```bash
# 1. operator: paste scripts/sql/011_listing_channels.sql into Supabase SQL Editor
# 2. seed rows from inventory
.venv/bin/python scripts/backfill_listing_channels.py            # dry-run
.venv/bin/python scripts/backfill_listing_channels.py --apply
# 3. tests
.venv/bin/python -m pytest tests/test_channels_store.py tests/test_channels_sync.py tests/web/test_channels_api.py tests/web/test_admin_tab_channels.py -q
```

Until `011` is applied, `GET /api/channels` 500s on the missing table and the writers log `listing_channels not updated (...)` and carry on — that is the intended behaviour, not a bug.

## Craigslist (`automation/publish/adapters/craigslist.py`, flow in `automation/craigslist.py`)

**One line:** one inventory row → one free "for sale by owner → furniture" post in the lot's own city, through the persistent Chrome profile; URL lands in `inventory.craigslist_url` + a `listing_channels` row.

- **Flow (recorded live 2026-10-03 on phoenix, atlanta, pittsburgh, nashville, boise, lasvegas):** `post.craigslist.org/c/<code>` (`CL_POST_CODES`, e.g. `phx`/`atl`/`pit`; the full subdomain also resolves) → `?s=copyfromanother` ("re-use previous posting?" — always `skip`, button `brand_new_post`; appears once the account has a post) → `?s=area` (one `<select>` already on the city — `go`) → `?s=subarea` (only some cities; `SUBAREA_PREF`, else first option) → `?s=type` radio `fso` (**by-owner only — by-dealer is paid, never chosen**) → `?s=cat` radio labelled `furniture` (matched by label, ids differ per city) → `?s=edit` (`PostingTitle` ≤70, `price`, `geographic_area`, `postal`, `PostingBody`, condition=good, language=english, `delivery_available` + `see_my_other` on, **no phone, no address**) → `?s=geoverify` (city + ZIP only, button "continue") → `?s=editimage` (`input[type=file]`, poll "this posting has N images") → `?s=preview` publish → confirmation link `https://<sub>.craigslist.org/…/<id>.html`. Steps are keyed by `?s=`; an unknown step, a login page, or a missing confirmation link raises — nothing is guessed.
- **Copy:** `craigslist.build_lot_post(row)` — title `<title> — <qty> Available, Stackable (City, ST)` fitted to 70 (a "(City, ST)" already on the title is replaced, not doubled), body = description/subtitle + qty + price + pickup + venues + black-whole.com + reply CTA. ZIP = `inventory.zip_code`, else `CITY_ZIP` (city centre), else **raise** — never invented. Photos = the lot's public R2 copies (hero first, max 8) cached under `SCRATCH_DIR/craigslist/<key>/`.
- **Cities:** `CITY_SUBDOMAINS` covers phoenix, atlanta, pittsburgh, nashville, boise, lasvegas, tampa, orlando, orangecounty (Stanton/Cypress/Anaheim) + the originals. `resolve_lot_subdomain` raises on an unknown city → the sync loop records an `error` row (visible in the Channels tab).
- **Login once:** `python run.py --login-only`, sign in at `accounts.craigslist.org` inside that window, close it. Not logged in → `RuntimeError("Craigslist profile not logged in …")` on the row.
- **Phone verification (`?s=pn`):** hit live on the 5th post of the day (5 cities, one account). CL parks the draft and wants a one-time SMS/voice code — that is the operator's phone, never automated; the adapter raises `PHONE_VERIFY_NEEDED` and the row goes `error`. Verify once in the profile's Chrome (the draft is under accounts.craigslist.org → drafts), then re-run. Craigslist also limits "each posting to a single area and category, once per 48 hours" — the sync pacing (`browser_channel_daily_cap`=4) is the right default, don't raise it.
- **Live posts 2026-10-03 (by hand, same flow):** 9006 phoenix `7977055260` · 31225-atl atlanta `7977057498` · gd-56-9685 pittsburgh `7977058878` · gd-32876-2 nashville `7977059862` · 31225 boise `7977060942`. All recorded in `inventory.craigslist_url` + `listing_channels`.
- **Gates:** sync loop = `channel_craigslist_enabled` (ships 0) + `browser_channel_daily_cap` / `_spacing_s`; CLI = `CRAIGSLIST_LIVE=1`. The adapter itself has no gate.
- **Bookkeeping:** `UPDATE inventory SET craigslist_url` (cleared on delist; there is no `craigslist_published_at` column, no migration) + `store.upsert(lot, "craigslist", …)`. Both wrapped — a store failure never breaks a post.
- **Edit = not automated:** `update_lot` raises `NotImplementedError` (row shows `error`); delist + re-list instead.
- **UNVERIFIED (check on the first live run):** `delete_posting` / `renew_posting` click the "delete"/"renew" control on `post.craigslist.org/manage/<id>`; selectors were not recorded. A failed delete raises → row `error` with "delete it by hand".
- **Account note:** the logged-in account is the operator's gmail; replies arrive through CL's relay to it (not the black-whole.com mailbox).

```bash
.venv/bin/python scripts/craigslist_lot.py post 9006 --dry-run      # exact copy, nothing posted
CRAIGSLIST_LIVE=1 .venv/bin/python scripts/craigslist_lot.py post 9006
CRAIGSLIST_LIVE=1 .venv/bin/python scripts/craigslist_lot.py delist 9006
.venv/bin/python scripts/craigslist_lot.py status
.venv/bin/python -m pytest tests/test_craigslist.py tests/test_craigslist_adapter.py -q
```
