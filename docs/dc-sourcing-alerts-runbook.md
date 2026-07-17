# DC / DMV sourcing alerts (BLACKWHOLE-19)

Sourcing-alert pipeline for the DC / MD / VA region. There is currently **zero**
DC inventory (closest stock is Atlanta, ~640 mi south); this watches the DMV
government-surplus pipeline so the first DC-area chair lot can be sourced.

Package: `automation/sourcing/` — standalone and additive. It reuses the shipped
geo helpers (`automation.alerts.geo`) and does **not** restructure the deals
pipeline.

## What it does

Alerts on any **new chair lot within 100 mi of DC**, across DC/MD/VA — the piece
the existing saved-search runner (`deals.saved_search_alerts`, single-`state`
filter only) can't express. A lot matches when it is:

1. a **chair lot** — its title or `canonical_category` reads as chairs
   (`deal_lots` has no `chair_type` column, so this is inferred), AND
2. **within 100 mi of DC** — using the lot's own `lat`/`lng` when present, else a
   `zip` → state-centroid fallback. A DC/MD/VA lot that only resolves to a state
   centroid passes on an honest same-region degrade (centroids carry ±200 mi of
   noise) rather than being dropped on a false distance test.

Every match carries an audit reason (`distance_miles`, `geo_precision`,
`in_dmv_state`).

## Configuration

The region is defined in `automation/sourcing/dmv.py`:

- `DC_ANCHOR = (38.8951, -77.0364)` — downtown Washington, DC
- `RADIUS_MILES = 100`
- `DMV_STATES = {DC, MD, VA}`

## CLI

```bash
# Print the DMV saved-search definitions + SQL to register them (NEVER writes)
python -m automation.sourcing searches

# Print the follow-through listing plan (list once sourced)
python -m automation.sourcing channels

# Dry-run: read deal_lots, print the matched-lot digest (SENDS NOTHING)
python -m automation.sourcing preview

# Attempt a real Telegram send (only if TELEGRAM_* is configured)
python -m automation.sourcing preview --send
```

Send is **OFF by default**. `preview` reads `deal_lots` (read-only) and prints
the digest; it sends to Telegram only with `--send` AND a configured bot.

## 1. Register the saved searches

`deal_lots` is GovDeals-only, so the six DMV saved searches (one per DC/MD/VA per
source) are registerable for the GovDeals rows into the `saved_searches` table.
`python -m automation.sourcing searches` prints ready-to-run `INSERT`s — run them
yourself against the DB (this tool never writes). Once registered with
`alert=true`, they fire through the existing hourly `deals-analyze` cron
(`deals.saved_search_alerts.run_saved_search_alerts`).

> The saved-search runner filters a single `state` only — the **100-mi radius**
> is applied on top by `python -m automation.sourcing preview`. PublicSurplus
> rows are intent-only (they don't flow through `deal_lots`); source them via the
> `auction_extractors/public_surplus_automation.py` scraper.

## 2. Wire the alert into the cron (optional, when going live)

To fire the radius-scoped digest automatically, call
`automation.sourcing.digest.run_dmv_sourcing_alert(send_enabled=True)` from a
scheduled job (e.g. alongside the hourly `deals-analyze` pass). It is a small
additive hook — no change to the deals pipeline is required. Left un-wired for
now so nothing sends until Abdel flips it on.

## 3. Follow-through — list once sourced

Once a DMV lot is won, cross-post per `automation/sourcing/dmv.py::FOLLOW_THROUGH`
(also printed by `... channels`):

| Metro | Platforms | Craigslist |
| --- | --- | --- |
| Washington DC | FB / eBay / Craigslist | washingtondc |
| Baltimore MD | FB / eBay / Craigslist | baltimore |
| Northern Virginia | FB / eBay / Craigslist | nova |
| Richmond VA | FB / eBay / Craigslist | richmond |

## Tests

`tests/test_sourcing_dmv.py` — 21 tests, fully DB-free (lots injected). Covers
chair detection, the 100-mi radius (in/out), state-precision degrade, the "new
only" filter, digest formatting, and send-off-by-default.
