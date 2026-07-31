# R2 — GovAuctions.app Data Model (inferred)

**Researcher:** R2 · **Date:** 2026-07-31 · **Target:** [govauctions.app](https://govauctions.app)

## 0. TL;DR

The OpenAPI spec is a **marketing artifact, not a schema** — it declares zero response
bodies. The real data model is fully exposed in the **Next.js RSC flight payload**
embedded in every listing page (`self.__next_f.push([1,"…"])`), which serialises the
complete `auction` object plus its computed siblings. That gave us exact field names,
types and values. Everything below is evidence-backed, not guessed.

Headline: he runs **one wide `auction` row** (~30 columns, source-native ids preserved),
a **rolling 365-day sold-comps corpus (~92,483 US rows)**, a **curated `model` dimension**
(~162 hand-picked item archetypes like `ford-f-150`, `office-chair`), a **source registry
with quality scores**, and a **per-lot bid time-series** (`priceHistory`). The Flip Score
is a closed-form function we **reverse-engineered and verified numerically** (§5.1).

The single biggest gap in his model, and the one that matters most to BLACKWHOLE:
**there is no `quantity` / per-unit field on the lot entity.** His methodology page says
per-unit math happens inside comp matching, but it is never persisted or exposed. For a
bulk-lot business (banquet chairs), that is the whole game.

---

## 1. Evidence base

| # | Artifact | URL | What it gave us |
|---|---|---|---|
| E1 | OpenAPI 3.1.0 spec | [`/api/v1/openapi.json`](https://govauctions.app/api/v1/openapi.json) | 4 endpoints, query params, enums, plan tiers. **No `components.schemas`** — response bodies are prose only |
| E2 | robots.txt | [`/robots.txt`](https://govauctions.app/robots.txt) | `Allow: /api/v1/openapi.json`, `Disallow: /api/`, honeypot `/auction/_honeypot`, explicit `Allow` for ClaudeBot/GPTBot/PerplexityBot |
| E3 | Sitemap index → 2 children | [`/sitemap.xml`](https://govauctions.app/sitemap.xml) | 45,000 + ~850 URLs |
| E4 | `sitemap/0.xml` (10.4 MB) | [`/sitemap/0.xml`](https://govauctions.app/sitemap/0.xml) | 41,189 US `/auction/{slug}` pages; full URL taxonomy |
| E5 | US listing page (GovDeals) | [`/auction/2017-audi-a6-black-ohio-govdeals-1537-88`](https://govauctions.app/auction/2017-audi-a6-black-ohio-govdeals-1537-88) | **Full serialised `auction` object** + JSON-LD `Product` |
| E6 | US listing page (GSA) | [`/auction/2016-ram-1500-4x4-truck-texas-gsa-3-1-qsc-i-26-488-008`](https://govauctions.app/auction/2016-ram-1500-4x4-truck-texas-gsa-3-1-qsc-i-26-488-008) | Second `auction` object → which fields are optional; self-hosted image path |
| E7 | Methodology page | [`/methodology`](https://govauctions.app/methodology) | **Exact Flip Score formula**, comp-matching rules, corpus size, refresh cadence |
| E8 | Sources registry | [`/sources`](https://govauctions.app/sources) | 32 platforms / 4 countries / 54,050 live; per-source quality metrics |
| E9 | Platform page | [`/platforms/govdeals`](https://govauctions.app/platforms/govdeals) | JSON-LD `Dataset` with per-category p25/median/p75 + sample n |
| E10 | Sold-prices hub + leaf | [`/sold-prices`](https://govauctions.app/sold-prices), [`/sold-prices/office-chair`](https://govauctions.app/sold-prices/office-chair) | The **`model` dimension** entity: 162 archetypes, per-state and per-month breakdowns |
| E11 | Price index | [`/tools/price-index/vehicles`](https://govauctions.app/tools/price-index/vehicles) | Category-level monthly series + methodology footnote |
| E12 | Open dataset (GitHub) | [`benswork-space/us-government-surplus-dataset`](https://github.com/benswork-space/us-government-surplus-dataset) + [`manifest.json`](https://raw.githubusercontent.com/benswork-space/us-government-surplus-dataset/main/manifest.json) | **His own declared 16-column public schema**, CC-BY-4.0, monthly |
| E13 | Developer page | [`/developers`](https://govauctions.app/developers) | Tiers, MCP server, redistribution ban |

*Polite-behaviour note:* robots.txt was read first; only ~12 page fetches total, ≥2 s
apart, standard browser UA, no `/api/` paths touched, no auth attempted.

---

## 2. Entity list

| Entity | Persisted? | Where observed | Cardinality |
|---|---|---|---|
| **`auction`** (live lot) | yes, primary table | E5, E6 flight payload | 54,050 live (40,193 US) |
| **`sold_comp`** (completed lot) | yes, rolling corpus | E7, E10 | ~92,483 US rows, 365-day window |
| **`source` / platform** | yes, small registry | E8 | 32 rows, 4 countries |
| **`model`** (item archetype) | yes, curated dimension | E10 | 162 slugs |
| **`category`** | yes, enum + slug | E4, E12 | 11–12 values |
| **`price_snapshot`** (`priceHistory`) | yes, time-series | E5 (`[{t, bid}]`) | ~1/day per live lot |
| **`comp_stat`** (aggregate) | derived/materialised | E9, E10, E11 | by category × state × month × model |
| **`sell_through`** (aggregate) | derived/materialised | E5, E6 | by category (and by state — `sellThroughState`) |
| **`flip_score` / `flipSummary`** | computed, Pro-gated | E5, E6, E7 | 1:1 with live lot |
| **`city` / geo** | derived dimension | 931 `/cities/{state}/{city}` pages | 931 US cities |
| **`county`** | derived dimension | 1,168 `/research/what-your-county-is-selling/{state}/{county}` | ~1,100 counties |
| user: `saved`, `alerts`, source ratings | yes (auth) | E2 disallow list, E8 "Rate this source" | not observable |

---

## 3. `auction` — the core entity (exact, from the wire)

Verbatim from the RSC payload on E5 (GovDeals lot), reformatted:

```json
{"id":"govdeals-1537-88","sourcePlatform":"govdeals",
 "sourceUrl":"https://www.govdeals.com/en/asset/1537/88",
 "title":"2017 Audi A6 - Black","description":"2017 Audi A6. Vehicle is a result of a seizure…",
 "category":"vehicles","condition":"As-is","currentBid":6500,"startingBid":10,
 "sourceAuctionId":1,"buyerPremiumPct":0,
 "auctionStart":"2026-07-27T09:08:58","auctionEnd":"2026-08-03T13:08:58Z","bidCount":99,
 "images":["https://files.lqdt1.com/photos/88/88_1537_187f731f-….jpg?cb=260722083447"],
 "thumbnailUrl":"https://files.lqdt1.com/photos/88/88_1537_….jpg?cb=260722083447",
 "sellerName":"Lorain County, OH","sellerType":"county",
 "locationCity":"Elyria","locationState":"OH","locationZip":"44035-6957",
 "latitude":41.346217,"longitude":-82.134059,
 "pickupDeadlineDays":10,"pickupNotes":"Contact seller through GovDeals for pickup details…",
 "createdAt":"2026-07-31T06:57:53.593Z","updatedAt":"2026-07-31T06:57:53.593Z","isActive":true,
 "compFallback":{"count":51,"median":3100,"p25":1876,"p75":6515,"confidence":"high"},
 "compValue":3100,"damageSeverity":"moderate","flipScore":19,
 "country":"US","currency":"USD"}
```

### 3.1 Field table

| Field | Type | Raw / Computed | Notes & evidence |
|---|---|---|---|
| `id` | string | **derived key** | `{sourcePlatform}-{source-native id parts}`. Stable, human-readable, no surrogate int |
| `sourcePlatform` | enum string | raw | 32 values (§4) |
| `sourceUrl` | string (URL) | raw | Always the *original government page*. Every listing links out |
| `sourceAuctionId` | integer\|null | raw | Present on GovDeals (`1`), absent on GSA. Source's auction/event id, distinct from lot id |
| `title` | string | raw | |
| `description` | string | raw | **Quality bug observed**: GSA lot E6 has `description:"$27"` — his GSA description parse is broken |
| `category` | enum slug | **computed** (normalised) | 11 values: `vehicles, heavy-equipment, electronics, office-furniture, tools-industrial, medical-scientific, military-surplus, jewelry, collectibles, real-estate, seized-property, miscellaneous` (E12) |
| `condition` | string | raw | Free text (`"As-is"`); not an enum |
| `currentBid` | number | raw | |
| `startingBid` | number\|null | raw | null on GSA |
| `buyerPremiumPct` | number | raw-ish, **unreliable** | `0` on the GovDeals lot — but E9 states GovDeals BP is 7.5–12.5%. So it's a per-lot field that is mostly unpopulated, with the real number living on the `source` registry instead |
| `bidCount` | integer | raw | |
| `auctionStart` | timestamp | raw | **Inconsistent tz**: GovDeals `2026-07-27T09:08:58` (naive), GSA `2026-07-24T09:00:00-04:00` (offset) |
| `auctionEnd` | timestamp | raw | GovDeals `…Z`, GSA `-04:00`. Same column, three tz conventions |
| `images` | string[] | raw **or self-hosted** | GovDeals → source CDN `files.lqdt1.com/...`; GSA → `/images/gsa/{ID}.jpg` (**re-hosted**). Per-source strategy, see §6 |
| `thumbnailUrl` | string | raw/self-hosted | GSA → `/images/gsa/thumb/{ID}.jpg` |
| `sellerName` | string | raw | e.g. `"Lorain County, OH"`, `"Fish and Wildlife Service – Fish and Wildlife Service"` (dupe artifact) |
| `sellerType` | enum string | **computed** | Observed `county`, `federal`. E12 calls it "selling level". Likely `{federal,state,county,city,school,transit,…}` |
| `locationCity` / `locationState` / `locationZip` | string | raw | `locationState` = 2-letter |
| `latitude` / `longitude` | float | **computed** (geocoded) | Powers `/…-near-me` pages and the bid-intensity map |
| `pickupDeadlineDays` | integer | **computed/templated** | 10 (GovDeals), 15 (GSA) — looks source-defaulted |
| `pickupNotes` | string | raw *or* templated | GovDeals text is boilerplate ("Most sellers require pickup within 10 business days"); GSA text is genuinely from the lot |
| `createdAt` / `updatedAt` | timestamp | system | `updatedAt` ≈ 06:5x UTC daily → matches "rebuilt once a day, around 06:00 UTC" (E7) |
| `isActive` | boolean | system | |
| `country` | enum | raw/routing | `US, UK, AU, CA` |
| `currency` | enum | raw | `USD, GBP, AUD, CAD` |
| `compValue` | number\|null | **computed** | Median of matched sold comps — the anchor for everything |
| `compFallback` | object\|null | **computed** | `{count:int, median, p25, p75, confidence:"high"\|…}` |
| `damageSeverity` | enum\|null | **computed** | Observed `"moderate"`; absent on the clean lot. Drives the "damage overrides everything" cap (E7) |
| `flipScore` | integer 0–100 | **computed** | Closed form, verified — §5.1 |

### 3.2 Page-level siblings (rendered next to `auction`, not on it)

| Field | Type | Observed value | Meaning |
|---|---|---|---|
| `ended` / `sold` | boolean | `false` / `false` | Lifecycle |
| `priceHistory` | `[{t:int_ms, bid:number}]` | 5 points over 5 days on E5, 1 point on E6 | **The time-series.** ~1 snapshot/day, appended on the daily rebuild |
| `sellThrough` | object | `{sellRate:0.7, known:210, medianBids:5, p25Bids:3, p75Bids:12, soldWithBids:147}` | **Identical on both lots** (different sources, different states, both `vehicles`) → it's a **category-level** aggregate, not per-lot |
| `sellThroughState` | null | null | Category × state variant; **Pro-gated / nulled for anonymous** |
| `marketStats` | null | null | Pro-gated |
| `compRange` | null | null | Pro-gated (the tight, title-matched comp band; `compFallback` is the loose one that free users see) |
| `flipSummary` | null | null | Pro-gated: "resale value − bid − BP − ~12% resale fee − category transport", plus projected closing range (E7) |
| `sameLocationOthers` | integer | 0 / 1 | Count of other lots at the same pickup site — a freight-amortisation signal |

**Free-vs-Pro is expressed as nulls in the same object.** Same shape, values withheld.
That is a clean pattern worth copying.

---

## 4. `source` entity + the id/URL contract

### 4.1 Source registry (E8) — fields per row

`name`, `slug`, `homepage_url`, `country`, `live_count` (refreshed daily),
`inventory_description`, `buyer_premium_text` + `buyer_premium_median_pct`,
`long_description`, and a **quality scorecard**:

| Metric | GovDeals | Public Surplus |
|---|---|---|
| Data-quality score | 100/100 "Excellent" | 98/100 "Excellent" |
| Photo coverage | 100 % | 100 % |
| Feed consistency | 100 % | 100 % |
| Location accuracy | — | 94 % |
| Buyer ratings | "No buyer ratings yet" (user-submitted, `Rate this source`) | same |

Market rollup (E8): US 40,193 / 18 platforms · UK 7,390 / 6 · AU 4,826 / 3 · CA 1,641 / 5
· **all 54,050 / 32**.

### 4.2 How a listing references its source — the id scheme

`id` is a **deterministic concatenation of the source's own primary key**, prefixed by
platform slug. There is no surrogate integer anywhere. Slug = `{title}-{state-name}-{id}`,
lowercased.

| Platform | count in sitemap | `id` template | Example | `sourceUrl` |
|---|---:|---|---|---|
| `govdeals` | 23,233 | `govdeals-{assetId}-{accountId}` | `govdeals-1537-88` | `govdeals.com/en/asset/1537/88` |
| `publicsurplus` | 4,372 | `publicsurplus-{auctionId}` | `publicsurplus-4054557` | |
| `govplanet` | 1,920 | `govplanet-{id}` | `govplanet-15530345` | |
| `civilview` | 1,810 | `civilview-{countyId}-{caseNo}` | `civilview-26-26001540`, `civilview-15-ch-26001682`, `civilview-86-24-1649-cv-c` | |
| `jjkane` | 1,203 | `jjkane-{id}` | `jjkane-1622167` | |
| `gsa` | ~1,150 | `gsa-{a}-{b}-{GSA lot no.}` | `gsa-3-1-QSC-I-26-488-008` | `gsaauctions.gov/auctions/preview/371466` ← **different id** |
| `hud` | 1,042 | `hud-{a}-{b}` | `hud-023-242952` | |
| `fanniemae` | 820 | `fanniemae-{uuid}` | `fanniemae-a638477f-5ab6-…` | |
| `bid4assets` | 685 | `bid4assets-{id}` | `bid4assets-1304633` | |
| `apple` | 675 | `apple-{id}` | `apple-313744204` | |
| `municibid` | 658 | `municibid-{id}` | `municibid-84571697` | |
| `purplewave` | 646 | `purplewave-{id}` | `purplewave-921390` | |
| `propertyroom` | 180 | `propertyroom-{id}` | `propertyroom-18714080` | |
| `mibid` | 72 | `mibid-{id}` | `mibid-7981` | |
| `ibid` | 25 | `ibid-{id}` | `ibid-414699` | |
| `homesteps` | ~50 | **`homesteps-{address-slug}`** | `homesteps-402-n-jefferson-st-hooker-ok-73945` | no numeric id exists → address *is* the key |
| `gsa-realestate` | 17 | `gsa-realestate-{id}` | `gsa-realestate-64` | |
| `cws` | ~5 | `cws-{id}` | `cws-425` | |
| CA: `gcsurplus`, `absurplus`, `bcauction` | — | `{src}-{id}` | `gcsurplus-748007` | under `/ca/` |
| AU: `allbids`, `grays` | — | `{src}-{id}` | `grays-25784632` | |
| UK: `ramco` 424, `eddisons` 188, `witham` 72 | 716 | `{src}-{uuid}` | `…-eddisons-25ad4c1e-432f-…` | under `/uk/auction/{slug}` |

**Two lessons:** (a) the GSA row proves `id` and `sourceUrl` can carry *different* source
identifiers — he stores both. (b) `homesteps` proves he needs a **fallback natural key**
when a source exposes no id at all.

### 4.3 Country routing

`/` = US (default), `/uk/…`, `/ca/…` are path-prefixed locales. Sitemap 1 is UK-only.
So the canonical key is really `(country, sourcePlatform, source_id)`.

---

## 5. Computed layer

### 5.1 Flip Score — formula confirmed

E7 states it literally: **`100 / (1 + (bid / median)²)`**. Verified against live data:

| Lot | `currentBid` | `compValue` | Formula | Site's `flipScore` |
|---|---:|---:|---:|---:|
| E5 Audi A6 | 6,500 | 3,100 | `100/(1+2.0968²) = 18.53` | **19** ✅ |
| E6 RAM 1500 | 6,050 | 8,125 | `100/(1+0.7446²) = 64.33` | **64** ✅ |

Properties: score = 50 exactly at the comp median; monotone decreasing in bid;
bounded (0,100); **deterministic** (he markets this — "no language model decides").
Additional documented rules layered on top:
- Built from **≤25 closest completed sales, recency-weighted over ~90 days**.
- Filtered by title similarity; **vehicles by model-year ±4 and make**; **electronics by
  configuration** ("a stripped laptop only comps against other stripped laptops").
- Scattered comp sets are **discarded**, thin matches **clamped into a narrower band**.
- **Damage override**: salvage / non-running / missing-parts ⇒ margin zeroed, score capped.
- Free on the listing page **only when score ≥ 80**; Pro unlocks the underlying numbers.

### 5.2 Deal Score (separate from Flip Score — ranks the Best-Deals feed)

- Compares **current bid *including* buyer's premium** against value (comp preferred,
  conservative category estimate as fallback).
- Blends **dollar savings** and **percent discount**, both **√-compressed** so one giant
  lot can't dominate.
- Weighted by **confidence**: closing-soon + real bidding = more certain; days left + no
  bids = less.
- **Hard rule: comp-verified always outranks a keyword estimate**, regardless of headline
  savings.
- **Refuses to score**: lots already bid ≥ value; heavily-damaged/salvage; suspiciously
  cheap non-runners; accessory/parts lots that word-match a whole machine.

### 5.3 Comp value — matching rules (E7)

| Rule | Detail |
|---|---|
| Corpus | rolling snapshot of **~92,483 completed US sales / last 365 days** |
| Match | same category + **significant title-word overlap** (filler words and stock numbers stripped first) |
| Vehicles | constrained to **±4 model years** and same make when named |
| **Per-unit math** | *"A 'lot of 14 laptops' is divided down to a per-unit price before we take the median, then scaled back up"* — but **no quantity field is persisted or exposed** |
| Minimum evidence | **≥5 solid matches that agree closely**, else no comp anchor → conservative estimate + lower rank |
| Real estate | handled separately: HUD/Fannie anchored to **published appraised price**; residential auction lots anchored to **Zillow ZHVI for the ZIP**, and only when the text shows beds/baths/sqft/street address (so a vacant lot never inherits a neighbourhood median) |
| Price basis | **realised final winning bid, excluding buyer's premium and taxes**; **no-bid lots excluded**; month needs ≥5 sales to chart |
| Refresh | full catalog rebuilt **daily ~06:00 UTC**; ended auctions drop off |
| Dedup | *"One item cross-posted to several platforms is merged into a single listing"* |

### 5.4 `comp_stat` aggregate shapes actually published

JSON-LD `Dataset.variableMeasured` on E9 encodes each stat as
`PropertyValue{name, description, value=median, minValue=p25, maxValue=p75, unitText:"USD"}`,
with `measurementTechnique:"Order statistics (25th percentile, median, 75th percentile) of
final winning bids"` and `temporalCoverage:"2025-07-31/2026-07-31"`.

| Grain | Where | Fields |
|---|---|---|
| category (national) | E9, E11 | `median, p25, p75, n, monthly series, 3-mo change %` |
| category × month | E11 | `2026-07 $2,325 · 2026-06 $2,050 · 2026-05 $1,675` |
| model (national) | E10 | `median, p25, p75, n`, monthly series, **recent sold examples** (`title, price, sold_date, state`) |
| model × state | E10 | `Washington $5 (251) · Texas $13 (19) · Ohio $177 (12)` |
| platform × category | E9 | GovDeals: Vehicles $2,025 [725–4,677] n=34,482; Electronics $78 [25–260] n=20,335; Furniture $14 [5–44] n=12,992; Tools $100 [32–360] n=8,082; Jewelry $100 [40–410] n=1,685; Medical $64 [25–250] n=4,470 |

### 5.5 `model` dimension (162 curated archetypes)

`/sold-prices/{model-slug}` — a hand-maintained list, not free-text. Fields per row:
`slug, display_name, category, median, p25, p75, n, monthly_series[], by_state[], recent_examples[]`.
Includes make+model (`ford-f-150`, `dell-latitude`, `herman-miller`-grade items surfacing
in examples) **and generic archetypes** (`office-chair`, `cafeteria-table`,
`conference-table`, `box-truck`, `skid-steer`).

Directly relevant to BLACKWHOLE: **`office-chair` median $5, IQR $5–$12, n=396**;
**`cafeteria-table` $17 (n=67)**; `conference-table` $13 (n=57); `desk` $10 (n=969);
`storage-cabinet` $29 (n=221). There is **no `banquet chair` / `stack chair` model** —
a visible hole in his taxonomy that our vertical owns.

---

## 6. Images — the actual policy (correcting "never re-hosted")

Three distinct behaviours observed in one crawl:

| Pattern | Example | Meaning |
|---|---|---|
| Source CDN URL stored raw in `images[]` | `https://files.lqdt1.com/photos/88/88_1537_….jpg?cb=…` | GovDeals — hotlink |
| **Proxied** through his own endpoint in JSON-LD | `https://govauctions.app/api/image?url=<urlencoded source>` | An image proxy/optimizer sits in front for the crawler-facing `Product.image` |
| **Self-hosted** local path | `/images/gsa/3-1-QSC-I-26-488-008.jpg`, `/images/gsa/thumb/…jpg` | GSA — bytes copied, plus a pre-generated thumbnail |

So: **per-source image strategy**, with a `/images/{source}/[thumb/]{id}.jpg` convention
for sources whose CDN can't be hotlinked. Also seen: `/images/govdeals/316-3268.jpg` on
one card — i.e. GovDeals falls back to local copies too. The API deliberately ships
**"facts only, no images"** (E1) — legal separation between the SEO surface and the paid feed.

---

## 7. OpenAPI spec — what it does and doesn't say

| Endpoint | Params | Response schema? |
|---|---|---|
| `GET /listings` | `country(enum:US, default US)`, `state`, `category`, `source(enum:gsa, default gsa)`, `q`, `priceMin`, `priceMax`, `active(bool, default true)`, `limit(≤100, default 25)`, `offset` | **none** — `200: "A page of listings"` |
| `GET /listings/{id}` | `id: string` | none; `404 Not found` |
| `GET /comps` | `q` (**required**), `category`, `country(US)` | none; `200 "Comp range (p25/median/p75)"`, `403 "Not in plan"` |
| `GET /flip-score` | `id` (**required**, "GSA listing id") | none; `200 "Flip Score, estimated value, discount"`, `403` |

Auth: `bearerAuth` (HTTP bearer) *or* `x-api-key` header. Tiers Free 100/mo, Hobby
$49/mo 5k, Enterprise. E13 adds an **MCP server** and an explicit licence term:
*"Redistribution or rebuilding a competing dataset is not permitted."*

**Interpretation.** `source` is enum-locked to `gsa` and flip-score is GSA-only because
GSA data is US-federal public domain — he can relicense it. Everything scraped from
GovDeals/Municibid/etc. powers the free SEO surface and the *derived* comps, but is
never resold as rows. That is a deliberate legal firewall, not a technical limit —
his own site clearly computes flipScore for GovDeals lots (E5, `flipScore: 19`).

## 8. His declared public schema (E12) vs. the internal one

The GitHub dataset is the **narrow public projection** of the wide internal row:

| Public column (16) | Internal equivalent | Notes |
|---|---|---|
| `id` | `id` | |
| `title`, `category`, `condition` | same | |
| `seller_type` | `sellerType` | |
| `state`, `city`, `zip` | `locationState/City/Zip` | lat/lon **withheld** |
| `currency` | `currency` | |
| `starting_bid` | `startingBid` | |
| `current_or_final_bid` | `currentBid` | *"Bid level (not guaranteed sale price)"* |
| `bid_count` | `bidCount` | |
| `buyer_premium_pct` | `buyerPremiumPct` | |
| `sold` | enum `true/false/unknown` | **the honest one** — he admits sale confirmation is uncertain |
| `ended_at` | `auctionEnd` | date only, UTC |
| `source_url` | `sourceUrl` | |
| — | `compValue`, `flipScore`, `damageSeverity`, `priceHistory`, geo | **all computed fields withheld** |

Public dataset: 5,971 rows, GSA-only, 2026-05-06 → 2026-06-29, 3,820 with bids,
CC-BY-4.0, monthly refresh, `schemaVersion: "1.0"`.

---

## 9. Weaknesses in his model (our openings)

1. **No `quantity` / `unit_count` / per-unit price on the lot.** Per-unit math happens
   only inside comp matching and is thrown away. A 500-chair lot and a 1-chair lot are
   the same row shape. **This is the #1 gap for a bulk-lot reseller.**
2. **No weight / dimensions / freight class.** Transport is a "category-based" constant
   in the Pro margin calc. No real freight quote is possible.
3. **`buyerPremiumPct` = 0 on a GovDeals lot** that E9 itself says carries 7.5–12.5%.
   Per-lot fee data is not actually captured; the registry median is doing the work.
4. **Timezone chaos in one column**: naive, `Z`, and `-04:00` all appear in
   `auctionStart`/`auctionEnd`.
5. **Description parsing broken on GSA** (`description:"$27"` on E6).
6. **`sold` is `unknown` for a large share** — closed-lot outcome isn't confirmable on
   most sources; only `bid_count` and last-seen bid are trustworthy.
7. **`priceHistory` is daily-granular** (~1 point/day). The last 60 minutes of a
   government auction — where anti-snipe extensions and the real price live — are invisible.
8. **No relist/duplicate-auction lineage.** Dedup merges cross-posted lots but there's no
   evidence of an `auction_id` dimension that survives a relist of the same asset.
9. **No `model` for our niche** — no banquet/stack chair archetype at all.
10. **`condition` is free text**, not an enum, so it can't be filtered numerically.

---

## 10. What OUR canonical schema needs

Design target: **a strict superset of his lot row, plus the four things he structurally
cannot answer** (quantity, freight, intra-hour price, outcome truth). Names below are
proposed for a `lots` / `lot_snapshots` / `sold_comps` trio that extends the existing
`deals.deal_lots` / `deal_snapshots` tables (which already use PK
`(asset_id, account_id, auction_id)` — keep that, it already beats his flat `id`).

### 10.1 `lots` — parity columns (mirror him, so we're never behind)

`source_platform`, `source_lot_id`, `source_auction_id`, `source_url`, `country`,
`currency`, `title`, `description`, `category` (our normalised enum), `native_category`,
`condition_raw`, `starting_bid`, `current_bid`, `bid_count`, `buyer_premium_pct`,
`auction_start_utc`, `auction_end_utc`, `seller_name`, `seller_type`,
`city`, `state`, `zip`, `lat`, `lon`, `pickup_deadline_days`, `pickup_notes`,
`image_urls[]`, `hero_image_url`, `is_active`, `first_seen_at`, `last_seen_at`.

### 10.2 `lots` — the differentiators he doesn't have

| Column | Type | Why |
|---|---|---|
| `quantity` | integer | **The whole business.** Already parsed by our `auction_extractors` LLM pipeline |
| `quantity_confidence` | enum `llm/regex/dom/manual` | We already track `llm_failed`; keep the provenance |
| `unit_price` | numeric generated | `current_bid / NULLIF(quantity,0)` — the only number that makes a 900-chair lot comparable to a 6-chair lot |
| `landed_unit_cost` | numeric | `(bid·(1+bp) + tax + freight) / quantity` — we already have `fees.landed_cost`; push it per-unit |
| `weight_lb`, `dim_in`, `freight_class` | numeric/text | Real SAIA quotes; his model has a category constant |
| `pallet_count` / `truckload_fraction` | numeric | Whether one lot fills a trailer decides the deal |
| `same_location_lot_count` | integer | He has `sameLocationOthers` — keep it, but use it to **amortise freight across lots**, which he can't |
| `fulfillment` | enum `local/dropship` | Already in our CRM contract; ties auction supply to the sales side |
| `relist_of_lot_id` | FK self | Lineage across relists so an outcome is never clobbered |
| `dedupe_group_id` | uuid | Cross-platform identity (his "merged into a single listing") **made explicit and inspectable** |
| `damage_severity` | enum `none/light/moderate/salvage` | Parity — it caps his score, it should cap ours |
| `outcome` | enum `no_bid/sold/withdrawn/unknown` | We already have this in `deals`; his public dataset admits `unknown` |
| `outcome_confidence` | enum | Be honest where he's honest, and better where we can be |

### 10.3 `lot_snapshots` — beat him on time resolution

`(lot_id, observed_at, current_bid, bid_count, end_utc, extended_flag)` — change-gated
(we already do this). Two rules that put us ahead of his 1/day `priceHistory`:

- **Poll cadence scales with time-to-close** (e.g. daily → hourly at T-24h → every
  2 min at T-15m). Government auctions hard-close with anti-snipe extension; re-read
  `end_utc` each poll (our `watcher_logic` already does).
- Persist `end_utc` **per snapshot**, so extension events become first-class data. He has
  no way to see a lot that got extended six times — that's a demand signal he cannot sell.

### 10.4 `sold_comps` — the corpus

`(lot_id, ended_at_utc, final_bid, bid_count, quantity, unit_final_price, category,
model_key, state, zip, source_platform, sold_confidence, buyer_premium_pct_at_sale)`.

Mandatory: **store `unit_final_price` as a persisted column, not a matching-time
derivation.** That single decision is what makes "what does a banquet chair go for in
GA in bulk" answerable in one query and lets us publish a per-unit price index he
structurally cannot.

### 10.5 `models` — our archetype dimension

`(slug, display_name, category, match_rules jsonb, min_sample_n, is_bulk)`. Seed with his
162 (they're evidence of what people search) **plus the bulk-seating archetypes he's
missing**: `banquet-chair`, `stacking-chair`, `folding-chair`, `chiavari-chair`,
`round-banquet-table`, `cafeteria-bench`. `is_bulk=true` models publish **per-unit**
medians by default.

### 10.6 `sources` — registry with the scorecard

`(slug, name, country, homepage_url, adapter, live_count, last_scraped_at,
buyer_premium_min/median/max, has_sold_prices, photo_coverage_pct, feed_consistency_pct,
location_accuracy_pct, data_quality_score, notes)`. We already have a verified scorecard
(2026-07-23) — this is where it lives, and it doubles as the public `/sources` page.
Add `sold_price_access` (`api/filter/none`) since our scorecard already knows which
sources expose sold prices (Municibid `StatusFilter=completed_only`, Purple Wave
`filters=sold:Yes&dateType=past`).

### 10.7 `comp_stats` — materialised aggregates

Grain: `(scope_type, scope_key, period_month, category?, model?, state?, source?)` →
`n, p25, median, p75, mean, unit_p25, unit_median, unit_p75`. Emit **both** absolute and
per-unit columns everywhere. Suppress below `min_sample_n` (his floor is 5 — match it).

### 10.8 Scoring columns (computed, versioned)

`comp_value`, `comp_count`, `comp_confidence`, `flip_score`, `deal_score`,
`projected_close_low/high`, `margin_low/mid/high`, **plus `score_version` and
`scored_at`**. He publishes his formula but not a version; when he changes it, every
historical number silently shifts. Versioning ours means our back-test stays valid.
Start by reproducing his exact `100/(1+(bid/median)²)` as `flip_score_v1` so we can say
"same grade, and here's the per-unit one too."

### 10.9 Non-negotiable invariants

1. `(country, source_platform, source_lot_id)` is the natural key; keep our
   `(asset_id, account_id, auction_id)` composite — it already survives relists.
2. Always store the **source's own id AND its canonical URL**; they can differ (GSA proves it).
3. All timestamps `TIMESTAMPTZ`, normalised to UTC on ingest, with `source_tz` retained.
   Do not repeat his three-conventions-in-one-column bug.
4. Never overwrite an outcome on re-sweep (already our rule in `deals/store.py`).
5. Price basis is always **final winning bid excluding buyer's premium** for comps, and
   **landed cost including premium** for scoring. Label every number with which basis it uses.
6. Images: per-source strategy column (`hotlink | proxy | rehost`), and our R2 bucket is
   the rehost target. Never write a Supabase Storage URL (402'd).

---

## 11. Open items

- `sourceAuctionId` semantics (`1` on GovDeals) are ambiguous — could be an event id or a
  lot-within-auction index.
- `compFallback` vs `compRange`: `compValue` equalled `compFallback.median` on both lots
  sampled, and `compRange` was null on both. Either free users only ever see the fallback,
  or both lots genuinely lacked a tight comp set. Two samples isn't enough to settle it.
- `sellThrough` was byte-identical across two different-source `vehicles` lots, which
  makes category-level the strong hypothesis — but a non-vehicle lot wasn't sampled.
- Corpus size is quoted three ways: 92,483 (US comps, E7), 82,046 (GovDeals completed
  sales last year, E9), ~150k (his marketing). Likely US-comps vs. per-platform vs. all-country.
