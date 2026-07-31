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
close). See the schema file for the exact SQL.

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

# the single cron entrypoint: poll-once always, plus discover for any
# source whose newest observation is older than --discover-stale-hours
# (default 6) or nonexistent
python -m recorder.cli run --discover-stale-hours 6
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
| `purple_wave`   | Official JSON search API (`www.purplewave.com/v1/search/search`), category-id filtered. `sold_sweep()` re-queries with `dateType=past`. | `api_final` — sold sweep returns real winning bids |
| `municibid`     | Server-rendered search-results HTML with an embedded full-result JSON marker, plus per-card HTML for bid counts (paginated, `bs4`-parsed). `sold_sweep()` hits `StatusFilter=completed_only`. | `api_final` — sold sweep returns real "Final Bid" prices |
| `mibid`         | Michigan's own Knockout.js homepage embeds the entire 2,000+ auction catalog as a literal JS array; per-lot bid data confirmed via `GET /AuctionBid/GetBasicInfo?guid=`. | `api_final` — `sold_sweep()` filters the embed to `status=4` (closed) and enriches each match with `GetBasicInfo` for a trustworthy final bid + bid count |
| `gsa`           | Official `api.data.gov` GSA Auctions JSON API (`GSA_API_KEY`, falls back to the shared `DEMO_KEY`). No closed/sold feed exists — a lot simply drops off the active list. | `last_snapshot` — the last observation before a lot vanishes from the active feed is the de-facto close |
| `govdeals`      | Thin import-only wrapper over `deals.adapters.govdeals.GovDealsAdapter` (the maestro JSON search API already built for the `deals/` closing-price tracker). No closed/sold feed used here. | `last_snapshot` |
| `public_surplus`| Independent plain-HTTP scrape of the server-rendered `publicsurplus.com` search + detail pages (legacy JSP, no JSON API). | `last_snapshot` — **closed/removed lots return HTTP 401** (a login wall), not 404 and not a distinguishable "closed" page, so `poll()` reads a 401 as `status='gone'`. Documented in detail in `recorder/sources/public_surplus.py`'s module docstring. |

## Coverage metric (the Phase-0 done-measure)

`coverage --days N` (default 7) reports, per source, how many lots whose
latest-known `end_date` fell within the last N days got **any** observation
recorded with `observed_at > end_date` ("covered") vs. not ("missed"), plus
a percentage and an `_all` roll-up row. This is the number that matters:
**Phase 0 is done when coverage stays above 90% for 7 consecutive days.**
Below that, the recorder is silently losing comps exactly like not having
one at all — the whole point was never missing a close.

## Deploy notes

**Render (target).** `render.yaml` adds a `recorder-run` cron service
(`*/5 * * * *`, `./scripts/recorder_cron.sh run`, same `blackwhole-secrets`
env group and plan tier as the four `deals-*` cron blocks). The 5-minute
cadence exists specifically to catch closes inside the schedule's 5-minute
"hot" window — drop it to `*/10 * * * *` if Render cron cost becomes a
concern; coverage will degrade gracefully, not catastrophically, since the
confirming poll still fires on the next tick regardless of cadence.
`scripts/recorder_cron.sh` mirrors `scripts/deals_cron.sh` line for line
(committed script, `cd` to repo root, `exec python -m recorder.cli "$@"`) —
that pattern exists because an inline `sh -c "... && ..."` form silently
quote-mangled and exited 127 the first time this project tried it.

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
