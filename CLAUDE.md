# Listing Automation — CLAUDE.md

## How to answer
Concise, direct — answer the question, show the result, stop. No restating, no preambles, no closing summaries, no narrating tool calls. Expand only when asked ("explain…", "walk me through…") or the task needs it.

## What this project is
Python + Playwright pipeline that turns a GovDeals URL into a Facebook Marketplace draft + eBay draft. Source-of-truth spec is `GovDeals_Automation_Blueprint.md`. Phases: scrape → llm → download → dewatermark → fb → ebay.
Also hosts: the FastAPI public site + admin dashboard (`automation/web/`), the Supabase `inventory` ledger, the vendored `auction_extractors/` scraper (read-only), and the `deals/` GovDeals deal-tracker (Render crons).
Detail: `docs/claude-reference/` (index at bottom).

## HARD RULES (verbatim — don't reintroduce fixed bugs, don't regress)

**Browser / scrape:**
- Headless mode hits Akamai 403 on GovDeals. Always non-headless. `persistent_context(headless=False)` is the default — don't flip it.
- `ClaudeCodeExtractor` only works when Claude Code is the orchestrator with a real TTY. From the dashboard subprocess it'll hang. The `default_extractors()` picker handles this automatically.
- Gotchas #1-#9 are all fixed — don't reintroduce (see `gotchas.md`).

**Dewatermark:**
- Dewatermark is **dewatermark.ai API only** (`DEWATERMARK_API_KEY` in `.env`). No local image processing, no heuristic watermark detection.
- Global response cache `~/.listing_automation/api_cache/<sha>.bin`: once a hash is here, **no future run anywhere on this machine ever calls the API for that hash again.**
- `RunBudget` caps: `MAX_API_CALLS_PER_RUN=50`, `MAX_API_CALLS_PER_DAY=250`. `DEWATERMARK_OFFLINE` defaults OFF; set `=1` to freeze spend while developing.
- **No fallback when the API fails.** Original stays in `_originals/`, emit `dewatermark:degraded`.
- Don't reintroduce histogram/heuristic checks — only reject if cleaned bytes are missing, empty, or equal the original (Gotcha #9).

**Lot photos (`automation/lot_images.py`):**
- **Cloudflare R2 is the canonical backend. Supabase Storage is dead** (402). **Never write a new Supabase Storage URL into `inventory`**, and treat any row still carrying one as broken.
- Resolving is centralized in `automation/lot_images.py`. Don't hand-roll `DOWNLOAD_ROOT / folder_name` again. Precedence is always `durable DB URLs → local disk → nothing`, never host-specific.
- Local disk is a fallback, never an answer to "can the bot show a buyer this lot" — `has_usable_images()` ignores disk on purpose.
- Run `scripts/check_offerable_images.py` (`--http`) after flipping any lot to `crm_offerable`.

**Inventory ledger (READ `inventory-ledger.md` BEFORE TOUCHING run.py OR app.py):**
- **Preserved on re-upsert:** user edits never get stomped by a re-run (`quantity_remaining`, `status`, `price_per_chair`, `hero_image`, platform URLs).
- Don't add columns to `auction_extractors/state/listings.db` for publish state. That DB is the upstream scrape cache — keep the read-only separation.
- Don't re-introduce "emit URL, forget URL" in `facebook.py` / `ebay.py`. The URL must flow back into `inventory.set_platform_url()` or the dedup check stops working.
- Don't bypass the ledger to work around a "stuck" row. Delete/edit via the admin Inventory tab or `DELETE /api/inventory/{lot_id}`, don't hack the DB directly.
- Sold showcase = `status IN ('sold_out','lost_sold_out')` AND `quantity_original > 0` AND has a photo — don't loosen it without a plan for the junk rows.
- **The FB catalog feed and CRM recommendations are status-gated** (`CATALOG_FEED_STATUSES` / `ROUTABLE_STATUSES`), so an archived lot can never be offered as stock. Keep it that way.

**DB:**
- All DB code goes through vendored `automation/db.py` (psycopg over Supabase). Schema lives in Supabase (managed via migrations), not created at runtime. RLS is **disabled** on all tables — enabling it is a still-open task.
- `auction_extractors/state/listings.db` stays SQLite-only and read-only to this repo.
- **Supabase free tier goes READ-ONLY at 500 MB** (hit 2026-08-28). **The rule going forward:** any new column that stores a provider response, a description, or any other unbounded blob needs an archival path *before* it ships, not after it fills the disk.
- `archive-raw`: export → read the object back → compare keys → only then null. R2 unconfigured is a hard error, never a silent skip.
- `VACUUM FULL` takes an ACCESS EXCLUSIVE lock — suspend the Render crons first if a blocked run would matter.

**auction_extractors:**
- `OLLAMA_MODEL=gpt-oss:120b-cloud` is load-bearing. Don't swap without re-benchmarking via the upstream `quantity_eval/`.
- Launch button on Auctions cards is enabled only for GovDeals URLs — `run.py` doesn't understand Public Surplus yet.

**deals/:**
- `mapping.py`: **price = `currentBid`** (never `assetBidPrice`), missing/garbled price **fails loud** (never silent $0). Hero URL needs the `{account_id}` subfolder.
- **A failure is never an answer.** `classify_category` raises `ClassificationUnavailable`; LLM columns stay NULL. Don't reintroduce a default return, and don't `.format()` a string containing JSON.
- **No-bid = `bid_count==0` only.** Outcome columns are never overwritten by a re-sweep.
- Don't switch `DEALS_LLM_PROVIDER` to cerebras without buying credits (HTTP 402). Gemini prepay is spent.
- Render secrets (`TELEGRAM_*`, `COMPS_*`) are set in the dashboard; values are never committed.
- Tracking list (`tracked_lots`): polling runs **in the web process** (`_tracking_loop`), not a Render cron; closed = `assetStatusCd != 'STA'` or clock+15 min; on close `record_outcome` writes the exact final into `deal_lots`. Detail: `docs/claude-reference/deals.md`.

**Ops:**
- Changes to `automation/web/app.py`, `templates/`, or `static/` require killing and relaunching `python -m automation.web`.

## Key paths / commands / env
- Run: `python run.py --login-only` (first time) → `python -m automation.web` → site `http://127.0.0.1:8765/`, admin `/admin`.
- Pipeline: `python run.py <govdeals-url>` (`--price`, `--skip-*`, `--force-republish`).
- `.env` (gitignored): `DEWATERMARK_API_KEY`, `GEMINI_API_KEY`, `OPENAI_API_KEY`, `BLACKWHOLE_DB_URL`, `R2_*`, `SUPABASE_STORAGE_URL/KEY`, `GROQ_API_KEY` + `DEALS_LLM_PROVIDER=groq`, `TELEGRAM_BOT_TOKEN/CHAT_ID`. `auction_extractors/.env`: Ollama + `HEADLESS=0`.
- Chrome profile: `~/.listing_automation/chrome_profile/` (logged into FB + eBay). Photos: `~/Desktop/Banquet chiars Pictures/`.
- Photos onto a lot: `scripts/backfill_listing_images.py --lot|--missing`; `scripts/import_deal_images.py --lot <key>`.
- Deals: `.venv/bin/python -m deals.cli discover|watch-once|archive-active|archive-raw|track-bidders|digest|backfill-classify|saved-search-alerts|track add|list|sync|history`; `scripts/check_llm_provider.py`; `scripts/reclaim_db_space.py --all`; `scripts/query_cold_archive.py`.
- Dewatermark audit: `python -m automation.dewatermark stats|verify <file>`.
- Tests: `.venv/bin/python -m pytest tests/deals/ -q` (no `pytest` console script).
- Pre-allowed session: `claude --permission-mode bypassPermissions`.
- North-star + cross-repo rules: `../CLAUDE.md` (workspace).

## Read before working on X
| Topic | File |
|---|---|
| Module map: `run.py`, `automation/*`, `llm/`, `web/` routes + admin tabs + APIs | `docs/claude-reference/repo-layout.md` |
| `automation/db.py`, Supabase cutover state, key env | `docs/claude-reference/db-and-env.md` |
| Gotchas #1-#9 (symptom → cause → fix) | `docs/claude-reference/gotchas.md` |
| End-to-end run, run/stdin API curls, first-run status, settings | `docs/claude-reference/runbooks.md` |
| Lot photos: R2 vs Supabase, resolver rules, backfill scripts, guard | `docs/claude-reference/lot-photos.md` |
| Inventory ledger: dedup flow, statuses, sold archive, locations, backfill | `docs/claude-reference/inventory-ledger.md` |
| Dewatermark cache layers, budget, audit tools | `docs/claude-reference/dewatermark.md` |
| Known TODOs (eBay draft URL etc.) + recent Done log | `docs/claude-reference/todos-and-history.md` |
| `auction_extractors/` integration, cache refresh, deps, upstream gotchas | `docs/claude-reference/auction-extractors.md` |
| `deals/` modules, LLM pacing, bidders, CLI, config gates, Render crons, v1 status | `docs/claude-reference/deals.md` |
| DB size / TOAST / R2 raw archive / reclaim procedure | `docs/claude-reference/database-size.md` |
