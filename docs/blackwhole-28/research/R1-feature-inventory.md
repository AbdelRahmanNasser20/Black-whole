# R1 — Full Feature Inventory of GovAuctions.app

Research date: 2026-07-31. All figures below are live snapshots from that date (site claims to update "daily" / "hourly") and will drift.

## 0. Method

1. Fetched `robots.txt` → found `Sitemap:` directive → fetched the sitemap index → fetched both child sitemaps.
2. Crawled ~17 representative pages (one per template) via `WebFetch`; one raw `curl` for a client-rendered page whose pricing text wasn't in the RSC-serialized markdown.
3. No auth attempted, no `/api/` calls beyond the public `openapi.json` (already known/allowed), no hammering — one fetch per URL, spread across the session.

---

## 1. robots.txt — full contents

```
User-Agent: *
Allow: /
Allow: /api/v1/openapi.json
Disallow: /auth/
Disallow: /saved
Disallow: /settings
Disallow: /alerts
Disallow: /api/
Disallow: /auction/_honeypot

User-Agent: GPTBot
Allow: /
User-Agent: ChatGPT-User
Allow: /
User-Agent: OAI-SearchBot
Allow: /
User-Agent: ClaudeBot
Allow: /
User-Agent: Claude-Web
Allow: /
User-Agent: Claude-User
Allow: /
User-Agent: Claude-SearchBot
Allow: /
User-Agent: PerplexityBot
Allow: /
User-Agent: Perplexity-User
Allow: /
User-Agent: GoogleOther
Allow: /
User-Agent: Google-Extended
Allow: /
User-Agent: Google-CloudVertexBot
Allow: /
User-Agent: Applebot-Extended
Allow: /
User-Agent: cohere-ai
Allow: /
User-Agent: CCBot
Allow: /
User-Agent: Amazonbot
Allow: /
User-Agent: Meta-ExternalAgent
Allow: /
User-Agent: Bytespider
Allow: /

Sitemap: https://govauctions.app/sitemap.xml
```

**Notable:** `Disallow: /auction/_honeypot` — a deliberate bot trap. Any crawler that ignores `robots.txt` and fetches every discovered path will eventually request `/auction/_honeypot`; that's almost certainly how Ben Wallace fingerprints non-compliant scrapers. **Do not fetch that path, ever, from any BLACKWHOLE tooling.** Also notable: he explicitly *allows* every major AI crawler by name (GPTBot, ClaudeBot, PerplexityBot, etc.) — he wants AI answer-engines citing him, consistent with the heavy JSON-LD / SEO investment mentioned in the brief.

---

## 2. Sitemap structure

`sitemap.xml` is an index pointing at two children:

| File | Entries | Content |
|---|---|---|
| `sitemap/0.xml` | 1,195 | All US-market SEO surface (category/state/price/near-me/platform pages) |
| `sitemap/1.xml` | ~630 | **UK auctions**, one `<url>` per individual listing |

### 2a. `sitemap/0.xml` breakdown (US)

| Prefix | Count | What |
|---|---|---|
| `/auctions/...` | 1,177 | Category, category+state, category+ending-soon, price-tier pages (see §3) |
| `/platforms/...` | 10 | One hub page per source: `govdeals`, `gsa-auctions`, `publicsurplus`, `municibid`, `govplanet`, `propertyroom`, `fannie-mae-homepath`, `purple-wave`, `jj-kane`, `bid4assets` |
| `/`, `/feed`, `/ending-soon`, `/hud-homes` | 4 | Core utility pages |
| `/government-auctions-near-me`, `/police-auctions-near-me`, `/car-auctions-near-me`, `/real-estate-auctions-near-me`, `/equipment-auctions-near-me` | 5 | Geo-intent SEO landers |

**Zero individual per-listing US detail pages are in the sitemap** — no `/auction/{slug}` entries at all, despite those pages existing and being internally linked (see §4.2). `/guides/*`, `/sold-prices`, and `/tools/*` are also absent from the sitemap despite being live, crawlable, linked-in-nav pages. Read together with the honeypot rule, this looks deliberate: the sitemap is scoped to pages Wallace wants ranked as evergreen SEO surface (durable URLs, high pagerank-in), while high-cardinality/high-churn pages (individual live auctions, which expire in days) and content he'd rather have discovered via internal links/backlinks (guides, tools) are left out, presumably to control crawl budget and avoid thin/duplicate-content signals from thousands of auctions that vanish every day.

### 2b. `sitemap/1.xml` — UK expansion (not in the known-facts brief — new finding)

Every entry matches:
```
https://govauctions.app/uk/auction/{item-title-slug}-{location-slug}-eddisons-{uuid}
```
Examples:
- `/uk/auction/industrial-air-dryer-unit-donaldson-ultra-cool-0140sp-serial-number-48698-west-yorkshire-eddisons-89af5fa4-6804-43e9-bd1e-b49100b67de2`
- `/uk/auction/alexander-dennis-enviro-400-open-top-sightseeing-bus-registration-sn12-egy-odome-eddisons-fa31d29a-a924-443a-a2a5-b48e00d7d7e1`

All `changefreq: daily`, `priority: 0.6`, `lastmod` clustered ~14:37 UTC on 2026-07-30 (a single batch sweep). Source is **Eddisons**, a UK commercial/industrial auctioneer — not a government source. So unlike the US side (individual listings excluded from sitemap), **UK individual listings are indexed one-by-one**. The About page (§5) confirms four geographic markets: US, UK, Canada, Australia — but only UK and US had any sitemap presence at fetch time; no `/ca/` or `/au/` URLs exist yet in either sitemap, suggesting Canada/Australia coverage is either API-only, not yet publicly templated, or still pending its own sitemap batch.

---

## 3. Page-template catalogue

| # | Template | Example URL | Gating |
|---|---|---|---|
| 1 | Homepage | `/` | Free |
| 2 | Live feed / main search | `/feed` | Free |
| 3 | Ending-soon feed | `/ending-soon` | Free |
| 4 | Category browse | `/auctions/vehicles` | Free |
| 5 | Category + state | `/auctions/vehicles/california` | Free |
| 6 | Category + ending-soon | `/auctions/vehicles/ending-soon` | Free |
| 7 | Price-tier lander | `/auctions/under-500`, `/under-1000`, `/under-5000` | Free |
| 8 | Individual listing detail (US) | `/auction/2013-ford-f-250-sd-ohio-govdeals-14436-4788` | Free page, **Pro-gated section inside** |
| 9 | Individual listing detail (UK) | `/uk/auction/{slug}-eddisons-{uuid}` | Free (assumed same pattern) |
| 10 | Platform hub | `/platforms/govdeals` (×10 platforms) | Free |
| 11 | HUD homes lander | `/hud-homes` | Free |
| 12 | Near-me geo lander | `/government-auctions-near-me` (×5 variants: government/police/car/real-estate/equipment) | Free |
| 13 | Sold-prices index | `/sold-prices` | Free (aggregate numbers); presumably drill-down pages exist per item/category but weren't found in the sitemap and weren't independently confirmed this pass |
| 14 | Tools index | `/tools` | Free |
| 15 | Flip Profit Calculator | `/tools/flip-profit-calculator` | Free, no signup |
| 16 | "What's It Worth?" estimator | `/tools/whats-it-worth` | Free |
| 17 | Guides index | `/guides` | Free |
| 18 | Individual guide article | `/guides/{slug}` (24+ articles seen) | Free |
| 19 | About | `/about` | Free |
| 20 | Pricing/subscribe | `/subscribe` | Free page describing the paid plan |
| 21 | Saved listings | `/saved` | **Disallowed in robots.txt, requires login** |
| 22 | Alerts management | `/alerts` | **Disallowed in robots.txt, requires login** |
| 23 | Settings | `/settings` | **Disallowed in robots.txt, requires login** |
| 24 | Auth | `/auth/*` | Disallowed in robots.txt |
| 25 | Public API spec | `/api/v1/openapi.json` | Explicitly `Allow`ed (only path under `/api/` that is) |
| 26 | Bot honeypot | `/auction/_honeypot` | Trap — never fetch |

---

## 4. Template-by-template detail

### 4.1 Homepage (`/`)

- H1-equivalent hero: **"Search all government auctions in one place, know what they're worth before you bid."**
- Live stat strip (as of 2026-07-31): **54,050 live auctions · 32 sources · updated daily +4,375 new today · 230,000+ auctions tracked historically · 180,000+ completed sales indexed for pricing.** (Brief's known fact said "~29 sources / 150k+ sold comps" — both numbers have grown since that snapshot; treat as a moving target, re-check before quoting competitively.)
- "Near me" search box + "Browse all →" link.
- Live listing cards: title, current bid, city/state, time-left, bid count, thumbnail, direct link. Example: *"(MO) 2020 Mercedes-Benz Metris Cargo — $6,700 · 4d 7h · MO · 66 bids."*
- Editorial "Unusual Government Auction" spotlight module (2014 Diamondbank Airboat & Trailer, $42,500, IL) — a hand-curated/algorithmic "weird find" slot, likely for social sharing.
- Pro upsell strip: **"GovAuctions Pro — unlimited email alerts"** → `/subscribe`.
- Category quick-links: vehicles, real estate, electronics, military surplus, tools, equipment, jewelry, collectibles, furniture, medical, seized property, misc.
- Footer: Categories / Guides / Browse (regional+specialty) / Resources (tools, sold-prices, legal) / Social (Instagram, TikTok, YouTube). Legal disclaimer: *"We are not affiliated with GSA, HUD, or any government agency."*

### 4.2 Individual listing detail — US (`/auction/2013-ford-f-250-sd-ohio-govdeals-14436-4788`)

Fields shown:
- Title, current bid ($0 shown — GovDeals-style ascending auction), bid count (55), time remaining + hard end timestamp, full address (city/state/zip), seller name (**"Ohio State University Surplus Department, OH"** — he resolves and surfaces the actual seller entity, not just "GovDeals"), category, condition (`As-is`), source platform badge.
- **"GovAuctions Summary"** — a short LLM-style rewrite of the listing ("A 2013 Ford F-250 SD crew cab with a 6.2L V8 that idles well, but the rear cab and frame are completely missing...") sitting above/alongside the original source description. This is a value-add layer, not a scrape passthrough.
- Single product photo (gallery depth not confirmed — may only be one image on this particular lot).
- **"Similar Auctions"** module — 6 comparable listings.
- **Free market-demand line**: *"Vehicles like this sell about 70% of the time, typically ~5 bids. Based on 210 completed sales over the last year."* — i.e., win-rate + typical-bid-count stats are given away free, not gated.
- **Pro-gated line, inline, not blurred**: *"Estimated flip margin and comp range available with Pro"* — this is the money moment: free users get qualitative market context, Pro users get the number.
- No Flip Score visible on this particular page (Flip Score is scoped to GSA-only per the API docs — this was a GovDeals lot, so the absence is consistent, not a bug).
- Primary CTA: **"Bid Now on GovDeals →"** — external link straight to the source platform (confirms: no scraped bidding, no intermediation).
- Secondary CTAs: "Remind me before it ends" (time-based reminder), "Save/Share," **"Create Free Alert"** — no login wall stated for alert creation on this page (though `/alerts` management itself is behind auth per robots.txt).
- Practical logistics note baked into the page: "Pickup requires 10 business days; buyer arranges transportation" — operational detail pulled from source metadata.

### 4.3 Category browse (`/auctions/vehicles`)

- SEO H1: **"Government Vehicle Auctions: Cheap Surplus Cars, Trucks & SUVs."**
- Stat line: **"7,400 active vehicle auctions across 10 official government sources, updated daily,"** timestamped.
- Grid of 12 cards, each with: discount badge (e.g. "64% off" — implies he's computing against an estimated retail comp even on the free browse grid), photo, year/make/model, bid price, location, bid count. Card title links to the internal `/auction/{slug}` detail page (not directly out to GovDeals) — confirms the internal detail page is the click-through target from browse grids, monetizing the page view before sending the user onward.
- Below the grid: state-by-state sub-browse links (e.g. "Florida — 732 auctions") and cross-links to other category pages.
- Primary CTA button: "Search all 7,400 Vehicles auctions near you — Filter by price, distance, and when the auction ends" → presumably routes into `/feed` with `category=vehicles` pre-applied (the richer filter UI lives on `/feed`, not on the static category page — see §4.5).

### 4.4 Platform hub (`/platforms/govdeals`)

- H1: **"GovDeals Auctions: Live Listings Nationwide (2026)."**
- Stat: **23,369 active GovDeals listings**, updated 2026-07-31.
- Platform-explainer copy: owner (Liquidity Services, NASDAQ: LQDT), seller types (municipalities/counties/school districts/transit agencies), fee structure (7.5–12.5% tiered buyer's premium), first-time-buyer friction ($1,000 cap during a 30-day probation period) — this is competitive-intel-grade detail about the underlying platform, presented as buyer education.
- 12 example listing cards (range: $1,550 recreational land parcel up to $66,000 Bobcat track loader).
- Category breakdown table for that platform (4,722 vehicles down to 12 seized-property).
- Geographic breakdown (all 50 states; OH leads at 2,987, CA second at 2,359).
- CTA: "Search all 23,369 GovDeals listings" / "Browse All" — internal, feeding the `/feed`-style filtered view.

This hub-page pattern (×10 platforms) is effectively a set of platform-specific programmatic-SEO pages that double as his own "source scorecard," publicly.

### 4.5 Live feed / main search (`/feed`)

This is the actual filterable search surface (the static `/auctions` and `/auctions/{category}` pages are SEO landers that funnel here). Confirmed filter/sort controls:
- Location: zip code or current-location input.
- Bid-count filter: any bids / no bids / ≤3 bids (a "sniper for zero-bid lots" filter — directly relevant to our own zero-bid arbitrage hunting).
- Price range.
- Category (12-way, same taxonomy as everywhere else).
- Sort: best deals / newest / ending soonest / price low→high / price high→low.
- "⚙️ Filters" panel + "Create Alert" button (turns the current filter combination into a saved alert — this is presumably the Pro-gated "unlimited alerts" hook; the free tier likely caps alert count, though the exact free-tier cap wasn't visible on this pass — **open question**).
- `?platforms=hud` style query param confirmed via the HUD-homes page CTA (`/feed?platforms=hud`), implying `/feed` accepts a `platforms` (source) query param in addition to category/price/bid-count/sort.

### 4.6 Ending-soon (`/ending-soon`)

- H1: "Government auctions ending soon." Window = auctions closing within 48 hours, cross-category.
- Chronological sort by close time; category context inferred from item type, not an explicit filter UI on this page.
- CTA: "Get notified about new ending soon auctions" → "Create Free Alert."

### 4.7 HUD homes (`/hud-homes`)

- H1: "HUD Homes for Sale (2026): Search HUD HomeStore Listings."
- Stats: 935 properties nationwide, 46 states, TX highest (130), HI lowest (1).
- Card fields: photo, address, list price, category tag.
- Filters: state, price range, bedroom count.
- Primary CTA routes internally to `/feed?platforms=hud`; actual bidding happens on `hudhomestore.gov` via HUD-registered brokers — page is explicit that GovAuctions is not the transaction layer here, unlike a live-auction lot where "Bid Now" goes straight to the source.

### 4.8 Price-tier lander (`/auctions/under-500`)

- H1: "Government Auctions Under $500: Cheap Surplus Deals."
- Stat: 24,314 active listings under $500 (2026-07-31).
- Same card shape as category pages. Minimal Pro upsell — just a footer link, no aggressive above-the-fold conversion push. Reads as pure top-of-funnel SEO content, not a monetization surface.

### 4.9 Near-me geo lander (`/government-auctions-near-me`)

- H1: "Government Auctions Near Me: Find Local Surplus & Seized Property."
- ZIP-code input (not browser-geolocation-gated) → "Show auctions."
- Static fallback (no ZIP entered) shows: 12 example listings, 40,188 total active auctions nationwide (note: this figure differs from the homepage's 54,050 at the same timestamp — likely this page's count is scoped to a subset of platforms/categories, or the two counts were captured moments apart during a live-updating count; either way, his own stat tiles aren't perfectly consistent site-wide, a possible weak point), state-by-state list (VT lowest at 9, CA highest at 4,374), and a category median-price table off 85,655 completed sales (vehicles $2,025 median, electronics $78, furniture $14).
- Four sibling variants exist: `/police-auctions-near-me`, `/car-auctions-near-me`, `/real-estate-auctions-near-me`, `/equipment-auctions-near-me` — same template, category pre-filtered.

### 4.10 Sold-prices (`/sold-prices`)

- Title tag: *"Government Auction Sold Prices: What Surplus Actually Sells For | GovAuctions.app."*
- Tagline: *"What does surplus actually sell for? These pages show the median, typical range, and monthly price trend from real completed United States government auctions — by make, model, and item type."*
- No search box on the index itself; browses by category (Vehicles, Electronics, Furniture, Equipment, Tools, Jewelry, Medical) down to item/model rollups.
- Row format: `{item} {median price} ({n} auctions)` — e.g. **"Dell Latitude $119 (1,265)."** Aggregate index; individual drill-down pages per item/model are implied by the "by make, model, and item type" copy and by the item-level rows, but no such drill-down URL was independently confirmed this pass (not in the sitemap, not clicked through) — **flag for a follow-up pass if this template matters to us.**
- Free, no login wall observed. Footer Pro-plan link only.
- Category-level aggregate example: Vehicles median $2,401 (32,334 auctions); Electronics median $95 (18,856 auctions).

This page is the free, public shop-window for the same underlying dataset the `/comps` API endpoint serves programmatically — the aggregate/median numbers are free, but presumably the full P25/median/P75 comp range *for a specific listing* is the thing gated to Pro (matches the `/auction/{slug}` detail-page finding in §4.2).

### 4.11 Tools index (`/tools`) + the two calculators

`/tools` lists three tools, none gated:
1. **Flip Profit Calculator** (`/tools/flip-profit-calculator`) — inputs: winning bid, auction source (dropdown: GSA/Municibid/Public Surplus/GovDeals/GovPlanet/PropertyRoom/AllSurplus/custom — note **AllSurplus** appears here as a recognized source even though it's not one of his 10 sitemapped `/platforms/` hubs), buyer's premium % (auto-populated per source, editable), state (for tax), sales tax %, estimated resale price, resale platform (eBay/Amazon/Facebook-local/custom), resale fee % (auto-populated, editable). Outputs: total landed cost, net profit, ROI. Explicitly **"No signup"** required. Upsells "automatic Flip Score calculations on live listings" as the Pro-tier version of this same math.
2. **"What's It Worth?"** (`/tools/whats-it-worth`) — single free-text "describe your item" input + category selector. Output: P25–P75 range + median + a confidence level tied to comp-sample size. Explicit disclaimer: *"a typical resale range at auction, not a guaranteed value or an appraisal."* Example seed terms shown: Ford F-150, Ford F-250, forklift, excavator, iPhone, iPad, drill, welder.
3. **Sold Prices by Item** — just links to `/sold-prices` (§4.10), listed as a "tool" for nav purposes.

### 4.12 Guides (`/guides`)

Free, ungated blog/content hub. ~24 articles across three sub-sections:
- **Start Here**: complete-guide-to-government-surplus-auctions, best-government-auction-sites (ranked by fees), best-government-surplus-aggregators (**his own competitor-comparison article** — direct BLACKWHOLE-relevant read), 10-things-before-bidding, best-categories-for-beginners, how-to-flip-government-surplus.
- **By category**: buy-a-government-surplus-vehicle, buy-a-surplus-military-vehicle, government-car-auctions-near-me, how-to-buy-hud-homes.
- **By platform (FAQ format, one per source)**: gsa-auctions-vs-govdeals-vs-public-surplus, govdeals-alternatives, gsa-auctions-faq, govdeals-faq, public-surplus-faq, govplanet-faq, propertyroom-faq, hud-homestore-faq, state-surplus-auctions-faq.
- **More**: auction-aggregator-landscape, government-auction-listing-counts-explained ("why listing counts mislead" — a swipe at competitors who inflate counts), the-strange-world-of-government-auctions, why-i-built-govauctions (founder story), financing-a-foreclosure-flip, working-capital-for-resellers.

**Founder-story article (`why-i-built-govauctions`) key points:**
- Ben Wallace, decade-long government-auction hobbyist, launched the site publicly April 2026 (started building "early 2026" per the About page).
- Stated motivation: cross-platform pricing is "wildly inefficient," ~40% of lots get **zero bids**.
- Flip Score pitched explicitly as **"a Kelley Blue Book for government surplus,"** built on 100,000+ archived completed sales.
- Hardest technical problems named: search quality (dedup + damage detection across sources) and reliable cross-category pricing (vehicles through decommissioned hospitals).
- No traffic/revenue/tech-stack numbers disclosed in this article.
- The site has its own `/guides/{slug}` article specifically titled **"GovAuctions vs GovAuctions.com"** (a different, similarly-named site) — worth a separate look if we ever want his framing of the competitive landscape, not pulled in this pass.

### 4.13 About (`/about`)

- Positioning line: **"a free, independent search layer over the official U.S. government surplus auction platforms."**
- Explicit non-intermediation pitch: *"we don't take cuts, charge for access, or sit between users and sellers — all listings link directly to official government sources."*
- Coverage claim: **"eight major official U.S. government auction platforms"** (named: GSA Auctions, GovDeals, Public Surplus, HUD, Fannie Mae HomePath, Purple Wave, GovPlanet — that's 7 named, "eight" implied one more, presumably Municibid or Bid4Assets per the platform-hub list in §2a) plus **four geographic markets: US, UK, Canada, Australia.**
- Claims real-time bid-price updates for major platforms ("rather than day-old snapshots") vs daily inventory refresh across all 50 states.
- Methodology note: Flip Score / deal-scoring derived from historical winning-bid data, with its own dedicated explainer page (not visited this pass — url likely `/guides/how-flip-score-works` or similar, unconfirmed).
- Pricing: **Pro $7/month** — recommended max bid, resale margin estimates, unlimited alerts. Matches known facts.

### 4.14 Pricing / `/subscribe`

`WebFetch`'s markdown conversion returned only nav chrome for this URL (title tag resolved to "Subscribe to Pro | GovAuctions.app," but body content wasn't in the extracted markdown). A raw `curl` pull of the HTML confirmed the page is a heavily-streamed Next.js RSC payload — the actual plan copy is client-rendered/streamed in a way that didn't grep out as plain text (one fragment decoded to `"This page could not be found"`, suggesting either an intercepting-route/modal pattern where `/subscribe` is meant to be reached via client navigation rather than a hard URL load, or a client-side redirect for unauthenticated direct hits). **Did not attempt further extraction** (would mean rendering JS, outside this pass's scope) — the $7/mo price point and feature list (recommended max bid, resale margin estimates, unlimited alerts) are already corroborated from the About page and the auction-detail-page upsell copy, so the plan substance is triangulated even though the dedicated pricing page itself resisted a clean static fetch. **Flag for R-series follow-up if anyone needs the literal pricing-page copy/layout** — would need a JS-rendering fetch (e.g., claude-in-chrome) rather than `WebFetch`.

### 4.15 Auth-gated / disallowed templates (not fetched — per robots.txt + task rules)

`/saved`, `/alerts`, `/settings`, `/auth/*` — all `Disallow`ed in robots.txt and reasonably assumed to require login. Not fetched, per polite-crawling instructions. Their existence + purpose is inferable from CTAs seen elsewhere: `/saved` = bookmarked listings ("Save/Share" button on detail pages), `/alerts` = manage the saved-search alerts created via "Create Free Alert" buttons scattered across `/feed`, `/ending-soon`, and detail pages, `/settings` = account/billing.

### 4.16 Public API spec (`/api/v1/openapi.json`)

Confirms the known facts exactly, with the added value of exact query-parameter names:

```
info.title: "GovAuctions Data API"
info.version: "1.0.0"
info.description: "Structured government-surplus auction data. Live listings (facts only,
  no images) cover sanctioned federal sources (GSA Auctions). Sold-price comps and the
  proprietary GovAuctions Flip Score resale signal are derived aggregates computed across
  the full US surplus market. Plans: Free (free); Hobby ($49/mo); Enterprise (contact us)."
```

| Endpoint | Method | Params |
|---|---|---|
| `/listings` | GET | `country` (enum US, default US), `state` (2-letter), `category`, `source` (enum `gsa`, default `gsa`), `q`, `priceMin`, `priceMax`, `active` (bool, default true), `limit` (int, max 100, default 25), `offset` (int, default 0) |
| `/listings/{id}` | GET | path `id` |
| `/comps` | GET | `q` (required), `category`, `country` (enum US, default US) — **403 if plan tier doesn't cover it** |
| `/flip-score` | GET | `id` (required, GSA listing id only) — **403 if plan tier doesn't cover it** |

---

## 5. Free vs. Pro — consolidated gating map

| Feature | Free | Pro ($7/mo) |
|---|---|---|
| Browse/search live listings (all sources, all pages) | ✅ | ✅ |
| Category/state/platform/near-me SEO landers | ✅ | ✅ |
| Individual listing detail page + "GovAuctions Summary" rewrite | ✅ | ✅ |
| Qualitative demand signal ("sells ~70% of the time, ~5 bids typical") | ✅ | ✅ |
| **Numeric estimated flip margin + comp price range on a specific listing** | ❌ | ✅ |
| **Recommended max bid** | ❌ | ✅ |
| Aggregate sold-price medians (`/sold-prices`, category/model rollups) | ✅ | — |
| Flip Profit Calculator (manual inputs) | ✅ | ✅ (auto-fills from live listing) |
| "What's It Worth?" estimator | ✅ | ✅ |
| Alerts | ✅ (create, at least one) | ✅ **unlimited** |
| Save/watch listings | ✅ (assumed, gated only by login not payment) | ✅ |
| API `/listings`, `/listings/{id}` (GSA only) | ✅ 100 calls/mo | ✅ Hobby 5k calls/mo |
| API `/comps`, `/flip-score` | ❌ (403) | plan-dependent (unclear if Free tier gets any comps calls, or if this is Hobby+/Enterprise only — **open question**) |

---

## 6. Other structural notes relevant to building our own aggregator

- **Internal detail pages, external bid CTA.** Every card click lands on his own `/auction/{slug}` page first (SEO + Pro-upsell impression), and only the "Bid Now" button leaves the site. This is a deliberate two-hop funnel — worth mirroring if BLACKWHOLE ever builds a public multi-source browse surface.
- **"GovAuctions Summary"** — an LLM rewrite layered over every scraped listing, not just a pass-through of the source description. Signals he runs an LLM (or LLM-adjacent heuristic) per listing at ingest time, which is compute cost per-lot he's absorbing to differentiate from raw source data.
- **Discount badges on browse-grid cards** ("64% off") imply he computes an estimated-retail comp for *every* listing shown on free grids, not just detail pages — the Pro gate is specifically on the *numeric flip-margin/comp-range for that one item*, not on having any pricing intelligence at all.
- **10 platform hub pages, but the calculator dropdown lists an 11th ("AllSurplus") and the About page says "eight major" sources** while the homepage stat tile says **32 sources** — his public source count is inconsistently stated across templates (8 vs 10 vs 11 vs 32). Read literally: 32 is likely counting individual state/agency sub-portals or the UK/CA/AU expansion sources bundled in, while 8–11 is the "major platform" marketing number. Don't take any single number as gospel when benchmarking our own source count against his.
- **UK expansion via Eddisons is live and indexed** (630 listings, one per URL) — this is outside our "US sources" scorecard entirely and suggests he's testing international expansion by white-labeling a single non-government commercial auctioneer feed under the `/uk/` path, distinct from his government-auction core premise. Worth watching if BLACKWHOLE ever considers non-US or non-government inventory.
- **Bot honeypot present and disallowed** — confirms he actively monitors for non-compliant scraping; any BLACKWHOLE-side aggregation work against his site (not that this is planned) would need to respect robots.txt strictly.
- **Stat-tile inconsistency** (54,050 vs 40,188 "active auctions" shown on two pages fetched near-simultaneously) suggests his aggregate counts are computed per-template rather than from one shared cached number — a minor data-integrity tell, not a competitive weakness per se, but useful if we ever want to publicly out-claim him on "our numbers are internally consistent."

---

## 7. Pages not independently confirmed this pass (candidates for a follow-up R-ticket)

- Per-item `/sold-prices` drill-down URL pattern (e.g. does clicking "Dell Latitude $119 (1,265)" go to a stable indexable URL, or is it a client-side expand?).
- Exact free-tier alert cap (copy says "unlimited" is the Pro perk, implying free is capped, but the cap number wasn't visible).
- Whether `/comps` and `/flip-score` API access exists at all on the Free tier (0 calls, or same-as-listings behavior with a smaller cap) vs. Hobby-only.
- Literal rendered content of `/subscribe` (resisted static fetch — needs JS rendering).
- The `/guides/how-many-government-auctions-are-there-listing-counts-explained` and `GovAuctions vs GovAuctions.com` articles — likely contain his own explicit competitive framing, not read this pass.
- Canada/Australia public route patterns (`/ca/`, `/au/`) — claimed in About-page copy but no live sitemap URLs found under those prefixes yet.
