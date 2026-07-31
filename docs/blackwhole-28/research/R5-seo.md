# R5 — GovAuctions.app SEO Surface (Programmatic Page Pattern)

Scope: identify GovAuctions.app's traffic-engine architecture — page-template families, approximate scale, on-page schema, and internal-link mesh — to inform a US-only v1 programmatic-SEO page set for BLACKWHOLE's own liquidation-auction aggregator + sold-comps dataset. No copy or design was reproduced; only structural pattern (URL shapes, data sources, schema.org types) was recorded.

All findings below are from live fetches on 2026-07-31.

---

## 1. Sitemap census

`robots.txt` → `Sitemap: https://govauctions.app/sitemap.xml` (a sitemap **index**, not a urlset).

| Sitemap index entry | Total `<url>` count | Content |
|---|---|---|
| `https://govauctions.app/sitemap/0.xml` | **1,247** | US programmatic hub/category/state/platform pages (evergreen, non-item pages) |
| `https://govauctions.app/sitemap/1.xml` | **486** | UK **individual sold-item** detail pages (`/uk/auction/{slug}-{uuid}`) only |

`sitemap/2.xml` through `/4.xml` → HTTP 404 (confirmed via direct fetch). No `/uk/sitemap.xml`, `/sitemap-cities.xml`, or `/sitemap-sold-prices.xml` exist. **Total declared sitemap URLs ≈ 1,733.**

**Important finding:** the sitemap is a small fraction of what's actually indexed. Individual US `/auction/{id}` listing pages (tens of thousands, by his own on-page counts — e.g. "1706 GovDeals Auctions in Virginia" alone), `/cities/{state}/{city}` pages, `/sold-prices/{item}` and `/sold-prices/{item}/{state}` pages, `/guides/*`, `/research/*`, and the `/au/` (Australia) and most of `/uk/` sections are **absent from both sitemap files** yet **are** turning up in live `site:` search samples (evidence in §3). `robots.txt` disallows almost nothing (`/auth/`, `/saved`, `/settings`, `/alerts`, `/api/` except the OpenAPI spec, `/auction/_honeypot`) and explicitly `Allow: /` for every major AI crawler (ClaudeBot, GPTBot, PerplexityBot, Google-Extended, etc.) — so the strategy for the long tail appears to be **crawl-discoverable via internal links, not sitemap-declared.** Only the evergreen, low-cardinality "hub" layer (categories × states × platforms) gets a sitemap; the high-cardinality, ephemeral layer (individual listings) relies on being reachable in ≤3 clicks from a hub page and on being fresh/frequently-relinked.

---

## 2. Template inventory — URL pattern → data source → approx. count

| # | URL pattern | Example | Data source | Approx. count | In sitemap? |
|---|---|---|---|---|---|
| 1 | `/` | homepage | live feed + stats | 1 | yes |
| 2 | `/auctions` | category × state directory hub | static index | 1 | yes |
| 3 | `/{topic}-near-me` | `/car-auctions-near-me`, `/police-auctions-near-me` | geo-IP live feed | 5 | yes |
| 4 | `/auctions/{category}` | `/auctions/vehicles`, `/auctions/military-surplus` | live listings + sold comps, category-scoped | ~20 (incl. `under-500/1000/5000` price-tier variants treated as pseudo-categories) | yes |
| 5 | `/auctions/{state}` | `/auctions/texas` | live listings + sold comps, state-scoped | 51 (50 states + DC) | yes |
| 6 | `/auctions/{category}/{state}` | `/auctions/vehicles/california` | live listings + sold comps, category×state | **~390 observed, NOT a full 10-category×51-state cartesian product (510)** — empty/thin combos appear to be pruned | yes |
| 7 | `/auctions/{category}/ending-soon` | `/auctions/vehicles/ending-soon` | live feed sorted by close time | 11 | yes |
| 8 | `/platforms/{source}` | `/platforms/govdeals` | per-source live listing count + sold comps | 10 confirmed in sitemap; on-page copy ("GovDeals: 25,959 Listings, Plus 13 Other Sites") implies **14 platforms total** | yes (root only) |
| 9 | `/platforms/{source}/{state}` | `/platforms/govdeals/virginia` | per-source, per-state live listings + sold comps | not in sitemap but indexed (site: sample hit multiple states/sources) — likely ~14 × ~50 ≈ **~700** | **no** |
| 10 | `/cities/{state}/{city}` | `/cities/texas/houston` | geo-radius live feed | indexed, not sitemapped; volume unknown but likely hundreds–low thousands (every US city with ≥1 nearby active lot) | **no** |
| 11 | `/sold-prices` | hub | index of item-keyword comps pages | 1 | unconfirmed |
| 12 | `/sold-prices/{item-keyword}` | `/sold-prices/honda-accord`, `/sold-prices/iphone`, `/sold-prices/tablet`, `/sold-prices/gold-ring`, `/sold-prices/chevrolet-tahoe` | **sold-comps DB, one popular item keyword at a time** (median / P25–P75 / lot count / monthly trend) | indexed widely; not sitemapped — this is his highest-intent, lowest-competition long-tail play | **no** |
| 13 | `/sold-prices/{item-keyword}/{state}` | `/sold-prices/dell-laptop/virginia` | sold-comps DB, item×state | indexed; volume unknown, likely the single largest template family by count if fully built out (items × 50 states) | **no** |
| 14 | `/auction/{slug}-{state}-{platform}-{lot-id}` or `/auction/{platform}-{lot-id}` (redirects 308 to the slugged canonical) | `/auction/2016-caterpillar-259d-cat-texas-govdeals-333-29073`, `/auction/white-board-easel-arizona-publicsurplus-4019523` | individual listing (live **or** sold — same template serves both states) | very large (tens of thousands+, US only, not sitemapped) | **no** |
| 15 | `/uk/auction/{slug}-{location}-{auctioneer}-{uuid}` | `/uk/auction/indespension-tow-a-van-tav4-braked-twin-axle-box-trailer-bpi-672d034f-…` | UK **sold-comp** detail pages (Eddisons, NCM, BPI, Witham/ex-MoD, Ramco) | **486** confirmed via sitemap | yes (only country/template combo that IS fully sitemapped at item level) |
| 16 | `/uk/…`, `/au/…` mirror of the entire US template set | `/uk/auctions/vehicles`, `/uk/sold-prices/excavator`, `/au/auctions/electronics`, `/au/auction/1-x-stamped-cartier-…` | same architecture, per-country data | indexed; per-country totals not established | **no** (except the UK item pages above) |
| 17 | `/guides/{slug}` | `/guides/complete-guide-government-surplus-auctions`, `/guides/govdeals-alternatives`, `/guides/best-government-auction-sites` | editorial/evergreen content, not templated from a table | ~6+ confirmed, likely 15–30 total | **no** |
| 18 | `/research/{slug}` | `/research/cheapest-homes-in-america`, `/research/what-the-government-cant-give-away` | data-journalism pieces computed from the same sold-comps DB (link-bait, not a repeating template) | small, hand-written | **no** |
| 19 | `/feed?...` | `/feed?category=vehicles&state=CA`, `/feed?platforms=govdeals&state=VA` | client-side filtered live search (the "browse all N" CTA target off every hub page) | N/A — not a static page, canonical-tagged/noindexed presumably | no |

**Reading the shape:** the sitemap-declared 1,247 US URLs are almost entirely the **evergreen combinatorial grid** (category × state × platform), which is small, stable, and cheap to keep 100% indexed. Everything with real per-item cardinality — `/auction/{id}`, `/sold-prices/{item}[/{state}]`, `/cities/{state}/{city}` — is deliberately kept **out of the sitemap** and left to internal-link discovery. That's consistent with those pages either being high-churn (listings expire) or extremely high-cardinality (item keywords × states), where a bloated sitemap would hurt more than help crawl budget.

---

## 3. Google indexing scale — site: search evidence

The WebSearch tool used here doesn't expose Google's numeric result-count estimate (only top result snippets), so exact indexed-page counts per prefix are **not obtainable with this tooling** — flagged as an open question below. What the samples do prove is *breadth of indexation across templates the sitemap doesn't declare*:

| Query | Sample results confirm indexation of… |
|---|---|
| `site:govauctions.app/auctions/` | category pages, state pages (New York, Texas, California, Massachusetts, Georgia), category root pages (jewelry, collectibles, police-auctions, military-surplus) |
| `site:govauctions.app/platforms/` | platform×state pages: `govdeals/minnesota`, `govdeals/mississippi`, `govdeals/new-mexico`, `govdeals/nevada`, `govdeals/virginia`, `govdeals/wisconsin`, `bid4assets/ohio`, `publicsurplus/montana` — **confirms template #9 is indexed despite zero sitemap coverage** |
| `site:govauctions.app/cities/` | `cities/oklahoma/oklahoma-city`, `cities/georgia/acworth`, `cities/pennsylvania/philadelphia`, `cities/florida/miami`, `cities/minnesota/maple-plain`, `cities/utah/tooele`, `cities/california/sacramento` — confirms template #10 indexed |
| `site:govauctions.app/guides/` | 6 distinct guide URLs surfaced organically |
| `site:govauctions.app "sold-prices"` | `/sold-prices/iphone`, `/sold-prices/tablet`, `/sold-prices/gold-ring`, `/sold-prices/honda-accord`, `/sold-prices/chevrolet-tahoe`, `/sold-prices/dell-laptop/virginia` (item×state variant) — confirms templates #12 and #13 indexed |
| `"govauctions.app/auction/"` (no `uk`) | `/uk/sold-prices/excavator`, `/uk/sold-prices/monitor`, `/uk/sold-prices/box-truck`, `/uk/auctions/vehicles`, `/uk/sold-prices/ipad` — confirms the `/uk/` mirror extends to sold-prices too, not just item pages |
| `site:govauctions.app/ca/ OR /au/` | `/au/auctions/electronics`, `/au/auction/1-x-stamped-cartier-auto-wrist-watch-…-grays-25810143` — confirms a **third** country (`/au/`) is live; his own About page also claims Canada coverage (unconfirmed as a live `/ca/` URL prefix in this pass) |

**Net read:** actual indexed surface is almost certainly several multiples of the 1,733 sitemapped URLs once `/auction/{id}` pages, `/platforms/{source}/{state}` pages, `/cities/`, `/sold-prices/{item}[/{state}]`, and the `/uk/` + `/au/` mirrors are counted — plausibly tens of thousands of indexed URLs total, matching the "hundreds of thousands of users" / "tens of thousands of live listings at a time" / "180,000+ completed sales" scale claimed on `/about`.

---

## 4. Anatomy of a programmatic page (4 samples)

### 4a. Category × State — `/auctions/vehicles/california`
- **Title:** "Government Surplus Vehicles for Sale in California | GovAuctions.app"
- **H1:** "Government Surplus Vehicles in California"
- **Sections:** live listing cards (12 items, images, final bid, location, bid count) → **"What Vehicles Sell For in California"** comps table (median winning bid across 878 completed CA vehicle sales, broken out by vehicle model, e.g. "Ford F-150 — median $2,857 / 66 sales / range $1,268–$6,581") → nationwide baseline stat line → "Other Categories in California" → "Vehicles in Other States" → FAQ → "Related Pages" (guides, `/sold-prices`, `/research/surplus-price-index`)
- **JSON-LD (confirmed via raw HTML, `<script type="application/ld+json">` count = 6):** `Organization`, `WebSite`+`SearchAction`, `ItemList` (wrapping the live cards), `Product`+`Offer` per card, `FAQPage`+`Question`/`Answer`, `BreadcrumbList`, `Dataset`+`PropertyValue` (for the comps stats block).

### 4b. Platform × State — `/platforms/govdeals/virginia`
- **Title:** "1706 GovDeals Auctions in Virginia - Updated Daily (2026) | GovAuctions.app"
- **H1:** "GovDeals Auctions in Virginia (2026)"
- Same schema stack as 4a (`ItemList`/`Product`/`Offer`, `FAQPage`, `BreadcrumbList`, `Dataset`). Comps block: "median winning bids from 956 completed Virginia auction sales." Internal links: category breakdown within-platform (`Vehicles (187)` → `/auctions/vehicles/virginia`, note this cross-links to the *category* URL, not a `/platforms/govdeals/virginia/vehicles` triple-combo — he caps combinatorial depth at 2 segments), "Browse All 1706 GovDeals Virginia Listings" → `/feed?platforms=govdeals&state=VA`, sibling-platform state page (`Ohio (2987)` → `/platforms/govdeals/ohio`).

### 4c. Sold-comps item page — `/sold-prices/honda-accord`
- **Title:** "Honda Accord Government Auction Prices: What They Sell For (Median $2,050) | GovAuctions.app"
- **H1:** "Honda Accord: what it sells for at government auction"
- **Sections:** "Median sale price by month" (trend), "Recent sold Honda Accord examples" (6-item list: year/model/price/date/state), **"Honda Accord sold prices by state"** (the internal-link fan-out into template #13: California $2,178/18 lots, Ohio $1,471/14 lots, New Jersey $572/13 lots…), "Other vehicles sold prices" (→ `/sold-prices/box-truck` etc.), FAQ.
- **JSON-LD:** `Organization`, `WebSite`, `FAQPage`, `BreadcrumbList`, `Dataset`+`PropertyValue` — **no `Product`/`Offer`** on this template (correct: it's a stats page about a keyword, not a single item).
- Links out to one live `/auction/{id}` example and to the state-scoped comps variant — this page is the connective tissue between the evergreen grid (§2 templates 4–9) and the per-item long tail (templates 12–14).

### 4d. Individual sold-item page — `/auction/white-board-easel-arizona-publicsurplus-4019523`
- **Title:** "White Board Easel - Sold for $27 | Government Auction Price (AZ) | GovAuctions.app"
- Fields: sold price, bid count, "Ended {date}", location, source platform/auctioneer as `brand`, category, description, single image (proxied through his own `/api/image?url=…` — **not stored/re-hosted, but not a raw hotlink either; it's server-side proxied**), pickup/as-is terms, "Similar items" rail, "Create Free Alert" CTA, upsell links to Pro plan + Developer API.
- **JSON-LD:** `Organization`, `WebSite`, **`Product`** (`name`, `image`, `category`, `brand.name` = source platform, `offers.Offer` with `price`, `priceCurrency`, `availability: schema.org/SoldOut`, `validFrom`/`priceValidUntil` = auction window), **`BreadcrumbList`** (Home → category → state → item). Live (non-sold) items presumably flip `availability` to `InStock`/`SoldOut` dynamically — not directly confirmed on a still-live lot in this pass, but the Offer shape strongly implies it.
- Note: this template is used for **both** UK sold-comp pages (`/uk/auction/…`, sitemapped) and US live+sold pages (`/auction/…`, not sitemapped) — same component, different indexing policy per country/section.

**Cross-cutting SEO mechanics observed:**
- Every page carries site-wide `Organization` (with `founder`, `sameAs` social/Wikidata/GitHub links — an E-E-A-T signal) and `WebSite`+`SearchAction` (sitelinks searchbox eligibility).
- `FAQPage` schema appears on every hub-level page (category, state, category×state, platform×state) — cheap, repeatable rich-result surface.
- `BreadcrumbList` on every page gives Google a clean hierarchy signal reinforcing the URL nesting.
- `Dataset`+`PropertyValue` on every comps-bearing page is an unusual but sensible choice — it marks the median/P25/P75/count block as structured statistical data, not prose.
- Internal-link mesh depth is capped at 2 path segments almost everywhere (`/auctions/{category}/{state}`, `/platforms/{source}/{state}`, `/sold-prices/{item}/{state}`) — he does **not** build 3-segment combos (no `/auctions/{category}/{state}/{city}`, no `/platforms/{source}/{category}/{state}`). Depth is capped; breadth (many independent 2-segment families) is preferred.
- Combinatorial pages are **pruned when empty** (391 observed category×state pages vs. 510 possible for the 10 fully-tallied categories) — thin/zero-inventory combos don't get a URL.
- A single honeypot link (`/auction/_honeypot`, `rel="nofollow"`, visually hidden) sits in the header of every page — bot-detection tripwire, not an SEO mechanism, but worth replicating for scraper-abuse defense if BLACKWHOLE builds a public aggregator surface.

---

## 5. International expansion pattern

Country is a **path prefix**, switched via a header dropdown (🇺🇸 US shown; not enumerated in static HTML, so full country list wasn't confirmed by DOM inspection alone). Confirmed live: **US** (no prefix), **`/uk/`** (486 sold-item pages sitemapped + guides/tools/sold-prices/feed/subscribe mirror), **`/au/`** (auctions + item pages confirmed via search, sourced from Grays/Pickles-style Australian surplus auctioneers). His `/about` page additionally claims Canada coverage, not independently confirmed as a live `/ca/` prefix in this pass. The template machinery (category/state-or-region grid + item detail + sold-comps) appears to be country-parameterized, not US-specific — i.e., the whole programmatic-SEO system was built to generalize across country the same way it generalizes across state.

---

## 6. Recommended programmatic-SEO page set — BLACKWHOLE US v1

Framed against what BLACKWHOLE already has: the `listing_automation/deals/` package (`Lot`/`Snapshot`/`Outcome` models, `deal_lots`/`deal_snapshots` Supabase tables, `landed_cost` via `fees.py`, category classification via `categories.py`/`classify.py`, image archiving via `archive.py`) is **already most of the backend a sold-comps aggregator needs** — it's GovDeals-only and furniture/General-Merchandise-scoped today, but the schema and pipeline generalize the same way GovAuctions.app's do. This is a build-on, not a build-from-scratch.

**Recommended v1 template set (ranked by leverage, i.e. cheapest to generate from data already modeled vs. SEO value):**

1. **`/sold-prices/{item-keyword}`** — the single highest-leverage template. Group `deal_lots` (once multi-source, not just GovDeals) by a normalized item keyword, compute median/P25/P75/count, render exactly the stats-page shape in §4c. This is what GovAuctions.app itself leans on hardest for long-tail, and it's a near-direct materialized view over data BLACKWHOLE's `deals/` schema already half-produces (`Outcome`, price, category). Needs: an item-keyword normalizer (title → canonical keyword, not yet built) and multi-source ingestion (today GovDeals-only, per repo's `SiteAdapter` seam in `deals/adapters/base.py`).
2. **`/auction/{slug}-{state}-{source}-{lot-id}`** — individual **sold**-lot detail page. `deal_lots`/`deal_snapshots` + `archive.py`'s already-archived images make this nearly free once the classification+fee pipeline is trusted; ship `Product`+`Offer`(`availability: SoldOut`)+`BreadcrumbList` JSON-LD from day one, matching §4d exactly.
3. **`/auctions/{category}/{state}`** — the evergreen grid. Use `deals/categories.py`'s canonical bucket list (furniture cluster + General Merchandise today; expand as sources widen) × US states, but **prune empty combos** the way GovAuctions.app does — don't generate a page for a category/state pair with zero `deal_lots` rows.
4. **`/auctions/{state}`** and **`/auctions/{category}`** — the two single-axis roll-ups sitting above #3, needed as the internal-link hub layer.
5. **`/platforms/{source}`** and **`/platforms/{source}/{state}`** — once BLACKWHOLE ingests more than GovDeals (Public Surplus, Municibid, MiBid, Purple Wave, GSA, iBid Illinois, HiBid, Bid4Assets, PropertyRoom are all already in the "verified source scorecard"), these are a cheap second grid over the same `deal_lots` table, keyed on `source` instead of `category`.
6. **`/sold-prices/{item-keyword}/{state}`** — second-order fan-out from #1, only worth building once #1's keyword normalizer exists and per-keyword volume justifies the state split (GovAuctions.app clearly treats this as a *later* refinement, not v1 — it's unsitemapped even on his own site).
7. **FAQPage + BreadcrumbList + Dataset/PropertyValue JSON-LD on every templated page** — cheap, copy this mechanic (not the copy) wholesale; it's low-effort structured data that both product pages and comps pages benefit from.
8. **Defer to v2:** `/cities/{state}/{city}` (geo long-tail, huge cardinality, lower per-page value), `/guides/*` and `/research/*` (hand-written content, not template-driven), country-prefix expansion (no reason to internationalize before the US v1 proves out), price-tier pseudo-categories (`/under-500` etc.).

**Key structural decision to make before building:** cap combinatorial depth at 2 path segments (category×state, platform×state, item×state) and prune empty cells — this is the single biggest reason GovAuctions.app's sitemap-declared page count (1,247 for the entire evergreen US grid) stays small and 100%-indexed while the real long tail (item pages, item×state comps) is left for crawl-discovery rather than sitemap bloat. BLACKWHOLE should mirror that split: sitemap the evergreen grid, let internal links carry the item-level long tail.

---

## Sources consulted

- https://govauctions.app/sitemap.xml , /sitemap/0.xml , /sitemap/1.xml
- https://govauctions.app/robots.txt
- https://govauctions.app/auctions/texas , /auctions/vehicles/california , /platforms/govdeals/virginia
- https://govauctions.app/sold-prices/honda-accord
- https://govauctions.app/auction/white-board-easel-arizona-publicsurplus-4019523
- https://govauctions.app/about
- https://govauctions.app/ (homepage, raw HTML)
- Google `site:govauctions.app` queries scoped to `/auctions/`, `/platforms/`, `/cities/`, `/guides/`, `/sold-prices`, `/uk/auction/`, `/ca/ OR /au/` (via WebSearch tool, July 2026)
