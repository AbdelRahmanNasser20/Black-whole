# SEO Pulse — a BrightEdge-shaped SEO loop for black-whole.com (design)

**Summary**
- BrightEdge = five things: a keyword database, rank / share-of-voice tracking, a site audit, Search-Console-aligned page recommendations, and automatic on-page fixes. Price: ~$30k+/yr. We copy the loop, not the product.
- SEO Pulse = one command (`scripts/seo_pulse.py run`) that audits every sitemap page, pulls Search Console data, expands keyword ideas from Google autocomplete, and writes one ranked recommendation list the operator acts on. Trend comes from a snapshot per run.
- Nothing auto-edits the site. BrightEdge's "Autopilot" is replaced by recommendations + optional LLM drafts the operator pastes in. That is the 10 % the operator keeps.

## What BrightEdge does → what we do

| BrightEdge module | What it is | SEO Pulse equivalent | Data source |
|---|---|---|---|
| Data Cube | keyword + SERP database | `keywords`: seed terms × cities → Google autocomplete suggestions; plus every query Search Console already shows us for | Google autocomplete (free, no key) + GSC |
| Share of Voice / rank tracking | your positions vs competitors over time | per-query position + impressions snapshot per run; movers table vs the previous run. No competitor ranks (that needs paid SERP data) | GSC |
| ContentIQ | technical + content site audit | `audit`: every sitemap URL → status, redirect, title/meta length, H1 count, canonical, noindex, JSON-LD validity, word count, images without alt, inbound links, orphans, duplicate titles | own crawl of the live site |
| Recommendations (GSC-aligned) | "this page should target this keyword, fix this title" | `recommend`: striking-distance queries (pos 4–20), content gaps (impressions, no page covers it), CTR gaps, audit issues ranked by the page's impressions | audit ⨝ GSC |
| Copilot | generative drafts of titles/descriptions/FAQ | `--draft`: Groq `openai/gpt-oss-20b` drafts title ≤60 / meta ≤155 / H1 / 3 FAQs for the top recommendations | GROQ_API_KEY (optional) |
| Autopilot / Site Agent | auto internal links, image, mobile fixes | **not copied.** Internal links are already templated (nearby lots, city pages). Fixes stay operator-approved | — |
| AI-search visibility | "is ChatGPT citing you" | not copied, no data source | — |

## Goals
- One command, one markdown report, summary first. Runs on the laptop or as a weekly Render cron (Telegram summary).
- Zero new infrastructure: no new table, no migration. Snapshots are JSON files (`SEO_PULSE_DIR`, default `~/.listing_automation/seo_pulse/`).
- Degrades: no Search Console access → audit + keyword ideas still run and the report says what is missing.

## Non-goals / anti-goals
- No auto-edits to templates or inventory rows. No auto-publishing of drafted copy.
- No competitor rank scraping (Google blocks it; paid SERP APIs are a later decision).
- No new admin tab. The report is a file + a Telegram line.

## Search Console access (the one thing the operator has to do once)
Three ways, in order of effort:
1. **Export from the UI** (30 s): Search Console → Performance → Export → Download CSV → pass the zip/folder: `--gsc ~/Downloads/black-whole.com-Performance-on-Search-*.zip`.
2. **Claude in Chrome** reads the Performance tables from the logged-in UI (done for the 2026-11-05 baseline). Nothing to set up; not usable from a cron.
3. **Service account** (for the cron): a Google Cloud service account JSON added as a *user* on the GSC property, env `GSC_SERVICE_ACCOUNT_JSON` (path or inline JSON) + `GSC_SITE_URL=https://black-whole.com/`. Scope `webmasters.readonly`. Gives query × page rows, which the UI export does not.

## Constraints
- Crawl is polite: ≤ 1 request in flight, 0.2 s gap, custom user agent, sitemap only (≈ 70 URLs).
- Groq drafting is capped at `SEO_PULSE_MAX_DRAFTS` (default 5) requests per run on `openai/gpt-oss-20b` (the bucket deals/ does not use).
- Read-only on the DB (no DB at all in v1; cities for keyword seeds come from the crawled `/chairs/{city}` URLs).

## Decided by the builder (my calls)
- Query ↔ page matching without the API: a query is "covered" when every content token of the query (stopwords removed, plural stripped) appears in some page's title + H1. Imperfect but good enough to rank the list; the API path uses the real query × page rows.
- Thresholds: striking distance = position 4–20 and ≥ 3 impressions; content gap = ≥ 3 impressions and not covered; CTR gap = ≥ 20 impressions, position ≤ 20, CTR < 2 %.
- Report location: `SEO_PULSE_DIR/report-YYYY-MM-DD.md` + stdout. Snapshot `SEO_PULSE_DIR/snapshot-YYYY-MM-DD.json`.

## Done when
`scripts/seo_pulse.py run --base https://black-whole.com --gsc <export>` produces a report with real recommendations for the live site; `tests/test_seo_pulse.py` green; weekly cron defined in `render.yaml`.
