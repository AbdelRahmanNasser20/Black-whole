# Deals Dashboard (BLACKWHOLE-12) — Design

**Date:** 2026-07-03
**Status:** Approved (chat session, all three scoping questions answered "recommended")

## Goal

An admin surface where the operator can search, filter, and sort every lot
the deals tracker knows about, with honest landed-cost pricing, so "is this
a good deal?" is answerable at a glance. Today the only windows into
`deal_lots` are a per-lot archived viewer, a Telegram digest, and raw SQL.

## Decisions (user-approved)

1. **Location:** new `Deals` tab on the existing admin dashboard
   (`/admin`, dark terminal theme) — same pattern as tabs 01–06.
2. **Ranking:** filters + sorts + a computed **landed cost** column.
   No composite deal score in v1.
3. **Data scope:** `deal_lots` (Supabase) only. The chairs auction cache
   keeps its own `04 Auctions` tab.

## API

`GET /api/deals` in `automation/web/app.py`, querying Supabase `deal_lots`
via `automation.db`. Server-side filtering + pagination (table grows every
discover run).

Query params (all optional):

| param | meaning | default |
|---|---|---|
| `q` | case-insensitive substring over `title` + `description` (ILIKE) | — |
| `category` | exact `canonical_category` | all |
| `state` | exact 2-letter `state` | all |
| `max_bids` | `bid_count <= N` (0 = no-bid only) | — |
| `ending_within` | hours; `end_utc <= now() + N hours` and lot still open | — |
| `status` | `active` (outcome_complete IS NOT TRUE and end_utc > now) / `closed` (outcome_complete) / `all` | `active` |
| `sort` | `ends` / `landed` / `bid` / `bids` / `newest` | `ends` |
| `dir` | `asc` / `desc` | asc for `ends`, desc otherwise |
| `limit` / `offset` | pagination | 50 / 0 |

Sort keys map to SQL columns via a **whitelist dict** (never interpolate
user input): `ends → end_utc`, `landed/bid → current_bid` (landed cost is
monotonic in bid for a fixed fee model, so SQL can sort by `current_bid`),
`bids → bid_count`, `newest → first_seen_at`.

Response shape:

```json
{
  "total": 456,
  "rows": [ { ...deal_lots cols..., "landed_cost": 123.4,
              "govdeals_url": "https://www.govdeals.com/en/asset/{asset_id}/{account_id}",
              "viewer_url": "/deals/{asset_id}/{account_id}/{auction_id}" } ],
  "facets": { "categories": [{"value": "Furniture", "count": 212}, ...],
              "states": [{"value": "TX", "count": 31}, ...] },
  "stats": { "total_lots": 456, "candidates": 25, "ending_24h": 25 }
}
```

- `landed_cost` computed per row with the existing `deals.fees.landed_cost`
  (qty=1) using a `FeeModel` from env:
  `DEALS_BUYER_PREMIUM_PCT` (default `0.125`), `DEALS_TAX_PCT` (default
  `0.0`), `DEALS_FREIGHT` (default `0.0`). Same defaults the digest uses.
- Facets reflect the **full active set** (one grouped query each), not the
  currently-filtered subset — cross-filtered facet counts are out of scope
  for v1. Counts guide, not gate.
- `stats.candidates` = count of `deal_candidates` view;
  `stats.ending_24h` = active lots with `end_utc <= now()+24h`.
- Timestamps serialize as ISO strings (psycopg returns datetimes).

## UI — `Deals` admin tab

New `<button class="tab" data-tab="deals">` + panel in
`templates/index.html`, logic in `static/app.js`, styles in `app.css`
(reuse existing table/filter classes where they exist).

- **Header strip:** `N lots tracked · N candidates (0-bid <24h) · N ending <24h`.
- **Filter bar:** search input (debounced 300ms), category `<select>`
  (from facets, with counts), state `<select>`, `0 bids` checkbox
  (max_bids=0), ending-within `<select>` (any / 6h / 24h / 48h / 7d),
  status toggle (active / closed / all).
- **Table** (dense, sortable by clicking headers): thumbnail
  (`archived_hero_url` or `hero_image_url`, 🪑 fallback), title
  (→ GovDeals live page, new tab; secondary link → archived viewer),
  canonical category, city/state, bids, current bid, **landed cost**,
  ends-in countdown (relative, red < 2h, yellow < 24h), and for closed
  lots an outcome badge + `final_bid`.
- **Pagination:** prev/next over `limit/offset`, page size 50.
- Every filter/sort change re-fetches from the API (no client-side
  filtering of a full dump).
- Tab restore: `restoreLastTab()` already generic over `data-tab` — no
  special-casing needed.

## Not in v1

Composite deal score, Public Surplus lots, map view, star/watch buttons,
per-facet cross-filtered counts, CSV export.

## Error handling

- DB unreachable → 503 with detail (matches `/api/auctions` behavior).
- Bad param values (non-int `max_bids`, unknown `sort`) → 422 via FastAPI
  type validation / whitelist fallback to default sort.
- Empty result → table renders "no lots match" row, filters stay usable.

## Testing

- Unit tests (`tests/web/test_deals_api.py`): query-builder pure function
  (params → WHERE clause + args) covering each filter, combined filters,
  sort whitelist fallback, pagination clamps.
- Landed-cost wiring: row with bid 100, premium 12.5% → 112.5.
- Smoke: run server, `curl /api/deals?...` variants, drive the tab in a
  real browser (filters, sort click, pagination) before shipping.
