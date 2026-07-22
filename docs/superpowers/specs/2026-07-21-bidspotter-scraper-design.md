# BidSpotter Scraper — Design Spec (source key `bs`)

**Date:** 2026-07-21 · **Branch:** `bidspotter-scraper` · **Status:** approved design, verified against code + live site

Add BidSpotter.com as the third auction-site source in `auction_extractors/`,
alongside GovDeals (`gd`) and Public Surplus (`ps`). BidSpotter lots surface in
the dashboard's `04 Auctions` tab exactly like Public Surplus lots do: scraped
into the shared SQLite archive → transferred to Supabase `auction_listings` →
served by `/api/auctions` → rendered as cards. The `deals/` deal-tracker is
**out of scope** (see Follow-ups).

---

## 1. Two locked decisions (do not re-litigate)

1. **Scope = Auctions-tab pipeline only.** New scraper script + shared-plumbing
   edits, mirroring how `ps` was integrated. No `deals/` adapter.
2. **Quantity = trust BidSpotter's structured field.** BidSpotter exposes lot
   quantity as structured data on ~80-85% of cards. When present, use it
   directly with `quantity_source="structured"` — a **new trusted source
   value**. When absent, fall back to the existing regex-title seed + LLM pass
   (`quantity_source="llm"`). The downstream trust guard widens from
   `== "llm"` to a frozenset `{"llm", "structured"}`.

## 2. Verified site facts (fixtures + live probes, 2026-07-21)

All of the below was re-verified against saved pages
(`auction_extractors/tests/fixtures/bidspotter_search_chairs.html`, trimmed
from a live save) and one live POST. Corrections to the original
investigation are marked **▲**.

- **Plain HTTP works. No browser.** `requests` with `User-Agent: Mozilla/5.0`
  (a full spoofed Chrome UA triggers AWS WAF) and cookie
  `user_preference_pagesize=120`. The WAF challenge is probabilistic: an
  HTTP **202** response carrying header `x-amzn-waf-action` means "retry";
  retry with backoff.
- **Search URL:** `https://www.bidspotter.com/en-us/search-results?searchTerm=<term>&page=<n>`
  (`pageSize` URL param is ignored; page size comes from the cookie).
  Total count: `<div class="lot-search-results-items-info">725 item(s)</div>`.
  Pagination: `<ul class="pagination-content" ... data-pages="7">`.
  **▲ Page size is only 120 when the cookie is honored** — a save without the
  cookie returned 60 cards/page with `data-pages="13"` for the same 725
  results. The parser must read `data-pages` from each response, never assume
  a fixed page count or page size.
- **Card markup** (verified): `<article class="panel item ">` (note trailing
  space; `featured` variant exists: `class="panel item featured"` — **▲ an
  exact-class match finds 0 cards; match on the class prefix**). Inside:
  `div.lot-single` with `id="lot-{lotGuid}"` and data attrs
  `data-auction-ref` (e.g. `stone-10117`), `data-auction-type`
  (`timed`|`live`), `data-auction-id`, `data-currency`, plus
  `data-show-piecemeal`.
  - title + lot URL: `div.lot-header h3 a[href]` → path
    `/en-us/auction-catalogues/{clientSlug}/catalogue-id-{auctionRef}/lot-{lotGuid}`
    (absolutize with `https://www.bidspotter.com`).
  - **quantity (STRUCTURED):** `li.quantity strong.bulk-quantity-value`
    (whitespace-padded text, may contain thousands commas). Present on 49/60
    cards in the fixture page.
  - location: `span.lotlocation` → `Location: <strong>Grand Rapids, Michigan</strong>`
    (**▲ the city/state sits inside a nested `<strong>`** — parse the strong's
    text, not the span's flat text).
  - image: `img#i{lotGuid}[data-src]` →
    `https://cdn.globalauctionplatform.com/auctions-2026/{auctionRef}/images/{guid}.jpg?h=175`
    — strip the query string for full-res.
  - lot number: `div.number` ("Lot 32") or `span.lot-number` ("32").
  - description snippet: `div.description p` (present on cards; good enough
    for the LLM quantity fallback — no per-lot detail fetch in v1).
  - Prices are NOT in the static HTML (skeleton placeholders).
- **Embedded per-lot JSON** (verified): one assignment
  `window.gapAmplitudeConfig.forItem = {…};` mapping lotGuid → object with
  string fields `Lot ID`, `Lot Number`, `Auction ID`, `Auction Reference`,
  `Auction Currency`, `Auction Type`, `Auction House Name`, `Lot Quantity`,
  `Auction End Time UTC`, `Lot End Time UTC` (ISO, e.g.
  `2026-08-12T16:14:30Z`). **▲ Caveat: `Lot Quantity` defaults to `"1"` when
  the card has no structured quantity** — verified: all 49 cards with a DOM
  quantity agree exactly with forItem, and all 11 cards without one show
  forItem `"1"`. Therefore **the DOM `bulk-quantity-value` element's presence
  is the "structured" discriminator**; forItem supplies end times / auction
  house / currency, not the trust decision. (An earlier top-level
  `forItem: null` also appears in the page — the parser must match the
  `forItem = {…};` object assignment, not the first `forItem` occurrence.)
- **Prices via batched unauthenticated POST** (live-verified 2026-07-21):
  `POST https://www.bidspotter.com/en-us/lot/reload-timed-bid-info?v=1.3.0.1&c=lotsearch`
  (URL confirmed from the page's `SearchBiddingInfoSettings.bidReloadInfoUrl`),
  `Content-Type: application/json`, body = JSON array
  `[{"LotId": "<guid>", "BidderHasBids": false}, …]` (all of a page's GUIDs in
  one call). **▲ Response rows are wrapped:** a JSON array of
  `{"Model": {…}}` objects (per the site's own `updateSuccess` handler —
  unwrap with `row.get("Model", row)`). Model fields (verified live):
  `LotId`, `LeadingBid`, `StartPrice`, `NextMinimumBid`, `TotalBids`,
  `Reserve`, `ReserveMet`, `Quantity`, `SecondsRemaining`,
  `EndTimeUtc` (`/Date(epoch_ms)/`), `Currency`, `HammerPrice`, `LotClosed`,
  `IsBiddingClosed`, `TimezoneId`. Captured fixture:
  `auction_extractors/tests/fixtures/bidspotter_bid_info.json`.
  Price rule: `LeadingBid` when `TotalBids > 0`, else `StartPrice`; format
  GovDeals-style `"USD 7.00"`. Best-effort: on POST failure leave `price`
  empty and still archive the row.
- **Data model context:** 3-level hierarchy house → catalogue (`auctionRef`) →
  lot (guid). Natural cache key = lot GUID; a relist mints a new GUID (so the
  relist/end_date logic in `hydrate_from_cache` never mis-hits). Per-unit
  "piecemeal" bidding exists (`data-show-piecemeal`, `data-piecemeal-enabled`;
  LeadingBid × Quantity = total) — v1 stores `LeadingBid` as-is. `live`
  auctions (~10%) can't be meaningfully price-polled; archive them anyway,
  gate their inclusion in alerts/serving behind `BIDSPOTTER_INCLUDE_LIVE`.

## 3. Architecture

New file `auction_extractors/bidspotter_automation.py`, structurally mirroring
`public_surplus_automation.py`: module-level parse functions (importable by
tests and `/api/test-scrape`), `main()` + `if __name__ == "__main__": main()`,
`--test` / `RUN_TEST=1` mock mode, dotenv root-then-package-`.env`
(`override=True`), stage-prefixed stdout, archive-before-filter, best-effort
Telegram.

Pipeline (stage prefixes are parsed by the dashboard's `_SCRAPE_STAGES`;
the real set across scrapers is `[0] [1] [1b] [1c] [1d] [1e] [2] [3a]` — the
original brief omitted `[1e]`):

```
[1]  scrape      fetch search pages (WAF-retry GET, paginate to data-pages
                 or BIDSPOTTER_MAX_PAGES), parse cards + forItem JSON
[1b] enrich      per page: batched reload-timed-bid-info POST → price,
                 time_left (best-effort); then hydrate_from_cache for the
                 no-structured-quantity subset only
[1d] llm refine  refine_quantities_with_llm on rows still lacking a trusted
                 quantity → quantity_source="llm" / "llm_failed" / "llm_missing"
[1e] cache       listings_db.store_listings(ALL rows)  ← archive before filter
[2]  rank        keep-filter (medical gate, non-chair terms, trusted_quantity
                 > MIN_CHAIR_QUANTITY=50, live-auction gate) then sort
                 quantity DESC, price ASC
[3a] telegram    ranked alert + LLM-degradation alert (both best-effort)
```

Two subtleties the implementation MUST honor:

1. **`refine_quantities_with_llm` has no source-based skip and returns
   copies** (`out = [dict(x) for x in listings]`). Passing the full list would
   overwrite structured quantities. Pass only the untrusted subset and stitch
   the returned copies back into the main list by index.
2. **`hydrate_from_cache` overwrites `quantity` from cache when the cached row
   has one.** Hydrate only the no-structured-quantity subset, so a fresh
   structured value (seller may edit quantities) is never clobbered by a stale
   cached one. Hydration exists to spare the LLM pass on already-LLM'd lots.

### Card-dict contract

Same keys GD/PS produce (the contract asserted by
`tests/test_publicsurplus_parse.py::CARD_KEYS`):

| key | BidSpotter value |
|---|---|
| `title` | `h3 a` text, HTML-unescaped |
| `link` | absolute lot URL (`https://www.bidspotter.com/en-us/auction-catalogues/...`) |
| `quantity` | int from `bulk-quantity-value` (commas stripped), else regex-title seed, else LLM |
| `quantity_source` | `"structured"` when the DOM field is present; else `"regex_title"` seed → `"llm"`/`"llm_failed"`/`"llm_missing"` after the LLM pass |
| `quantity_confidence` | `"high"` for structured; PS-style `"low"/"medium"` for the seed; LLM's own for LLM |
| `location` | text of `span.lotlocation strong` (no `"Location:"` prefix), `""` if absent |
| `price` | `"{Currency} {amount:.2f}"` from bid-info (`LeadingBid` if `TotalBids>0` else `StartPrice`); `""` on POST failure |
| `lot_number` | `"{auctionRef}#{lotNumber}"`, e.g. `"stone-10117#93"` (display-only) |
| `end_date` | forItem `Lot End Time UTC` (fallback `Auction End Time UTC`) — already ISO UTC, parseable by `top_chairs._is_active` |
| `time_left` | derived from bid-info `SecondsRemaining` (`"34d 16h"`), `""` if unavailable |
| `image_url` | `data-src` with query string stripped (full-res) |
| `description` | card snippet text (`div.description p`), tag-stripped + unescaped |

Extra keys (`auction_type`, `auction_house`, `bid_count`, `lot_guid`) are
allowed — `listings_db.upsert_listing` reads only known columns.

### Cache key

`listings_db.extract_asset_id()` gains a third pattern:
`bidspotter.com` URL containing `/lot-{guid}` → **`bs:<lotGuid>`** (GUID =
8-4-4-4-12 lowercase hex). Unrecognized URLs still return `""` (row skipped —
unchanged). No `source` column is added to the schema; source is derived from
the `link` fragment (`bidspotter.com`), exactly like `gd`/`ps`. No SQLite
migration needed.

### Trust-guard widening (the one shared behavior change)

`top_chairs.py`:

- `TRUSTED_QUANTITY_SOURCE = "llm"` → `TRUSTED_QUANTITY_SOURCES = frozenset({"llm", "structured"})`
  (the old singular constant is removed; its only importer is
  `tests/test_quantity_trust.py`, updated in the same task).
- `trusted_quantity()` membership test instead of equality.
- `_load_from_cache` SQL: `quantity_source = ?` → `quantity_source IN (?, ?)`.
- `regex_title` / `regex_fulltext` / `llm_failed` / `llm_missing` remain
  untrusted everywhere.

`automation/auctions_supabase.py` — **design correction:** the original brief
said "widen its trusted-source filter"; in reality
`_load_from_supabase` has **no `quantity_source` filter at all** (SQL is just
`quantity >= / <= / link ILIKE`). Its module docstring promises output
"byte-for-byte identical" to `top_chairs`, so the missing filter is an
accidental parity gap from the Supabase cutover. This feature **adds** the
filter: `AND quantity_source = ANY(%s)` with `list(TRUSTED_QUANTITY_SOURCES)`.
Known behavior change: any legacy `auction_listings` rows with untrusted
sources (regex seeds never LLM-refined, `llm_failed`) stop being served by
`/api/auctions` — which is what the BLACKWHOLE-4 read-side guard already does
on the SQLite path.

### Source plumbing (enumerated touch-points)

| file | change |
|---|---|
| `auction_extractors/listings_db.py` | `extract_asset_id`: `bs:<guid>` pattern + docstring |
| `auction_extractors/top_chairs.py` | `Source` Literal + `_BIDSPOTTER_URL_FRAG = "bidspotter.com"` + frag map in `_load_from_cache` + trust frozenset + SQL `IN` + validation tuple + CLI `--source` choices |
| `auction_extractors/__main__.py` | `top --source` choices `("gd","ps","bs")` |
| `auction_extractors/__init__.py` | no change (exports `get_top_chairs` only — **design correction:** it has no source validation of its own) |
| `automation/auctions_supabase.py` | `Source` Literal, `_SOURCE_FRAG["bs"]`, validation tuple, trusted-source `ANY(%s)` filter, `cache_stats` CASE arm for `bs` |
| `automation/web/app.py` | `_SCRAPE_SCRIPTS["bs"]`, `_SCRAPE_LABELS["bs"]`; `/api/scrape/start` accepts `("gd","ps","bs","both")`; `_run_scraper` `"both"` → `["gd","ps","bs"]` (see below); `--test` flag passed for `bs` like `ps`; `/api/auctions` + `/api/test-scrape` accept `bs`; `_test_scrape_sync` `bs` branch; `_asset_id_from_link` `bs` pattern (favorites — **design correction:** the brief missed this duplicate; without it BidSpotter cards can't be starred); `/api/listings` source filter + `_source_of` |
| `automation/web/templates/index.html` | Auctions `#auc-source` seg + scrape dropdown (`data-scrape="bs"`, "Both" relabeled "All (gd → ps → bs)"); Listings-DB `#ldb-source` seg; Test-Scrape `#ts-source` seg |
| `automation/web/static/app.js` | shared `SOURCE_NAMES` map used by the 4 hardcoded `'gd' ? 'GovDeals' : 'Public Surplus'` spots; `_assetIdFromLink` `bs` pattern; Listings-DB `src-bs` pill class; test-scrape `both` fan-out includes `bs` |
| `automation/web/static/app.css` | `.src-pill.src-bs` + `.ts-source-pill[data-source="bs"]` colors |
| `scripts/transfer_listings_to_supabase.py` | **no change** (verified: `quantity_source` is in `COLS` and copied verbatim; `"structured"` passes through) |
| `auction_extractors/.env.example` | BidSpotter env block |
| `scripts/run_discovery.sh` + `scripts/daily_scrape.sh` | add the BS step (plain HTTP — safe in the Chromium-less cloud image). Without the cron, BS rows exceed `max_stale_days=2` within two days and vanish from the tab |

**`"both"` semantics decision:** keep the key `"both"` (back-compat with any
scripted callers) but redefine it as "all configured scrapers", i.e.
`["gd", "ps", "bs"]`, and relabel the dropdown item "All (gd → ps → bs)". No
new `"all"` alias — one key, one path.

**Launch-button gating — design correction:** no change needed. The Auctions
card gate is `link.includes('govdeals.com')` (allow-list), so BidSpotter links
are already disabled with the tooltip "Pipeline only supports GovDeals URLs".
Same for "queue all" (filters to govdeals.com) and the Listings-DB launch
button (`r.source === 'gd'`).

### New env vars (defaults in parentheses)

`BIDSPOTTER_SEARCH_TERMS` ("chairs", comma-separated),
`BIDSPOTTER_PAGE_SIZE` (120), `BIDSPOTTER_MAX_PAGES` (20),
`BIDSPOTTER_HTTP_DELAY_SEC` (0.5), `BIDSPOTTER_WAF_RETRIES` (4),
`BIDSPOTTER_WAF_BACKOFF_SEC` (2), `BIDSPOTTER_INCLUDE_LIVE` (1).
Reused shared vars: `USE_LLM_QUANTITY`, `QUANTITY_LLM_PROVIDER` (+ provider
keys), `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID`, `INCLUDE_MEDICAL`,
`LISTINGS_DB_PATH`/`LISTINGS_DB_DISABLE`.

## 4. Error handling

- **WAF 202:** a single `_fetch()` helper wraps every BidSpotter HTTP call;
  status 202 + `x-amzn-waf-action` header → linear-backoff retry up to
  `BIDSPOTTER_WAF_RETRIES`; exhaustion raises (a term's failure is caught at
  the term loop, mirroring PS's per-term isolation).
- **Bid-info POST failure:** caught per page; cards keep `price=""` /
  `time_left=""` and are still archived.
- **LLM failure:** unchanged semantics — `quantity=None`,
  `quantity_source="llm_failed"`, degradation Telegram alert. Structured rows
  are never routed through the LLM, so an LLM outage cannot degrade them.
- **Telegram:** best-effort, never raises (mirrors PS).
- **Missing forItem entry for a card:** `end_date=""` — `_is_active` then
  falls back to the `last_seen_at` staleness check (same as PS browser rows).

## 5. Testing strategy

Fixtures (committed, real saved data):
- `auction_extractors/tests/fixtures/bidspotter_search_chairs.html` — trimmed
  live search page: 3 real cards (one WITHOUT structured quantity + featured
  variant; two WITH, incl. locations) + the trimmed `forItem` JSON +
  pagination (`data-pages="13"`) + count element.
- `auction_extractors/tests/fixtures/bidspotter_bid_info.json` — live-captured
  response for those 3 GUIDs (wrapped `{"Model": {…}}` shape).

Tests (pattern: `test_publicsurplus_parse.py` — standalone `main()` +
`test_*` pytest entry, zero network):
1. `test_bidspotter_parse.py` — card parser: count=3, CARD_KEYS contract,
   title/link/quantity/structured-source/confidence/location/end_date/
   image_url(query stripped)/lot_number per card; the no-quantity card seeds
   `regex_title`; garbage/empty HTML → `[]`.
2. forItem extraction: GUID-keyed map, `Lot End Time UTC` → `end_date` ISO
   UTC, quantity cross-check.
3. Bid-info parsing: fixture → price `"USD 7.00"` (LeadingBid), Model
   unwrapping, `/Date(ms)/` handling, `TotalBids=0` → StartPrice (synthetic
   row built inline in the test), missing GUID → price stays `""`.
4. WAF retry: fake session returning 202+header then 200 → retried result;
   persistent 202 → raises after N+1 attempts.
5. `extract_asset_id`: BS lot URL → `bs:<guid>`; garbage/other URLs → `""`;
   existing gd/ps patterns unchanged.
6. Widened trust guard (update `test_quantity_trust.py`): `structured` rows
   trusted; `regex_title`/`regex_fulltext`/`llm_failed`/`llm_missing`
   untrusted; `TRUSTED_QUANTITY_SOURCES == frozenset({"llm","structured"})`.
7. `top_chairs._load_from_cache` serves a `structured` row from a temp SQLite
   DB and still excludes `regex_title`.

Commands (this venv has **no pytest console script**):
```
.venv/bin/python -m pytest auction_extractors/tests/ -q
.venv/bin/python -m pytest tests/ -q          # repo-level suite (shared files touched)
```
Integration smoke: `python -m auction_extractors top --source bs --no-condition --include-expired`
after one manual scraper run.

## 6. Out of scope / follow-ups

- **`deals/` adapter** for BidSpotter (`SiteAdapter` seam in
  `deals/adapters/base.py`) — backlog ticket.
- **Per-house buyer premiums** (separate fees endpoint) — needed for landed
  cost; out of v1.
- **Piecemeal per-unit pricing** (`LeadingBid × Quantity` totals) — v1 stores
  `LeadingBid` as-is; revisit with landed-cost work.
- **Live-auction pricing** — `live` lots (~10%) can't be price-polled; they
  are archived and gated by `BIDSPOTTER_INCLUDE_LIVE`.
- **Per-lot detail-page fetch** — card description snippets suffice for the
  LLM fallback in v1; a detail fetch (bs_lot.html was saved) could add full
  descriptions later.
- **BLACKWHOLE-4 regex-code deletion** — still separate cleanup.

## 7. Self-review (done)

- Placeholder scan: none (all selectors/fields carry verified values).
- Consistency: `quantity_source` values named identically in §1/§3/§5;
  `TRUSTED_QUANTITY_SOURCES` spelling consistent; stage prefixes match
  `_SCRAPE_STAGES` keys.
- Scope: no deals/, no fees, no piecemeal math; `"both"` decision documented.
- Ambiguity: forItem `"1"` ambiguity resolved via DOM-presence rule; page-size
  variance resolved via read-`data-pages`; response `Model` wrapper stated.
