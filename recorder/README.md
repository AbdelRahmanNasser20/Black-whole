# recorder — closing-price recorder (BLACKWHOLE-28 Phase 0)

## What it is, and why it exists

`recorder/` sweeps active furniture/seating listings from six government- and
municipal-surplus auction sites — GovDeals, Public Surplus, Purple Wave,
Municibid, MiBid (Michigan), and GSA Auctions — and appends every observation
as an immutable row in Supabase's `listing_snapshots` table. It then polls
each tracked lot on an adaptive cadence that tightens as the lot's close
approaches, because catching the exact closing price (the final bid, or the
last-known state before the listing vanishes) is the entire point.

**Every day without a recorder is a day of comps lost forever.** A closed
auction cannot be re-scraped after the fact — GovDeals, Public Surplus, and
GSA all simply drop a closed lot from their active feeds with no historical
API. Once a close is missed, that data point is gone, permanently, and every
future pricing/margin model built on top of `sold_comps` is that much
blinder. This is why Phase 0 is scoped as "crude, ugly, correct, ship this
week" rather than a polished multi-source pipeline: recording the comp beats
not recording it, every time.

## Schema

`listing_snapshots` (`scripts/sql/004_listing_snapshots.sql`) is
**append-only** — recorder code only ever `INSERT`s into it. No `UPDATE`, no
`DELETE`, anywhere in this package. Each row is one observation of one lot at
one point in time: `source`, `source_lot_id`, `observed_at`, `status`
(`active` | `closed` | `gone`), `current_bid`, `bid_count`, `end_date`, and
`raw` — the untouched source payload as JSONB.

`raw` is sacred: it is never reshaped, filtered, or normalized before being
stored. For a poll that finds a lot has vanished, `raw` is the probe evidence
itself: `{"recorder_probe": {"result": "not_found", "http_status": <int>,
"url": <str>}}`.

`sold_comps` is a **derived VIEW**, not a table — it is recomputed from
`listing_snapshots` on every query, never written to directly. If the
closing-price derivation logic turns out to be wrong, the fix is to `CREATE
OR REPLACE VIEW`, not to re-scrape the past (which, per the paragraph above,
is usually impossible anyway). Its `capture_method` column is `api_final`
when the latest snapshot is `status='closed'` with a non-null `current_bid`
(a real reported winning bid), else `last_snapshot` (the last state we
observed before the lot disappeared — a lower-confidence estimate of the
close). See the schema file for the exact SQL. Migration `014_sold_comps_bidbox_final.sql`
(**PENDING — not applied to prod**) adds `bidbox_final` for GovDeals finals
and drops non-sales (reserve not met, cancelled, no bid) — see "GovDeals
finals" below.

## CLI usage

```bash
# one-shot sweep of active + recently-closed lots, every source
python -m recorder.cli discover

# same, but just one source (useful for smoke-testing / rate-limit debugging)
python -m recorder.cli discover --source mibid

# re-check every tracked lot that's due for a poll right now
python -m recorder.cli poll-once

# Phase-0 done-metric: closed lots vs. how many got a post-close observation
python -m recorder.cli coverage --days 7

# the single cron entrypoint: poll-once always, the GovDeals 7-day SOA
# re-check, plus discover for any source whose newest observation is older
# than --discover-stale-hours (default 6) or nonexistent
python -m recorder.cli run --discover-stale-hours 6

# GovDeals lots that ended in the last N days and never got a bidbox final:
# read the bidbox, print a summary. Writes only with --apply.
python -m recorder.cli finals-backfill --source govdeals --since-days 8 [--limit N] [--apply]

# permanent private archive of closed GovDeals lots (detail, photos, bidbox,
# our timeline) into R2. Dry-run (no request) unless --apply.
python -m recorder.cli archive-backfill --source govdeals|allsurplus --since-days 30 [--limit N] [--lot a/b/c ...] [--apply] [--force]

# LLM analysis of archived lots, cached per lot (runs once; --force re-runs)
python -m recorder.cli archive-analyze [--limit 30] [--lot a/b/c] [--force] [--source allsurplus]

# per-source circuit breaker table; exit 1 if any source is open > 24 h
python -m recorder.cli health
```

`python -m recorder` is an equivalent shorthand for `python -m recorder.cli`
(see `recorder/__main__.py`).

**Startup guard.** Every real command checks that `listing_snapshots` exists
before touching anything else (`SELECT to_regclass('listing_snapshots')`) and
exits `3` with a clear message if the schema hasn't been applied yet, instead
of a raw stack trace. `--help` never triggers this check — argparse exits on
`-h`/`--help` before the guard runs, so `--help` never needs network or DB
access.

**Exit codes.** `0` = clean run. `1` = at least one source failed (discover
or poll) — the other sources still ran; check stderr for `RECORDER ERROR
source=<name> ...` lines. `2` = argparse usage error. `3` = schema not
applied.

## Adaptive polling cadence

Defined in `recorder/schedule.py` (pure functions, no I/O):

| Time to `end_date`                    | Poll interval |
|----------------------------------------|---------------|
| unknown, or > 24h away                 | 6 hours       |
| ≤ 24h away                             | 1 hour        |
| ≤ 1h away                              | 5 minutes     |
| already passed, no post-close observation yet | due immediately (the "confirming" poll) |

Anti-snipe: `end_date` is re-read from the source on every poll. A poll that
finds a lot still active with a *later* `end_date` than previously recorded
just appends a fresh `active` observation and keeps polling on the tightened
schedule — nothing here treats "past end_date" as terminal until a fresh
snapshot actually confirms `closed` or `gone`. A lot leaves the poll set
(`store.tracked_active()`) the moment its latest snapshot is `closed` or
`gone`.

## Per-source access notes

| Source          | Access method                                                                 | Sold-price capture |
|-----------------|--------------------------------------------------------------------------------|---------------------|
| `purple_wave`   | Official JSON search API (`www.purplewave.com/v1/search/search`), category-id filtered. `sold_sweep()` re-queries with `dateType=past`, `filters=...;sold:Yes`, and a computed `dateRanges=<year>,<year-1>` window (see CRITICAL 1 note in `purple_wave.py`'s docstring — `dateType=past` alone sweeps the oldest slice of an 18-year archive, 0 usable comps). **Pending operator sign-off:** `www.purplewave.com/robots.txt` disallows `/v1/` for crawlers — it's the site's own public frontend API (no auth, no anti-bot challenge encountered), but that disallow line hasn't been explicitly cleared with the operator; flag before scaling volume on this source. | `api_final` — sold sweep returns real winning bids |
| `municibid`     | Server-rendered search-results HTML with an embedded full-result JSON marker, plus per-card HTML for bid counts (paginated, `bs4`-parsed). `sold_sweep()` hits `StatusFilter=completed_only`. | `api_final` — sold sweep returns real "Final Bid" prices |
| `mibid`         | Michigan's own Knockout.js homepage embeds the entire 2,000+ auction catalog as a literal JS array; per-lot bid data confirmed via `GET /AuctionBid/GetBasicInfo?guid=`. | `api_final` — `sold_sweep()` filters the embed to `status=4` (closed) and enriches each match with `GetBasicInfo` for a trustworthy final bid + bid count |
| `gsa`           | Official `api.data.gov` GSA Auctions JSON API (`GSA_API_KEY`, falls back to the shared `DEMO_KEY`). No closed/sold feed exists — a lot simply drops off the active list. **`bid_count` is really `biddersCount`** (GSA doesn't publish a bid-count field) — it's the number of distinct bidders, not the number of bids; don't read it as a bid-count in comps analysis. | `last_snapshot` — the last observation before a lot vanishes from the active feed is the de-facto close |
| `govdeals`      | Thin import-only wrapper over `deals.adapters.govdeals.GovDealsAdapter` (the maestro JSON search API already built for the `deals/` closing-price tracker). After close, the per-lot bidbox (`fetch_bid_state`). | `bidbox_final` — see "GovDeals finals"; `last_snapshot` only when the bidbox is purged (204) |
| `allsurplus`    | Same maestro API as `govdeals` with `businessId` `GI` (`recorder/sources/allsurplus.py`, a `GovDealsSource` subclass): whole GI catalog (~1,470 live, 13 pages, ≤ `RECORDER_ALLSURPLUS_MAX_PAGES` 40) every discover. Any GI asset — even one the GovDeals sweep returns — is recorded here, never under `govdeals`; `AD` lots stay `govdeals`. **Multi-currency** (USD/EUR/GBP/ZAR/CNY/AUD/BRL): `raw.currencyCode` is the lot's currency; never aggregate finals across currencies (`sold_comps.currency`, migration `019_allsurplus_source.sql`, PENDING). | `bidbox_final` — same bidbox, same rules |
| `public_surplus`| Independent plain-HTTP scrape of the server-rendered `publicsurplus.com` search + detail pages (legacy JSP, no JSON API). | `last_snapshot` — **closed/removed lots return HTTP 401** (a login wall), not 404 and not a distinguishable "closed" page, so `poll()` reads a 401 as `status='gone'`. Documented in detail in `recorder/sources/public_surplus.py`'s module docstring. |

## GovDeals finals (bidbox)

**Problem.** The search sweep only sees live lots, so every GovDeals close
used to be `last_snapshot` — the last bid seen before the lot vanished.
Soft close adds ~3 min per late bid, so that missed the finish: an audit
found 10 of 15 lots 15-73 % low (5282/3780/2: recorded $460, sold $1,725).

**Fix.** GovDeals' per-lot bidbox
(`GET /bids/bidbox/GD/{asset}/{account}/{auction}`) keeps answering after
close with the final `currentBid`, `bidCount`, the extended
`assetAuctionEndDateUTC` and a closed `assetStatusCd`. `poll()` sends every
tracked lot whose stored `end_date` has passed to the bidbox (≤1 request/s)
before anything else:

| Bidbox says | Recorded |
|---|---|
| `STA`, clock in the future (extended) | `active` row with the new end time + bid; keeps polling |
| `STA`, clock passed < 15 min (`deals.tracking.CLOSE_GRACE`) | nothing — read again next run |
| any other code, or `STA` past the grace | `closed` row, `current_bid`/`bid_count`/`end_date` from the bidbox |
| 204 (purged) | old path: `fetch_detail` corroboration → `gone` (`last_snapshot`), logged `RECORDER INFO` |
| network / HTTP error / no `currentBid` | nothing — retry next run |

Outcome (names match `deals.tracking.close_outcome` in PR #105):
`RNM`/`CNB` → `reserve_not_met`, `CAN` → `cancelled`, 0 bids → `no_bid`,
`SOA`/`SOL` → `sold`, anything else (`CLO`, `HFR`, stuck `STA`) → `unknown`
with the raw code kept. Codes seen live 2026-09-28: STA, SOA, RNM, CNB.

**Where it's stored — no new column.** `raw` is
`{"recorder_capture": {"method", "status_code", "outcome", "http_status",
"url", ...}, "bidbox": <untouched payload>}`. `method` is `bidbox_final`,
`bidbox_recheck`, or `bidbox_live` (the extended case). The current
`sold_comps` view already reads these closed rows as `api_final`; PENDING
migration `014_sold_comps_bidbox_final.sql` labels them `bidbox_final`,
appends `outcome`/`status_code`, and drops non-sales.

**7-day re-check.** `run` re-reads the bidbox once for each `SOA` final
whose row is ≥7 days old (≤20 per run), to catch payment default / relist.
It always writes one `bidbox_recheck` row (`changed: true|false`; on a 204
it carries the prior numbers with `result: purged`) — that row is also the
"done" marker, so each lot is re-checked exactly once. Re-check rows skip
change-gating on purpose.

**Backfill.** `finals-backfill` finds GovDeals lots whose last known clock
fell in `--since-days`, whose latest row is `closed`/`gone`, and that have no
bidbox row; it reads each bidbox and prints: checked, with final, purged,
outcomes, and a sample of last_snapshot vs bidbox final. Dry-run by default;
`--apply` writes the terminal rows (through `filter_changed`).

**DB backoff.** The finals/re-check reads back off when the Supabase session
pooler is full (`max clients reached`) instead of failing
(`store._read_with_backoff`).

## GovDeals scope — "track everything" (2026-09-29)

`RECORDER_GOVDEALS_SCOPE=all` (the default) makes `discover` sweep the whole
site: one unfiltered search sorted by close time, paged until it runs dry
(≤ `RECORDER_GOVDEALS_ALL_MAX_PAGES`, 300). `furniture` is the old chairs
cluster + `FURNITURE_TERMS`.

Measured 2026-09-29: **27,578 live lots = 230 pages ≈ 4 min at 1 req/s, ~76 MB
of JSON.** Whole-site closes run ~4,200/day (deal_lots, Jul–Aug: ~29k/week).

Per-run requests, and how a 5-minute run stays bounded:

| Step | Requests | Bound |
|---|---|---|
| discover (only when stale, ≥12 h on Render) | ~231 search pages | `ALL_MAX_PAGES`; ≤1 req/s |
| poll: past-end lots → bidbox | ≤120 | `RECORDER_GOVDEALS_BIDBOX_MAX_PER_RUN`, **just-closed first**; the rest wait a run |
| poll: upcoming lots → search refetch | usually 1–5 pages | only lots closing inside `RECORDER_GOVDEALS_REFETCH_HORIZON_HOURS` (24); ≤ `RECORDER_GOVDEALS_REFETCH_MAX_PAGES` (40) |
| 7-day SOA re-check | ≤20 | unchanged |
| lot archive | ≤15 lots × (≤1 bidbox + 1 detail + ≤6 photos) | `RECORDER_ARCHIVE_MAX_PER_RUN`, `RECORDER_ARCHIVE_TIME_BUDGET_S` (90 s) |

Every maestro call (search, bidbox, detail) shares one ≤1 req/s throttle
(`PoliteGovDealsAdapter` — `deals/` stays import-only; the subclass throttles
the inherited `discover`/`refetch`). A run that overruns just makes the next
cron tick a no-op (advisory lock).

**DB guard — read before enabling on a cron.** Every new lot is stored with its
~2.7 KB search `raw`; whole site means ~125k new lots a month → hundreds of MB
a month into `listing_snapshots`, against Supabase's 500 MB read-only ceiling.
`discover` refuses `all` while `pg_database_size` is over
`RECORDER_DB_MAX_MB_FOR_SCOPE_ALL` (450) and sweeps furniture instead, loudly.
It was 595 MB on 2026-09-29, so the switch is inert until space is reclaimed
and `listing_snapshots.raw` gets an archival path (like `deals/raw_archive.py`).

## Lot archive (the moat)

At close the recorder keeps the whole listing — maestro detail (116 keys incl.
the description), gallery, bidbox final, our snapshot/bid timeline — as one
gzip JSON document in R2 at `archive/lots/govdeals/{asset}_{account}_{auction}.json.gz`,
plus up to `LOT_ARCHIVE_PHOTOS` (6) photos resized to ≤1024 px JPEG q75 at
`archive/lots/govdeals/{slug}/{i}.jpg`. Private: never disguised, never public —
the admin page streams it behind the session cookie.

- `run` archives a bounded batch every tick (lots closed ≥1 h ago, newest
  first). Idempotent (document = done marker), read-back verified, R2 unset is a
  hard error for `archive-backfill --apply` and a `RECORDER NOTE` in `run`.
- Non-sales (reserve not met, no bid, cancelled) lose their detail at close
  (204), so they are archived `partial` at once from the bidbox + our snapshots,
  with the sweep's cover photo when the CDN still has it.
- Admin: tab `13 Archive` and `/admin/archive/govdeals/{a}/{b}/{c}` — the page
  rebuilt from the archive, with the LLM analyzer panel (cached, re-run button).
- Sample 2026-09-29: 30 lots, ~436 KB/lot → whole site ≈ 54 GB/month,
  ≈ $9.65/month after a year on R2. Detail + costs:
  `docs/claude-reference/lot-archive.md`.
- Index table `scripts/sql/015_lot_archive_index.sql` is **PENDING**; until it
  is applied the list reads `_meta/` sidecars from R2.

## Source health (circuit breaker, 2026-10-02)

**Problem.** A dead source cost every run: ~46k Public Surplus 30 s connect
timeouts, ~74k "unrecognized page shape", 5.9k Municibid 403s; `discover`
for public_surplus/municibid/mibid re-fired on every 5-minute run because an
aborted discover inserts nothing, so the source never looked fresh. One PS
poll batch could burn ~1 h → the run overran → the advisory lock turned the
next ticks into no-ops → GovDeals closes were missed.

**Fix** (`recorder/health.py`, pure): one breaker per source.
`closed → open` after `RECORDER_BREAKER_FAILURES` (3) failed attempts;
`open → half_open` (one probe) at `next_attempt_at`; success closes it.
Backoff once open = min(`RECORDER_BREAKER_BASE_MIN` (10) min × 2^k,
`RECORDER_BREAKER_MAX_H` (24) h).

| Counts as | |
|---|---|
| failed attempt | discover raised or returned 0 observations (every adapter "aborts" that way); a poll batch that raised, was aborted by its `PollBudget`, or had ≥ 80 % of ≥ 3 lots fail (the `BLOCK_SUSPECT_*` thresholds) |
| success | discover returned observations; a poll batch that inserted ≥ 1 row or had no failed lot |
| neither | a few lots failed, nothing new — no change |

- `run` loads the breaker once, skips open sources (`poll source=X skipped:
  circuit open until …`, `discover source=X skipped: …`), records every
  outcome and saves once. Ends with `run: elapsed=…s`.
- Discover staleness = newest row **or** last clean discover, whichever is
  newer — a quiet source no longer re-discovers every run.
- `poll-once` abandons lots whose `end_date` is more than
  `RECORDER_POLL_ABANDON_DAYS` (14) days past (no row written; reported
  `abandoned=n`).
- `PollBudget` (`sources/base.py`): a per-lot poller (public_surplus,
  municibid, mibid) stops its batch after
  `RECORDER_POLL_MAX_CONSECUTIVE_FAILURES` (10) failures in a row.
- HTTP: `polite_get/post` timeout is `(10, 30)` (connect, read). A 429 is
  returned at once, and the host then fails fast (`RateLimited`, no request)
  until `Retry-After` (default 60 s) has passed.
- Telegram `health` topic pings on open/close transitions only, best-effort,
  never raises. `RECORDER_HEALTH_TELEGRAM=0` silences it.
- `python -m recorder health` prints state / consecutive_failures /
  last_success_at / next_attempt_at / last_error; **exit 1** if a source has
  been open with no success for > 24 h.
- State table `recorder_source_health` = `scripts/sql/018_recorder_source_health.sql`
  (**PENDING — not applied**). Until it is: in-memory per run + a
  `RECORDER NOTE`; `RECORDER_HEALTH_FILE=/path.json` keeps it across runs on
  one machine (laptop/dev only — Render disks are ephemeral).

## Coverage metric (the Phase-0 done-measure)

`coverage --days N` (default 7) reports, per source, how many lots whose
latest-known `end_date` fell within the last N days got **any** observation
recorded with `observed_at > end_date` ("covered") vs. not ("missed"), plus
a percentage and an `_all` roll-up row. This is the number that matters:
**Phase 0 is done when coverage stays above 90% for 7 consecutive days.**
Below that, the recorder is silently losing comps exactly like not having
one at all — the whole point was never missing a close.

`coverage` also prints a `listing_snapshots table size: <pretty>` line
(`pg_total_relation_size` via `pg_size_pretty`) after the table — see
"Storage" below.

**`sold_comps`'s `bid_count > 0` gate** (the view's last `WHERE` clause)
deliberately excludes closed/gone lots whose `bid_count` came back honestly
`NULL` rather than `0` — a lot the recorder never got a priced observation
for isn't a "no-bid close," it's a gap in *our* coverage. Two known sources
of that gap today: residual `municibid` lots whose `sold_sweep()` enrichment
call didn't land before the lot dropped off the completed list, and
`public_surplus` lots that closed before the recorder's first poll caught
them (no `sold_sweep()` on that source — see the per-source table above).
Those lots are missing from `sold_comps` on purpose, not a bug — don't
"fix" the gate to include them without a real price.

## Deploy notes

**Render (target).** `render.yaml` adds a `recorder-run` cron service
(`*/5 * * * *`, `./scripts/recorder_cron.sh run --discover-stale-hours 12`,
same `blackwhole-secrets` env group and plan tier as the four `deals-*` cron
blocks). The 5-minute cadence exists specifically to catch closes inside
the schedule's 5-minute "hot" window — drop it to `*/10 * * * *` if Render
cron cost becomes a concern; coverage will degrade gracefully, not
catastrophically, since the confirming poll still fires on the next tick
regardless of cadence. `scripts/recorder_cron.sh` mirrors
`scripts/deals_cron.sh` line for line (committed script, `cd` to repo root,
`exec python -m recorder.cli "$@"`) — that pattern exists because an inline
`sh -c "... && ..."` form silently quote-mangled and exited 127 the first
time this project tried it.

**Storage.** The size line showed it was needed, so change-gating landed
2026-08-28. `discover()` has no memory of what it has already seen, so every
stale-refresh used to re-INSERT a fresh row for every active lot even when
nothing about it had moved — **31,383 of 47,115 rows were strictly interior to
a run of identical `(status, current_bid, bid_count, end_date)`**, two thirds
of a 112 MB table. `store.filter_changed()` now drops those before the insert;
`store.is_changed()` holds the rules and is pure, so it is tested without a
database (`tests/recorder/test_change_gating.py`).

One rule is worth knowing before you touch it: an observation later than the
lot's `end_date` is kept **even when every field is identical**, because
`coverage()` decides a close was *caught* by finding a snapshot past
`end_date`. Gate that away and a caught close is misreported as missed — the
one thing this recorder exists to get right. Anti-snipe extensions also keep
landing, because `end_date` is itself a gated field.

The historical duplicates were removed by `scripts/compact_listing_snapshots.py`
(backed up to R2 first, `sold_comps` checksummed before and after).
`--discover-stale-hours 12` remains the Render cron's value but is no longer
load-bearing as a stopgap. `poll()`'s inserts were never the problem — they
only fire for lots due per `schedule.is_due`, which is already sparse.

**Interim launchd (laptop, today).** Until the Render cron is deployed,
`scripts/recorder_local.sh` + `scripts/launchd/com.blackwhole.recorder.plist`
run the same `recorder.cli run` command locally every 300s. The plist is
**not installed by this commit** — install it by hand once the recorder has
been smoke-tested:

```bash
mkdir -p ~/.blackwhole/logs
cp scripts/launchd/com.blackwhole.recorder.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.blackwhole.recorder.plist
```

`recorder_local.sh` hardcodes the **main checkout's** venv Python
(`/Users/abdelnasser/Projects/blackwhole/listing_automation/.venv/bin/python`)
because launchd has no shell profile / venv activation, and this file may
currently live inside a worktree that has no venv of its own. Retire this
plist (`launchctl unload` + delete) once `recorder-run` is confirmed healthy
on Render.

**GSA_API_KEY.** The `gsa` adapter works out of the box against the shared
public `DEMO_KEY` (rate-limited, printed as a warning on every use), but for
production cadence sign up for a free key at
[api.data.gov/signup](https://api.data.gov/signup/) and set `GSA_API_KEY` in
`.env` (local) / the `blackwhole-secrets` env group (Render). Free tier is
5,000 calls/day, 5 calls/5s.
