# BLACKWHOLE-28 Phase 0 — Closing-Price Recorder (crude, ugly, correct)

Notion epic: BLACKWHOLE-28-epic-liquidation-aggregator-platform (P0).
Phase 0 mandate: **every day without a recorder is a day of comps lost forever** — auction
closing prices cannot be backfilled. Ship a crude, correct, append-only snapshot recorder
covering 6 sources THIS WEEK. Beauty comes later; the raw data is sacred.

## What it is

A new top-level package `recorder/` in listing_automation that:
1. Sweeps active furniture/seating listings from 6 sources: **GovDeals, Public Surplus,
   Purple Wave, Municibid, MiBid (Michigan), GSA Auctions**.
2. Appends every observation as an immutable row in a new Supabase table
   `listing_snapshots` (raw source payload in a JSONB column, never touched).
3. Polls tracked lots on an adaptive cadence — 6h far out, 1h inside 24h, **5 min inside
   the final hour**, one confirming poll after `end_date` passes — because catching the
   close is the whole game.
4. Derives `sold_comps` as a **view** over snapshots (recomputable; never re-scrape the past).
5. Exposes a CLI (`discover` / `poll-once` / `coverage` / `run`) driven by cron
   (Render) and an interim local launchd job.

## Global Constraints (binding for every task)

- **DB access**: only via `from automation import db` (`db.fetch_one/fetch_all/execute/executemany`).
  Never `psycopg.connect` directly. `%s` placeholders only — never interpolate values into SQL.
- **Append-only**: recorder code only ever INSERTs into `listing_snapshots`. No UPDATE, no
  DELETE, anywhere.
- **`raw` is sacred**: the untouched source payload dict for that lot observation. For a
  poll that finds the lot deleted, `raw` = `{"recorder_probe": {"result": "not_found",
  "http_status": <int>, "url": <str>}}` (the probe evidence IS the observation).
- `status` values: exactly `active` | `closed` | `gone`.
- `source` values: exactly `govdeals` | `public_surplus` | `purple_wave` | `municibid` |
  `mibid` | `gsa`.
- All datetimes tz-aware UTC (`datetime.now(timezone.utc)`); parse source dates to UTC.
- **Polite throttling**: ≥1.0s sleep between requests to the same host; honest desktop-Chrome
  User-Agent; on 403/429 back off and return what you have — never retry-hammer. No proxies.
- **No edits to `deals/` or `auction_extractors/`** — import-only reuse.
- Anti-snipe rule: trust the source's `end_date` on every poll (re-read it each time). A
  post-end poll that finds the lot still active with a later `end_date` just appends an
  `active` observation with the new `end_date` — the lot stays in the poll set.
- A lot leaves the poll set when its **latest** snapshot has status `closed` or `gone`.
- Adapters that cannot reach their source at runtime must raise/log loudly and return
  partial results — a source returning 0 rows must be visible in output, never silent.
- Tests: offline, on captured fixture payloads, under `tests/recorder/`. Run with
  `/Users/abdelnasser/Projects/blackwhole/listing_automation/.venv/bin/python -m pytest tests/recorder/ -q`
  (the worktree has no venv; the main checkout's venv has requests/httpx/psycopg/pytest).
  Live network calls are allowed during development to capture fixtures — save them under
  `tests/recorder/fixtures/<source>/`.
- The worktree root contains a `.env` (gitignored) with `BLACKWHOLE_DB_URL` — live DB smoke
  is allowed but unit tests must not require the network or DB.
- Follow existing repo idioms (see `deals/` for style: plain functions, dataclasses, no ORM).

## Canonical interfaces (Task 1 builds these; later tasks import them)

```python
# recorder/models.py
@dataclass
class Observation:
    source: str
    source_lot_id: str          # source-native unique id; govdeals uses "asset/account/auction"
    status: str                 # active | closed | gone
    raw: dict
    current_bid: Decimal | None = None
    bid_count: int | None = None
    end_date: datetime | None = None      # tz-aware UTC
    observed_at: datetime | None = None   # None -> DB default now()

# recorder/schedule.py  (pure functions, no I/O)
def poll_interval(now: datetime, end_date: datetime | None) -> timedelta
    # end_date None -> 6h; end passed -> timedelta(0) (confirming poll due now);
    # <=1h to end -> 5 min; <=24h -> 1h; else 6h
def is_due(now: datetime, last_observed_at: datetime, end_date: datetime | None) -> bool
    # now - last_observed_at >= poll_interval(now, end_date); additionally True when
    # end_date has passed and last_observed_at < end_date (the confirming poll)

# recorder/store.py  (all SQL here, via automation.db)
def insert_observations(obs: Iterable[Observation]) -> int
def tracked_active(source: str | None = None) -> list[dict]
    # latest snapshot per (source, source_lot_id) via SELECT DISTINCT ON ... ORDER BY
    # source, source_lot_id, observed_at DESC, filtered to latest-status == 'active'.
    # Returns dicts: {source, source_lot_id, observed_at, end_date, current_bid, bid_count}
def newest_observed_at(source: str) -> datetime | None
def coverage(days: int = 7) -> list[dict]
    # per source: lots whose latest-known end_date fell in the window; how many have ANY
    # observation with observed_at > end_date (covered) vs not (missed); pct.

# recorder/sources/base.py
FURNITURE_TERMS = ["chairs", "seating", "banquet", "folding chairs", "stackable chairs", "office furniture"]
class RecorderSource(Protocol):
    SOURCE: str
    def discover(self) -> list[Observation]              # active-lot sweep on furniture scope
    def poll(self, lots: list[dict]) -> list[Observation] # lots = tracked_active rows due now
    def sold_sweep(self) -> list[Observation]            # sources that serve completed lots; else []
def polite_get(url, *, headers=None, params=None, timeout=30) -> requests.Response
    # shared helper: honest UA, >=1s spacing per host (module-level last-request clock)
def polite_post(url, *, headers=None, json=None, timeout=30) -> requests.Response
```

DDL (Task 1 commits it; controller applies to Supabase): `scripts/sql/004_listing_snapshots.sql`

```sql
CREATE TABLE IF NOT EXISTS listing_snapshots (
  id            BIGSERIAL PRIMARY KEY,
  source        TEXT NOT NULL,
  source_lot_id TEXT NOT NULL,
  observed_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  status        TEXT,
  current_bid   NUMERIC(12,2),
  bid_count     INTEGER,
  end_date      TIMESTAMPTZ,
  raw           JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_listing_snapshots_lot
  ON listing_snapshots (source, source_lot_id, observed_at DESC);
-- sold_comps: DERIVED view; wrong logic later => recompute, never re-scrape
CREATE OR REPLACE VIEW sold_comps AS
WITH latest AS (
  SELECT DISTINCT ON (source, source_lot_id) *
  FROM listing_snapshots
  ORDER BY source, source_lot_id, observed_at DESC
), last_priced AS (
  SELECT DISTINCT ON (source, source_lot_id)
         source, source_lot_id, current_bid, bid_count
  FROM listing_snapshots
  WHERE current_bid IS NOT NULL
  ORDER BY source, source_lot_id, observed_at DESC
)
SELECT l.source, l.source_lot_id,
       COALESCE(l.current_bid, p.current_bid) AS final_price,
       COALESCE(l.bid_count,  p.bid_count)    AS bid_count,
       COALESCE(l.end_date, l.observed_at)    AS sold_at,
       CASE WHEN l.status = 'closed' AND l.current_bid IS NOT NULL
            THEN 'api_final' ELSE 'last_snapshot' END AS capture_method,
       CASE WHEN l.status = 'closed' AND l.current_bid IS NOT NULL
            THEN 'high' ELSE 'medium' END AS confidence
FROM latest l LEFT JOIN last_priced p USING (source, source_lot_id)
WHERE l.status IN ('closed','gone')
  AND COALESCE(l.current_bid, p.current_bid) IS NOT NULL
  AND COALESCE(l.bid_count,  p.bid_count, 0) > 0;
```

---

## Task 1 — Core: models, schedule, store, source base, migration SQL

Create `recorder/__init__.py`, `recorder/models.py`, `recorder/schedule.py`,
`recorder/store.py`, `recorder/sources/__init__.py`, `recorder/sources/base.py`, and
`scripts/sql/004_listing_snapshots.sql` exactly per the canonical interfaces + DDL above.

Details:
- `insert_observations` uses `db.executemany` with an INSERT that passes `raw` through
  `json.dumps(...)` into a `%s::jsonb` placeholder; when `observed_at` is None omit it so
  the DB default applies (build two batches or use `COALESCE(%s, now())`).
- `tracked_active` must implement latest-per-lot with `DISTINCT ON` in a subquery and
  filter `status = 'active'` in the outer query (filtering inside the DISTINCT ON would
  return a stale active row for a lot that has since closed — that is the bug to avoid).
- `coverage(days)`: window = latest-known `end_date` within `now() - days .. now()`. A lot
  counts covered when any observation exists with `observed_at > end_date`. Return
  per-source dicts {source, closed_lots, covered, missed, pct} plus an `_all` roll-up row.
- `polite_get`/`polite_post`: `requests` with UA
  `Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36`,
  a module-level per-host monotonic clock enforcing ≥1.0s spacing, `raise_for_status()`
  NOT called automatically (callers inspect status).
- Tests `tests/recorder/test_schedule.py` (every cadence boundary: far, 24h edge, 1h edge,
  just-before-end, just-after-end confirming poll, after-post-end-observation) and
  `tests/recorder/test_store.py` (Observation→row param mapping incl. Decimal/None/naive-dt
  rejection; SQL of tracked_active contains DISTINCT ON subquery pattern — assert via the
  captured SQL passed to a monkeypatched `automation.db.fetch_all`).
- `tests/recorder/__init__.py` + `tests/recorder/fixtures/.gitkeep`.

Done when: pytest tests/recorder/ passes offline; files import cleanly with
`.venv/bin/python -c "import recorder.store, recorder.schedule, recorder.sources.base"`.

## Task 2 — Official-API adapters: Purple Wave + GSA

Create `recorder/sources/purple_wave.py` and `recorder/sources/gsa.py` implementing
`RecorderSource` (import `Observation`, `polite_get`, `FURNITURE_TERMS` from Task 1).

Purple Wave (verified recon 2026-07-23: **open JSON API**, no anti-bot):
- Search endpoint family: `https://api.purplewave.com/v1/search/search?perPage=2000` style
  (VERIFY live with curl first — the exact host/path/params from a page load of
  purplewave.com search; capture a real JSON response as fixture).
- `discover()`: query furniture/seating terms (or their category param if cleaner), map each
  item to Observation(status='active', current_bid, bid_count, end_date, raw=item).
- `sold_sweep()`: same search with `filters=sold:Yes&dateType=past` (verify param names
  live) → Observation(status='closed') — Purple Wave serves final prices (api_final).
- `poll(lots)`: re-fetch by lot id (per-item endpoint if one exists, else targeted search);
  lot vanished → status='gone' with recorder_probe raw.

GSA Auctions (official api.data.gov API):
- `GET https://api.gsa.gov/assets/gsaauctions/v2/auctions?api_key=...&format=JSON` (verify
  current param shape live). API key from env `GSA_API_KEY`, fallback literal `DEMO_KEY`
  with a printed warning (DEMO_KEY is rate-limited; operator signs up free at api.data.gov).
- GSA serves ACTIVE auctions only → capture_method is snapshot: `discover()` filters
  furniture/seating by name/description terms case-insensitively; `poll(lots)` = one full
  list fetch, match tracked ids; a tracked lot absent from the active list after its
  end_date → status='gone' (that IS the close signal; final price = last snapshot).
- `sold_sweep()` returns [].

Both: parse money to Decimal, dates to UTC. Save ≥1 real captured JSON fixture per source
under `tests/recorder/fixtures/{purple_wave,gsa}/` and write offline parser tests
(`test_purple_wave.py`, `test_gsa.py`): N≥5 items parsed, non-null id/status, bid Decimal,
end_date tz-aware, `raw` round-trips the source item unmodified. Record in the module
docstring which endpoint + params you verified live and when.

Done when: pytest passes offline; one live smoke of each `discover()` returns >0
Observations for purple_wave (GSA may legitimately be thin — >0 auctions parsed overall is
enough even if 0 match furniture; note the count in your report).

## Task 3 — Server-JSON adapters: Municibid + MiBid

Create `recorder/sources/municibid.py` and `recorder/sources/mibid.py`.

Municibid (verified: server-JSON, Cloudflare present but plain requests OK):
- `https://municibid.com/Search/Results?FullTextQuery=chairs` style server-JSON (VERIFY
  live: it may be an endpoint returning JSON or HTML with embedded JSON — capture what it
  actually returns and parse that). Sold lots via `&StatusFilter=completed_only` →
  `sold_sweep()` with status='closed' (Municibid serves final prices).
- discover(): sweep FURNITURE_TERMS; poll(lots): per-lot page/endpoint re-fetch; missing →
  'gone'.

MiBid Michigan (verified: embedded JSON on mibid.michigan.gov, no anti-bot):
- Search/browse pages embed lot JSON (VERIFY live; capture a page, locate the JSON blob —
  typically a `<script>` state object — and parse it). ~39 furniture lots at recon time.
- discover() via search for furniture terms; poll(lots) per-lot page; MiBid shows live +
  final-at-close bids → a post-end poll that still serves the lot with a final price maps
  to status='closed' (api_final-grade); page 404 → 'gone'.

Same fixture + offline-test contract as Task 2 (`fixtures/{municibid,mibid}/`,
`test_municibid.py`, `test_mibid.py`, module-docstring recon notes). If Cloudflare blocks
plain requests on Municibid at runtime, do NOT escalate to browser automation in this task
— make the adapter fail loudly (clear exception message naming the block) and note it in
your report; the epic handles anti-bot escalation later.

Done when: pytest passes offline; live smoke: each `discover()` returns >0 Observations
(if a source is legitimately empty for furniture right now, prove the parser works via the
fixture test and say so in the report).

## Task 4 — Existing-source adapters: GovDeals + Public Surplus

Create `recorder/sources/govdeals.py` and `recorder/sources/public_surplus.py`. Import-only
reuse of existing code — no edits to `deals/` or `auction_extractors/`.

GovDeals — wrap `deals.adapters.govdeals.GovDealsAdapter` (maestro JSON API; the key is
scraped fresh per run and self-heals — never hardcode it):
- `source_lot_id` = `f"{asset_id}/{account_id}/{auction_id}"` (matches `deals.models.lot_key`).
- `discover()`: `GovDealsAdapter().discover(category_ids="372,47B,47C,47A,46,47D,28E,266", search_text="chairs")`
  — the furniture cluster used by deals — mapping each `Lot` to Observation(status='active',
  current_bid=Lot.current_bid, bid_count, end_date=Lot.end_utc, raw=Lot.raw). Also sweep
  `search_text` over the remaining FURNITURE_TERMS with no category filter, cap pages
  modestly (max_pages<=10 per term).
- `poll(lots)`: parse keys back from source_lot_id, use `adapter.refetch(keys)` →
  dict of Snapshots; map to Observations (raw = snapshot's dict via dataclasses.asdict or
  the refetch payload if exposed); a key missing from the refetch result after end_date →
  status='gone' with recorder_probe raw. GovDeals lots vanish at close → last_snapshot
  capture; a refetched snapshot whose status says sold/closed maps to 'closed'.
- `sold_sweep()`: [].

Public Surplus — plain-HTTP search path (see
`auction_extractors/public_surplus_automation.py` for the existing parse: the search cards
embed an epoch end-time; reuse its importable helpers if clean, else re-implement the
minimal search-page parse with `polite_get` — parsing only title/bid/end/id, NOT the LLM
quantity machinery):
- `discover()`: search FURNITURE_TERMS on publicsurplus.com search pages; Observation per
  card (status='active', end_date from embedded epoch, raw = the parsed card dict + page URL).
- `poll(lots)`: per-auction page `https://www.publicsurplus.com/sms/auction/view?auc=<id>`;
  parse current bid/end; ended-with-bid → 'closed' (PS shows the closed page for a while);
  404/removed → 'gone'.
- `sold_sweep()`: [].

Fixtures + offline tests (`fixtures/{govdeals,public_surplus}/`, `test_govdeals_source.py`,
`test_public_surplus.py`). For govdeals the "fixture" is a captured `Lot.raw` dict +
refetch snapshot (JSON files) — test the mapping functions on them offline.

Done when: pytest passes offline; live smoke: govdeals discover >0 (it is high-volume),
public_surplus discover >0.

## Task 5 — CLI, cron, launchd, coverage report, README

Create `recorder/cli.py` (argparse, `python -m recorder.cli <cmd>`), `recorder/__main__.py`
delegating to it, `scripts/recorder_cron.sh`, `scripts/launchd/com.blackwhole.recorder.plist`,
`recorder/README.md`, and the `render.yaml` additions.

CLI commands:
- `discover [--source S]` — for each registered source (registry dict built here in cli.py,
  all 6 adapters): run `discover()` + `sold_sweep()`, `insert_observations`, print per-source
  inserted counts. Per-source try/except: one source failing must not kill the others; a
  failed source prints a loud `RECORDER ERROR source=<s>` line and sets a nonzero exit code
  at the end (after all sources ran).
- `poll-once` — `store.tracked_active()`, filter with `schedule.is_due(now, ...)`, group by
  source, call `poll(due)`, insert, print counts (`polled=N inserted=M gone=K closed=J`).
- `coverage [--days 7]` — print the `store.coverage` table (source, closed_lots, covered,
  missed, pct) — this is the Phase-0 done-metric (>90% target).
- `run [--discover-stale-hours 6]` — poll-once always; additionally run discover for any
  source whose `store.newest_observed_at(source)` is older than the threshold (or None).
  This is the single cron entrypoint.
- Startup guard: exit 3 with a clear message when the `listing_snapshots` table is missing
  (`db.fetch_one` on `to_regclass('listing_snapshots')`), so cron logs show schema-not-
  applied instead of a stack trace.

`scripts/recorder_cron.sh`: `#!/usr/bin/env bash`, `set -euo pipefail`, `cd` to repo root,
exec `python -m recorder.cli "$@"` (mirror the existing `scripts/deals_cron.sh` pattern —
read it first and match it, it exists because inline `sh -c` quoting broke once).

`render.yaml`: add cron service `recorder-run` (`*/5 * * * *`, `./scripts/recorder_cron.sh run`)
following the four existing `deals-*` cron blocks exactly (same env group
`blackwhole-secrets`, same plan tier). Comment noting the 5-min cadence exists to catch
closes; drop to `*/10` if Render cost is a concern.

launchd (interim, laptop): plist running
`/Users/abdelnasser/Projects/blackwhole/listing_automation/.claude/worktrees/BLACKWHOLE-28-phase0/scripts/recorder_local.sh`
every 300s (StartInterval), stdout/err to `~/.blackwhole/logs/recorder.log`. Also create
`scripts/recorder_local.sh`: cd to the script's own repo root (`$(dirname $0)/..`), use the
MAIN checkout's venv python (`/Users/abdelnasser/Projects/blackwhole/listing_automation/.venv/bin/python`)
to run `-m recorder.cli run`. Do NOT install the plist — the controller does that.

`recorder/README.md`: what/why (2 paragraphs, the never-backfill rule), the schema +
sold_comps-is-derived rule, CLI usage, cadence table, per-source access notes (from the
adapter docstrings), coverage metric definition, deploy notes (Render cron + interim
launchd + GSA_API_KEY signup), and the Phase-0 done criterion (7 consecutive days,
coverage >90%).

Tests: `tests/recorder/test_cli.py` — registry contains all 6 sources; `run` calls poll +
conditional discover (monkeypatch store/newest_observed_at + fake sources); per-source
error isolation (one raising source doesn't prevent the next, exit code nonzero).

Done when: pytest passes; `.venv/bin/python -m recorder.cli --help` shows all 4 commands;
`render.yaml` parses as YAML (`python -c "import yaml,sys; yaml.safe_load(open('render.yaml'))"`).
