# Continuous Deal Scanner + Resale-Analysis Agent — Design

Date: 2026-07-17 · Status: approved · Builds on: `deals/` v1
(PR #37, spec `docs/superpowers/plans/2026-07-03-govdeals-deal-tracker-v1.md`)

## Problem

`deals/` v1 proved discovery works (7,156 lots swept Jul 3–6) but nothing
runs it continuously: 6,800 lots have already ended with **no outcome
recorded** because `watch-once` only ran manually. And nothing judges a
lot's resale value — the only "analysis" is the 0-bid digest, which is
itself blocked on missing Telegram env. The money-making loop
(find lots closing cheap → judge resale profit → act before close →
learn from outcomes) does not exist yet.

## Goal

A fully automatic loop that (a) sweeps **all of GovDeals** continuously,
(b) watches every lot through its close and records the outcome,
(c) runs a comp-grounded resale-profit analysis on lots heading toward a
cheap close **while there is still time to bid**, (d) alerts on
high-margin verdicts and relists, and (e) gives the operator a
Zillow-style browser over the inventory with saved lists, tags, and
saved searches.

Decisions locked during brainstorming:

- Full loop (live pre-close analysis + relist detection + historical
  learning), phased; **continuous watch loop ships first**.
- Runtime: **Render cron** (maestro API is plain JSON — no browser).
- Scope: **whole site**, nationwide. Distance is a **filter knob**
  (alerts + UI), never a hard exclusion; landed cost still includes
  freight so distance is priced in.
- Analysis funnel: deterministic **cheap-close pre-filter** — no LLM
  spend outside the funnel.
- Resale value: **real eBay sold comps** via a self-hosted comps proxy
  on the Raspberry Pi (`black-whole`, residential T-Mobile IP). LLM
  never invents a dollar figure; comps-absent verdicts are flagged.
- Brains: **Gemini Flash** for extraction/query-gen/comp-judging;
  **Claude CLI locally** re-ranks the daily shortlist.
- Output: verdict rows in Supabase → Deals-tab columns/filters →
  Telegram only above a margin threshold.

## Research findings that shaped the design (2026-07-17)

- eBay official APIs are a dead end for sold data: Finding API
  decommissioned Feb 2025; Marketplace Insights is partner-gated and
  effectively unobtainable.
- Plain HTTP scraping is TLS-fingerprinted (Akamai). Live-tested:
  plain curl → 403; `curl_cffi` desktop-Chrome from a datacenter-ish
  pattern → challenge; **`curl_cffi` iOS-Safari fingerprint + warm-up
  navigation chain + referer chain + 5–9s jitter from the Pi's
  residential IP → clean 200s with full sold results**.
- Paid fallbacks exist if the Pi path degrades: SoldComps $29/mo (10k
  searches), Countdown API ~$66–82/mo, SerpAPI $150/mo — all verified
  to return sold prices.
- Proven analysis architecture (Alibaba LLP, deployed at Xianyu scale):
  LLM extracts identity → retrieve real sold comps → LLM filters truly
  comparable ones → price from comps with LLM reasoning → reject
  low-confidence. Never LLM-only pricing.
- Bulk-lot valuation practice: per-unit sold median × N × recovery
  discount (industry recovery rates 20–50%; use 0.3–0.5 as the bulk
  floor tier), sanity-checked against sell-through rate (STR ≥ ~30%
  with ≥10 comps = buyable demand).
- Closest product analogs: Zillow (browse + saved searches + saved
  homes + alerts) for the UI; GovAuctions.app (comp-verified Flip
  Score per card) for the verdict presentation.

## Architecture

```
Render cron ──► deals.cli discover  (whole site, ~6h)     ─┐
Render cron ──► deals.cli watch-once (15–30 min)           ├─► Supabase
Render cron ──► deals.cli analyze   (hourly, funnel-fed)   │   deal_lots
Render cron ──► deals.cli digest    (daily)                │   deal_snapshots
                                                           │   deal_verdicts
                    │  comps queries (HTTPS + shared key)  │   deal_lists/tags
                    ▼                                      │   saved_searches
Pi `black-whole` ~/comps  eBay sold-comps microservice    ─┘
                    │
Operator ──► /admin Deals tab (browse/save/tag/saved-search)
         ──► deals.cli rank (Claude CLI, local shortlist re-rank)
         ◄── Telegram (margin alerts, relist alerts, digest)
```

### Phase 1 — Continuous watch loop (foundation)

- **Whole-site discover**: category list becomes config
  (`DEALS_SWEEP_CATEGORIES`, empty = all categories from the maestro
  category endpoint). Sweep paced with jitter; per-category error
  isolation so one bad category never kills the sweep.
- **Render cron jobs** (new `render.yaml` entries or dashboard-created):
  - `discover` every 6h
  - `watch-once` every 15–30 min
  - `analyze` hourly (Phase 2)
  - `digest` daily
  Each is `python -m deals.cli <cmd>` with repo env
  (`BLACKWHOLE_DB_URL`, `GEMINI_API_KEY`, `TELEGRAM_*`, `COMPS_*`).
- **Outcome backfill** (one-shot script): the 6,800 ended-no-outcome
  lots get closed from their last snapshot; rows with a single
  observation are marked `outcome_complete = false` — honest data,
  never fabricated.
- **Config gate closed**: `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`
  added to local `.env` + Render env.

### Phase 2 — Analysis agent (`deals/analyze.py` + `deals/comps.py`)

**Funnel (pure SQL/logic, zero LLM cost):** lots with
`outcome IS NULL AND end_utc < now()+24h AND (bid_count = 0 OR
current_bid <= DEALS_ANALYZE_MAX_BID)` and no fresh verdict. Expected
volume: dozens–hundreds/day.

**Per-lot pipeline** (each step per-lot error-isolated):

1. **Identity extraction** (Gemini Flash, vision): title + description
   + hero/gallery → `{brand, model, item_type, quantity, condition,
   identifiers}`.
2. **Query generation** (same Flash call): 2–3 eBay search queries,
   most-specific first.
3. **Comps retrieval**: `CompsProvider` protocol (mirrors the
   `llm/default_extractors()` pluggable pattern).
   - `PiCompsProvider` (default): `GET http://<pi>:8788/comps?q=…`
     with `X-Comps-Key`. 503/timeout → try next query → else degrade.
   - Future backends: `soldcomps`, `countdown` (env-selected,
     `DEALS_COMPS_PROVIDER`).
4. **Comp judging** (Flash): given lot identity + comp titles/prices,
   keep only truly-comparable comps; require ≥3 kept comps for a
   comp-grounded verdict.
5. **Valuation**: `per_unit = median(kept comps)`;
   `est_resale = per_unit × quantity × recovery_tier` where
   recovery_tier = 1.0 for single items and
   `DEALS_BULK_RECOVERY` (default 0.4) for bulk lots (quantity > 5)
   (piece-out ceiling recorded separately as
   `per_unit × quantity × 0.8`). Demand gate: comp count and
   sold-vs-active ratio recorded; low-demand verdicts capped at
   `confidence = low`.
6. **Margin**: `est_resale − landed_cost(bid, premium, tax, freight)`
   via existing `deals/fees.py`. Freight estimated from distance
   (flat $/mile config) — crude v1, refine later.
7. **Verdict row** → `deal_verdicts`.
8. **Alert**: margin_pct ≥ `DEALS_ALERT_MIN_MARGIN_PCT` and confidence
   ≥ medium → Telegram (title, bid, est. resale, margin, distance,
   hours left, lot URL, top comp URLs).

**Degraded mode**: comps unavailable (Pi down/cooling/no comps kept) →
Flash produces an estimate anyway, verdict stored with
`method = 'llm_estimate'`, `confidence = 'low'`, never alerted unless
explicitly enabled. **The LLM's number is never presented as a comp.**

**Claude local re-rank**: `deals.cli rank` (run locally, TTY) feeds the
day's top-N verdicts to Claude CLI for adversarial review (comp
relevance, liquidity, condition risk, freight sanity) → writes
`rank_score` + `rank_notes` back onto verdicts. Optional but
recommended daily habit; dashboard shows rank when present.

### Phase 2.5 — Deal Browser UI (Zillow-model, evolves `10 Deals` tab)

- **Saved lists**: heart on card/row → list picker (create-inline).
  `deal_lists` + `deal_list_items`.
- **Tags**: free-form chips per lot (`deal_lot_tags`), filterable.
- **Saved searches**: current filter state (q, categories, state,
  max_bids, ending_within, min margin, max distance, …) saved under a
  name (`saved_searches`), optional `alert` flag — evaluated after
  each discover/analyze pass; new matches → Telegram.
- **Verdict surfacing**: est. resale, margin %, confidence, method,
  comp links, rank score as columns + a detail drawer; sort by margin.
- **Distance filter**: computed from lat/lng vs home base
  (`DEALS_HOME_LAT/LNG`), selectable radius.
- Card design reference: GovAuctions.app (score + projected range per
  card); interaction reference: Zillow saved-homes/saved-searches.

### Phase 3 — Relist detection

- After each discover sweep: new lots matched against closed `no_bid`
  lots from the same seller account (title similarity ≥ threshold via
  trigram/token-set ratio + same native category). Match →
  `relist_of = (prior lot key)` on the new row + Telegram
  "relisted at opening price" alert (respects saved-search filters).
- Over time, own-outcome history becomes a second comp source
  (GovAuctions model) — deferred beyond v1 of this spec.

## Pi comps microservice (BUILT — 2026-07-17, this session)

Lives on `black-whole` (Raspberry Pi, aarch64, Tailscale
`100.99.195.81`), `~/comps/comps_service.py`, systemd user unit
`comps.service` (linger on, auto-restart). FastAPI, port 8788.

- `GET /comps?q=<query>` (`X-Comps-Key` auth) → `{count, median, mean,
  items[{listing_id, title, price, condition, sold_note, url}],
  cached, fetched_at}`.
- `GET /health` → cache size + cooldown state.
- Fetch discipline (verified live; comments in the file say do not
  "optimize" away): `curl_cffi impersonate="safari_ios"`, warm-up
  chain homepage → active search → sold search, referer chain, 5–9s
  jitter, ≤25 searches/session, challenge detection ("Pardon Our
  Interruption" or <50KB body) → drop session + 15-min cooldown
  (503s with `retry_at`).
- Cache: SQLite `~/comps/cache.db`, normalized-query key, 7-day TTL.
- Parser targets the 2026 `.s-card`/`data-listingid` layout
  (`.s-card__title/__price/__subtitle/__caption`); the legacy
  `.s-item` layout no longer appears — if eBay A/Bs it back, the
  parser needs the dual variant (known risk).
- Key in `~/comps/.env` (`COMPS_KEY=…`, chmod 600). Same key goes into
  repo/Render env as `COMPS_URL` + `COMPS_KEY`.

**Render → Pi connectivity** (implementation decision, pick in plan):
Tailscale on Render (userspace) or a `cloudflared` tunnel from the Pi
exposing 8788 behind the shared key. Local dev reaches it over the
tailnet directly.

## Data model (Supabase; DDL of record in `scripts/sql/`)

```sql
deal_verdicts (
  asset_id, account_id, auction_id,          -- FK deal_lots PK
  analyzed_at timestamptz,
  identity jsonb,                            -- brand/model/type/qty/condition
  queries text[],
  method text,                               -- 'comps' | 'llm_estimate'
  comps jsonb,                               -- kept comps [{title,price,url}]
  comp_count int, per_unit numeric,
  recovery_tier numeric, est_resale numeric,
  piece_out_ceiling numeric,
  landed_cost numeric, margin numeric, margin_pct numeric,
  confidence text,                           -- low|medium|high
  reasoning text,
  rank_score numeric, rank_notes text,       -- Claude re-rank
  alerted_at timestamptz,
  PRIMARY KEY (asset_id, account_id, auction_id, analyzed_at)
)
deal_lists      (id, name, created_at)
deal_list_items (list_id, asset_id, account_id, auction_id, added_at,
                 PRIMARY KEY (list_id, asset_id, account_id, auction_id))
deal_lot_tags   (asset_id, account_id, auction_id, tag, added_at,
                 PRIMARY KEY (asset_id, account_id, auction_id, tag))
saved_searches  (id, name, params jsonb, alert bool, created_at,
                 last_run_at)
-- deal_lots additions:
ALTER TABLE deal_lots ADD COLUMN relist_of jsonb;      -- prior lot key
```

## Config (env)

| Var | Default | Meaning |
|---|---|---|
| `DEALS_SWEEP_CATEGORIES` | (empty = all) | comma list of native category codes |
| `DEALS_ANALYZE_MAX_BID` | 25 | funnel: max current bid to qualify |
| `DEALS_ANALYZE_WINDOW_H` | 24 | funnel: hours-to-close window |
| `DEALS_ALERT_MIN_MARGIN_PCT` | 100 | Telegram gate |
| `DEALS_BULK_RECOVERY` | 0.4 | recovery tier for bulk lots (qty > 5) |
| `DEALS_HOME_LAT` / `DEALS_HOME_LNG` | — | distance computation |
| `DEALS_FREIGHT_PER_MILE` | 0 | crude freight estimate for landed cost |
| `COMPS_URL` / `COMPS_KEY` | — | Pi service endpoint + shared key |
| `DEALS_COMPS_PROVIDER` | `pi` | comps backend selector |

## Error handling

- Every cron entry point wraps in per-item error isolation (existing
  `deals/` pattern); one lot/category/query failure never aborts a pass.
- Comps failures degrade (documented above), never block verdict rows.
- Telegram remains best-effort (`telegram_alerts.py` never raises).
- Missing-price lots keep failing loud at mapping (v1 invariant kept).
- Pi unreachable ≥ N consecutive analyze passes → single Telegram
  ops-alert (not one per lot).

## Testing

- Pure logic unit-tested under `tests/deals/` (`python -m pytest`):
  funnel query builder, valuation math (bulk tiers, piece-out ceiling),
  comp-judge prompt I/O parsing, relist matcher, saved-search
  evaluator, verdict-alert gating.
- Comps parser tested against the checked-in `sample.html` fixture
  (captured 2026-07-17) so eBay layout drift is caught by CI, not
  in production.
- Pi service smoke test script (hits `/health` + one cached query).
- Live-smoke checklist per phase before calling it done (mirror of
  v1's live-smoke).

## Rollout order

1. Phase 1 (cron + whole-site + backfill + Telegram env) — everything
   else is starved without it.
2. Phase 2 (analyze + comps client + verdicts + alerts).
3. Phase 2.5 (UI: lists/tags/saved-searches/verdict columns).
4. Phase 3 (relist detection).

## Deferred / non-goals

- Public Surplus adapter (seam exists in `deals/adapters/base.py`).
- Per-seller premium calibration; exact contested-lot final prices.
- Own-outcome comp model (GovAuctions style) — needs months of data.
- Paid comps backend implementation (interface reserved; add only if
  the Pi path degrades).
- Residential-proxy pools, multi-IP rotation — out of scope while one
  Pi at ≤ a few hundred searches/day suffices.
