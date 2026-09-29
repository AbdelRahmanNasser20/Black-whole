## Lot archive — every closed GovDeals lot, kept (2026-09-29)

- Summary: at close the recorder copies the whole GovDeals listing into private R2, so it can be rebuilt after GovDeals purges it.
  - Code: `recorder/lot_archive.py` (archiver + stores), `recorder/lot_analysis.py` (LLM analyzer), `automation/web/lot_archive_view.py` (admin reads).
  - Admin: tab `13 Archive` (`/admin/archive` redirects there) and the rebuilt page `/admin/archive/govdeals/{asset}/{account}/{auction}`.
  - **Private.** Photos are the seller's own, NOT disguised. Everything is under `/admin` or `/api` (session-walled). Never link an archive key from a public page, `/deals`, the storefront or a feed. `tests/web/test_lot_archive_routes.py` guards it.

### Why

- GovDeals keeps a closed lot's detail (116 keys incl. description), gallery and bidbox for a few weeks, then purges it (204).
  - Sold lots: detail answered 26 days after close (5282/3780/2, 28/16539/1).
  - Reserve-not-met / no-bid lots: detail is **already 204** hours to days after close (9 of 9 sampled). The bidbox still answers; the sweep's cover photo is often still on the CDN. So non-sales are archived `partial` at once, from the bidbox + our snapshots.
  - `listing_snapshots` keeps prices only. The archive keeps the listing.

### R2 layout (`slug` = `{asset}_{account}_{auction}`)

| Key | What |
|---|---|
| `archive/lots/govdeals/{slug}.json.gz` | the document — the "done" marker. `detail`, `gallery_urls`, `bidbox`, `search_raw`, `timeline` (our snapshots + bidbox samples), `photos`, `summary`, `completeness` (`full`/`partial`) |
| `archive/lots/govdeals/{slug}/{i}.jpg` | up to `LOT_ARCHIVE_PHOTOS` (6) photos, ≤1024 px, JPEG q75, metadata-free |
| `archive/lots/govdeals/_meta/{slug}.json` | ~450 B list-page summary |
| `archive/lots/govdeals/_analysis/{slug}.json` | cached LLM analysis |

- Order: photos → `_meta` → document. Every photo and the document are read back before the next step. A crash mid-lot is simply redone.
- `summary` is re-derived from the untouched payloads on every read (like the `sold_comps` view) — fix `summarize()`, every archived lot shows the fix.
- Idempotent: a lot with a document costs zero requests. `--force` (with `--lot`) overwrites.
- Stores: R2 (default; `LOT_ARCHIVE_R2_BUCKET` = a separate private bucket). `LOT_ARCHIVE_STORE=local` (+ `LOT_ARCHIVE_LOCAL_DIR`) = dev store on disk. R2 unset = hard error for a write; `run` logs a `RECORDER NOTE` and carries on.
- 🚨 The shared bucket `blackwhole-images` has a public `r2.dev` base. Archive objects are never linked, but anyone who guesses a key (`archive/lots/govdeals/5282_3780_2/0.jpg`) can fetch it. Before the archive grows, set `LOT_ARCHIVE_R2_BUCKET` to a bucket with public access OFF (or block `/archive/*` on the public domain).

### When it runs

- `recorder.cli run` (every 5 min): after poll + re-check, `cmd_archive_pending` archives ≤ `RECORDER_ARCHIVE_MAX_PER_RUN` (15) lots closed ≥1 h ago in the last 30 days, newest first, inside `RECORDER_ARCHIVE_TIME_BUDGET_S` (90 s). `RECORDER_ARCHIVE_ENABLED=0` turns it off.
- A lot is `not_ready` (retry next run) when: closed < 1 h ago; bidbox still live/in grace or unreadable; a SOLD lot's detail is thin (< 20 keys) and the close is < 3 days old.
- Backfill (dry-run by default, no request sent):

  ```bash
  .venv/bin/python -m recorder.cli archive-backfill --source govdeals --since-days 30 --limit 100          # count
  .venv/bin/python -m recorder.cli archive-backfill --source govdeals --since-days 30 --limit 100 --apply  # archive
  .venv/bin/python -m recorder.cli archive-backfill --lot 5282/3780/2 --lot 28/16539/1 --apply [--force]
  .venv/bin/python -m recorder.cli archive-analyze --limit 30 [--lot a/b/c] [--force]
  ```

### LLM analyzer

- Reuses the deals analyzer: identity prompt + parser (`deals.llm_steps`), category prompt + parser (`deals.classify`), `deals.quantity.lot_quantity`, eBay comps + `judge_comps` + `valuation` when `COMPS_URL`/`COMPS_KEY` are set, `deals.fees.landed_cost`.
- Reply budgets are larger than the stock calls (identity 900, category 300). The provider default is now `openai/gpt-oss-120b`, a reasoning model: the stock 300/64 came back empty / cut mid-JSON on real lots. Same failure rule — an unparseable reply is `unavailable`, never a default.
- "Was it a deal?": final per-unit vs the median per-unit of similar closes from `deal_lots` (whole-site history, sold/low_bid) + `sold_comps`, matched on the identity's keywords. Comps whose unit count is unknown are skipped against a multi-unit lot (a "Bulk Stacking Chairs" lot read as 1 chair made $11.50/chair look 0.57× the market). < 3 comps = "not enough comps". Verdicts: ≤0.6× steal, ≤0.85× good deal, ≤1.15× market price, else above market.
- Fair resale: eBay comps (p25–p75) when the Pi service answers; else the model's own per-unit guess ±25%, labelled `llm_estimate` / low confidence.
- Cached once per lot; the page's **re-run** button (`POST /api/archive/govdeals/{a}/{b}/{c}/analyze`) overwrites it. The provider NAME is stored, never the key.

### Scope switch ("track everything")

- `RECORDER_GOVDEALS_SCOPE=all` (default) = whole-site sweep; `furniture` = the old chairs cluster.
- Live count 2026-09-29: **27,578 lots, 230 search pages (120/page), ~76 MB of JSON, ~4 min at 1 req/s.** Cap `RECORDER_GOVDEALS_ALL_MAX_PAGES` (300).
- Every maestro call (search pages, bidbox, detail) shares one ≤1 req/s throttle (`PoliteGovDealsAdapter`); photo downloads use `polite_get` (≤1 req/s per host).
- Per-run bounds: bidbox ≤ `RECORDER_GOVDEALS_BIDBOX_MAX_PER_RUN` (120), just-closed first; the search refetch only takes lots closing inside `RECORDER_GOVDEALS_REFETCH_HORIZON_HOURS` (24) and walks ≤ `RECORDER_GOVDEALS_REFETCH_MAX_PAGES` (40).
- 🚨 **DB guard.** Whole site ≈ 125k new lots/month, each stored with its ~2.7 KB search `raw` in `listing_snapshots` — hundreds of MB a month against a 500 MB read-only ceiling. `run`/`discover` refuse `all` while the database is over `RECORDER_DB_MAX_MB_FOR_SCOPE_ALL` (450 MB) and sweep furniture instead, with a `RECORDER ERROR`. On 2026-09-29 the database was 595 MB, so the switch is inert until space is reclaimed and `listing_snapshots.raw` gets an R2 archival path.

### Costs (measured on the 30-lot sample, 2026-09-29)

- 30 lots: 13.07 MB total, **~436 KB/lot** (docs 4.9 KB, photos 99%). Sold lots with 6 photos run ~0.8–1.2 MB; non-sales with 1 cover photo ~120 KB.
- Whole site (~125k closes/month): **~54 GB/month**. After 12 months ~653 GB → **~$9.65/month**, ~$62 over the first year (10 GB free, $0.015/GB-mo, zero egress). Class A ops ≈ 8 PUTs/lot ≈ 1M/month — at the 1M free-tier line.
- Furniture scope (~2.2k closes/month): ~1 GB/month, inside the free 10 GB for ~10 months.
- Knobs: `LOT_ARCHIVE_PHOTOS=3` roughly halves it.

### Index (migration 015 — PENDING)

- `scripts/sql/015_lot_archive_index.sql` = `lot_archive` table (~250 B/row). **Not applied.** Until it is, the list page reads the `_meta/` sidecars (one LIST per 1,000 lots + one GET each, cached in-process) and the recorder checks "already archived" by listing R2. Fine at hundreds of lots, slow at tens of thousands.
