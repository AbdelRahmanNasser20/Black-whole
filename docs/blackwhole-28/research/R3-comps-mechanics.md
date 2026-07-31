# R3 — How GovAuctions.app Captures Closing Prices

**Research date:** 2026-07-31
**Question:** Is a poll-and-snapshot recorder genuinely equivalent to Ben Wallace's sold-comps dataset, or does he have a source we don't know about?

---

## VERDICT (up front)

**Poll-and-snapshot is not merely *equivalent* to his method — it *is* his method, and he documents it himself.**

There is **no bulk data purchase, no partnership, and no platform export.** Three independent lines of hard evidence:

1. **His entire sold-comps dataset begins 2026-05-06** — one month *after* his 2026-04-06 public launch. His own price-index page prints the range: *"93,021 completed lots (May 6, 2026 – Jul 31, 2026)"*. Nothing predates his launch.
2. **His published open dataset (CC-BY, on GitHub) has `ended_at` spanning exactly 2026-05-06 → 2026-06-29 and zero rows before that.** I downloaded and histogrammed it (5,971 rows, two months, nothing earlier).
3. **His own data dictionary admits the snapshot mechanic**: `current_or_final_bid` is *"the **current or last-observed bid**, not a confirmed hammer price"*, and the `sold` column carries an `unknown` enum value *"when undeterminable."* A purchased/exported dataset never has undeterminable outcomes. A poll-and-snapshot recorder always does.

**Strategic consequence: his moat is ~86 days of snapshots, not a dataset we can't reach.** As of today he is only 86 days ahead of a standing start. And on several sources he indexes (Municibid `&StatusFilter=completed_only`, Purple Wave `filters=sold:Yes&dateType=past`, HiBid past-auction archives, PropertyRoom closed lots) the *platforms themselves* publish retroactive sold archives going back years — which he apparently has **not** backfilled. A backfill against those sources would put us **ahead of him on history**, not behind.

---

## (a) Launch timeline — established

| Evidence | Date | Link |
|---|---|---|
| GitHub account `benswork-space` created | **2026-02-06** | [api](https://github.com/benswork-space) |
| "I built GovAuctions in early 2026" (his words) | early 2026 | [/about](https://govauctions.app/about) |
| Show HN post → front page, ~20k visitors week 1 | **2026-04-06 16:21 UTC** | [HN 47662945](https://news.ycombinator.com/item?id=47662945) |
| Earliest Wayback snapshot of govauctions.app | **2026-04-06 16:37 UTC** (16 min after the HN post) | [Wayback availability API](http://archive.org/wayback/available?url=govauctions.app&timestamp=20251001) |
| Sold-comps data collection begins | **2026-05-06** | [/tools/price-index](https://govauctions.app/tools/price-index) |
| Open dataset repo created | **2026-06-11** | [repo](https://github.com/benswork-space/us-government-surplus-dataset) |
| "now in US/UK/CA/AU, deal scores + bid estimates" + API launch | **2026-07-01** | [HN 48747650](https://news.ycombinator.com/item?id=48747650) |
| Public MCP server repo | **2026-07-13** | [govauctions-mcp](https://github.com/benswork-space/govauctions-mcp) |

**Ben Wallace = HN user [`player_piano`](https://news.ycombinator.com/user?id=player_piano)** (234 karma, account has only 2026 activity). His GitHub account is 6 months old with 11 repos, all 2026 (`airbitrage` "toy arbitrage app", `sfrent`, `SF-311`, `sf-food-truck`, `canido-mcp`, `scrapmetal-python`…). **There is no pre-2026 data-collection footprint anywhere.** He did not have a hoard sitting around.

Wayback has **no snapshot of govauctions.app before HN launch day**. The domain was dark until 2026-04-06.

---

## (b) Do his sold comps predate his launch? — **NO. Decisively no.**

### The single best piece of evidence: his own price-index header

> **"93,021 completed lots (May 6, 2026 – Jul 31, 2026)"**
> — [govauctions.app/tools/price-index](https://govauctions.app/tools/price-index)

### Corroboration 1 — every month-by-month chart on the site starts 2026-05

The methodology footer on every price page says *"over the trailing 365 days … a month needs at least 5 sales to chart."* If he had any data before May 2026, those months would chart. **They don't — on any page, in any category.**

| Page | Sample size | Months actually charted |
|---|---|---|
| [/tools/price-index/vehicles](https://govauctions.app/tools/price-index/vehicles) | **34,482** completed lots | 2026-05, 2026-06, 2026-07 **only** |
| [/sold-prices/iphone](https://govauctions.app/sold-prices/iphone) | 469 | 2026-05, 2026-06, 2026-07 |
| [/sold-prices/forklift](https://govauctions.app/sold-prices/forklift) | 365 | 2026-05, 2026-06, 2026-07 |
| [/sold-prices/tractor](https://govauctions.app/sold-prices/tractor) | 528 | 2026-05, 2026-06, 2026-07 |
| [/tools/price-index/jewelry](https://govauctions.app/tools/price-index/jewelry) | 1,685 | 2026-06, 2026-07 |

34,482 vehicle sales cannot fail the ≥5-sales-per-month bar for nine consecutive months. The months are missing because **the data does not exist.**

The phrase *"in the last 12 months" / "trailing 365 days"* is a **rolling-window definition in his copy, not a claim of 12 months of history.** This is the trap the surface reading falls into.

### Corroboration 2 — I downloaded his open dataset and histogrammed it

Repo: **[benswork-space/us-government-surplus-dataset](https://github.com/benswork-space/us-government-surplus-dataset)** (CC-BY-4.0, refreshed monthly, GSA-only)
CSV: `https://raw.githubusercontent.com/benswork-space/us-government-surplus-dataset/main/us-gsa-surplus-auctions.csv`

```
rows 5971
ended_at histogram:  2026-05 → 3066     2026-06 → 2905
earliest 2026-05-06     latest 2026-06-29
sold flag: true 3739 | false 2106 | unknown 126
```

Repo commit history: initial release **2026-06-11**, one refresh **2026-07-01**. Four commits total.

### Corroboration 3 — the Flip Score window matches the dataset age

[/methodology](https://govauctions.app/methodology): Flip Score uses *"up to 25 of its closest completed sales (recency-weighted over roughly the last 90 days)."* 90 days ≈ the entire life of his comps table. He designed the model around the only data he has.

### Note on the headline numbers — they are inconsistent marketing copy

| Page | Claim |
|---|---|
| [Homepage](https://govauctions.app/) | "54,050 live from 32 sources · 230,000+ tracked in total · **180,000+ completed sales** · **135,000+ past U.S. sales**" |
| [/about](https://govauctions.app/about) | "final winning bids from **180,000+** completed government auction sales" |
| [/methodology](https://govauctions.app/methodology) | "roughly **92,483** completed U.S. sales from the last 365 days… 40,193 live listings from **18** platforms" |
| [/tools/price-index](https://govauctions.app/tools/price-index) | "**93,021** completed lots (May 6, 2026 – Jul 31, 2026)" |
| [/platforms/govdeals](https://govauctions.app/platforms/govdeals) | "**82,046** completed government auction sales in the last year" |
| [/guides/best-government-auction-sites](https://govauctions.app/guides/best-government-auction-sites) | "**125,000+** past U.S. auction sales" |

Read: **~93k categorized US comps**, ~135k US total, ~180k including UK/CA/AU. All accumulated in 86 days ≈ **~1,000–2,000 captured closes/day**. Treat any single number as stale copy; the only figure with a stated date range is the price-index one.

---

## (c) Per-source comps coverage — partial, GovDeals-dominated, with real gaps

### His own platform-share table (listings, trailing 30 days)

From [/research/state-of-government-surplus](https://govauctions.app/research/state-of-government-surplus), which explicitly warns it is *"a **sample** of the platforms we index, not the entire government-auction universe."*

| Platform | Share | In our scorecard? |
|---|---|---|
| **govdeals** | **60.1%** | ✅ maestro JSON |
| PublicSurplus | 11.2% | ✅ plain HTTP |
| **civilview** | 7.9% | ❌ **NEW LEAD** |
| GovPlanet | 4.9% | ✅ AWS-WAF |
| **jjkane** (J.J. Kane Auctioneers) | 3.1% | ❌ **NEW LEAD** |
| GSA Auctions | 2.8% | ✅ official API |
| HUD HomeStore | 2.4% | ❌ (real estate) |
| bid4assets | 1.8% | ✅ |
| **apple** (Apple Auctioneering) | 1.7% | ❌ **NEW LEAD** |
| Municibid | 1.7% | ✅ completed_only |
| purplewave | 1.6% | ✅ sold:Yes |
| PropertyRoom | 0.5% | ✅ |
| Michigan MiBid | 0.2% | ✅ |
| Illinois iBid | 0.1% | ✅ |
| gsa-realestate | 0% | ❌ |
| **cws** (CWS Marketing) | 0% | ❌ **NEW LEAD** |

Also named on [/guides/best-government-auction-sites](https://govauctions.app/guides/best-government-auction-sites) and [/compare](https://govauctions.app/compare): Fannie Mae HomePath, Freddie Mac HomeSteps.

### Which sources actually appear in his *sold* examples

Sampled the "recent sold" lists across four price pages:

| Page | Platforms appearing in sold examples |
|---|---|
| /sold-prices/tractor | GovDeals ×6 (100%) |
| /sold-prices/forklift | GovDeals ×3, GSA ×3 |
| /sold-prices/iphone | GovDeals, PropertyRoom |

**Observed comps sources: GovDeals, GSA, PropertyRoom, Public Surplus.** Purple Wave, Municibid, HiBid, iBid, MiBid did **not** appear in any sold example I pulled — despite Purple Wave being an obvious source for tractors, where 6/6 examples were GovDeals. That is a gap.

### The gaps are exactly the shape of poll-and-snapshot

1. **Rate ceiling.** He indexes **23,369 active GovDeals listings** ([/platforms/govdeals](https://govauctions.app/platforms/govdeals)). At typical 7–14 day GovDeals auction durations that alone implies ~1,700–3,300 lots closing per day — already at or above his **entire** ~1,000–2,000/day capture rate across all 32 sources. He is not capturing every close even on his biggest source.
2. **`sold: unknown` (126/5,971 ≈ 2.1%)** — lots that vanished before he could resolve an outcome. Pure snapshot artifact.
3. **`starting_bid` empty on many rows** — field-level gaps from partial page reads, not an export.
4. **"Ended auctions drop off automatically"** ([/methodology](https://govauctions.app/methodology)) — he is describing the exact disappearance problem a snapshot recorder exists to solve.
5. **Real-estate comps are not sale prices.** His housing research is built on HUD / Fannie Mae HomePath / Freddie Mac HomeSteps **list/asking prices**, not hammer prices ([HN comment 48820646](https://news.ycombinator.com/item?id=48820274)). The 538-lot "real estate" comp bucket is soft.
6. **Anomaly worth checking:** `civilview` is 7.9% of his quarter share, yet his own state pages show only ~4 active CivilView listings in FL and ~2 in TX. Either one large batch skewed the quarter, or the share table is mis-attributed.

---

## (d) Public statements by Ben about his pipeline

### Launch day (2026-04-06), HN — he explicitly disclaimed scraping GovDeals

> *"Happy to. This is a side-project, so I spun it up pretty quickly using Next.js and Tailwind, and it is hosted on Vercel right now. **I use a few free government APIs for the data** (listed on the site, you can sign up for a key for free for all of them I think), **plus a custom workflow I built that parses and ingests many other online auctions that states are mandated to make public**, but which aren't part of any API or data pipeline I could find."* — [player_piano](https://news.ycombinator.com/item?id=47662945)

> *"**I have no intention of scraping govdeals or anything like that**, which means I have to do my own ingestion and cleaning of the data."*

> *"One thing I'm currently fixing is the workflow for bringing in many of the listings that sites like GovDeals cover but are not part of the available APIs. **Scraping sites like GovDeals is kind of shady and not something I want to do**, so I am ingesting and cleaning a lot of data from state/government websites myself. **While I fix that, I've removed those references from the site.**"*

> *"**I'm not scraping sites like govdeals**, so no need to worry about that thankfully."*

**This is the most important behavioral fact in the whole report.** In April he pulled GovDeals *off the site* rather than scrape it. By July, **GovDeals is 60.1% of his index and dominates his sold-comps examples.** Between April and July he changed his mind and built GovDeals ingestion anyway. His public position ("we don't scrape") and his current data reality have diverged.

That divergence explains the API design perfectly: **`/listings` is GSA-only ("sanctioned sources only, currently gsa")** because GSA is 17 U.S.C. §105 public domain and explicitly sanctioned — he'll redistribute *that* raw. Everything else he only exposes as a **derived aggregate** (P25/median/P75), which is a legally safer transformation. From his dataset README:

> *"GSA is a U.S. federal agency, so these listings are works of the U.S. government and are **not subject to copyright** (17 U.S.C. §105). **GSA is also a source we index with explicit sanction.** The dataset is data only — no images."*

### The data dictionary — the confession

From the [dataset README](https://github.com/benswork-space/us-government-surplus-dataset):

> *"`current_or_final_bid` is the **current or last-observed bid, not a confirmed hammer price**. Treat it as a *bid level*."*
> *"`sold` | enum | `true` / `false` / `unknown` — **derived**; `unknown` when undeterminable."*
> *"`bid_count` | number | Number of **bids observed** (GSA bid counts are reliable)."*

### Methodology page — the pipeline in his words

From [/methodology](https://govauctions.app/methodology):

- *"Rolling snapshot maintained daily"*, *"rebuilt once a day, around 06:00 UTC"*
- *"Ended auctions drop off automatically"* from the live catalog
- Dedup: *"One item cross-posted to several platforms is merged into a single listing"*; normalization strips *"plate numbers, model codes, quantities, and condition words"*
- Comp match gate: *"at least five solid matches that agree closely enough"*; vehicles constrained *"within four model years, and to the same make"*; bulk lots normalized per-unit; real estate anchored to *"appraised prices (HUD/Fannie Mae) or ZIP-level Zillow index"*
- Flip Score: `100 / (1 + (bid / median)²)`, median = 50 points, recency-weighted over ~90 days, damage descriptions zero the margin

### Where he says nothing

No blog, no X handle, no bio on HN or GitHub, no podcast, no IndieHackers/ProductHunt presence found. He has never claimed a data purchase, licensing deal, or platform partnership — because there isn't one to claim.

---

## Bonus: quality weakness discovered on OUR vertical

`get_sold_comps` is **open and unauthenticated** on his hosted MCP endpoint (`https://govauctions.app/api/mcp`, per the [MCP README](https://github.com/benswork-space/govauctions-mcp)). Two probes:

```
q="banquet chairs"  → category "electronics" (auto-resolved), count 60,
                      p25 $6 / median $6 / p75 $6, confidence "high", windowDays 365
q="stacking chairs" → identical: electronics, 60, $6/$6/$6, confidence "high"
```

**His comps engine returns garbage on bulk seating.** It miscategorizes chairs as *electronics*, collapses all three percentiles to a single $6 value, returns an identical degenerate answer for two different queries, and still self-reports `confidence: "high"`. `count: 60` looks like a hard cap on the match set. Meanwhile his own furniture bucket carries 12,992 sold lots with a $14 median — i.e. he's averaging single-chair junk lots against 900-chair pallets with no per-unit or lot-size awareness (despite claiming per-unit normalization in his methodology).

This is a direct wedge: **lot-size-aware, per-unit comps in the categories a real reseller actually buys.** He has breadth; he does not have depth where it pays.

---

## Competitive note (adjacent, surfaced during research)

**BidProwl** ([bidprowl.com](https://bidprowl.com)) — [HN 47961378](https://news.ycombinator.com/item?id=47961378), **2026-04-30**, 302 points, by user `scarsam`. *"I aggregated 28 US Government auction sites into one search."* Claims 180,276 active listings, ~53,000 new/week, Postgres + full-text search, *"Next.js, Postgres, TypeScript scrapers per source, daily refresh."* Unlike Ben, he openly says he scrapes. He states the hard part was **dedup**, not scraping. He also noted *"I have had about 10 scrapers from various places scraping the site in the last week"* — the aggregator layer is itself being aggregated.

Two HN front-page aggregators launched within four weeks of each other (Apr 6 and Apr 30, 2026). The live-listings aggregation layer is now crowded. **The time-series sold-comps layer is not** — and nobody has depth on it.

---

## Implications for our build

1. **Start the recorder today.** He is 86 days ahead, not 3 years. Every day we don't run a watcher is a day of permanently unrecoverable closes on GovDeals.
2. **We already have the mechanism.** `deals/watcher_logic.py` + `deal_snapshots` implements exactly what he does — trust `end_utc`, re-read it each poll for anti-snipe extension, treat disappearance-from-search as the close, outcome = last snapshot before the drop. His `sold: unknown` enum is our identical edge case.
3. **Backfill past him.** Municibid (`&StatusFilter=completed_only`), Purple Wave (`filters=sold:Yes&dateType=past`), HiBid past-auction archives, and PropertyRoom closed lots expose *retroactive* sold history. He appears not to have used any of it. A one-time backfill there buys history he cannot buy back.
4. **Prefer confirmed hammer prices where obtainable.** His whole dataset is "last-observed bid." The GovDeals maestro per-lot detail endpoint (`POST /assets/{asset_id}/{account_id}/false`) plus `GET /bids/bidbox/GD/{a}/{acct}/{auction}` — already documented in our own `listing_automation/CLAUDE.md` — can give exact contested-lot final state. **That is a genuine quality advantage over him**, not parity.
5. **Beat him on depth, not breadth.** Per-unit and lot-size-aware comps, correct categorization, and honest confidence scoring. His banquet-chair answer is $6 with "high" confidence.
6. **Chase the four sources he has that we don't:** `civilview`, `jjkane` (J.J. Kane Auctioneers), `apple` (Apple Auctioneering), `cws` (CWS Marketing) — together ~12.7% of his index.
7. **Legal posture, mirrored.** GSA is public domain (17 U.S.C. §105) and safe to redistribute raw. For scraped commercial platforms, publish only derived aggregates. His API terms — *"Redistribution or rebuilding a competing dataset is not permitted"* — mean **we must not build from his API**; we build from the same primary sources he does.

---

## Sources

- [govauctions.app/about](https://govauctions.app/about) · [/methodology](https://govauctions.app/methodology) · [/tools/price-index](https://govauctions.app/tools/price-index) · [/sold-prices](https://govauctions.app/sold-prices) · [/research](https://govauctions.app/research) · [/research/open-dataset](https://govauctions.app/research/open-dataset) · [/research/state-of-government-surplus](https://govauctions.app/research/state-of-government-surplus) · [/platforms/govdeals](https://govauctions.app/platforms/govdeals) · [/compare](https://govauctions.app/compare) · [/developers](https://govauctions.app/developers) · [/api/v1/openapi.json](https://govauctions.app/api/v1/openapi.json)
- [Show HN: GovAuctions (2026-04-06)](https://news.ycombinator.com/item?id=47662945) · [Show HN: US/UK/CA/AU (2026-07-01)](https://news.ycombinator.com/item?id=48747650) · [Cheapest homes (2026-07-07)](https://news.ycombinator.com/item?id=48820274) · [BidProwl (2026-04-30)](https://news.ycombinator.com/item?id=47961378)
- [github.com/benswork-space/us-government-surplus-dataset](https://github.com/benswork-space/us-government-surplus-dataset) · [github.com/benswork-space/govauctions-mcp](https://github.com/benswork-space/govauctions-mcp)
- [Wayback availability API for govauctions.app](http://archive.org/wayback/available?url=govauctions.app&timestamp=20251001) (earliest snapshot 2026-04-06 16:37 UTC; `web.archive.org` CDX was unfetchable from this environment — availability API used instead)

**Fetch failures noted:** `web.archive.org` (blocked by tooling, twice — worked around via `archive.org/wayback/available`); `rdap.org/domain/govauctions.app` (403 — domain registration date not obtained; `whois` for `.app` returns IANA TLD record only, Google Registry does not expose a public RDAP path from here); `govauctions.app/tools/price-index/furniture` (404 — slug differs); `govauctions.app/research/zero-bid-auctions` (404 — real slug is `/research/what-the-government-cant-give-away`).
