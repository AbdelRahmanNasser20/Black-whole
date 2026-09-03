<!-- Moved verbatim from ../../CLAUDE.md on 2026-08-28 (trim to <=8 KB). Original kept as ../../CLAUDE.md.pre-trim-2026-08-28 -->

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
