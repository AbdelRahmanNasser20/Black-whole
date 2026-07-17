# BLACKWHOLE-22 — Firecrawl / Browserbase cloud-browser pilot

**Status:** EVAL / pilot scaffold. Ships OFF. No paid account, no live calls in CI.
**Feeds:** BLACKWHOLE-20 (automation build) · child of BLACKWHOLE-5 (multi-platform push).
**Date:** 2026-07-17.

## TL;DR — recommendation

**GO, but scoped: pilot Browserbase for the *posting* leg, keep local Playwright as
default. Firecrawl is a NICE-TO-HAVE for the *scrape* leg, not the thing to buy first.**

The ticket's real risk is the **posting** side — driving Facebook Marketplace / eBay
from a browser that gets fingerprinted or contended when the local Chrome profile is
busy (the facebook-crm poller shares that profile). Browserbase solves exactly that:
it is a remote, stealthed, fresh-IP Chrome you drive with the **same Playwright code
we already have** — literally one `connect_over_cdp(url)` swap. Firecrawl is a
scrape/extract API; it helps the GovDeals/PublicSurplus *reading* leg (where Akamai
403s headless Chromium today), but that leg mostly already works via the internal
JSON APIs, so it's lower value.

Expected cost at our volume is **~$20/mo (Browserbase Developer)**, well inside the
epic's approved $20–50/mo. Decision needed from Abdel: whether to sign up for the
Browserbase Developer plan and (optionally) a Firecrawl Hobby plan to run the live
one-lot cycle the acceptance criteria call for. **No account has been created — Abdel
does all signups.**

---

## Why this matters (the de-risk)

Today's pipeline (`README.md`, `ARCHITECTURE.md`): **GovDeals/PublicSurplus scrape →
dewatermark → Facebook Marketplace + eBay drafts.** Two pain points:

1. **Scrape leg:** "GovDeals *must* be scraped non-headless (Akamai 403s headless
   Chromium)" (`README.md:150`). We run a real desktop Chrome to dodge Akamai.
2. **Post leg:** FB/eBay drafts are posted from a persistent Chrome profile that is
   **shared with the facebook-crm poller** — only one browser can hold that profile at
   a time (`automation/browser.py::_clear_stale_profile_lock`). Contention + a single
   residential fingerprint is the fragility the ticket wants to de-risk.

A cloud browser gives us: (a) a fresh IP + managed stealth fingerprint per session,
(b) concurrency without profile-lock fights, (c) no "keep a Mac awake with a logged-in
Chrome" dependency for cloud/cron runs.

---

## Firecrawl vs Browserbase — they solve different halves

| | **Firecrawl** | **Browserbase** |
|---|---|---|
| What it is | Scrape/crawl/extract **API** — URL in, clean markdown/JSON out | Remote **headless Chrome** you drive with Playwright/Puppeteer over CDP |
| Best for | The **scrape/read** leg (source listings → structured data) | The **post/interact** leg (fill + submit FB/eBay forms) — and hard scrapes |
| Fits our code | New `SiteAdapter` (`deals/adapters/`) swapping `requests.post` for a Firecrawl call | Drop-in: our existing Playwright/Patchright posting code, `connect_over_cdp(url)` |
| Stealth / anti-bot | Stealth Mode = **5 credits/page** (vs 1) for Cloudflare-class sites | Basic Stealth on paid tiers; Advanced Stealth on Scale (custom) |
| Form submission | Has an Interact API (click/type, `cdpUrl`) at **2 credits/browser-min**, but newer/less battle-tested for a full posting flow | Native — it's a real browser; posting is just our current script |
| Captcha | Bundled in stealth path | Auto-captcha solving on paid tiers |
| Session model | Stateless scrape; interactive session is opt-in | Persistent session, 15-min cap on free, longer on paid |

**Key takeaway:** Browserbase is the natural fit for the leg the ticket is worried
about (posting from a fingerprint-prone browser) because it keeps our Playwright code.
Firecrawl is the better tool for turning source pages into clean data, but that leg is
already served by the internal JSON APIs, so it's incremental.

---

## Pricing (July 2026)

### Browserbase — billed on browser-hours

| Plan | $/mo | Browser-hours | Overage | Concurrency | Proxy incl. | Stealth |
|---|---|---|---|---|---|---|
| Free | $0 | 1 hr | — | 3 | none | none |
| **Developer** | **$20** | **100 hr** | **$0.12/hr** | 25 | 1 GB (+$12/GB) | Basic |
| Startup | $99 | 500 hr | $0.10/hr | 100 | 5 GB (+$10/GB) | Basic |
| Scale | custom | 500+ | usage | 250+ | usage | Advanced |

Residential proxies bill separately (~$8/GB); datacenter/stealth proxy ~$0.30/GB.

### Firecrawl — billed on credits (scrape/crawl/map = 1 credit/page)

| Plan | $/mo (annual) | Credits/mo | ~$/page |
|---|---|---|---|
| Free | $0 | 1,000 | $0 |
| **Hobby** | **$16** | 5,000 | ~$0.0032 |
| Standard | $83 | 100,000 | ~$0.00083 |
| Growth | $333 | 500,000 | ~$0.00066 |

Gotchas: **Stealth Mode = 5 credits/page** (Cloudflare-class sites); Search = 2/10
results; Interact = 2/browser-min; **credits don't roll over**.

---

## Cost estimate at our volume

Assume a working month: **~50 lots posted**, each lot = 1 source scrape + ~5 min of
browser time to fill FB + eBay drafts (generous). Round up to ~200 lots for headroom.

**Browserbase (the recommended buy):**
- 200 lots × 5 browser-min = ~17 browser-hours/mo → **well within the 100 hr Developer bucket.**
- Proxy: a listing flow is light on bandwidth; ~1 GB included covers a pilot month.
- **→ $20/mo flat.** Even a heavy month rarely touches overage. Per-listing marginal
  cost ≈ **$0.01** in browser-time.

**Firecrawl (optional scrape add-on):**
- 200 lots + detail pages ≈ 400–1,000 pages/mo. GovDeals behind Akamai likely needs
  Stealth (5 credits/page) → ~2,000–5,000 credits/mo.
- **→ Free tier may cover a pilot; Hobby ($16/mo) if we lean on it.** Per-listing
  marginal cost ≈ **$0.02–0.08** with stealth.

**Combined pilot ceiling: ~$20–36/mo** — inside the epic's approved $20–50/mo, and
squarely meets the "4 hours of operator time → 4 minutes is worth its fee" bar.

---

## What was built in this PR (thin PoC, ships OFF)

`automation/cloud_browser.py` — one module, two seams, both hard no-ops unless a flag
**and** a key are set:

- `resolve_cloud_cdp_endpoint()` → when `LISTING_CLOUD_BROWSER=browserbase` +
  `BROWSERBASE_API_KEY`, creates a Browserbase session and returns its CDP
  `connectUrl`. `automation/browser.py::_try_connect_cdp` now tries that URL **first**,
  then falls back to the local poller CDP, then to launching local Chrome — so with no
  key the behavior is byte-for-byte what it is today. Cloud failures degrade to local,
  never raise.
- `firecrawl_scrape(url)` → thin Firecrawl REST wrapper for the scrape leg; raises
  `CloudBrowserNotConfigured` unless `FIRECRAWL_ENABLED=1` + `FIRECRAWL_API_KEY`.

Transport is `requests` (already a dep) — no vendor SDK, no new dependency. Tests
(`tests/test_cloud_browser.py`, 11 cases) mock `requests` and pin the OFF-by-default,
no-network contract, mirroring the `PUBLICSURPLUS_ALLOW_BROWSER` guard.

### Env vars (all unset = OFF)

```bash
# Browserbase (post leg)
LISTING_CLOUD_BROWSER=browserbase      # empty = local browser (default)
BROWSERBASE_API_KEY=bb_...
BROWSERBASE_PROJECT_ID=proj_...
BROWSERBASE_STEALTH=1                   # optional: fingerprint + proxies

# Firecrawl (scrape leg)
FIRECRAWL_ENABLED=1
FIRECRAWL_API_KEY=fc_...
```

---

## Acceptance criteria — status

- [x] Written go/no-go recommendation with cost + reliability numbers (this doc).
- [x] Thin, flag-gated integration mergeable with no paid account; tests green.
- [ ] **One full live listing cycle** via the cloud service — **BLOCKED on Abdel
      signing up** for Browserbase (and optionally Firecrawl) and dropping keys in
      `.env`. Once keys exist, flip `LISTING_CLOUD_BROWSER=browserbase` and run one lot
      end-to-end to capture real success/block rate + per-listing cost.

## Decisions for Abdel

1. **Sign up for Browserbase Developer ($20/mo)?** — recommended; this is the actual
   de-risk. Add `BROWSERBASE_API_KEY` + `BROWSERBASE_PROJECT_ID` to `.env`.
2. **Firecrawl Hobby ($16/mo) or stay on Free?** — optional; only if we want Firecrawl
   on the scrape leg. Free tier is enough to trial it.
3. Then I run the one-lot live cycle and fill in the measured numbers, feeding
   BLACKWHOLE-20's build/no-build call.

## Sources

- Browserbase pricing — https://www.browserbase.com/pricing
- Browserbase Playwright/CDP connect — https://docs.browserbase.com/
- Firecrawl pricing — https://www.firecrawl.dev/pricing
- Firecrawl docs (actions / interact) — https://docs.firecrawl.dev/introduction
