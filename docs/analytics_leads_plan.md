# Lead analytics plan — does the site work turn into leads and sales?

**Summary**
- Every lead now carries *where the buyer came from* (first touch), stored in our own DB — true even if a browser blocks scripts.
- Cloudflare shows the **visits** (Web Analytics) and the **lead events** (Zaraz). Our weekly script joins the two per channel.
- Baseline (last 30 days, 2026-10-07): ~5,980 visitor-days, **8 leads** (all freight quotes), **0 deposits**. Everything else was 0. That is the line to beat.

Everything below is the detail.

---

## 1. What a lead and a sale are, in data

- **A lead** = one row in any of these, created by a buyer on black-whole.com:
  - `inquiries` — the contact form (lot pages, home, /sell).
  - `subscribers` — the "alert me about new chairs" form.
  - `freight_quotes` with `source='storefront'` — the freight widget on a lot page (quoted *or* unquotable: both are a person asking).
  - `deposits` (any status) — a buyer started a Stripe checkout.
  - `site_visits` rows with `path = '/_event/tel_click'` or `'/_event/mailto_click'` — a click on the phone or email link (the browser posts a tiny beacon to `POST /event`; no new table).
- **A sale** =
  - `deposits.status IN ('paid','refunded')` with `paid_at` set — money actually moved through Stripe (not live yet: `STRIPE_SECRET_KEY` unset).
  - Until Stripe is live, a sale is recorded by hand: `freight_quotes.status='won'` on the Sales tab, or a row in `sales` (`thread_url, buyer_name, listing_lot_id, listing_title, sold_at`). The report prints these on one line (`HAND-RECORDED SALES`) — **counted, not attributed**: the `sales` table has no source column, and most real sales close in a Facebook chat or at pickup, off-site. If you want SEO → sale answered, note the buyer's channel (ask them, or copy the lead's `attr_*`) in the quote note when you mark it won.
- **Attribution** = the five `attr_*` columns on each lead table (migration 022):
  - `attr_source`, `attr_medium`, `attr_campaign` — the `utm_*` tags on the link the buyer FIRST arrived on.
  - `attr_referrer` — the host that sent them (e.g. `m.facebook.com`), empty = typed/bookmark.
  - `attr_landing` — the first page they saw.
  - "First touch": `site.js` writes this to `localStorage` once (90-day life) and sends it with every lead POST. A buyer who found us on Google on Monday and came back by typing the address on Friday is still a Google lead.
  - Raw in, bucketed out: the columns hold what the browser saw; the channel label is computed by `automation/attribution.py::channel()` at report time, so fixing a rule never needs a backfill.

## 2. The funnel, per channel

| Step | Where it is counted | Note |
|---|---|---|
| Visits | `site_visits` (one row per public page view) → **headline number: Cloudflare Web Analytics** | our log is bot-heavy (only a UA regex filter); Cloudflare filters bots |
| People | `count(DISTINCT visitor)` = visitor-days | one person per day, per channel |
| Lot page views | `site_visits.lot_id IS NOT NULL` | "did they look at a lot" |
| Lead events | the five lead sources above | server-side truth |
| Checkout started | `deposits` created | |
| Deposit paid | `deposits.paid_at` in window | the only true sale signal |

- **Conversion %** = leads ÷ people (visitor-days). Views would make it look 100× worse than it is.
- **Channels** (`attribution.channel()`): `google_organic` (google.* referrer, no tag), `google_feed` (`utm_source=google&utm_medium=feed` — the Merchant Center feed), `google_ads`, `facebook` (facebook/instagram/messenger referrer or `utm_source=facebook`), `craigslist`, `ebay`, `email` (`utm_source=apollo|smartlead|…`), `ai_assistant` (chatgpt/perplexity/claude referrers), `direct` (no referrer, or one of our own domains), `referral` (any other site), `utm:<tag>` (a tag we have no rule for), `unattributed` (lead rows written before migration 022).
- Typed short links already carry tags: `black-whole.com/cl` = craigslist, `/fb` = facebook, `/ou`, `/nd`, `/ig`, `/call`, `/card` (`automation/web/short_links.py`).

## 3. Cloudflare — what to click (≤5 steps each)

**A. Web Analytics (visits by referrer) — already on; just read it**
1. dash.cloudflare.com → **black-whole.com**.
2. Left menu **Analytics & Logs → Web Analytics**.
3. If it says Enable, click **Enable** (Cloudflare injects the beacon; nothing to deploy).
4. Weekly read: **Referrers** card (google, facebook, craigslist, ebay) and **Paths** (which lot pages).
5. Set the date picker to the same window as the report (7 or 30 days) and add the filter **Exclude Bots = Yes** — without it the visit number includes the same scanners our own log does.

**B. Zaraz (lead events in the Cloudflare dashboard) — free, 1,000,000 events/month**
1. dash.cloudflare.com → **black-whole.com** → **Tag Management** (Zaraz).
2. Click **Get started** if it is the first time, then add **one tool** — Cloudflare only injects the `zaraz` script when at least one tool is enabled ([Zaraz FAQ](https://developers.cloudflare.com/zaraz/faq/)). Cheapest: **Add tool → Custom HTML**, name it `lead-counter`, leave the HTML empty, and give it a **trigger** of type *Track event* with event name `lead` (add a second trigger for `checkout_start`). Save.
3. **Settings → "Auto-inject script"** must be ON (it is the default). From then on Cloudflare adds `zaraz` to every page it proxies.
4. The site already calls `zaraz.track('lead', {kind, lot_id, source})` on every contact / subscribe / freight-quote / tel / mailto, and `checkout_start` when Stripe opens. Until steps 2–3 those calls are a silent no-op.
5. Read it at **Zaraz → Monitoring → Events**: counts by event name (`lead`, `checkout_start`). The properties (`kind` / `lot_id` / `source`) are not shown there — they are visible in the browser with `zaraz.debug('<key>')` (Settings → Debug key). The per-channel join is the weekly script (§4), not Zaraz.

**C. Verify within 24 h**
1. Open black-whole.com from a Google search result on your phone; open a lot; click the phone number. (The phone/email links arrive with PR #127's footer and PR #128's `/about` — before those merge, use the freight form on a lot page instead: it is a `lead` too.)
2. Cloudflare → Web Analytics → Referrers shows `google`.
3. Cloudflare → Zaraz → Monitoring → Events shows 1 `lead`.
4. `.venv/bin/python scripts/lead_funnel_report.py --days 1` shows 1 click under `google_organic` (after migration 022 for form leads; clicks work before it).

Why this and not more:
- **Cloudflare Web Analytics has no custom events** ([FAQ](https://developers.cloudflare.com/web-analytics/faq/)), so a lead cannot be seen there. **Zaraz** has `zaraz.track()` ([docs](https://developers.cloudflare.com/zaraz/web-api/track/)), a **Monitoring** page that counts events by name ([docs](https://developers.cloudflare.com/zaraz/monitoring/)), and every feature on every plan with 1M free events/month ([pricing](https://developers.cloudflare.com/zaraz/pricing-info/)). At our volume (~500k logged views/month, mostly bots that never run JS) it stays free.
- **Workers Analytics Engine** rejected: needs a Worker, is queried by GraphQL/SQL, has no dashboard funnel — more code for a number our own DB already has.
- **Google Analytics** stays out (cookie banner for no extra signal), as decided in `docs/seo.md`.

## 4. The weekly report

```
.venv/bin/python scripts/lead_funnel_report.py --days 7        # text
.venv/bin/python scripts/lead_funnel_report.py --days 30 --json
```
- Read-only SQL (no writes). Columns: `visits`, `people` (visitor-days), `lot_views`, then leads by kind (`contact`, `subscr`, `freight`, `clicks`, `checkout`), `leads`, `conv%`, `paid`, `paid_usd`; then the lot pages with the most lead events.
- How to read it: one line per channel, sorted by leads. **Compare `leads` week to week per channel** — a channel whose `people` go up but `leads` do not is traffic, not buyers.
- Leads show as `unattributed` until the migration is applied (operator gate — do it once):
  ```
  .venv/bin/python scripts/apply_sql.py scripts/sql/022_lead_attribution.sql
  ```
  Additive, idempotent, nullable varchar(200) columns; the app needs no restart (it re-probes until it sees the column).
- The `/freight-estimate/email` legacy endpoint stores no attribution (nothing we serve calls it any more).

## 5. Baseline — last 30 days, run 2026-10-07 (before migration 022)

```
LEAD FUNNEL — last 30 days (per first-touch channel)
people = visitor-days (one person per day, per channel — a person who arrives two ways counts twice); conv% = leads / people.
site_visits is NOT bot-filtered beyond a UA regex: use Cloudflare Web Analytics for the headline visit number, this report for leads.

channel                                         visits     people  lot_views    contact     subscr    freight     clicks   checkout      leads      conv%       paid   paid_usd
-------------------------------------------------------------------------------------------------------------------------------------------------------------------------------
unattributed                                         0          0          0          0          0          8          0          0          8       0.00          0          0
direct                                          465974       5660       2381          0          0          0          0          0          0       0.00          0          0
referral                                           170        170          0          0          0          0          0          0          0       0.00          0          0
google_organic                                     130         29          8          0          0          0          0          0          0       0.00          0          0
email                                               85         50         85          0          0          0          0          0          0       0.00          0          0
facebook                                            70         49         54          0          0          0          0          0          0       0.00          0          0
ai_assistant                                        15         12          5          0          0          0          0          0          0       0.00          0          0
duckduckgo                                           6          5          0          0          0          0          0          0          0       0.00          0          0
bing                                                 3          3          0          0          0          0          0          0          0       0.00          0          0
utm:scriptsvgonloadconfirmxsss-utm_source18          1          1          0          0          0          0          0          0          0       0.00          0          0
TOTAL                                           466454       5979       2533          0          0          8          0          0          8       0.13          0          0

NOTE: leads are 'unattributed' for inquiries, subscribers, freight_quotes, deposits — migration 022 is not applied — run .venv/bin/python scripts/apply_sql.py scripts/sql/022_lead_attribution.sql

LOT PAGES WITH MOST LEAD EVENTS — last 30 days
  31225              4
  folder:ATL_Grey_blueish_chairs_399     2
  gd-28859-2863      1
  gd-420-9312        1
```

What it says:
- **8 leads in 30 days, all freight-quote requests; 0 contact-form, 0 alert signups, 0 deposits.** None attributed yet (columns do not exist until 022 runs).
- **466k "direct" views are bots** (no referrer, no tag, real-looking UAs; 1,479 of them arrive via the `bwliquidation.com` redirect domains). Real human traffic is the small rows: ~130 Google organic, ~85 email (Apollo tags), ~70 Facebook, ~15 AI assistants. Cloudflare Web Analytics is the number to quote for visits.
- The 170 `referral` rows are mostly port-scanner hits on `blackwholeliquidation.com:8080` etc. — noise, ignore.
- The `utm:…xsss…` row is someone probing with a script tag in `utm_source`; the label is sanitised on purpose.
- Lot `31225` (Boise) is where the leads happen.

Target after the SEO / content / city-page work lands: `google_organic` people ↑ **and** `leads` in that row ↑. If the second does not move in 4–6 weeks, the pages bring readers, not buyers.

## 6. Keep this true in the follow-up PRs

- `visits.track()` only logs paths in `visits._TRACKED_EXACT` / `_TRACKED_PREFIX` (`/`, `/listings`, `/listings/*`). **TODO for the city-landing-page PR: add the new prefix (e.g. `/chairs/`) to `_TRACKED_PREFIX`** or those pages never count as visits. Content/E-E-A-T pages (`/about`, guides) likewise if you want them in the funnel.
- First-touch capture needs only `site.js` on the page; every template that includes `/static/site/site.js` is covered (landing, listings, listing_detail, sell, map, reserve). A new page that loads it is covered automatically.
- Lead POSTs must keep sending `attribution` (site.js does it in one place per form); any new lead endpoint takes `attribution=attribution.from_payload(payload)` and passes it to its writer.
- Nothing here sends anything: `MAX_SENDS_PER_DAY` / `MIN_SECONDS_BETWEEN_SENDS` are untouched. Read + attribution only.
