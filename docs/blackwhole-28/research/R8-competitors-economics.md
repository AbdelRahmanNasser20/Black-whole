# R8 — Competitor Sweep + Hosting Economics

Research date: 2026-07-31. Scope: government-auction aggregators / surplus-search / comps tools beyond GovAuctions.app and BidRadar.app, plus the infra economics of running a GovAuctions-scale service.

---

## Part 1 — Competitor Sweep

### 1.1 Summary table

| Product | Sources covered | Sold comps? | Pricing | Founder / alive signal | Verdict |
|---|---|---|---|---|---|
| **GovAuctions.app** (baseline, known) | ~29 US sources, live listings API is GSA-only for now; the *website* search also covers UK (ex-MoD, police, council), Canada (GCSurplus, GovDeals.ca), Australia (Allbids, Grays, Pickles) | Yes — claimed comp count **varies by page/date: 105k → 125k → 150k → 180k+** "completed government auction sales" (grew across 2026, or inconsistent copy — treat as "six-figure and rising") | Free browse/search; **Pro $7/mo** (max-bid rec, comps, margin est., Flip Score, unlimited alerts); API: Free 100 calls/mo, Hobby $49/mo 5k calls, Enterprise | Solo — **Ben Wallace** (HN handle `player_piano`), built early 2026, Show HN launch April 2026 hit #1, ~20k visitors in a day. Actively patching bugs live in the HN thread. Alive, growing. | The bar to beat |
| **BidProwl** (`bidprowl.com`) | **27 sources across 4 buckets**: Federal (GSA Auctions, GSA Fleet, DLA Disposition, IRS, US Marshals, HUD HomeStore, USPS Surplus, Treasury), Platforms (GovDeals, GovPlanet, PublicSurplus, Municibid, PropertyRoom, GovLiquidation), Aggregators (HiBid, Proxibid, Purple Wave, Ritchie Bros, JJ Kane), Real Estate (Fannie Mae HomePath, Freddie Mac HomeSteps, Hubzu, Auction.com, CivilView, Auction Network) — **110,000+ live listings** claimed | Yes — **"deal score" 0-100** vs. 90-day median sold price by category+state; **"sold-price comps from 271,000+ closed auctions back to 2018"** (bigger claimed corpus than GovAuctions) | Free tier (Flip Score, 5 email alerts, 50-item watchlist, category-level medians); **Pro $9/mo** (instant alerts, live bidding on closing auctions, full comps + resale-margin est., AI search, demand signals, stealth mode); **Pro+ $29/mo** (API 10k calls/mo vs Free's 1k, CSV export); Enterprise custom. **API pricing beats GovAuctions' Hobby tier** ($29/mo/10k calls vs. $49/mo/5k calls) | **Sam Ojling** (per site copy). Has a public `/developers` API doc, ZIP-radius search (claims "only gov-auction API with ZIP-radius search"), MCP server interface for AI agents. Well-built, feature-parity-or-ahead of GovAuctions on paper. | **Closest direct competitor to GovAuctions** — same shape (search + comps + Flip Score + API), undercuts on API price, has more claimed live listings. Worth revisiting if we build comps. |
| **BidRadar.app** (known, confirmed) | GovPlanet, Municibid, PropertyRoom, HiBid, ShopGoodwill, MaxSold — "more added regularly" | Yes, **Pro-gated only**: "eBay Flip Score" — resale value from eBay *sold* data (not gov-auction sold comps like the other two) | Free ($0, 1 watchlist keyword, 4h delay); **Basic $14.99/mo** ($152.88/yr, 5 keywords, 1h delay); **Pro $29.99/mo** ($305.88/yr, 15 keywords, real-time + eBay Flip Score) | Anonymous (confirmed — no founder/company info anywhere on site, just a copyright line). No API. | Alert/watchlist tool first, search second — narrower than GovAuctions/BidProwl; most expensive of the three at the top tier and has no API, so it doesn't compete for the data-licensing/API angle we care about. |
| **GovernmentAuctions.org** | Broad "100,000+ US & Canada government/police/seized auctions + foreclosures," 170k+ registered members claimed | Not evident — reads as a lead-gen/directory site, not a live-data aggregator | Not disclosed in crawl; historically these directory sites monetize via paid membership/lead-gen, not usage-based API | Registered servicemark of **Cyweb Holdings Inc.** — a legacy directory business, not an indie project. Long-running (predates the current wave). | Old-web directory model, not a real-time aggregator/comps competitor. Different category — SEO/lead-gen, not data product. |
| **AllSurplus** (`allsurplus.com`) | Not an aggregator of *other* sites — it's **one of Liquidity Services' own marketplace brands** (sibling to GovDeals-adjacent Liquidity Services properties), covering business + government surplus sellers (state colleges, city treasuries, etc.) | No | Not a search/comps tool — it's a direct-sale marketplace | Corporate (Liquidity Services), not indie. | Not a comps/aggregator competitor — it's a source, arguably one GovAuctions-class tools should *cover*, not compete with. |
| **Gov Radar** (`govradar.bid`) | **GSA Auctions only** (via gsaauctions.gov) — DoD/NASA/USPS surplus. "Showing 1-100 of 1,080" active listings at crawl time. | No visible sold-comps feature, just current bid + countdown + "AI-powered recommendations" | Not disclosed — appears free | Builder not identified. Listings' end-dates looked suspiciously uniform ("EndsJul 31, 2026" across all 100 shown) — possibly a data-freshness bug or a young/thin scraper. | Small, single-source, no comps, no visible monetization. Early-stage or side-project tier — not a real threat, but shows the category is crowded with GSA-only clones. |
| **USGovBid** (`usgovbid.com`) | N/A — this is **an actual auction house** ("Auction Liquidation Services" division running live auctions for NJ/DE municipalities, school boards, law enforcement), not a search aggregator | Ad hoc — shows individual past sale totals ("SOLD $53,000") but no structured comps dataset | N/A (they take a cut of auctions they run) | Site credits "Verdi Productions" as web designer; active, contactable, listings through Sept 2026. | Not a competitor — it's a *source*, one more auction house among hundreds, not an aggregator. Mis-surfaced by search; noted for completeness. |
| **govauctions.com** (note: distinct domain from **govauctions.app**) | Unknown — WebFetch blocked (HTTP 403); Google Play also lists a "GovAuctions.com - Shop Surplus" app | Unknown | Unknown | Unknown | **Flag only** — a same-name-adjacent `.com` domain exists separate from Ben Wallace's `.app`. Could be a legacy/unrelated brand or a trademark-adjacent risk for anyone naming a product "GovAuctions X." Worth a manual look before we pick our own product name if it's close to "GovAuctions." |
| Apify **"GovDeals Scraper"** | Single-source (GovDeals, also lists GovPlanet coverage in its description) scraper-as-a-service | No — raw structured data only, no comps layer | Apify usage-based (not fetched in detail — out of scope, it's B2B scraping infra not a consumer/reseller product) | Apify marketplace listing (third-party "parseforge" author), active. | Not a direct competitor — it's a data-extraction commodity tool other builders (including possibly us) could buy instead of building scrapers. Relevant as a "buy vs. build" data-sourcing option, not as a rival product. |

### 1.2 Reading on the competitive landscape

- **The category is more crowded than the brief assumed.** Beyond GovAuctions.app and BidRadar.app, there is at minimum one close structural twin (**BidProwl** — same three-part offer: free search → paid comps/Flip-Score → paid API, at a lower API price point) and one legacy directory (GovernmentAuctions.org) that's been doing SEO/lead-gen in this space for years without ever building a real-time data product. Sam Ojling's BidProwl claims **more sources (27) and a bigger sold-comps corpus (271k) than GovAuctions currently claims on its lower-end pages (105k–150k)**, though GovAuctions' own `/about` page has since updated to 180k+, so the two are leapfrogging each other on claimed corpus size — a strong signal this is an active, contested niche, not an open field.
- **Nobody has clearly won yet.** Feature sets across GovAuctions, BidProwl, and BidRadar largely converge on: free tiered search → paid alerts/comps/Flip-Score → paid API. None has an obvious moat beyond "who scraped more sources first and has the cleanest UX." Ben Wallace's advantage so far looks like distribution (HN #1, ~20k visitors day one) more than data breadth.
- **Pricing has compressed fast.** GovAuctions ($7/mo Pro) and BidProwl ($9/mo Pro) are within $2 of each other; BidRadar is priced ~4x higher ($14.99–$29.99/mo) but is watchlist/alerts-first with a narrower source list and no API — a different (higher-friction, higher-willingness-to-pay?) segment.
- **API pricing is the one place BidProwl is meaningfully cheaper**: $29/mo for 10k calls vs. GovAuctions' Hobby $49/mo for 5k calls. If BLACKWHOLE ever sells API access (e.g., to other resellers), that's the number to beat, not GovAuctions'.
- **Solo-builder pattern holds.** GovAuctions = solo (Ben Wallace, visible on HN fixing bugs live). BidRadar = anonymous, likely solo/small. BidProwl = credited to one person (Sam Ojling) with no team page found. This whole category appears to be indie/small-team built on cheap modern infra (see Part 2) — consistent with a $6-49/mo pricing ceiling that wouldn't support a VC-funded team.
- **What nobody in this sweep has**: none of the three real competitors (GovAuctions, BidProwl, BidRadar) appear to integrate directly with a *resale channel* (FB Marketplace, eBay) the way BLACKWHOLE's Listing Engine does. They stop at "find the deal / know the comp" — they don't help you *list* what you bought. That's outside this ticket's scope but worth flagging as a possible differentiation angle for later strategy work, not something to act on here.

---

## Part 2 — Hosting Economics

### 2.1 Assumptions carried in from the brief

- ~29 sources polled at adaptive cadence (faster near auction close, slower otherwise).
- Snapshot table grows **append-only**, estimated **50,000–200,000 rows/day**.
- Comps corpus target: 150k+ (matching GovAuctions' claim) growing over time as auctions close.
- Stack under evaluation: Next.js/Vercel (matches GovAuctions' known stack) + Postgres (Supabase vs. Neon vs. a flat-rate VPS).

### 2.2 Row-growth math (why 50k–200k/day is the right range)

- 29 sources × adaptive polling. A conservative model: each source's *active* listing set gets a fresh snapshot row every poll, active listings per source range from a few hundred (small municipal sites) to tens of thousands (GSA, GovDeals at any given time nationally).
- At the low end: 29 sources × ~2,000 avg active listings × 1 poll/day (slow cadence, off-peak) ≈ **58,000 rows/day**.
- At the high end: high-value/near-closing lots polled every 15-60 min (adaptive cadence spikes near auction close, matching the deals/ module's own `watcher_logic.py` design already in this codebase) pushes the same active set through 3-6x more snapshots on close-heavy days ≈ **170,000-200,000+ rows/day**.
- **This matches the brief's 50k-200k/day estimate.** At a slim row size (~200-400 bytes: ids, price, bid_count, timestamp, status — no images/text duplicated per snapshot, those live in a separate `listings` table updated in place), that's:
  - 50k rows/day × 300B ≈ 15 MB/day ≈ **~5.5 GB/year**
  - 200k rows/day × 300B ≈ 60 MB/day ≈ **~22 GB/year**
- **Conclusion: this is a small-data problem, not a big-data problem.** Even at the high end, one year of raw snapshots is well within a $25/mo Postgres tier. The real cost driver is *compute* (running 29 pollers + classification), not storage.

### 2.3 Postgres options compared

| Option | Storage cost | Compute cost | Notes | Fit |
|---|---|---|---|---|
| **Supabase Free** | 500 MB DB included | Shared CPU, 500 MB RAM, paused after 1 week idle, max 2 active projects | Already the project's current tier (workspace CLAUDE.md §12). **500 MB DB cap will be hit within the first ~1-3 months** of snapshot accumulation at 15-60 MB/day raw, faster once you add TOAST overhead, indexes, and the existing `inventory`/`contacts`/`messages` tables already in this same project. | **Phase 0 only** — fine for prototyping, not for a live snapshot feed. |
| **Supabase Pro** | $25/mo base, 8 GB DB included, then metered per-GB beyond | $10/mo compute credit covers one Micro instance (2-core ARM, 1 GB RAM); bigger instances cost the delta (Small +$5, Medium +$50, Large +$100) | **No scale-to-zero** — you pay for the selected compute tier 24/7 even if pollers idle overnight. 8 GB covers ~1.5-3.5 years of raw snapshots at this volume — comfortable headroom. Studio UI (already the operator's chosen data-viewing tool per workspace CLAUDE.md) is a real point in its favor for solo-operator ergonomics. | **Best fit for the 10-source and GovAuctions-scale rows** if we're staying on the *same* Supabase project the rest of BLACKWHOLE already uses — one bill, one dashboard, Studio browsing for free. |
| **Neon** | $0.35/GB-month storage (down 80% post-Databricks acquisition) | $0.106/CU-hr (Launch) or $0.222/CU-hr (Scale); **scale-to-zero after 5 min idle** — compute cost drops to ~$0 between poller runs | Free tier: 100 CU-hrs/mo, 0.5 GB storage, autoscale to 2 CU. At our snapshot volumes (~22 GB/yr high end), storage cost alone would be **~$0.64-7.70/mo** depending on how many months of history retained; compute is genuinely pay-per-second if pollers run in short bursts rather than a long-lived daemon. | **Cheapest at low/bursty compute usage** (e.g., a cron-style poller that wakes, writes, sleeps) — but it's a *second* Postgres the operator would have to manage alongside the existing Supabase project, breaking the "one helper, one path" workspace rule (CLAUDE.md §14) unless deliberately scoped as a separate, standalone product (which this aggregator likely is — it's not BLACKWHOLE's inventory, it's a new SaaS). |
| **$6-12/mo flat VPS + self-hosted Postgres** (Hetzner CX22 ≈ €4.35-4.59/mo ≈ **$5/mo**; DigitalOcean/Linode equivalent ≈ **$6-12/mo**) | Flat, included in VPS price (40 GB NVMe on Hetzner CX22, 2 vCPU/4 GB RAM) | Flat — no metering, no surprise bills, no scale-to-zero cold-starts | Everything (pollers, Postgres, cron, classification calls) can run on **one box**. No egress-fee surprises (the exact failure mode that killed Supabase Storage for this operator's *other* project — see `listing_automation/CLAUDE.md` "Lot photos" section, R2 migration after a 402 Supabase quota-blowup). Ops burden: you own backups, upgrades, monitoring — no managed-service safety net. | **Cheapest sane architecture for a standalone new SaaS at Phase-0/10-source scale**, *if* the operator is comfortable owning a VPS. Given this operator already runs Playwright/Chromium daemons and cron-ish jobs locally and on Render (see `deals/` cron services in listing_automation), a small Hetzner/DO box for this new product is well within existing skill range. |

**Named cheapest-sane pick by scale:**
- **Phase 0 (prototype, <1 GB data, low traffic):** Supabase Free tier, reusing patterns already known from this workspace. $0/mo.
- **10-source aggregator (validate before over-building):** **Hetzner CX22 VPS ($5-6/mo) running Postgres + poller cron + a lightweight API**, OR Neon free/low-usage tier if compute stays bursty. Either beats paying $25/mo Supabase Pro before there's revenue to justify it. If the operator wants zero ops burden and is willing to pay for it, Supabase Pro ($25/mo) is the "just works, same tooling as everything else" choice.
- **GovAuctions-scale (29 sources, six-figure comps corpus, real traffic):** Supabase Pro (Small/Medium compute tier, ~$30-75/mo) if staying managed, or a **$12-24/mo VPS with a second box for redundancy** if self-hosting. At this scale the DB itself is still cheap (low-tens of GB); the real cost driver becomes hosting the classification/polling compute and, per below, LLM token spend.

### 2.4 Vercel / Next.js hosting

| Tier | Cost | Included | Notes |
|---|---|---|---|
| Hobby | $0 | Enough for prototyping, non-commercial | Vercel's ToS bars commercial use on Hobby — not viable once monetized (Stripe checkout live). |
| **Pro** | **$20/mo per seat** + $20/mo usage credit | 1 TB bandwidth, 1M function invocations, 1000 GB-hrs serverless execution, 10M edge requests, 6000 build minutes, 5000 image optimizations | Overages: bandwidth $0.15/GB past 1 TB, functions $0.60/million past 1M. **At GovAuctions' claimed traffic (~20k visitors in one HN spike day, presumably far less on a normal day), 1 TB/mo bandwidth is generous headroom** for a text+JSON-heavy site that hotlinks images from source CDNs (per known facts — GovAuctions never re-hosts images, which is exactly what keeps its own bandwidth bill low). This hotlinking choice is itself a cost-control decision worth copying if we build a similar aggregator. |
| Enterprise | Custom | SLA, SSO, higher limits | Not relevant at any scale discussed here. |

**Read-through: Vercel Pro's $20/mo/seat + $20 credit is close to a wash for a solo builder at low-to-moderate traffic** — the included credit likely absorbs normal function/bandwidth usage for a JSON-API-plus-light-pages site, especially if (like GovAuctions) images are never proxied through your own infra.

### 2.5 Stripe fees

- Standard US card rate: **2.9% + $0.30** per transaction.
- Subscriptions add **+0.7% of billing volume** on top (covers recurring billing, dunning, retries) — so a $7/mo or $9/mo charge like GovAuctions/BidProwl's Pro tier nets roughly: `$7.00 − ($7.00×0.036) − $0.30 ≈ $6.45` (using combined ~3.6% + $0.30). At $9/mo it's `$9.00 − ($9.00×0.036) − $0.30 ≈ $8.38`.
- International cards +1.5%, currency conversion +1%, disputes $15 flat — minor at this scale unless fraud/chargebacks spike.
- Volume discount kicks in at $80k+/month processed (2.2% + $0.30) — far beyond Phase 0/10-source scale; not relevant yet.
- **No fixed monthly fee** on Stripe's standard plan — cost is fully variable, which matters for a pre-revenue "Phase 0" build: zero subscribers = zero Stripe cost.

### 2.6 LLM classification cost per 1,000 listings

Assume classification = one LLM call per listing (category/condition/quantity extraction — same shape as this codebase's existing `deals/classify.py` Gemini classifier and `listing_automation`'s `default_extractors()`), roughly 500-1,000 input tokens (title+description+category context) and ~50-150 output tokens (structured JSON) per listing.

| Model | Input $/1M tok | Output $/1M tok | Est. cost per 1,000 listings (750 in / 100 out avg) | Notes |
|---|---|---|---|---|
| **Gemini 2.5 Flash-Lite** | $0.15 | $1.25 | ~$0.11 + $0.125 = **~$0.24/1k listings** | Cheapest paid tier with real quality; already this codebase's precedent (`GEMINI_API_KEY` used across `automation/llm/gemini.py` and `deals/classify.py`). Flash-Lite is the volume-classification workhorse. |
| **Gemini 2.5 Flash** (current default in `deals/`) | $0.30 | $2.50 | ~$0.225 + $0.25 = **~$0.48/1k listings** | Already in production use in this workspace for deal classification — known-good baseline. Deprecates Oct 2026 per Google's roadmap; migration to 3.1 Flash-Lite ($0.25/$1.50, ~**$0.34/1k**) or 3 Flash ($0.50/$3.00, ~**$0.68/1k**) is a live near-term action item if this pipeline is still running post-October. |
| **GPT-4o-mini** | $0.15 | $0.60 | ~$0.11 + $0.06 = **~$0.17/1k listings** | Cheapest of the frontier-lab options; this codebase already has an `automation/llm/openai.py` extractor as the standard A/B secondary. |
| **Groq (Llama free tier)** | $0 | $0 | **$0/1k listings** (within free-tier rate limits) | Free tier: 30 req/min, 1,000-14,400 req/day depending on model, 6k-30k tokens/min. **At 50k-200k snapshot rows/day this free tier is nowhere near sufficient if classifying every snapshot** (14,400/day ceiling on the most generous model vs. 50k-200k rows), but classification doesn't need to run per-snapshot — it only needs to run **once per distinct listing** (not per price-check poll), which is a much smaller number. If distinct new/changed listings across 29 sources run in the low thousands/day, Groq's free tier can plausibly cover it at $0, same rationale already written into this workspace's plan (workspace CLAUDE.md §5, "Groq registered as a fourth option — free tier wins for high-volume inbox"). |

**Bottom line on LLM cost:** classification is a rounding error next to hosting/DB costs at this scale. Even 200,000 listings/month (well above what "new distinct listings" would realistically be, since most of the 50k-200k/day is *repeat snapshots of the same lots*, not new items) classified with GPT-4o-mini costs **~$34/month**. The actually-new-listings-per-day count (likely low hundreds to low thousands across 29 sources) makes this **$1-10/month** in practice, or **$0** if kept inside Groq's free tier.

### 2.7 Monthly cost table — three scales

| Line item | **Phase 0** (prototype, our own use, <10 sources, low traffic) | **10-source aggregator** (early validation, small public traffic) | **GovAuctions-scale** (~29 sources, six-figure comps, real daily traffic) |
|---|---|---|---|
| Hosting (Next.js/frontend+API) | Vercel Hobby — **$0** (non-commercial only; switch to Pro the moment Stripe goes live) | Vercel Pro — **$20** (seat) — usage covered by included credit | Vercel Pro — **$20-40** (1-2 seats; possible modest overage on bandwidth if images ever get proxied instead of hotlinked) |
| Database | Supabase Free (reuse existing project) — **$0** | Hetzner CX22 VPS running Postgres — **$5-6**, *or* Supabase Pro if managed-only preferred — **$25** | Supabase Pro (Small/Medium compute) — **$30-75**, *or* self-hosted VPS Postgres w/ backups — **$12-24** |
| Compute for pollers/cron (if not co-located on DB VPS) | **$0** (run on operator's existing laptop/Render free tier) | **$0-7** (folded into the same VPS, or Render free/starter cron) | **$7-25** (dedicated small VPS or Render paid cron services — this codebase already runs `deals-discover`/`deals-watch`/`deals-analyze`/`deals-digest` as four separate Render cron services, a directly reusable pattern) |
| LLM classification | **~$0-1** (low volume, Groq free tier or a few hundred GPT-4o-mini calls) | **~$1-10** (GPT-4o-mini or Gemini Flash-Lite on low-thousands new listings/day) | **~$10-35** (same per-listing rate, more sources = more new listings/day; still small relative to hosting) |
| Stripe | **$0** (no subscribers yet) | **~2.9-3.6% + $0.30/txn**, no fixed fee — scales with revenue, not a fixed cost | Same variable rate — at meaningful subscriber counts this becomes real money but it's revenue-proportional, not a fixed infra cost |
| **Total fixed infra (excl. Stripe's variable cut)** | **$0/mo** | **~$26-43/mo** | **~$67-175/mo** |

**Cheapest sane architecture named, per scale:**
- **Phase 0:** Supabase Free + Vercel Hobby + Groq free-tier classification. **$0/mo.** Exactly matches this workspace's existing pattern (already on Supabase, already has Gemini/OpenAI/Groq extractor abstractions in `listing_automation/automation/llm/`) — no new tooling to learn.
- **10-source aggregator:** **A single $5-6/mo Hetzner CX22 VPS running Postgres + the poller cron + a small API**, paired with **Vercel Pro ($20/mo)** for the public Next.js frontend only (keep frontend/hosting and data/compute on separate, appropriately-priced tiers rather than paying Supabase Pro's $25/mo floor before there's a reason to). **~$26-33/mo total**, cheaper than matching GovAuctions/BidProwl's own likely spend at this scale.
- **GovAuctions-scale:** Either **stay on the VPS pattern and scale the box** (Hetzner CPX-series, $12-24/mo, still Postgres-on-a-box) for cost discipline, or **move to Supabase Pro** once the operator-ergonomics win (Studio UI, one connection string, matches the rest of BLACKWHOLE's stack per workspace CLAUDE.md §14) outweighs the ~$30-50/mo premium over self-hosting. Given this operator's stated preference for "one helper, one path" (§14) and already-paid-for Supabase familiarity, **Supabase Pro is the pragmatic pick at this scale even though it's not the cheapest possible number** — self-hosted Postgres is the "cheapest sane" answer only if ops time is free, which it isn't for a single operator already running four other systems.

---

## Sources

- [GovernmentAuctions.org](https://www.governmentauctions.org/)
- [AllSurplus](https://www.allsurplus.com/en) / [Liquidity Services marketplaces](https://liquidityservices.com/buy-surplus)
- [BidProwl — home](https://bidprowl.com/) / [About](https://bidprowl.com/about) / [Developers/API](https://bidprowl.com/developers)
- [BidRadar.app](https://bidradar.app/)
- [Gov Radar](https://govradar.bid/)
- [USGovBid](https://www.usgovbid.com/)
- [GovAuctions.app — About](https://govauctions.app/about)
- [Show HN: GovAuctions lets you browse government auctions at once](https://news.ycombinator.com/item?id=47662945) (Algolia HN API mirror)
- [Vercel Pricing](https://vercel.com/pricing) / [Fluid compute pricing docs](https://vercel.com/docs/functions/usage-and-pricing)
- [Supabase Pricing overview (via UI Bakery summary)](https://uibakery.io/blog/supabase-pricing)
- [Neon Pricing breakdown (via swyftstack)](https://swyftstack.com/blog/neon-pricing-explained)
- [Hetzner Cloud CX22 pricing (via vpsfor.dev)](https://vpsfor.dev/posts/hetzner-cx22-pricing-2026/)
- [Stripe fees explained 2026 (via checkoutpage.com)](https://checkoutpage.com/blog/stripe-processing-fees)
- [Gemini 2.5 Flash / Flash-Lite pricing (via pricepertoken.com)](https://pricepertoken.com/pricing-page/model/google-gemini-2.5-flash)
- [GPT-4o-mini pricing (via devtk.ai)](https://devtk.ai/en/models/gpt-4o-mini/)
- [Groq API free tier limits 2026 (via Grizzly Peak Software)](https://www.grizzlypeaksoftware.com/articles/p/groq-api-free-tier-limits-in-2026-what-you-actually-get-uwysd6mb)
