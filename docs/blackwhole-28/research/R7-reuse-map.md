# R7 — Reuse Map: Auditing Our Own Stack (`listing_automation/`)

Scope: local filesystem only, no web fetches. Goal — for the three target
capabilities (append-only snapshot recorder / SourceAdapter aggregator with
cross-post dedup / search+alerts+valuation+public-site), classify every
relevant piece of our own code as **EXISTS-USE-AS-IS**, **EXISTS-NEEDS-REFACTOR**,
or **MISSING**. Nothing here proposes rebuilding anything that already works.

## 0. TL;DR

The `deals/` package (merged 2026-07-03, PR #37, "GovDeals auction
deal-tracker v1") is **already almost the entire target system** for a single
source: a `SiteAdapter` Protocol seam, a discover→watch→analyze→digest
pipeline, a typed time-series schema, saved-searches/tags/lists, Telegram
alerting on 4 topics, LLM-graded comps valuation, and a FastAPI public page
that survives the source deleting the original listing. It has 23 unit-test
files (`tests/deals/`). The gap is **breadth, not depth**: only one
`SiteAdapter` is implemented (GovDeals), even though this repo already has
two other hardened, source-specific scrapers sitting unwired
(`public_surplus_automation.py`, `bidspotter_automation.py`). Writing
`PublicSurplusAdapter` / `BidSpotterAdapter` — thin wrappers, like
`GovDealsAdapter` — is the highest-leverage next step, not new scraping work.

Cross-source dedup and a raw-JSONB append-only comps-grade snapshot log are
genuinely missing; everything else asked for in this ticket exists in some
form.

## 1. Sources with code in this repo today

| Source | Mechanism | File | Wired into `deals/`? |
|---|---|---|---|
| GovDeals | maestro JSON API (self-healing key) | `auction_extractors/govdeals_chairs_extraction.py` | ✅ `deals/adapters/govdeals.py::GovDealsAdapter` |
| Public Surplus | plain HTTP | `auction_extractors/public_surplus_automation.py` | ❌ no adapter — only reachable via `auction_extractors.top_chairs.get_top_chairs("ps", ...)` (read-only cache API), not through `deals/` |
| BidSpotter | plain HTTP + AWS-WAF evasion (source key `"bs"`) | `auction_extractors/bidspotter_automation.py` | ❌ no adapter — same as above, cache-only via `top_chairs.get_top_chairs("bs", ...)`; has its own test fixtures (`auction_extractors/tests/fixtures/bidspotter_*`) |

`auction_extractors/listings_db.py` is a **single shared SQLite cache**
(`state/listings.db`) keyed by `asset_id`, already prefixing non-GovDeals rows
(`ps:`, `bs:`) so all three sources coexist in one cache table today — this is
itself a small proof-of-concept for a shared multi-source store, just at the
"scrape cache" layer rather than the "tracked time-series" layer `deals/`
occupies.

The 8 other sources named in the assignment's verified scorecard (Municibid,
MiBid, GovPlanet, Purple Wave, GSA Auctions, iBid, HiBid, Bid4Assets,
PropertyRoom) have **zero code in this repo** — the verification work is done
(per the workspace-level scorecard), the adapter-writing is not started.

## 2. Capability (a): multi-source append-only snapshot recorder

| Piece | Verdict | Where |
|---|---|---|
| `Snapshot` dataclass (asset/account/auction key, observed_at, bid_count, current_bid, end_utc, status) | EXISTS-USE-AS-IS | `deals/models.py:33-38` |
| `deal_snapshots` table + `append_snapshot()` | EXISTS-USE-AS-IS (as an outcome-tracking log) | `deals/store.py:48-53, 85-91`; DDL in `deals/store.py:48-54` mirrored in `scripts/sql/deals_schema.sql` |
| Change-gating (`is_snapshot_change`) — only appends when bid_count/current_bid/end_utc/status actually changed | EXISTS-USE-AS-IS | `deals/watcher_logic.py:27-31` |
| Lane-based poll scheduler (`schedule_lane`/`next_poll_delay`: COLD 6h / WARM 1h / HOT ≤5s) | EXISTS-USE-AS-IS — source-agnostic, operates over `Lot`/`Snapshot` + `adapter.refetch()`, not GovDeals-specific | `deals/watcher_logic.py:1-25`, driven by `deals/watch.py::poll_once` |
| One-shot closer for lots the watcher missed while down | EXISTS-USE-AS-IS | `deals/backfill.py::run_backfill` |

**EXISTS-NEEDS-REFACTOR:**
- `deal_snapshots` is a **narrow typed schema** (4 tracked columns), not a
  raw-JSONB append-only log. `deal_lots.raw JSONB` (`deals/store.py:38`)
  preserves the full source payload, but only on the **current-state** row —
  every prior payload is overwritten on each `upsert_lot()` (`deals/store.py:75-83`,
  `ON CONFLICT ... DO UPDATE`). A comps/pricing dataset in the spirit of
  GovAuctions.app's "150k+ sold comps" wants the historical raw payload
  retained at each observation, not just the 4 derived numbers. Adding
  `raw JSONB` to `deal_snapshots` (mirroring `deal_lots.raw`) is a small
  additive migration, not a redesign.
- `deal_lots` itself is **mutable current-state**, upserted per `(asset_id,
  account_id, auction_id)` PK — correct for "what's this lot doing right
  now," but it is a different layer from a time-series/comps table. Keep
  both; don't conflate them.
- The PK triple (`asset_id, account_id, auction_id`) is GovDeals-native.
  Extending to other sources needs either a `source` column + generalized key,
  or one snapshot table per source with a shared shape — an open design
  decision (see Open Questions), not a blocker.

**MISSING:**
- No adapter yet feeds Public Surplus or BidSpotter lots into `deal_snapshots`
  at all — only GovDeals lots are polled today.
- No source-agnostic raw-JSONB observation log designed for retention across
  many sources/pollings exists anywhere in the codebase.

## 3. Capability (b): SourceAdapter aggregator + cross-post dedup

| Piece | Verdict | Where |
|---|---|---|
| `SiteAdapter` Protocol (`discover`/`refetch`/`fetch_gallery`) | EXISTS-USE-AS-IS — this **is** the SourceAdapter seam the ticket asks to build | `deals/adapters/base.py` (`@runtime_checkable`) |
| `GovDealsAdapter` — reference implementation | EXISTS-USE-AS-IS | `deals/adapters/govdeals.py` |
| GovDeals maestro key self-healing (scrapes `maestroApiKey` out of the live Angular bundle each process start, hardcoded fallback) | EXISTS-USE-AS-IS, reused not reimplemented | `auction_extractors/govdeals_chairs_extraction.py:563-589::_resolve_maestro_key`; called directly from `deals/adapters/govdeals.py:14-18::_headers` via `_g._resolve_maestro_key()` |
| `run_discovery(adapter, ...)` orchestration (categories, LLM classify-on-catchall, image archiving, relist scan) | EXISTS-USE-AS-IS — takes any `SiteAdapter`, already source-agnostic in its own code | `deals/discover.py` |
| Per-source raw→`Lot` mapper pattern | EXISTS-USE-AS-IS as a pattern | `deals/mapping.py::asset_to_lot` (GovDeals-specific; the template to replicate per source) |
| Same-source same-listing dedup across overlapping search terms | EXISTS-USE-AS-IS | `auction_extractors/govdeals_chairs_extraction.py:552-560::_dedup_listings` — keyed by `link` → `lot_number` → `title`; also used by `automation/web/app.py:1773` |
| Same-seller relist detection (Jaccard token similarity across closed vs. fresh lots) | EXISTS-USE-AS-IS — closest existing analog to fuzzy cross-listing matching | `deals/relist.py::title_similarity`, `find_relist` |

**EXISTS-NEEDS-REFACTOR:**
- Only **one** concrete `SiteAdapter` exists. `public_surplus_automation.py`
  and `bidspotter_automation.py` are hardened, tested, shipping scrapers with
  no `deals/adapters/*.py` wrapper — each is realistically an ~80-line file
  shaped exactly like `deals/adapters/govdeals.py` (map their existing
  card/JSON shape into `Lot` via a new `mapping.py`-style function, implement
  `discover`/`refetch`/`fetch_gallery`). This is the single highest-leverage
  gap in the whole audit.
- `deals/models.py::Lot` is auction-shaped (bid_count/current_bid/reserve
  fields) and keyed on GovDeals' tri-key. A future fixed-price/buy-now source
  would need nullable bid fields or a sibling dataclass — not a blocker for
  the auction sources already in-repo (Public Surplus and BidSpotter are both
  auction-style too).

**MISSING:**
- **Cross-source dedup has zero code.** `_dedup_listings()` is same-source
  only (dedupes GovDeals search-term overlap against itself).
  `deals/relist.py`'s title-similarity matcher is same-source too (same
  `account_id`, different `auction_id`). Nothing compares a lot discovered via
  GovDeals against a lot discovered via BidSpotter/Public Surplus/etc. for
  "this might be the same physical item." `relist.py`'s Jaccard-token +
  threshold approach (`SIM_THRESHOLD = 0.6`) is the right pattern to extend,
  not a new algorithm to invent.
- No `SiteAdapter` implementations exist yet for any of the other verified
  sources (Municibid, MiBid, GovPlanet, Purple Wave, GSA, iBid, HiBid,
  Bid4Assets, PropertyRoom).

## 4. Capability (c): search / alerts / valuation / public site

### Search
EXISTS-USE-AS-IS:
- `automation/web/deals_query.py::build_where/order_clause/enrich` — full
  parameterized filter+sort+enrich builder (`q`, `category`, `native`,
  `state`, `max_bids`, `ending_within`, `status`, `min_margin`, `list_id`,
  `tag`), driving `GET /api/deals` (`automation/web/app.py:567`).
- Saved lists / tags / saved-searches, each with full CRUD REST endpoints:
  `deal_lists`/`deal_list_items` (`app.py:702-754`), `deal_lot_tags`
  (`app.py:756-788`), `saved_searches` (`app.py:789-820`). DDL:
  `scripts/sql/deal_verdicts.sql:19-35`.
- Category tree UI + counts: `GET /api/deals/tree` (`app.py:651`).

### Alerts
EXISTS-USE-AS-IS:
- `automation/telegram_alerts.py::send_message/send_message_sync` — routes to
  a **4-topic** "BlackWhole Alerts" supergroup per **BLACKWHOLE-24**
  (`leads`/`deals`/`health`/`poller`, resolved from `TELEGRAM_TOPIC_*` env
  vars, `automation/config.py:22-25`), unknown/unconfigured topic falls back
  to General, never raises.
- Four independent alert producers already call it: `deals/saved_search_alerts.py`
  (new lots matching a saved search since `last_run_at`), `deals/relist.py`
  (relist detection), `deals/analyze.py` (high-margin verdict alerts,
  `should_alert`), `deals/digest.py` (daily 0-bid-<24h digest,
  `deal_candidates` view in `scripts/sql/deal_candidates_view.sql`).
- `automation/alerts/` (`matcher.py`, `blast.py`, `geo.py`,
  `email_sender.py`/`resend_sender.py`) — a **separate, already-shipped**
  demand-side (buyer subscriber) alert engine: new inventory → geo/qty/type
  match → provider-agnostic email send, dry-run-by-default, dedup via
  `alert_sends UNIQUE(subscriber_id, lot_id, channel)`
  (`scripts/sql/002_alerts_blast.sql`). Its haversine + zip→state-centroid
  precision-degrade pattern (`automation/alerts/geo.py`) is directly reusable
  for any lot↔location matching.
- `automation/sourcing/` (`alerts.py`, `digest.py`, `dmv.py`) — a **third**
  alert variant (BLACKWHOLE-19): true 100-mile-radius sourcing alerts (DC/MD/
  VA), built because `saved_search_alerts` only supports single-state
  filtering, not radius. Proves the alerting pattern already generalizes past
  `deals/`'s original scope; also dry-run-by-default with the same
  `Sender = Callable[[str], (ok, err)]` shape.

EXISTS-NEEDS-REFACTOR:
- **Five independent call sites** compose their own alert text and call
  `send_message_sync` separately (`deals.saved_search_alerts`,
  `deals.relist`, `deals.analyze`, `automation.sourcing.digest`,
  `automation.alerts.blast`). None are broken, but there's no shared
  "digest/alert composer" — worth consolidating once a 4th/5th source makes
  the duplication costly, not before.

### Valuation
EXISTS-USE-AS-IS:
- `deals/comps.py::PiCompsProvider` / `comps_provider_from_env()` — pluggable
  comps client, degrades to `None` (never blocks) when `COMPS_URL`/`COMPS_KEY`
  unset.
- `deals/valuation.py` — pure landed-cost/margin math, comp-median with a
  bulk-recovery-tier discount (`bulk_recovery_tier`), `MIN_COMPS = 3` gate
  before trusting a comp-grounded value, confidence tiering (`low`/`medium`/
  `high`).
- `deals/llm_steps.py` — retrieval-then-reasoning pattern: LLM extracts
  identity + search queries, then judges which retrieved comps are genuinely
  the same item — **the LLM never sets a price**, only retrieves/filters.
- `deals/ebay_parse.py::parse_sold_page` — eBay sold-search HTML parser; the
  canonical implementation the Pi comps microservice deploys a copy of.
- `deals/rank.py` — a second, independent LLM pass: shells out to local
  `claude -p` for an adversarial re-rank of the day's top verdicts.
- `deals/fees.py::FeeModel/landed_cost` — env-driven buyer-premium/tax/freight,
  already the shared math both the digest and the valuation pipeline use.

### Public site
EXISTS-USE-AS-IS:
- `automation/web/app.py` is a **single FastAPI process already serving both**
  the admin dashboard (`/admin`) **and** a public storefront (`/`,
  `/listings`, `/listings/{lot_id}`, `/sell`, `/contact`) with a distinct
  "brutalist-industrial" public theme (Jinja templates in
  `automation/web/templates/`, separate CSS from admin).
- `GET /deals/{asset_id}/{account_id}/{auction_id}` (`app.py:533`,
  template `deal_listing.html`) **reconstructs a listing page from our own
  store after GovDeals removes the original** — functionally exactly the
  "sold-comp permalink" a comps-dataset product needs per listing, already
  built, GovDeals-only today.
- SEO/feed plumbing already exists: `/robots.txt`, `/sitemap.xml`,
  `/catalog/facebook.csv` (`app.py:855-905`).

Correction to the prompt's framing: **HTMX is not actually wired in.**
`automation/web/app.py` is plain FastAPI + Jinja + hand-rolled fetch/JS. Per
the workspace `CLAUDE.md` §5 #2, "Streamlit vs. FastAPI+HTMX" is still a
stated-but-undone decision — don't assume HTMX exists; the current
server-rendered-Jinja-plus-JSON-API shape is still highly reusable, just not
literally HTMX.

MISSING:
- No public multi-source browse/search — `/listings` (site inventory) and
  `/api/deals` (deal tracker) are each single-domain; `/api/deals` is
  GovDeals-only by construction (one adapter). Adding a `source` column +
  filter to `deals_query.py` once more adapters exist is a small addition.
- No public "query comps across everything" endpoint analogous to
  GovAuctions.app's `/comps?q=` — `deals/comps.py`+`valuation.py` compute
  comps internally per-lot for our own buy-decision alerts; nothing exposes
  that as a public or paid API surface.

## 5. LLM provider-agnostic layer

- `automation/llm/` (`base.py::Extractor` Protocol,
  `default_extractors()` env-driven picker — Gemini → OpenAI → DomFallback/
  ClaudeCode, with automatic A/B secondary logging into `llm_compare_logs`)
  **is** a genuinely reusable, already-abstracted multi-provider LLM layer.
  EXISTS-USE-AS-IS as the pattern to extend.
- **But `deals/` does not use it.** `deals/classify.py::classify_category`
  and `deals/llm_steps.py::_gemini` both call `from google import genai`
  directly, hardcoded to `config.GEMINI_API_KEY`, bypassing
  `automation/llm/` entirely. EXISTS-NEEDS-REFACTOR: no cross-provider
  fallback if Gemini is unreachable/unset — `classify_category` degrades to
  `("other", 0.0)` and `llm_steps` raises `LlmStepError` which
  `deals/analyze.py` counts as a per-lot error, so the pipeline never crashes,
  but it isn't provider-agnostic the way `automation/llm/` already is. Wiring
  `deals/` onto the existing `Extractor` protocol (or a shared "text
  completion" abstraction under it) is a scoped refactor — the prompts
  themselves don't need to change.
- `deals/rank.py` is a **third** distinct LLM integration pattern in `deals/`
  alone (shells out to the local `claude` CLI) — worth noting as another
  divergence, not necessarily wrong (it's explicitly "local-only, TTY
  machine").

## 6. Compact answer to the ticket's explicit checklist

| Ask | Answer |
|---|---|
| `deals/` `SiteAdapter` Protocol seam | EXISTS-USE-AS-IS — `deals/adapters/base.py`. One implementation (`govdeals.py`); the seam itself needs zero changes to add more. |
| `deal_lots`/`deal_snapshots` vs. append-only raw-JSONB snapshots table | `deal_lots` = mutable current-state (correct, keep). `deal_snapshots` = typed 4-column time series, change-gated, **no raw JSONB retained** — EXISTS-NEEDS-REFACTOR if a comps-dataset ambition needs full historical payloads. |
| GovDeals maestro key self-healing | EXISTS-USE-AS-IS — `auction_extractors/govdeals_chairs_extraction.py::_resolve_maestro_key`, reused directly (not duplicated) by `deals/adapters/govdeals.py`. |
| `_dedup_listings()` | EXISTS-USE-AS-IS but **same-source only** — `auction_extractors/govdeals_chairs_extraction.py:552`. Cross-source dedup is MISSING; closest analog is `deals/relist.py`'s title-similarity matcher (also same-source). |
| Telegram alert channels (BLACKWHOLE-24 4-topic supergroup) | EXISTS-USE-AS-IS — `automation/telegram_alerts.py`, topics `leads`/`deals`/`health`/`poller`. Already fanned out to 5 independent producers (EXISTS-NEEDS-REFACTOR: no shared composer). |
| LLM provider-agnostic layer | EXISTS-USE-AS-IS as a **pattern** (`automation/llm/`) but EXISTS-NEEDS-REFACTOR in practice — `deals/` bypasses it with hardcoded direct Gemini calls in `classify.py` and `llm_steps.py`. |
| FastAPI+HTMX storefront | EXISTS-USE-AS-IS as **FastAPI + Jinja** (not actually HTMX yet — see §4). Public site + admin dashboard share one process; deal-specific public page (`/deals/{a}/{acc}/{auc}`) already reconstructs closed listings. |

## Open questions

1. Should `deal_lots`' PK (`asset_id, account_id, auction_id` — GovDeals-native)
   generalize to `(source, native_id)` for multi-source, or should each new
   source get its own `*_lots` table mirroring `deal_lots`' shape? Neither the
   workspace nor repo `CLAUDE.md` decides this.
2. Is the comps/valuation pipeline (`deals/comps.py` + `valuation.py`, tuned
   to margin-on-resale for **our** buy decisions) meant to become the same
   thing as a public "sold comps" product, or should the two stay separate
   (internal buy-signal engine vs. a public comps API)?
3. Is `deal_snapshots` lacking `raw JSONB` an oversight or a deliberate
   storage-cost tradeoff (Supabase free tier is 500MB–1GB per workspace
   `CLAUDE.md` §8/§12)? Determines whether adding it is a quick migration or
   needs a storage-budget conversation first.
4. `public_surplus_automation.py` and `bidspotter_automation.py` are shipped
   and tested but unwired from `deals/` — repo `CLAUDE.md`'s `deals/` section
   says "GovDeals-only for v1 (Public Surplus is deferred)." Is that scope
   decision still current, or ready to revisit now that BidSpotter exists too?
5. Should the 5 independent alert-composition call sites converge on one
   shared composer now, or is the duplication acceptable until a 4th/5th
   source adapter makes it genuinely costly to maintain separately?
