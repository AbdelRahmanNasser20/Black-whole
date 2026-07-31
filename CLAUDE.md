# Listing Automation — CLAUDE.md

## How to answer
Default to concise, direct responses — answer the question, show the
result, stop. No restating the question, no preambles ("Great question…",
"Let me…"), no closing summaries of what just happened, no narrating
tool calls you're about to make. Skip section headers and bullet lists
unless the answer genuinely has multiple independent parts. One sentence
is better than three if it's complete. Only expand into detail, step-by-
step explanations, or long-form structure when the user explicitly asks
for it ("explain…", "walk me through…", "give me the full…", etc.) or
when the task inherently requires it (e.g. a multi-step plan the user
needs to review before execution).

## What this project is
Python + Playwright pipeline that turns a GovDeals URL into a Facebook
Marketplace draft + eBay draft. Source-of-truth spec is
`GovDeals_Automation_Blueprint.md`. The implementation phases mirror that
blueprint (scrape → llm → download → dewatermark → fb → ebay).

## Repo layout
- `run.py` — CLI entry (`python run.py <govdeals-url>`, `--login-only`, `--price`, `--skip-*`, `--force-republish`). Consults the inventory ledger before FB/eBay phases and skips already-published lots (emits `skipped_duplicate`); upserts a row post-run.
- `automation/`
  - `config.py` — paths, defaults, env-var loading (`.env` via python-dotenv)
  - `browser.py` — Playwright persistent context. Auto-clears stale `SingletonLock`/Chromium on launch.
  - `govdeals.py` — Phase 1 scrape. Pulls images from `<img>`, `srcset`, `data-src`, CSS background, AND a regex sweep of the raw HTML for `webassets.lqdt1.com/.../photos/...` URLs.
  - `downloader.py` — async httpx with `Referer: govdeals.com` + Chrome UA (CDN 403s without these).
  - `dewatermark.py` — dewatermark.ai REST API only. Per-folder sidecar + global response cache + budget caps.
  - `quality.py` — bottom-right histogram check; runs after every API call to guard against the API returning still-watermarked output.
  - `listing_images.py` — durable listing images via Supabase Storage (BLACKWHOLE-6). Uploads cleaned photos to the shared **public `listing-images` bucket** (same bucket + key contract as the CRM's BWCRM-18) and returns durable public URLs stored in `inventory.hero_image_url` (flat key `<lot>.<ext>`) + `inventory.image_urls` (gallery, prefixed `<lot>/NN.<ext>`). Uses `SUPABASE_STORAGE_URL` / `SUPABASE_STORAGE_KEY` / `LISTING_IMAGES_BUCKET` (deliberately separate from the stale `SUPABASE_URL`/`ANON`). Best-effort: unconfigured/failed upload → URLs stay NULL and the site falls back to local `/image/` serving. Public reads need no key. Backfill via `scripts/backfill_listing_images.py`.
  - `lot_images.py` — **the one resolver for "where are this lot's photos?"** (BLACKWHOLE-31). Precedence is always `durable DB URLs → local disk → nothing`, never host-specific. Stdlib-only and takes its DB reader by injection (`resolve_lot(lot_id, fetch_row=...)`), so the CRM repo imports it too — via workspace `core/images.py`, which re-exports this module rather than copying it. `resolve(row)` returns a `LotImages` with `.urls` (deduped photo set — safe to send from any host), `.hero` (the cover, honoring the `hero_image_url` column), `.local_paths`, `.source`. `hero_src()` / `gallery_srcs()` are the web-template helpers `web/app.py` now delegates to. See "Lot photos" below.
  - `facebook.py`, `ebay.py` — fill drafts, stop before publish.
  - `templates.py` — FB description + eBay HTML description with placeholders.
  - `favorites.py` — auction-favorites store + Telegram countdown alert schedule (6d/3d/2d/1d/1h/5m). Persists `favorites` + `alert_log` tables in the same `inventory.db`. The scheduler loop runs inside the FastAPI process (`web/app.py`); alert sends go through `telegram_alerts.py`. Marking `auction_extractors` cards via the dashboard's `04 Auctions` tab writes here.
  - `telegram_alerts.py` — thin async `send_message(text)` over the Telegram Bot API. Reuses `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` from `automation.config`. Best-effort, never raises — returns `(ok, error_str)`.
  - `inventory.py` — ledger on Supabase Postgres (via `automation/db.py`; cut over from SQLite 2026-05-29). Two tables: `inventory` (one row per lot, keyed by `lot_id`) + `inquiries` (customer contact-form submissions). Source of truth for "what we've parsed, what's up where, how many are left." See "Inventory ledger" section below.
  - `llm/` — pluggable extractors:
    - `claude_code.py` — stdin/stdout sentinel protocol; ONLY works when Claude Code drives the script with a TTY.
    - `gemini.py` — google-genai, `gemini-2.5-flash`; needs `GEMINI_API_KEY`. Primary.
    - `openai.py` — OpenAI `gpt-4o-mini` via `openai` SDK; needs `OPENAI_API_KEY`. Secondary (A/B log) when Gemini is primary.
    - `dom_fallback.py` — pure DOM heuristic, no external calls. Used when no LLM is reachable.
    - `default_extractors()` picks: Gemini primary if key, else OpenAI primary if key, else DomFallback. Secondary = OpenAI when Gemini is primary. Override with `LISTING_LLM_MODE=gemini|openai|claude_code|ollama|dom`.
  - `progress.py` — emits `<<<EVENT>>>{json}` lines parsed by the dashboard.
  - `web/` — FastAPI app (`python -m automation.web` → http://127.0.0.1:8765). Serves **two surfaces off one process**:
    - **Public site** (brutalist-industrial theme, `public.css` / `public.js`, separate from the admin theme):
      - `GET  /` — landing (`landing.html`) with live stat tiles from `inventory.stats()` and a featured strip from `list_public()`
      - `GET  /listings` — card grid with client-side filters (type, city, min qty, search)
      - `GET  /listings/{lot_id}` — gallery + spec sheet + inquiry form
      - `GET  /sell` — seller intake form
      - `POST /contact` — persists to `inquiries` table
    - **Admin dashboard** (`/admin`, `index.html`, dark terminal theme `app.css`). Tabs: `01 Launcher` / `02 Drafts` / `03 A/B` / `04 Auctions` / `05 Inventory` / `06 Inquiries` / `07 Listings DB` / `08 Test Scrape` / `09 Subscribers` / `10 Deals`. Launcher shows an `UP NEXT` strip when the run queue is non-empty. Inventory tab = editable table (qty/price/status inline, FB/eBay URL backfill, Republish, Delete, Backfill-from-folders). Inquiries tab = chronological cards with status transitions. Deals tab (BLACKWHOLE-12) = search/filter/sort over the `deals/` tracker's `deal_lots` with a landed-cost column (`DEALS_BUYER_PREMIUM_PCT`/`DEALS_TAX_PCT`/`DEALS_FREIGHT`, defaults 12.5%/0/0), plus a category-tree sidebar (branch = canonical bucket, twig = native GovDeals category; clicking a node filters the table).
    - **Admin JSON APIs**: `/api/inventory[...]`, `/api/inventory-stats`, `/api/inventory/backfill`, `/api/inventory/{lot_id}/platform`, `/api/inquiries[...]` (list/patch/delete), `/api/runs/queue` + `/api/runs/queue/clear` (serial run queue — see `auction_extractors` section), `/api/favorites[...]` (Auctions tab star/unstar + alert state), `/api/deals` (Deals tab: q/category/native/state/max_bids/ending_within/status/sort/dir/limit/offset → rows + facets + stats; query builder in `automation/web/deals_query.py`), `/api/deals/tree` (canonical→native category tree with lot/zero-bid/ending-24h counts).
- `auction_extractors/` — integrated data pipeline + read-only API (see "auction_extractors" section below).

## Connecting to the shared DB

Workspace-level decision (see `workspace/CLAUDE.md §14`): all new DB code goes through Supabase Postgres. This repo uses a **self-contained, vendored** helper `automation/db.py` (psycopg over Supabase) rather than the workspace `core/db.py`, so it runs in standalone web/CI clones where `core/` isn't present. Same documented surface:

```python
from automation import db   # db.connect, db.fetch_one, db.fetch_all, db.execute, db.executemany
```

`BLACKWHOLE_DB_URL` is read from this repo's `.env` (gitignored) by `automation/config.py`. Rows come back as plain dicts (psycopg `dict_row`), and timestamptz columns read back as `datetime` objects (not ISO strings).

**Current adoption: `inventory.py` + `favorites.py` + the A/B compare feature are cut over to Supabase** (project `blackwhole` / `nihgzltpjriekyqqucbd`) as of 2026-05-29. `inventory.connect()` is an alias for `db.connect()`. The local SQLite file at `~/.listing_automation/inventory.db` is no longer read or written, and `~/.listing_automation/compare_ratings.json` / `llm_compare_*.json` are no longer read or written (existing logs were imported via `scripts/migrate_compare_logs.py`; the files can be deleted any time). Schema lives in Supabase (managed via migrations), not created at runtime; the cutover added `auction_favorites` / `auction_alerts_sent` / `llm_compare_logs`. Note: Supabase has RLS **disabled** on all tables — the server connects as the pooler `postgres` role (bypasses RLS), but enabling RLS + policies for the anon key is a separate, still-open task.

`auction_extractors/state/listings.db` stays SQLite-only and read-only to this repo (upstream scrape cache).

## Key environment
- macOS only paths assumed (`~/Desktop/Banquet chiars Pictures/`).
- `.env` carries `DEWATERMARK_API_KEY` and (optional) `GEMINI_API_KEY`. Already gitignored.
- Persistent Playwright profile lives at `~/.listing_automation/chrome_profile/`. Logged into FB + eBay there.
- A/B compare logs and ratings live in Supabase table `llm_compare_logs` (one row per dual-extractor run, `id` = unix-ts). The old `~/.listing_automation/logs/llm_compare_*.json` + `compare_ratings.json` files are dormant — kept on disk as a backup but not read or written.

## Gotchas (all fixed — don't reintroduce)

| # | Symptom | Cause | Fix landed in |
|---|---------|-------|---------------|
| 1 | Dashboard `/` returns 500 | starlette 1.0 changed `TemplateResponse` signature | `automation/web/app.py` — `TemplateResponse(request, name, ctx)` |
| 2 | `--login-only` exits instantly under `!` prefix | no TTY → `sys.stdin.readline()` returns EOF immediately | `run.py::_login_only` — wait for `ctx.on('close')` event instead of stdin |
| 3 | `Failed to create ProcessSingleton` on launch | prior Chromium still alive holding the profile lock | `automation/browser.py::_clear_stale_profile_lock` — kills PID from `SingletonLock` symlink target + unlinks all `Singleton*` files |
| 4 | LLM phase hangs forever in dashboard | `ClaudeCodeExtractor` waits on stdin nobody feeds | `automation/llm/dom_fallback.py` + `default_extractors()` env-aware picker (auto: gemini → claude_code-if-TTY → dom) |
| 5 | 0 images downloaded; folder has only `_screenshots/` | GovDeals modernized to lightGallery v2 (`.lg-object`); some images render via `srcset` / CSS bg / inline JSON | `govdeals.py` `EXTRACT_JS` — scans `<img src/srcset/data-src>`, `<source srcset>`, CSS `background-image`, AND raw-HTML regex for `webassets.lqdt1.com/.../photos/...` |
| 6 | Image fetch returns 403 from CDN | no `Referer` / no browser UA | `automation/downloader.py::DOWNLOAD_HEADERS` |
| 7 | Related-listing thumbnails leak into the upload | sidebar uses same `lqdt1.com/photos/<other_lot>/` URLs | `govdeals.py` post-filter: `f"/photos/{lot_id}/" in u` |
| 8 | `click.prompt` aborts under dashboard subprocess (`Aborted!`) | Even with `--price` flag passed, `click.prompt` still tries to read stdin; no TTY → EOF → abort | `run.py` — check `price_override is not None` first, else check `sys.stdin.isatty()` before prompting, else auto-accept suggestion. Same pattern for the "Press Enter to close" at end of run — replaced with a sleep loop under no-TTY. |
| 9 | Dewatermark silently shipped watermarked images | The old bottom-right histogram quality check, written for a small corner stamp, failed open against GovDeals' new full-image tiled `www.govdeals.com` watermark and rejected every cleaned API output | `automation/quality.py` — replaced histogram heuristic with pure byte-identity check (cleaned != original); trust dewatermark.ai's HTTP 200 |

**Other things worth knowing:**
- Headless mode hits Akamai 403 on GovDeals. Always non-headless. `persistent_context(headless=False)` is the default — don't flip it.
- `ClaudeCodeExtractor` only works when Claude Code is the orchestrator with a real TTY. From the dashboard subprocess it'll hang. The `default_extractors()` picker handles this automatically.
- Dewatermark is **dewatermark.ai API only** (`DEWATERMARK_API_KEY` in `.env`). No local image processing, no heuristic watermark detection. Every image not in the global response cache goes to the API; failures leave originals in `_originals/` and emit `dewatermark:degraded`.

## How to run end-to-end
1. First time only: `python run.py --login-only` (browser opens with FB + eBay tabs; log into both, close window).
2. `python -m automation.web` — public site at http://127.0.0.1:8765/, admin dashboard at http://127.0.0.1:8765/admin.
3. Paste GovDeals URL on the Launcher tab → Run. Confirm price when prompted. Drafts appear under the Drafts tab. Inventory ledger picks up the row automatically (see next section).

## Lot photos — READ BEFORE WRITING ANY IMAGE-PATH CODE

**Cloudflare R2 is the canonical backend. Supabase Storage is dead.** The shared
Supabase project blew its egress quota and Storage is 402-restricted — every
`…supabase.co/storage/v1/object/public/listing-images/…` URL returns HTTP 402,
not the image. R2 (`R2_*` in `.env`, public base
`https://pub-4ac6bae8ec024e3aaccf3317c8873840.r2.dev`) serves the same key
contract with zero egress fees. `listing_images.upload_lot_images()` already
dispatches to `r2_images` whenever R2 is configured, so the *upload* path needs
no thought — but **never write a new Supabase Storage URL into `inventory`**,
and treat any row still carrying one as broken. `lot_images.storage_backend(url)`
answers which backend a URL belongs to; `deals/archive.py` still uploads to
Supabase and is the one module that hasn't been moved over.

**Resolving is centralized in `automation/lot_images.py`.** Don't hand-roll
`DOWNLOAD_ROOT / folder_name` again. The bug that motivated this: the CRM poller
resolved photos off `folder_path`, which only exists on the operator's laptop,
so on the server it found nothing and sent text-only replies — five buyers lost
on lot 31225 (~945 chairs). Rules the module encodes:
- `image_urls` (the gallery) is the photo **set**. `hero_image_url` is the
  **cover**. File 0 is uploaded under both keys, so unioning them double-attaches
  the first photo — `.urls` deliberately doesn't.
- Local disk is a fallback, never an answer to "can the bot show a buyer this
  lot" — `has_usable_images()` ignores disk on purpose.
- `_originals/` and `_screenshots/` are internal; only top-level files in a lot
  folder are listing photos.

**Getting photos onto a lot** — two scripts, by whether we physically have it:
```bash
# lots we own (reads the operator's Desktop folder)
./.venv/bin/python scripts/backfill_listing_images.py --lot 31225
./.venv/bin/python scripts/backfill_listing_images.py --missing

# lots we're offering but never picked up (active_bid) — mirrors the seller's
# own GovDeals photos into R2; finds asset/account from the row automatically
./.venv/bin/python scripts/import_deal_images.py --lot wa-steilacoom-50
```

**The guard.** A `crm_offerable` lot with no usable photos is the exact failure
that cost those buyers, and it's silent. `scripts/check_offerable_images.py`
exits non-zero on any such lot; `--http` also proves the URLs return 200 (which
is what catches a backend going dark, as Supabase did). Run it after flipping
any lot to `crm_offerable`.

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

## Current status

**End-to-end pipeline is working.** First full successful run finished 2026-04-17 on `https://www.govdeals.com/en/asset/305/10340`:

- scrape ✓ 1 image (only lot the filter kept)
- llm ✓ `dom_fallback` (no Gemini key set yet)
- download ✓ 1 file with proper `Referer` + UA
- dewatermark ✓ 1 cleaned via `dewatermark.ai` API
- facebook ✓ draft created, URL has `listing_id=3...`
- ebay ✓ but URL landed on `ebay.com/sh/lst/active?sku=...` (the "active listings search" page), **NOT a real draft URL** — eBay selectors need verification

Dashboard lives at `http://127.0.0.1:8765`. Run triggered via:

```bash
curl -X POST http://127.0.0.1:8765/api/runs/start \
  -H "Content-Type: application/json" \
  -d '{"url":"https://www.govdeals.com/en/asset/<seller>/<lot>"}'
```

Price confirmation (when interactive prompt is expected) can be sent programmatically:

```bash
curl -X POST http://127.0.0.1:8765/api/runs/stdin \
  -H "Content-Type: application/json" \
  -d '{"line":"12"}'
```

## Dewatermark behavior — READ BEFORE TOUCHING

**dewatermark.ai API only.** No IOPaint, no local inpainting, no heuristic watermark detection — the API is the watermark remover. Every image not already in the global response cache is sent to `https://platform.dewatermark.ai/api/object_removal/v2/erase_watermark` with `X-API-KEY: $DEWATERMARK_API_KEY`.

- `automation/dewatermark_cache.py` owns three layers consulted before any API call:
  1. Per-folder sidecar `<folder>/.dewatermark_state.json` keyed by sha256 of the original. Records method (`api_cache` / `api`), status (`clean` / `failed`), api_calls counter.
  2. Global response cache at `~/.listing_automation/api_cache/<sha>.bin`. Survives lot deletions and machine restarts. Once a hash is here, **no future run anywhere on this machine ever calls the API for that hash again.**
  3. `RunBudget` (per-run counter + 24h rolling window) gates each call. Default caps: `MAX_API_CALLS_PER_RUN=50`, `MAX_API_CALLS_PER_DAY=250`. Override in `.env`.
- **`DEWATERMARK_OFFLINE` defaults OFF.** The real API runs on every un-cached image. Set `DEWATERMARK_OFFLINE=1` in `.env` to freeze spend while developing.
- **No fallback when the API fails.** If the API errors or returns byte-identical bytes to the input, the sidecar marks the hash `failed`, the original stays in `_originals/`, and a `dewatermark:degraded` event is emitted. There is nothing else to try.
- **Sanity check on API output.** `quality.watermark_likely_present()` does one thing: reject only if the cleaned bytes are missing, empty, or equal the original. Don't reintroduce histogram/heuristic checks — the GovDeals watermark is semi-transparent and tiled full-image, so pixel-delta thresholds can't distinguish clean from dirty (see Gotcha #9).
- Audit tools (no API calls):
  - `python -m automation.dewatermark stats` — today/month/all-time call counts + global cache size
  - `python -m automation.dewatermark verify <path-to-cleaned-file>` — byte-compares against the matching `_originals/<file>` and prints `clean` or `watermarked`

## Known TODOs (ordered roughly by blast-radius)

1. **eBay flow doesn't land on a real draft.** Final URL was `ebay.com/sh/lst/active?sku=...` (seller hub search), not `sell.ebay.com/sell/form/...`. Selectors in `automation/ebay.py` need end-to-end verification. First step: open the URL the script lands on, compare against what a manual "List an item" flow produces, update `SELL_URL` and the subsequent form-fill selectors.
2. `facebook.py` and `ebay.py` selectors are best-effort. They print `[fallback]` warnings instead of crashing — check the dashboard log for which ones are firing on real runs.
3. Quantity parsing in `govdeals.py` JS uses `\((\d{1,5})\)` — brittle. The `dom_fallback` description-priority logic (added 2026-04-17) compensates, but the JS regex is still worth tightening at the source.
4. **Dashboard cost-tracking tile.** `dewatermark_usage.jsonl` exists; surface `today: N calls / cache: M hashes` somewhere on the A/B tab.

## Done (recently completed — kept here briefly so future-Claude knows what changed)

- Watermark idempotency + API cost log + budget caps + offline-by-default — 2026-04-17. See "Dewatermark behavior" above.
- `_extract_quantity` in `dom_fallback.py` now prefers description match over DOM when DOM quantity is `< 20` (the Athens-style bug). Tunable via `DOM_QUANTITY_SUSPICION_THRESHOLD`.
- `folder_name` is now built **after** the LLM finalizes quantity. `govdeals.scrape()` writes screenshots to `~/.listing_automation/scratch/<lot>_<ts>/` and returns metadata; `run.py` calls `govdeals.finalize_folder(meta, primary.quantity)` post-LLM, which mkdir's the real folder and moves screenshots in.
- Dashboard price-confirm UI now actually surfaces: `run.py` emits `progress.emit("price", suggested=N, confirmed=None)` BEFORE blocking, then again with `confirmed=N` after. In no-TTY mode it waits up to `LISTING_PRICE_CONFIRM_TIMEOUT` seconds (dashboard sets this to 120s) for stdin from `/api/runs/stdin`, then auto-accepts on timeout.

## auction_extractors

`auction_extractors/` is a **sibling package** copied in from its own
project (upstream: `/Users/abdelnasser/Desktop/Black_whole_projects/auction_extractors`).
It scrapes GovDeals + Public Surplus for bulk chair lots, caches every
listing in `auction_extractors/state/listings.db` as a permanent archive,
and exposes a **read-only** `get_top_chairs()` — no UI.

The dashboard's `04 Auctions` tab is the UI layer, owned by this project.
It calls `get_top_chairs()` via `GET /api/auctions`, renders cards, and
wires `Launch` / `Queue all` buttons to `POST /api/runs/queue`, which
drains serially through `run.py` (one at a time; Launcher's `UP NEXT`
strip shows what's pending).

**Integration points:**
- `automation/web/app.py` imports `from auction_extractors import get_top_chairs`.
- `/api/auctions?source=gd&n=15&min_qty=50&condition=1&active_only=1&max_stale_days=2`
  — thin wrapper over the API; in-memory 10-min cache keyed by query
  string (condition scoring is 3-10s, so we don't re-run on every click).
  `POST /api/auctions/refresh` busts the cache.
- `/api/runs/queue` replaces the old single-URL `/api/runs/start`
  behavior. Accepts `{url: str}` (legacy) or `{urls: list[str]}`. While a
  run is active, new requests append to `state.pending`. On run end
  `_start_next()` pops the head and spawns the next subprocess.
  `POST /api/runs/queue/clear` drops the pending FIFO.

**Refreshing the auction cache** — from the dashboard:

1. Open `04 Auctions`.
2. Click `⟳ run scraper ▾` → pick GovDeals, Public Surplus, or Both.
3. The `SCRAPE` strip shows live stdout from the subprocess. On finish,
   the cache is busted and cards reload automatically.

The dashboard spawns the scraper as a subprocess with
`cwd=auction_extractors/` (same pattern as `run.py` for the pipeline).
No second server, no cron, no terminal. Scrapes and pipeline runs use
separate state objects so you can `Launch` a listing while a scrape is
in progress.

CLI fallback (for headless / automated runs):

```bash
cd auction_extractors
../.venv/bin/python govdeals_chairs_extraction.py
../.venv/bin/python public_surplus_automation.py
```

**Refreshing the cache — dashboard-driven, no scheduler.** The previous
launchd agent (`com.listing-automation.daily-scrape.plist`) was removed
on 2026-04-21 because it couldn't actually run: macOS TCC blocks
launchd from executing anything under `~/Desktop/`, and every 06:00
fire since Apr 20 died with `Operation not permitted`. We now rely on
two in-dashboard mechanisms instead:

1. **Cache-stats header on the Auctions tab** — shows total lots and
   newest-scraped age, colored green/yellow/red by freshness. Powered
   by `GET /api/auctions/cache-stats`.
2. **Staleness banner** — when the newest `last_seen_at` is older than
   `MAX STALE DAYS` (default 7), a yellow `⚠ Cache is N days old
   · ⟳ scrape GovDeals now` bar renders above the grid. Clicking the
   button triggers the same flow as `⟳ scrape now → GovDeals`.

`scripts/daily_scrape.sh` is still there as a manual entry point —
run `./scripts/daily_scrape.sh` from the repo root whenever you want
to refresh both scrapers without touching the UI. If you want a real
cron/launchd schedule back, move the repo off `~/Desktop/` first
(`~/Code/` is the usual choice) to avoid the TCC block, then reinstate
a plist pointing to the new path.

**Note on dashboard restarts.** Changes to `automation/web/app.py`,
`templates/`, or `static/` require killing the running server and
relaunching `python -m automation.web`. The FastAPI process caches
templates and Python modules.

**Env:** `auction_extractors/.env` holds Ollama URL + scraper flags. The
LLM model (`OLLAMA_MODEL=gpt-oss:120b-cloud`) is load-bearing — benchmarks
showed smaller local models hallucinate quantities. Don't swap without
re-benchmarking via the upstream `quantity_eval/`.

**Day-to-day gotchas (from upstream HANDOFF.md):**
1. Read-only. Empty DB → empty Auctions tab. Run the scrapers to fill it.
2. `image_url` can be empty on older rows; the card renders a 🪑 fallback.
3. GovDeals blocks headless browsers via Akamai → set `HEADLESS=0` in
   `auction_extractors/.env` if you see "Access Denied".
4. Public Surplus `end_date`: the plain-HTTP scrape path (default since
   2026-06-10) fills it from the epoch embedded in each search card; only
   the Playwright fallback path leaves it empty (`active_only` still
   works there via the `last_seen_at` staleness check). PS quantity is
   LLM-only — title-regex ships solely as the `llm_failed`-tagged
   fallback (`USE_LLM_QUANTITY` / `FETCH_PUBLIC_SURPLUS_DESCRIPTION`
   default ON; `PUBLICSURPLUS_USE_API=0` forces the browser scraper).
5. `max_stale_days=2` assumes ~daily scrapes. If you scrape weekly,
   raise it or `active_only` drops everything.
6. **Launch button on Auctions cards is enabled only for GovDeals URLs**
   — `run.py` doesn't understand Public Surplus listings yet.

**Dependencies:** base deps (`requests`, `python-dotenv`, `python-dateutil`)
are in `[project.dependencies]` because `top_chairs.py` is on the query
path. Scraper-only deps (`openai`, `openpyxl`, `claude-agent-sdk`) are
gated behind the `[extractors]` optional-dependencies group. Install
both with `pip install -e '.[dev,extractors]'`.

Full API contract, SQL schema, and the reasoning behind every decision
live in the upstream `HANDOFF.md`. Check it before debugging scraper
internals.

## deals/ — GovDeals auction deal-tracker (v1)

**What it is.** A separate, modular package (strangler-fig, built alongside
the `automation/` monolith — merged 2026-07-03, PR #37) that sweeps *every*
GovDeals lot, watches each one through its close, and records how it ends
(no-bid / low-bid / final price) so we can find lots that expire cheap enough
to buy and resell. **GovDeals-only for v1** (Public Surplus is deferred; the
`SiteAdapter` Protocol in `deals/adapters/base.py` is the seam it plugs into).
Full spec + rationale: `docs/superpowers/plans/2026-07-03-govdeals-deal-tracker-v1.md`.

**Pipeline & modules** (all pure-logic is unit-tested — `pytest tests/deals/`):
- `models.py` — `Lot`/`Snapshot` + `Outcome`/`Lane` enums + `lot_key`.
- `adapters/govdeals.py` — maestro JSON sweep (reuses `auction_extractors`'s key resolver). `discover()` / `refetch()` / `fetch_gallery()`.
- `mapping.py` — `asset_to_lot`: **price = `currentBid`** (never `assetBidPrice`), missing/garbled price **fails loud** (never silent $0), currency preserved. Hero URL = `…/assets/photos/{account_id}/{photo}` (the `{account_id}` subfolder is required — omitting it 404s).
- `llm_provider.py` — **one LLM transport for every deals workload** (added 2026-07-29): provider choice, pacing, circuit breaker. `classify.py` and `llm_steps.py` (the analyze pass) both call `chat(prompt, max_tokens=…)`. Env: `DEALS_LLM_PROVIDER=groq|cerebras|gemini` (else first key present, groq first), `DEALS_LLM_MODEL`, `DEALS_LLM_TPM` (default 5500), `DEALS_LLM_RPM` (default 20).
  - **Pacing budgets tokens, not just requests.** Groq's free tier binds on two axes at once — 14,400 req/day *and* 6,000 tok/min — and the two workloads differ ~5× in prompt size (classify ~290 tok ⇒ ~20 req/min; analyze ~1,500 tok ⇒ ~4 req/min). A flat RPM limiter tuned for one either crawls on the other or 429s. `_Paced` spends a rolling 60s budget of *estimated* tokens (`len//4 + max_tokens`) plus a request count. Overlapping crons can't see each other's usage, so `_chat_openai_compatible` also retries a 429 honoring `Retry-After`.
  - Measured demand: classify ~3,062/day + analyze ~1,200/day (capped by `DEALS_ANALYZE_LIMIT=50` hourly; ~2,311/day are actually eligible) ≈ **4,300/day against a 14,400/day ceiling**. Raising `DEALS_ANALYZE_LIMIT` to 100 would clear the funnel and still fit. `track-bidders` uses no LLM.
- `categories.py` + `classify.py` — 3-way category: native code / deterministic code-map / **LLM** + `category_agreement` so LLM-vs-code accuracy is measurable (esp. the ~37% "General Merchandise" catch-all). Two rules learned the hard way (2026-07-29):
  - **A failure is never an answer.** The original built its prompt with `_PROMPT.format(...)` over a template containing the literal JSON example `{"label": …}`; `str.format` read that as a replacement field and raised `KeyError('"label"')` on the first line inside the `try`, before any network call — and a bare `except` returned `("other", 0.0)`. Result: **39,758 rows labelled `other` at confidence exactly 0.0, min = max = 0.0, not one request ever sent**, for weeks, while the sweep reported "classified: 2,937" nightly. `classify_category` now raises `ClassificationUnavailable` and `apply_classification` leaves the three LLM columns **NULL**. Don't reintroduce a default return, and don't `.format()` a string containing JSON.
  - **Model choice is set by requests-per-day, not quality.** The sweep classifies ~3,100 lots/day. Groq's `llama-3.3-70b-versatile` allows 1,000 req/day (drops 2/3 of every sweep); `llama-3.1-8b-instant` allows 14,400 — hence the default. On 15 real lots the two agreed on 14. Check any provider/model with `python scripts/check_llm_provider.py [--headroom <model>…]`, which prints the live `x-ratelimit-*` headers. **Cerebras is not free on a new org** — a fresh account sits at $0.00 and returns HTTP 402 on every call.
- `backfill_classify.py` — drains the ~41k lots the bug left unclassified, rate-limited and resumable (a filled row stops being pending, so it is a safe no-op once caught up). `--reset-fakes` blanks the provable `other`/0.0 rows. Runs hourly on Render as `deals-backfill-classify`; locally via `python -m deals.cli backfill-classify` or `scripts/backfill_classifications.py`.
- `fees.py` — `landed_cost`: the bid is **not** the cost; adds buyer premium + tax + freight.
- `watcher_logic.py` + `watch.py` — closing-watcher. Trusts `end_utc` and re-reads it each poll (absorbs the anti-snipe extension; increment is seller-configurable, never hardcoded). Closed lots vanish from search → outcome = last snapshot before the drop. **No-bid = `bid_count==0` only** (a 1-bid lot sits at the opening price).
- `store.py` — Supabase `deal_lots` + `deal_snapshots`. PK `(asset_id, account_id, auction_id)` so a relist can't clobber a prior auction's outcome; outcome columns are never overwritten by a re-sweep; snapshots are change-gated. DDL of record: `scripts/sql/deals_schema.sql`.
- `archive.py` — downloads hero+gallery → Supabase `listing-images` bucket (content-addressed paths keyed on the cache-buster-free URL, idempotent). Images are archived **CDN-downsized** (`?h={DEALS_ARCHIVE_IMG_HEIGHT}&webp=true`, default 800px webp ≈ 40 KB each vs 200–800 KB full-res) to control Supabase storage spend; set `DEALS_ARCHIVE_IMG_HEIGHT=0` for full-res. Two entry points: inline during `discover` for 0-bid seating candidates, and `archive-active` (CLI) which backfills every active un-archived lot zero-bid-first/soonest-ending-first with `--limit` + `--max-mb` guards. Galleries come from the **maestro per-lot detail endpoint** (`POST /assets/{asset_id}/{account_id}/false`, body `{"businessId":"GD","siteId":1}` — the body is load-bearing, without it `assetPhotos` returns empty; headers need `x-api-correlation-id`). No browser needed. The same endpoint also returns full description/specs — it's the seam for exact contested-lot final prices (previously thought not to exist).
- `bidders.py` — **rival-bidder intelligence.** The search firehose gives a numeric `highBidder` but no name; the only endpoint that names anyone is `GET /bids/bidbox/GD/{asset}/{account}/{auction}` (`GovDealsAdapter.fetch_bid_state`), which returns `highBidderUsername` masked to two characters (`ja*****`) plus `visitors`/`hits`/`watcherCount`. GovDeals publishes **no bid history**, so a lead change only exists while it's live — this is sampling, not backfill, and rows we miss are gone. Writes `deal_bid_observations`, one row per observed change of `(bid_count, current_bid, high_bidder)` (traffic drift alone doesn't write, or the lead changes drown in noise). Rolls up into the `deal_bidder_rivals` view: bidder id → handle stem, lots led, states, categories, lots won. DDL of record + rationale: `scripts/sql/deal_bid_observations.sql`. Kept apart from `deal_snapshots` on purpose — snapshots answer "is this lot still cheap?", observations answer "who keeps beating us?", and the two want different cadences.
- `discover.py` / `digest.py` — orchestration + the 0-bid Telegram digest (`deal_candidates` view: `scripts/sql/deal_candidates_view.sql`).
- Viewer: `GET /deals/{asset_id}/{account_id}/{auction_id}` in `automation/web/app.py` rebuilds a listing from our store after GovDeals removes the page.

**How to run** (from repo root, venv active):
```bash
.venv/bin/python -m deals.cli init-schema                    # create tables (once)
.venv/bin/python -c "from automation import db; from deals.digest import VIEW_SQL; db.execute(VIEW_SQL)"  # create deal_candidates view (once)
.venv/bin/python -m deals.cli discover --categories 372      # narrow-first: furniture (cluster: 372,47B,47C,47A,46,47D,28E,266)
.venv/bin/python -m deals.cli archive-active --limit 100 --max-mb 60   # backfill images for active lots before they expire
.venv/bin/python -m deals.cli watch-once                     # one poll pass over lots due for polling
.venv/bin/python -m deals.cli track-bidders --favorites       # who's leading the lots you starred
.venv/bin/python -m deals.cli track-bidders --category all --title-like chair --min-bids 1 --limit 150

.venv/bin/python -m deals.cli digest                         # format + Telegram-send the 0-bid <24h candidates

.venv/bin/python scripts/check_llm_provider.py               # is the classifier's LLM actually reachable?
.venv/bin/python scripts/check_llm_provider.py --headroom llama-3.1-8b-instant   # live rate-limit headers
.venv/bin/python -m deals.cli backfill-classify --limit 450   # drain the unclassified backlog
```
`discover` with no `--categories` sweeps the full furniture cluster + General Merchandise. Tests: `.venv/bin/python -m pytest tests/deals/ -q` (note: this venv has no `pytest` console script — use `python -m pytest`).

**Config gates (not code — required for full function):**
- `SUPABASE_STORAGE_URL` / `SUPABASE_STORAGE_KEY` — needed for image **byte**-archiving (`archive.py`). Absent → uploads no-op (rows still land, per-lot error-isolated; hero images still *display* off the CDN). **In `.env` since 2026-07-06** (copied from `facebook_scraper_Claude/.env`; `SUPABASE_STORAGE_URL` is the project base — `archive.py`/`listing_images.py` append `/storage/v1/...`). Storage budget: Supabase free tier is 1 GB and the shared `listing-images` bucket already carries ~200 MB of inventory photos — watch `storage.objects` before big backfills.
- `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` — needed for `digest` to actually send (else `telegram_not_configured`). Not in the current `.env`.
- `BLACKWHOLE_DB_URL` (Supabase) is in `.env`. **LLM: `GROQ_API_KEY` + `DEALS_LLM_PROVIDER=groq`** (added 2026-07-29 — the key was already in `facebook_scraper_Claude/.env`). **The whole pipeline runs on Groq's free tier**; nothing here needs a paid account. `GEMINI_API_KEY` is present but its **prepay balance is spent** (`429 RESOURCE_EXHAUSTED — "Your prepayment credits are depleted"`) and nothing reads it any more — `llm_steps.py` was ported off hardcoded `gemini-2.5-flash` onto `llm_provider.py` on 2026-07-29. `CEREBRAS_API_KEY` is set but **unusable**: a new Cerebras org starts at $0.00 and returns HTTP 402 on every call (their free tier is gone), so don't switch `DEALS_LLM_PROVIDER` to it without buying credits.

**Deals cron (Render, 2026-07-17 spec; `deals-bidders` added 2026-07-28, `deals-backfill-classify` 2026-07-29):** six cron services in `render.yaml`, all dispatching through `scripts/deals_cron.sh` (committed script — avoids the inline `sh -c` quote-mangling that broke run_discovery.sh's predecessor): `deals-discover` (every 6h, `discover --categories all --max-pages 200` — `--categories all` sweeps the **whole site** via an empty maestro `categoryIds`), `deals-watch` (`watch-once`, every 20 min), `deals-analyze` (`analyze`, hourly), `deals-digest` (`digest`, 13:00 UTC ≈ 9am ET), `deals-bidders` (`track-bidders` over contested chair lots, every 30 min), `deals-backfill-classify` (`backfill-classify --limit 450 --rpm 18`, hourly at :30 — 10,800/day, under Groq's free 14,400 with room for the sweep's own ~3,100). All six pull `fromGroup: blackwhole-secrets`; the operator must set `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` / `COMPS_URL` / `COMPS_KEY` values in the Render dashboard (keys are declared in the `blackwhole-secrets` group, values are never committed).

**v1 status / follow-ups:** live-smoke done (456 furniture lots stored, 334 zero-bid, 20 candidates). Bidbox is now wired — but through `track-bidders` (see `bidders.py`), not the watcher: 115 contested chair lots sampled on the first pass, 78 distinct rivals identified. The still-open piece is having **`watch.py` read the final bid off bidbox at close** instead of inferring it from the last snapshot before the lot drops out of search — lot 17/28505's auction 3 is the proof it's needed, recorded as `low_bid $200 / 1 bid` when it actually ran to 9 bids / $500. Also deferred: Public Surplus adapter, whole-site scale + proxies, per-seller premium calibration, async/batched image archiving (333 sync downloads in one sweep is slow).

## Skill / settings notes
- `.claude/settings.json` allowlists the project's common Bash commands (venv, pip, pytest, playwright, python run.py). Re-pickup needs `/hooks` open or session restart since Claude only watches files that existed at session start.
- For a fresh session with everything pre-allowed: `cd .../listing_automation && claude --permission-mode bypassPermissions`. CLAUDE.md (this file) auto-loads on start.
