# Design: black-whole.com cloud deploy (Render + Cloudflare, discovery-only)

> Supersedes the Mac+Tunnel plan in `DEPLOY_BLACK_WHOLE.md` for the cloud path.
> Notion: https://app.notion.com/p/37a0f6db872381eb8e25f455e61a4f03

## Goal
Run the app as an always-on website at **https://black-whole.com/** on a cloud
host (not the operator's Mac), with **GovDeals auction discovery scraping from
the cloud**, and the **admin surface locked** to the operator via Cloudflare
Access.

## Scope decision (locked with operator)
- **Discovery-only in the cloud.** The cloud runs the website + the auction
  *discovery* scrape (GovDeals maestro JSON API). The heavy **publish pipeline**
  (per-lot photo scrape + dewatermark + Facebook/eBay drafts) **stays on the
  Mac** — it needs a residential IP + a logged-in Chrome profile.
- **Storefront listing photos deferred** → tracked in **issue #8**. The Auctions
  tab photos work (they're `webassets.lqdt1.com` CDN URLs); the storefront's
  per-listing dewatermarked photos live on the Mac's Desktop and will render
  broken in the cloud until moved to object storage.

## The fact that makes this simple: the web app is stateless
Every surface the cloud serves reads from **Supabase Postgres** (already cloud):
- inventory, inquiries, favorites → Supabase (via `automation/db.py`)
- auction listings → Supabase `auction_listings` (via `automation/auctions_supabase.py`)

So the **web container is stateless** — its only hard dependency is
`BLACKWHOLE_DB_URL`. No local SQLite, no Chromium, no disk, no Mac filesystem.

## Proven, not assumed: maestro API works from a datacenter IP
A CI probe (`.github/workflows/probe-maestro.yml`) ran the exact discovery call
from an **Azure datacenter IP** (`172.178.118.86`): key scraped fresh from the
live JS bundle, `POST maestro.lqdt1.com/search/assets/advanced` → **HTTP 200,
real results**. So cloud discovery is confirmed, not hoped-for. If it ever
regresses, the fallback is running the scrape on the Mac (cron) — it still lands
in Supabase, which the cloud reads.

## Architecture
```
visitor → black-whole.com
            │
            ▼  Cloudflare: DNS + proxy + Access (login gate on admin paths)
            ▼
   Render Web Service  ──reads/writes──►  Supabase Postgres
   (FastAPI, stateless,                    (inventory, inquiries,
    uvicorn 0.0.0.0:$PORT)                  favorites, auction_listings)
            ▲
            │ same DB
   Render Cron Job (daily): discovery scrape → sync to Supabase
     govdeals_chairs_extraction.py  →  scripts/transfer_listings_to_supabase.py
     (throwaway SQLite staging buffer; durable output is Supabase)

   Operator's Mac (unchanged): run.py full pipeline → writes Supabase
```

### Components
1. **Web service** (`automation.web`) — public site + admin dashboard. Stateless.
   Env: `BLACKWHOLE_DB_URL` (required), `LISTING_WEB_HOST=0.0.0.0`,
   `LISTING_WEB_PORT=$PORT`, `LISTING_WEB_RELOAD=0`, optional LLM keys
   (`GEMINI_API_KEY`/`OPENAI_API_KEY`) for the Auctions condition scoring.
2. **Discovery cron** — runs the GovDeals JSON-API scraper then the Supabase
   sync. Output is Supabase, so the staging SQLite is ephemeral; no shared disk
   needed. Same image, different command.
3. **Cloudflare Access** — self-hosted application gating the admin prefixes;
   allow-policy on the operator email (one-time PIN).

### Route protection (Cloudflare Access)
- **OPEN (public):** `/`, `/listings`, `/listings/{lot_id}`, `/sell`,
  `POST /contact`, `/static/*`, `/image/*` (do NOT gate — listing photos).
- **PROTECT (admin):** `/admin`, all `/api/*`, `/screenshot/*`.

## Image / Dockerfile
- Base `python:3.12-slim`. `pip install .` (base deps) + `openai` (quantity
  refine via API). **No `playwright install`** — the JSON discovery path never
  launches Chromium, so the ~400MB browser + system libs are omitted.
- Web command: `python -m automation.web` (binds `0.0.0.0:$PORT`).
- Cron command: `python auction_extractors/govdeals_chairs_extraction.py &&
  python scripts/transfer_listings_to_supabase.py` (run from repo root with the
  package importable).

## Secrets handling
- Nothing secret is committed. `BLACKWHOLE_DB_URL` + LLM keys + the Cloudflare
  API token are set in the Render dashboard / passed at runtime only.
- `.env` stays gitignored.

## Verification (done = all three pass)
1. `https://black-whole.com/` loads the public landing + listings from Supabase.
2. A listing detail page renders (proves `/image/*` reachable through CF; photos
   themselves are issue #8).
3. `https://black-whole.com/admin` triggers a Cloudflare Access login; only the
   operator email gets in; `https://black-whole.com/api/inventory` is also gated.

## Out of scope (follow-ups)
- Storefront photo hosting → **issue #8** (move dewatermarked images to R2).
- Full publish pipeline in the cloud (needs residential IP + Chrome profile).
- Additional operator emails on the Access policy (Daniel/David) — add later.
