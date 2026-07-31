# BLACKWHOLE-28 — Liquidation Aggregator Platform: Operator Review Spec

**Draft date:** 2026-07-31 · **Inputs:** R1–R8 (all 8 reports delivered, no gaps) · **Read time:** ~15 min
**Status:** needs Abdel's sign-off on §8 before any RED/AMBER source ships.

---

## 1. Executive summary

### The headline

**Ben Wallace's moat is 86 days of polling. Not a dataset, not a deal, not an API we can't reach.**

[R3](R3-comps-mechanics.md) settled the one question that decides whether this epic is worth doing: *how does he get closing prices?* Answer — **exactly the way our Phase 0 recorder does**, and he says so himself in his own data dictionary.

Three independent proofs:

| Proof | Evidence |
|---|---|
| His comps corpus starts **2026-05-06** — one month *after* his 2026-04-06 [Show HN launch](https://news.ycombinator.com/item?id=47662945) | His own [/tools/price-index](https://govauctions.app/tools/price-index): *"93,021 completed lots (May 6, 2026 – Jul 31, 2026)"* |
| His [public CC-BY dataset](https://github.com/benswork-space/us-government-surplus-dataset) was downloaded and histogrammed | 5,971 rows, `ended_at` spans **2026-05-06 → 2026-06-29**, zero rows earlier |
| His data dictionary confesses the mechanic | `current_or_final_bid` = *"the current or **last-observed bid**, not a confirmed hammer price"*; `sold` carries an **`unknown`** enum *"when undeterminable"* |

A purchased or exported dataset never has undeterminable outcomes. A poll-and-snapshot recorder always does. **There is no hidden source.**

Two consequences that change the plan:

1. **He is 86 days ahead of a standing start, not three years.** Every day we don't run the recorder is a day of permanently unrecoverable closes.
2. **We can get ahead of him on history.** Municibid (`&StatusFilter=completed_only`), Purple Wave (`filters=sold:Yes&dateType=past`), HiBid past-auction archives and PropertyRoom closed lots all publish **retroactive** sold archives going back years. He appears never to have backfilled any of them. A one-time backfill buys history he cannot buy back.

### What he actually built

A **solo-built Next.js/Vercel site** launched April 2026 that is three products stacked:

- **Free layer** — search across 31–32 sources / 4 countries (US, UK, AU, CA), ~54,050 live listings, ~1,733 sitemapped SEO pages + tens of thousands of crawl-discovered ones, 24+ guide articles, two free calculators, and public aggregate sold-price medians.
- **Pro layer, $7/mo** — the *number*: comp range on a specific lot, recommended max bid, estimated flip margin, unlimited alerts. Free users get the qualitative version (*"vehicles like this sell about 70% of the time, typically ~5 bids"*); Pro gets the dollar figure.
- **API layer** — Free 100 calls/mo, **Hobby $49/mo / 5k calls**, Enterprise. `/listings` is **enum-locked to `source=gsa`** because GSA is federal public domain (17 U.S.C. §105). Everything scraped from commercial platforms only ever leaves as a **derived aggregate** (P25/median/P75). That is a deliberate legal firewall, and it is the single most instructive thing about his design.

### His two exploitable weaknesses

1. **No `quantity` field anywhere on the lot entity.** ([R2 §9](R2-data-model.md)) A 500-chair lot and a 1-chair lot are the same row shape. His methodology *claims* per-unit normalization inside comp matching, but it's never persisted or exposed. **For a bulk-lot reseller that is the whole game.**
2. **His comps engine returns garbage on our vertical.** ([R3 bonus](R3-comps-mechanics.md)) Probing his open MCP endpoint: `q="banquet chairs"` → auto-categorized as **electronics**, count 60, p25/median/p75 all **$6**, self-reported `confidence: "high"`. `q="stacking chairs"` returns the byte-identical answer. His `office-chair` model shows median **$5** across n=396 — he is averaging single-chair junk against 900-chair pallets. He has no `banquet-chair` / `stacking-chair` archetype at all.

**He has breadth. He does not have depth where it pays.**

### The uncomfortable part

[R6](R6-legal.md) ran the ToS on 12 sources. **Only GSA Auctions is GREEN.** Eight are RED, three AMBER. **Four of our six Phase 0 sources (GovDeals, Public Surplus, Municibid, Purple Wave) are RED**; MiBid is AMBER; only GSA is clean. GovPlanet's terms name *"used equipment pricing tools, dashboards or other visualization products"* almost verbatim as the prohibited use.

This does **not** stop Deliverable A. It does force one decision up front: **the line between recording privately and publishing publicly** (§8, Decision 1).

---

## 2. Feature + data-model summary

### 2.1 Page/feature inventory (from [R1](R1-feature-inventory.md))

| Surface | What it does | Gating |
|---|---|---|
| `/feed` | The real filterable search. Zip radius, price range, category, **bid-count filter (any / none / ≤3)** — a zero-bid sniper filter, directly relevant to us. Sort: best deals / newest / ending soonest / price | Free |
| `/auction/{slug}` | Internal detail page. Title, bid, bid count, hard end time, full address, **resolved seller entity** ("Ohio State University Surplus Department"), condition, an **LLM-written "GovAuctions Summary"**, similar-auctions rail, free sell-through line, Pro-gated margin line, **"Bid Now on {source} →"** external CTA | Free page, Pro section inside |
| `/sold-prices` + `/sold-prices/{item}` + `/sold-prices/{item}/{state}` | The comps shop-window. Median / P25–P75 / n / monthly trend / recent examples / by-state fan-out | Free (aggregates) |
| `/platforms/{source}` ×10 and `/platforms/{source}/{state}` | Per-source hub with live count, fee structure, category + geographic breakdown. Doubles as his public source scorecard | Free |
| `/tools/flip-profit-calculator` | Bid → buyer premium (auto per source) → tax → resale price → resale fee → net profit + ROI. **No signup** | Free |
| `/tools/whats-it-worth` | Free-text item → P25–P75 + median + confidence tied to sample size | Free |
| `/guides/*` (24+), `/research/*` | Content + data-journalism link bait, including his own competitor-comparison article | Free |
| `/alerts`, `/saved`, `/settings` | Auth-gated; robots-disallowed | Login |
| `/api/v1/openapi.json` | 4 endpoints: `/listings`, `/listings/{id}`, `/comps`, `/flip-score`. **Zero response schemas declared** — it's a marketing artifact | Public spec |

**The two-hop funnel is worth copying:** every card click lands on *his* page first (SEO impression + Pro upsell), and only the "Bid Now" button leaves the site.

**Also worth copying:** `/auction/_honeypot` — a `rel="nofollow"`, visually-hidden, robots-disallowed link in the header of every page. It's a scraper tripwire. **Never fetch that path from any BLACKWHOLE tooling**, and put one on our own public surface.

### 2.2 His data model (from [R2](R2-data-model.md), read off the Next.js RSC payload)

One wide `auction` row, ~30 columns, source-native ids preserved, **no surrogate integer key**:

```
id = "{sourcePlatform}-{source-native id parts}"     e.g. govdeals-1537-88, gsa-3-1-QSC-I-26-488-008
```

Core fields: `sourcePlatform, sourceUrl, sourceAuctionId, title, description, category (normalized to 11 slugs), condition (free text), currentBid, startingBid, bidCount, buyerPremiumPct, auctionStart, auctionEnd, images[], thumbnailUrl, sellerName, sellerType, locationCity/State/Zip, latitude, longitude, pickupDeadlineDays, pickupNotes, isActive, country, currency`.

Computed siblings: `compValue`, `compFallback {count, median, p25, p75, confidence}`, `damageSeverity`, `flipScore`, plus page-level `priceHistory [{t, bid}]`, `sellThrough`, `sameLocationOthers`.

**Free-vs-Pro is expressed as `null` in the same object shape** (`compRange: null`, `flipSummary: null`, `marketStats: null`). Clean pattern — copy it.

**Flip Score, reverse-engineered and numerically verified:**

```
flipScore = 100 / (1 + (bid / comp_median)²)
```

- Audi A6: bid 6,500 / comp 3,100 → 18.53 → site shows **19** ✅
- RAM 1500: bid 6,050 / comp 8,125 → 64.33 → site shows **64** ✅

Score = 50 exactly at the comp median, monotone decreasing, bounded (0,100), fully deterministic. Built from ≤25 closest completed sales, recency-weighted over ~90 days (≈ the entire life of his data). Damage descriptions cap it. Shown free only when ≥80.

**His documented comp-matching rules:** same category + significant title-word overlap (filler/stock numbers stripped); vehicles constrained to ±4 model years + same make; **≥5 solid agreeing matches required or no comp anchor**; price basis = final winning bid **excluding** buyer's premium and taxes; no-bid lots excluded; month needs ≥5 sales to chart; full catalog rebuilt **daily ~06:00 UTC**.

**His observable bugs** (each one is a differentiator for us): `buyerPremiumPct: 0` on a GovDeals lot he elsewhere says carries 7.5–12.5%; three timezone conventions in one column (naive / `Z` / `-04:00`); GSA `description` parse broken (`"$27"`); `priceHistory` is ~1 point/**day**, so the last hour of an auction — where anti-snipe extensions and the real price live — is invisible; no relist lineage; `condition` is free text so it can't be filtered numerically.

### 2.3 What our schema must add

[R2 §10](R2-data-model.md) specifies a strict superset. The four things he structurally cannot answer:

| Column | Why |
|---|---|
| `quantity` + `quantity_confidence` (`llm/regex/dom/manual`) | **The whole business.** Our `auction_extractors` LLM pipeline already parses this |
| `unit_price` (generated: `current_bid / NULLIF(quantity,0)`) and `landed_unit_cost` | The only number that makes a 900-chair lot comparable to a 6-chair lot |
| `weight_lb`, `dim_in`, `freight_class`, `pallet_count` | Real SAIA freight quotes; his model uses a category constant |
| `sold_comps.unit_final_price` **as a persisted column, not a matching-time derivation** | Makes "what does a banquet chair go for in GA in bulk" a one-query answer and lets us publish a per-unit price index he cannot |

Plus: `relist_of_lot_id`, `dedupe_group_id`, `same_location_lot_count` (his `sameLocationOthers`, but used to **amortize freight across lots**), `outcome` + `outcome_confidence`, and `score_version` + `scored_at` on every computed number so a formula change doesn't silently rewrite history (he publishes his formula but not a version).

**Non-negotiable invariants:** natural key `(country, source_platform, source_lot_id)`; store the source's own id **and** its canonical URL (GSA proves they differ); all timestamps `TIMESTAMPTZ` normalized to UTC on ingest with `source_tz` retained; never overwrite an outcome on re-sweep; label every price with its basis (comps = final bid ex-premium, scoring = landed cost incl. premium); images per-source strategy column (`hotlink | proxy | rehost`) with **R2 as the rehost target — never a Supabase Storage URL** (402'd).

---

## 3. Source rollout plan

### 3.1 The legal gate — read this first

[R6](R6-legal.md) verdicts across 12 sources:

| Verdict | Sources | Count |
|---|---|---|
| 🟢 **GREEN** | GSA Auctions (official free [Auctions API](https://gsa.github.io/auctions_api/), bound by the OPEN Government Data Act) | 1 |
| 🟡 **AMBER** (silent ≠ permission) | MiBid, iBid Illinois, PropertyRoom | 3 |
| 🔴 **RED** (explicit prohibition) | GovDeals, Public Surplus, Municibid, GovPlanet, Purple Wave, HiBid, Bid4Assets, BidSpotter | 8 |

Representative clauses:

- **GovPlanet / RB Global** §2(d) — bars incorporating auction data into *"used equipment pricing tools, dashboards or other visualization products."* This is a near-verbatim description of Deliverable B.
- **HiBid** §9 — bans scraping, bans automated access, **and** bans *"bypass[ing] any measures we may use to prevent or restrict access, including our robot exclusion headers"* (i.e. "robots.txt didn't say no" is explicitly not a defense).
- **BidSpotter** §4.2 — bans *"creat[ing] a database in electronic or structured manual form by systematically… downloading, caching, printing and/or storing the material"*, plus §4.1 restricts all use to **"personal, non-commercial use only."**
- **Municibid** — bans *"accessing… through automated means"* and separately *"scraping, reproducing, republishing, selling, reselling."*
- **Public Surplus** — bans *"any robot, spider, other automatic device, or manual process to monitor or copy our web pages."*
- **GovDeals + Bid4Assets** (both Liquidity Services) — ToS text **unretrievable**: Akamai Bot Manager + client-only Angular SPA returns 403 to every non-browser fetch, and Wayback has zero snapshots of any `*terms*` path. RED is inferred from confirmed site-wide bot-blocking + no sanctioned channel + Ben's own refusal to treat GovDeals as sanctioned. **A 5-minute manual read in our existing Patchright session would convert this from inferred to evidence-based.**

**The pattern Ben himself follows** — and the one this spec recommends adopting — is: *record everything privately, redistribute only GSA raw, publish everything else only as derived aggregates.* His `/listings` endpoint is enum-locked to `gsa`; his comps endpoint serves P25/median/P75 computed across the full market. That split is the product design, not a limitation.

### 3.2 Ranked rollout

**Tier 0 — build now, no sign-off needed**

| Source | Access | Legal | Why first |
|---|---|---|---|
| **GSA Auctions** | Official free [API](https://gsa.github.io/auctions_api/), JSON + XML | 🟢 GREEN | The only source we can redistribute raw. Ships the public API + open dataset play on day one. Ben's whole `/listings` product is this one source |

**Tier 1 — Phase 0 recorder sources, pending Decision 1**

| Source | Access (verified) | Legal | Volume share (Ben's own index) | Notes |
|---|---|---|---|---|
| **GovDeals** | maestro JSON, self-healing key — `deals/adapters/govdeals.py` **already shipped** | 🔴 RED (inferred) | **60.1%** | Also has a per-lot detail endpoint (`POST /assets/{a}/{acct}/false` + `GET /bids/bidbox/...`) that gives **exact final bid state** — a genuine quality edge over Ben's "last-observed bid" |
| **Public Surplus** | plain HTTP; `public_surplus_automation.py` shipped but **unwired from `deals/`** | 🔴 RED | 11.2% | ~80-line adapter away |
| **Purple Wave** | `/v1/search/search` JSON (unauthenticated but robots-disallowed) | 🔴 RED | 1.6% | **Publishes retroactive sold archive** (`filters=sold:Yes&dateType=past`) — backfill target |
| **Municibid** | server JSON, `&StatusFilter=completed_only` | 🔴 RED | 1.7% | **Publishes retroactive sold archive** — backfill target |
| **MiBid** | plain | 🟡 AMBER | 0.2% | robots.txt affirmatively allows generic-UA *"reference"* use but name-blocks every AI crawler. **State agency — just email `DTMBSurplus@michigan.gov` and ask.** Cheapest sign-off available |

**Tier 2 — expansion, each needs its own gate**

| Source | Access | Legal | Notes |
|---|---|---|---|
| **iBid Illinois** | plain Drupal, permissive robots | 🟡 AMBER | Genuinely silent in both robots and ToS. Ask Illinois CMS |
| **PropertyRoom** | permissive robots (file dated 2022) | 🟡 AMBER | Silent, but ToS lives as ~10 fragmented Freshdesk articles — a clause could exist that we didn't surface. Publishes closed lots (backfill target) |
| **HiBid** | GraphQL + Cloudflare | 🔴 RED | **Ben doesn't index HiBid at all** — a real differentiation angle with meaningful small-municipality furniture volume. Also the most aggressively-drafted ToS of all 12 |
| **GovPlanet** | AWS-WAF | 🔴 RED | The single most on-point prohibition found |
| **Bid4Assets** | Akamai-blocked, no robots.txt retrievable | 🔴 RED | Real estate — near-zero furniture value anyway |
| **BidSpotter** | plain, 6 sitemaps; `bidspotter_automation.py` shipped but unwired | 🔴 RED | Non-commercial-use clause is independently disqualifying |

**Tier 3 — Ben has them, we don't (~12.7% of his index)**

`civilview` (7.9%), `jjkane` (3.1%), `apple` / Apple Auctioneering (1.7%), `cws` / CWS Marketing (0%). **[R4](R4-sources.md) verdict: skip all four for our vertical.** CivilView is sheriff-sale real estate only; J.J. Kane is fleet vehicles only; Apple Auctioneering runs on HiBid anyway (no new integration); CWS is a thin general-merchandise catch-all behind a WAF. Chasing these is **breadth theater** — it buys index-count bragging rights and zero chairs.

**Tier 4 — do not build**

HUD HomeStore and GSA Real Estate both serve **`Disallow: /`** (whole-site crawl ban). Fannie Mae HomePath, Freddie Mac HomeSteps, Witham/mod-sales, Pickles = real-estate or vehicles only. All zero-value for a goods comps dataset.

**Tier 5 — international, defer entirely.** [R4](R4-sources.md) found genuine office-furniture volume at **Grays (AU)**, **Allbids (AU)** and **NCM Auctions (UK)**, and spotted that NCM + Ramco share an **Auction Technology Group / Metropress** backend (one integration could unlock several UK houses). Interesting, but there is no reason to internationalize before US v1 proves out.

### 3.3 The backfill move — highest-leverage single action

Ben's comps start 2026-05-06 and he **never backfilled the platforms that publish retroactive sold history**. A one-time sweep of:

- **Municibid** `&StatusFilter=completed_only`
- **Purple Wave** `filters=sold:Yes&dateType=past`
- **HiBid** past-auction archives
- **PropertyRoom** closed lots

...would give us sold history **predating his entire dataset**. This is the only action in this spec that produces an asset he cannot replicate by working harder. It is also, note, on three RED sources and one AMBER — so it sits behind Decision 1.

---

## 4. SEO / traffic pattern to replicate

From [R5](R5-seo.md). The mechanic is copyable; the copy and design are not — we build our own.

### 4.1 The structural insight

His sitemap declares only **~1,733 URLs**, but his indexed surface is plausibly tens of thousands. He deliberately splits:

- **Sitemapped:** the low-cardinality, evergreen combinatorial grid — category × state, platform × state, price tiers, near-me landers. Small, stable, cheap to keep 100% indexed.
- **Not sitemapped, left to internal-link discovery:** everything high-cardinality or high-churn — individual `/auction/{id}` pages (they expire), `/sold-prices/{item}[/{state}]`, `/cities/{state}/{city}`, guides, research.

**Depth is capped at 2 path segments everywhere** (`/auctions/{category}/{state}`, `/platforms/{source}/{state}`, `/sold-prices/{item}/{state}`). No 3-segment combos exist. **Breadth over depth: many independent 2-segment families.**

**Empty cells are pruned.** ~390 category×state pages exist out of 510 possible — thin combos never get a URL.

### 4.2 Schema.org stack on every page

`Organization` (with `founder` + `sameAs` — an E-E-A-T signal) · `WebSite` + `SearchAction` (sitelinks searchbox) · `BreadcrumbList` · `FAQPage` on every hub page (cheap repeatable rich-result surface) · `ItemList` + `Product`/`Offer` per card on listing grids · **`Dataset` + `PropertyValue`** on every comps block, with `measurementTechnique: "Order statistics (25th percentile, median, 75th percentile) of final winning bids"` and an explicit `temporalCoverage`.

The `Dataset` markup on stats blocks is the unusual, smart one — it tells Google the median/P25/P75 block is structured statistical data, not prose.

He also `Allow`s **every** major AI crawler by name (GPTBot, ClaudeBot, PerplexityBot, Google-Extended, CCBot, Bytespider…). He wants answer-engines citing him.

### 4.3 Recommended BLACKWHOLE US v1 page set (ranked by leverage)

1. **`/sold-prices/{item-keyword}`** — highest leverage, and the one page family where our per-unit data beats his outright. Needs an item-keyword normalizer (title → canonical archetype) which we don't have yet.
2. **`/auction/{slug}-{state}-{source}-{lot-id}`** — individual **sold**-lot permalink. We already have `GET /deals/{asset_id}/{account_id}/{auction_id}` which reconstructs a listing from our store **after the source deletes it**. Ship `Product` + `Offer(availability: SoldOut)` + `BreadcrumbList` from day one.
3. **`/auctions/{category}/{state}`** — the evergreen grid off `deals/categories.py`'s canonical buckets × states, **pruning empty cells**.
4. **`/auctions/{state}` + `/auctions/{category}`** — the single-axis hub layer above #3.
5. **`/platforms/{source}` + `/platforms/{source}/{state}`** — cheap second grid over the same table once >1 adapter exists.
6. **`/sold-prices/{item-keyword}/{state}`** — second-order fan-out, only after #1's normalizer earns it.
7. **JSON-LD stack on every templated page** from day one — near-zero cost, compounding return.
8. **Defer to v2:** `/cities/{state}/{city}`, hand-written guides/research, country prefixes, price-tier pseudo-categories.

**Our own model dimension must seed the archetypes he's missing:** `banquet-chair`, `stacking-chair`, `folding-chair`, `chiavari-chair`, `round-banquet-table`, `cafeteria-bench` — each flagged `is_bulk=true` so it publishes **per-unit** medians by default. Seed the rest from his 162 (they're free evidence of what people actually search).

---

## 5. Competitive landscape + cost

### 5.1 The field is more crowded than assumed ([R8](R8-competitors-economics.md))

| Product | Sources | Comps | Pricing | Founder | Read |
|---|---|---|---|---|---|
| **[GovAuctions.app](https://govauctions.app)** | ~31–32 / 4 countries, 54,050 live | Yes — claims 93k–180k depending which page you read | Free; **Pro $7/mo**; API Free 100 / **Hobby $49 for 5k** / Ent. | Ben Wallace ([`player_piano`](https://news.ycombinator.com/user?id=player_piano)), solo, launched Apr 2026 | The bar |
| **[BidProwl](https://bidprowl.com)** | **27 sources, 110k+ live** | Yes — claims **271,000+ closed auctions back to 2018** | Free; **Pro $9/mo**; **Pro+ $29/mo for 10k API calls** | Sam Ojling, solo. [Show HN Apr 30 2026, 302 pts](https://news.ycombinator.com/item?id=47961378) | **Closest structural twin.** Undercuts Ben's API 2:1. Openly says he scrapes. Says the hard part was **dedup**, not scraping |
| **[BidRadar.app](https://bidradar.app)** | 6 sources | eBay-sold Flip Score (not gov comps) | $0 / $14.99 / $29.99 | Anonymous | Alerts-first, no API, 4× the price. Different segment |
| GovernmentAuctions.org | "100k+" directory | No | Membership/lead-gen | Cyweb Holdings | Legacy SEO directory, not a data product |
| Gov Radar, USGovBid, Apify GovDeals Scraper | 1 source / auction house / scraper-as-a-service | No | — | — | Not competitors |

**Reading:** two HN front-page aggregators launched **within four weeks of each other** (Apr 6 and Apr 30, 2026). BidProwl claims *more* sources and a *bigger* comps corpus than Ben. **The live-listings aggregation layer is now crowded and nobody has won.** Pricing has already compressed to $7–$9/mo.

**But:** BidProwl's "271k back to 2018" claim deserves skepticism given [R3](R3-comps-mechanics.md) established how hard retroactive gov-auction sold data is to obtain — it's either backfilled from the same retroactive archives Ben ignored (which would validate our §3.3 move) or it's marketing. Worth one hour of verification before treating it as fact.

**What nobody has:** none of the three integrate with a **resale channel**. They stop at "find the deal / know the comp." BLACKWHOLE's Listing Engine + CRM already go the rest of the way. That's a differentiation angle nobody in this category can copy quickly.

**Name risk:** `govauctions.com` exists as a **separate domain** from Ben's `.app`, with a Google Play app. Check it before we name anything adjacent.

### 5.2 Row growth — this is a small-data problem

29 sources × adaptive polling ≈ **50k–200k snapshot rows/day** at ~200–400 bytes/row (ids, price, bid_count, timestamp, status — text and images live in a separate table updated in place):

- Low end: 50k/day × 300B ≈ 15 MB/day ≈ **~5.5 GB/year**
- High end: 200k/day × 300B ≈ 60 MB/day ≈ **~22 GB/year**

One year of raw snapshots fits inside a $25/mo Postgres tier. **The cost driver is compute (pollers + classification), not storage.** ⚠️ Note: this math assumes a *slim* snapshot row. Adding `raw JSONB` per snapshot (§6, and the right call) multiplies it — budget for that explicitly rather than discovering it.

### 5.3 Monthly cost at three scales

| Line item | **Phase 0** (prototype, <10 sources, our own use) | **10-source aggregator** (early validation, small public traffic) | **GovAuctions-scale** (~29 sources, six-figure comps, real traffic) |
|---|---|---|---|
| Frontend/API hosting | Vercel Hobby — **$0** (non-commercial only; must move to Pro the day Stripe goes live) | Vercel Pro — **$20** | Vercel Pro — **$20–40** |
| Database | Supabase Free — **$0** (⚠️ 500 MB cap will be hit in ~1–3 months of snapshots) | Hetzner CX22 VPS + Postgres — **$5–6**, *or* Supabase Pro — **$25** | Supabase Pro (Small/Medium) — **$30–75**, *or* self-hosted VPS — **$12–24** |
| Poller/cron compute | **$0** (laptop / Render free) | **$0–7** (same VPS or Render cron) | **$7–25** (we already run 4 Render cron services for `deals/`) |
| LLM classification | **~$0–1** | **~$1–10** | **~$10–35** |
| Stripe | $0 (no subs) | 2.9–3.6% + $0.30/txn, no fixed fee | same, revenue-proportional |
| **Total fixed infra** | **$0/mo** | **~$26–43/mo** | **~$67–175/mo** |

**LLM cost is a rounding error** — classification runs **once per distinct listing**, not per snapshot. Gemini 2.5 Flash-Lite ≈ **$0.24 / 1,000 listings**; GPT-4o-mini ≈ **$0.17 / 1,000**; Groq free tier ≈ **$0** within rate limits (14,400 req/day ceiling — fine for new-listings volume, nowhere near enough for per-snapshot). ⚠️ Gemini 2.5 Flash (current `deals/classify.py` default) **deprecates Oct 2026** — migration is a near-term action item regardless of this epic.

**Cost-control lesson from Ben:** he **hotlinks source-CDN images** rather than re-hosting, which is exactly what keeps his bandwidth bill inside Vercel Pro's included tier. We already learned the inverse lesson the hard way (Supabase Storage 402'd on egress → migrated to R2). Per-source image strategy column, hotlink by default, rehost only where the CDN blocks it.

---

## 6. Reuse vs. build (from [R7](R7-reuse-map.md))

**Bottom line: `deals/` is already most of this system for one source. The gap is breadth, not depth.**

### EXISTS — use as-is

| Capability | Where |
|---|---|
| **`SiteAdapter` Protocol seam** (`discover`/`refetch`/`fetch_gallery`) | `deals/adapters/base.py` — **needs zero changes to add sources** |
| `GovDealsAdapter` reference implementation + maestro key self-healing | `deals/adapters/govdeals.py` → `auction_extractors/govdeals_chairs_extraction.py::_resolve_maestro_key` (reused, not duplicated) |
| `Snapshot` model + `deal_snapshots` + `append_snapshot()` + change-gating | `deals/models.py`, `deals/store.py`, `deals/watcher_logic.py::is_snapshot_change` |
| **Lane-based adaptive poll scheduler** (COLD 6h / WARM 1h / HOT ≤5s), source-agnostic | `deals/watcher_logic.py` + `deals/watch.py::poll_once` |
| One-shot closer for lots missed while the watcher was down | `deals/backfill.py::run_backfill` |
| Parameterized search/filter/sort builder | `automation/web/deals_query.py` → `GET /api/deals` |
| Saved lists / tags / **saved searches**, full CRUD | `deal_lists`, `deal_lot_tags`, `saved_searches` in `automation/web/app.py` |
| **Telegram alerting, 4-topic supergroup** (leads/deals/health/poller), 5 producers already wired | `automation/telegram_alerts.py` |
| Valuation: landed cost, bulk-recovery-tier discount, `MIN_COMPS=3` gate, confidence tiering | `deals/valuation.py`, `deals/fees.py` |
| **LLM never sets a price** — it extracts identity + queries, then judges which retrieved comps match | `deals/llm_steps.py` (retrieval-then-reasoning). Keep this discipline |
| **Sold-lot permalink that survives source deletion** | `GET /deals/{asset_id}/{account_id}/{auction_id}` — functionally exactly the comps permalink §4.3 #2 asks for |
| Public site + admin in one FastAPI process, `/robots.txt`, `/sitemap.xml`, catalog feed | `automation/web/app.py` |
| Provider-agnostic LLM layer as a **pattern** | `automation/llm/` (`Extractor` protocol, `default_extractors()` env picker, A/B logging) |

### EXISTS — needs refactor

- **`deal_snapshots` has no `raw JSONB`.** `deal_lots.raw` preserves the full source payload but is **overwritten on every upsert** — every prior payload is lost. A comps dataset wants the historical raw retained per observation. Small additive migration; real storage-budget conversation (§5.2).
- **PK `(asset_id, account_id, auction_id)` is GovDeals-native.** Needs generalizing to `(country, source, native_id)` — or per-source tables. **Undecided anywhere in the codebase. Decide before rows accumulate.**
- **`deals/` bypasses `automation/llm/`.** `classify.py` and `llm_steps.py` both `from google import genai` directly, hardcoded to `GEMINI_API_KEY` — no cross-provider fallback. `rank.py` is a *third* pattern (shells out to local `claude -p`).
- **Five independent alert-composition call sites**, no shared composer. Not broken; consolidate when a 4th/5th adapter makes it costly.
- ⚠️ **Correction to a common assumption: HTMX is not wired in.** `automation/web/app.py` is plain FastAPI + Jinja + hand-rolled fetch/JS. Still highly reusable — just don't plan around HTMX existing.

### MISSING — genuinely new build

1. **More `SiteAdapter` implementations.** `public_surplus_automation.py` and `bidspotter_automation.py` are **shipped, hardened and tested but unwired** — each is realistically an **~80-line** wrapper shaped exactly like `govdeals.py`. **This is the single highest-leverage gap in the whole audit.** The other 8 scorecard sources have zero code.
2. **Cross-source dedup — zero code exists.** `_dedup_listings()` is same-source only; `deals/relist.py`'s Jaccard title-similarity (`SIM_THRESHOLD = 0.6`) is same-seller only. Extending that pattern is the right move, not inventing a new algorithm. Note BidProwl's founder publicly said **dedup was the hard part**, not scraping.
3. **Source-agnostic raw-JSONB observation log** (see above).
4. **Item-keyword normalizer** (title → canonical archetype) — the prerequisite for `/sold-prices/{item}`.
5. **Public multi-source browse + a public `/comps?q=` endpoint.** Our comps math exists but only computes internally per-lot for buy-signal alerts; nothing exposes it.

---

## 7. Deliverable B and C architecture (concise)

### 7.1 Deliverable A (the thing we never cut) — recorder, already in flight

Append-only `listing_snapshots` with sacred raw JSONB, 6 sources, adaptive polling tightening to 5-min in the final hour, `sold_comps` as a derived view. Two additions this research forces:

- **Persist `end_utc` per snapshot** so anti-snipe extension events become first-class data. Ben cannot see a lot that got extended six times — that's a demand signal he structurally cannot sell.
- **Prefer confirmed final state where obtainable.** GovDeals' per-lot detail endpoint (`POST /assets/{a}/{acct}/false`) + `GET /bids/bidbox/GD/{a}/{acct}/{auction}` give exact contested-lot final state. Ben's entire dataset is "last-observed bid." This is a **quality advantage, not parity.**

### 7.2 Deliverable B — aggregator product

```
                 ┌─────────────────────────────────────────────┐
  sources ──────►│  SiteAdapter (deals/adapters/*)  [EXISTS]    │
  (6 → 10)       └────────────────┬────────────────────────────┘
                                  │ discover / refetch
                 ┌────────────────▼────────────────────────────┐
                 │  ingest: map → normalize → classify          │
                 │  quantity extraction (LLM) [EXISTS, reuse]   │
                 │  cross-source dedup → dedupe_group_id [NEW]  │
                 └────────────────┬────────────────────────────┘
                                  │
        ┌─────────────────────────┼──────────────────────────┐
        ▼                         ▼                          ▼
  lots (current)          lot_snapshots               sold_comps (view)
  mutable, upsert         append-only + raw JSONB     unit_final_price
  [EXISTS, extend]        [EXISTS, add raw]           PERSISTED [NEW]
        │                         │                          │
        └─────────────────────────┴──────────────────────────┘
                                  │
                 ┌────────────────▼────────────────────────────┐
                 │  comp_stats materialized views [NEW]         │
                 │  grain: (scope, period, category, model,     │
                 │  state, source) → n, p25/med/p75 AND         │
                 │  unit_p25/unit_med/unit_p75. Suppress n<5    │
                 └────────────────┬────────────────────────────┘
                                  │
        ┌───────────────┬─────────┴─────────┬──────────────────┐
        ▼               ▼                   ▼                  ▼
  scoring          search+alerts       public SEO         API tiers
  flip_score_v1    [EXISTS, extend]    [NEW pages,        GSA raw +
  + unit variants  saved_searches      EXISTS plumbing]   derived
  score_version    Telegram/email                         aggregates
  [EXISTS, version]
```

**Build order:** (1) generalize the key + add snapshot raw JSONB **before rows pile up**; (2) two adapters from existing scrapers (~80 lines each); (3) cross-source dedup; (4) item-keyword normalizer; (5) `comp_stats` views with per-unit columns; (6) `/sold-prices/{item}` + sold-lot permalink pages; (7) the evergreen grid; (8) API tiers.

**Scoring:** implement his exact `100/(1+(bid/median)²)` as `flip_score_v1` so we can say *"same grade, plus the per-unit one."* Every score row carries `score_version` + `scored_at`.

### 7.3 Deliverable C — self-onboarding, self-healing scraper agent

The premise: onboarding source #11 should cost an afternoon, not a week, and a source that changes its markup should heal itself or page us — not fail silently.

**Four components:**

1. **Access-ladder prober.** Given a domain, walk the ladder [R4](R4-sources.md) already formalizes: (1) official API → (2) hidden internal JSON/XHR → (3) RSS → (4) browser scrape. Fetch robots.txt, locate ToS, look for sitemaps, sniff for JSON endpoints behind the search UI, record anti-bot signature (plain-curl 403 vs. browser-OK vs. Cloudflare/Akamai). **Output: a source dossier + a proposed access tier + a RED/AMBER/GREEN draft verdict — never an auto-approved adapter.** Legal verdicts stay human-gated, always.
2. **Declarative source manifest.** One file per source: endpoints, pagination, auth/key-resolution strategy, field→`Lot` mapping, category map, sold-archive URL if any, robots/ToS verdict + who signed off + when. The adapter becomes mostly data.
3. **Adapter synthesis + contract tests as the gate.** LLM drafts the `mapping.py`-style function against **captured golden fixtures** (we already do this — `auction_extractors/tests/fixtures/bidspotter_*`). The generated adapter ships only if it passes: known-lot round-trip, price-never-silently-zero (our existing `mapping.py` fails loud on garbled price — keep that), timestamp normalization, quantity extraction on a labeled set.
4. **Health monitoring → self-heal → escalate.** Per-source daily metrics: row-count delta vs. trailing median, field null-rate spikes, parse-error rate, HTTP status mix. On drift: re-run the key/selector resolver (the **GovDeals maestro-key self-healing already in production** is the working precedent), re-probe the ladder, re-synthesize the mapping against fresh fixtures, run contract tests. Pass → deploy + log. Fail → **Telegram `health` topic** with the dossier diff. Never silently degrade.

**Sequencing:** C is worth building **after** 3–4 adapters are hand-written, not before. You cannot automate a pattern you've only instantiated once. Every hand-written adapter is training data for the synthesizer and a fixture for the contract tests.

---

## 8. DECISIONS ABDEL MUST MAKE

> Each is one question + a recommendation. Nothing in §3 Tier 1/2 ships until Decision 1 is answered.

**1. RED/AMBER source policy — where is the line between *recording* and *publishing*?**
→ **Recommend: record privately now, publish nothing per-listing except GSA.** Adopt Ben's exact firewall: GSA raw is redistributable (17 U.S.C. §105 + OPEN Government Data Act); every RED/AMBER source feeds our private buy-decision engine and leaves only as **derived statistical aggregates** (P25/median/P75, n≥5), never as a listing mirror, never with source images. This is a continuation of the single-lot sourcing we already run in production, not a new posture — but the moment a public per-listing page or a paid listings feed carries a RED source, the exposure changes shape. **This is the decision that gates everything else.**

**2. Buy Ben's Hobby API at $49/mo to cross-validate our comps while ours accumulates?**
→ **Recommend: NO.** Three reasons. (a) His [developer terms](https://govauctions.app/developers) state *"Redistribution or rebuilding a competing dataset is not permitted"* — using his output to calibrate a competing comps dataset is the named prohibited use, and doing it while publishing our own would be indefensible if noticed. (b) His comps are **demonstrably broken on our vertical**: banquet chairs → "electronics," p25=median=p75=$6, `confidence: "high"`. We'd be validating against noise. (c) His corpus is 86 days old — it isn't a gold standard, it's a peer. **Spend the $49 on the Hetzner box + the retroactive backfill instead.** *Revisit only if* we later need an external sanity check on a category where he's genuinely deep (vehicles, n=34,482) — then buy one month, benchmark, cancel.

**3. Run the retroactive sold-archive backfill (Municibid `completed_only`, Purple Wave `sold:Yes&dateType=past`, HiBid archives, PropertyRoom closed)?**
→ **Recommend: YES, and treat it as the single highest-priority action after the recorder is stable** — subject to Decision 1, since 3 of the 4 are RED and 1 AMBER. This is the only move that produces history Ben cannot obtain by working harder. Start with Purple Wave + Municibid (both already in our verified scorecard, both retroactive archives confirmed).

**4. Where does the aggregator database live?**
→ **Recommend: a separate Postgres, not the shared BLACKWHOLE Supabase project.** The shared project is already carrying inventory/contacts/messages, its Free tier caps at 500 MB, and this data is a *different product* with different growth and different legal handling. Start on a **$5–6/mo Hetzner CX22 running Postgres + the poller cron** (§5.3). Accept that this is a deliberate, documented exception to workspace `CLAUDE.md` §14's "one helper, one path" — write the exception into §14 rather than quietly breaking it.

**5. Generalize the key to `(country, source, native_id)` and add `raw JSONB` to snapshots — now, before rows accumulate?**
→ **Recommend: YES to both, immediately.** The current PK `(asset_id, account_id, auction_id)` is GovDeals-native and every day of recording makes migration more expensive. `raw JSONB` per snapshot is what turns the recorder from a price log into a re-derivable dataset — if our parser is wrong in month 3, raw payloads let us re-derive; without them the mistake is permanent. Budget the storage explicitly (raw payloads multiply the §5.2 math); mitigate with compression + a retention policy on raw beyond N months, not by skipping it.

**6. Is Deliverable B a public SaaS, or an internal edge we keep to ourselves?**
→ **Recommend: internal-only until A has ~90 days of data, then public.** Publishing early puts us in a crowded, price-compressed fight ($7 vs $9) with two funded-by-nothing solo builders who both have a head start — while our actual advantage (per-unit bulk comps + the resale channel we already own) only exists once the data does. Ninety days of recording costs ~$0 and buys the thing nobody can copy.

**7. Breadth (chase Ben's 4 missing sources) or depth (bulk/per-unit comps)?**
→ **Recommend: depth, unambiguously.** `civilview` + `jjkane` + `apple` + `cws` = 12.7% of his index and **near-zero chairs** — real estate, fleet vehicles, and a WAF'd catch-all. Meanwhile his `office-chair` archetype shows a $5 median off n=396 because he averages single-chair junk against pallets, and `banquet-chair` doesn't exist in his taxonomy at all. **Own the bulk-seating vertical with correct per-unit math and honest confidence, and let him keep the breadth trophy.**

**8. Build Deliverable C (self-onboarding agent) now, or after 3–4 hand-written adapters?**
→ **Recommend: after.** Wire Public Surplus and BidSpotter first (~80 lines each, scrapers already shipped and tested), then GSA (official API, GREEN). Those three become the training set and the contract-test fixtures for the synthesizer. Building C against a single instantiated pattern (GovDeals) would encode GovDeals' quirks as the abstraction. **Do build the health-monitoring half of C early though** — per-source row-count/null-rate drift alerts into the Telegram `health` topic are cheap and catch silent breakage that would otherwise poison the dataset for weeks.

**9. If/when B launches: pricing and positioning?**
→ **Recommend: don't compete on the $7–$9 consumer tier.** That segment is already compressed and neither incumbent has a moat. Position on **bulk/per-unit comps for resellers who move pallets**, price higher, and make the **API the product** — the number to beat is BidProwl's **$29/mo for 10k calls**, not Ben's $49/5k. Also: our Listing Engine + CRM close the loop from "find the deal" to "list and sell it," which neither competitor can copy quickly.

**10. Product name / domain — clear it before anything public.**
→ **Recommend: 30 minutes of trademark and domain checking before naming.** `govauctions.com` exists as a **separate business** from Ben's `govauctions.app` (with a Google Play app), so anything in the "GovAuctions X" family is doubly contested. Pick a name with no adjacency to either.

---

## 9. Risks

| # | Risk | Severity | Mitigation |
|---|---|---|---|
| 1 | **Legal — RED-source ToS.** 8 of 12 sources explicitly prohibit automated access and/or database creation; GovPlanet names *"pricing tools, dashboards"* verbatim; BidSpotter adds a blanket non-commercial-use clause | **High** | Decision 1's record-private/publish-aggregate firewall. Never mirror per-listing content from a RED source publicly. Get MiBid + iBid to explicit YES by email (state agencies, cheap ask). Consider a "prior written permission" approach to 1–2 RED sources if B ever goes public |
| 2 | **GovDeals + Bid4Assets ToS text never actually read** — Akamai blocked every retrieval path, verdict is inferred | Medium | **5-minute manual read in our existing Patchright session.** Do this before Decision 1 is finalized — it converts the inferred RED to evidence-based either way |
| 3 | **Anti-bot escalation.** Ben runs a honeypot (`/auction/_honeypot`) and fingerprints non-compliant crawlers; BidProwl's founder reported ~10 scrapers hitting *his* site in one week. Sources will harden as this category gets noisier | Medium | Respect robots.txt strictly on every source. Never fetch `/auction/_honeypot`. Adaptive-but-polite cadence. Put our own honeypot on any public surface we ship |
| 4 | **Crowded, price-compressed market.** Two HN front-page launches four weeks apart; BidProwl claims more sources and a bigger corpus at a lower API price | Medium | Don't fight on breadth or on the $7–9 tier (Decisions 7, 9). Verify BidProwl's "271k back to 2018" claim before treating it as real |
| 5 | **Data quality — silent breakage.** A source changes markup, the adapter keeps returning rows, the rows are wrong, and it poisons comps for weeks before anyone notices | **High** | Ship the health-monitoring half of Deliverable C early (Decision 8). Row-count delta + field null-rate + parse-error rate per source into the Telegram `health` topic. `raw JSONB` (Decision 5) makes bad parses recoverable |
| 6 | **Storage/cost surprise from `raw JSONB`.** §5.2's "small data problem" math assumes slim rows; raw payloads per snapshot break that assumption | Medium | Budget explicitly. Compress. Retention policy on raw beyond N months. Change-gating already means we only append on actual change |
| 7 | **Key migration debt.** The GovDeals-native PK gets harder to change with every recorded day | Medium | Decision 5 — do it now, while row count is small |
| 8 | **Supabase egress/quota repeat.** We already got 402'd once and had to migrate images to R2 | Medium | Per-source image strategy column, hotlink by default (Ben's own cost control), R2 as the only rehost target, **never** a new Supabase Storage URL |
| 9 | **Gemini 2.5 Flash deprecates Oct 2026** — `deals/classify.py`'s current default | Low | Migrate to 3.1 Flash-Lite or GPT-4o-mini. Better: route `deals/` through the existing `automation/llm/` provider-agnostic layer it currently bypasses ([R7 §5](R7-reuse-map.md)) |
| 10 | **Operator bandwidth.** Abdel is already running four systems under time and medical pressure; this epic has three deliverables | **High** | Deliverable A is the only one that's time-critical (every day is unrecoverable data). B and C are patient. Bias every plan toward session-sized work — no step that requires a remembered ritual |
| 11 | **Name/trademark adjacency** (`govauctions.com` vs `.app`) | Low | Decision 10 |

---

## Appendix — reports index

| Report | Subject |
|---|---|
| [R1-feature-inventory.md](R1-feature-inventory.md) | Every page template, free-vs-Pro gating map, robots/sitemap, API spec |
| [R2-data-model.md](R2-data-model.md) | Entity list, exact `auction` fields off the wire, Flip Score verified, our target schema |
| [R3-comps-mechanics.md](R3-comps-mechanics.md) | **How he gets closing prices** — the verdict, launch timeline, his HN statements, per-source comps coverage |
| [R4-sources.md](R4-sources.md) | His 31-source roster, overlap with our scorecard, access-ladder probes on 21 new sources |
| [R5-seo.md](R5-seo.md) | Sitemap census, template inventory, JSON-LD stack, recommended US v1 page set |
| [R6-legal.md](R6-legal.md) | ToS + robots per source, GREEN/AMBER/RED verdicts, quoted clauses |
| [R7-reuse-map.md](R7-reuse-map.md) | Our own stack audited: EXISTS-USE-AS-IS / NEEDS-REFACTOR / MISSING |
| [R8-competitors-economics.md](R8-competitors-economics.md) | Competitor sweep incl. BidProwl, hosting/DB/LLM/Stripe economics at 3 scales |
