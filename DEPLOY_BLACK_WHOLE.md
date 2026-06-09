# DEPLOY: listing_automation → live website at black-whole.com (Cloudflare)

> Self-contained handoff for a fresh Claude Code session. Branch:
> `deploy/black-whole-cloudflare`. Notion ticket:
> https://app.notion.com/p/37a0f6db872381eb8e25f455e61a4f03

## Goal
Run this app as a real website at **https://black-whole.com/**, with the
**admin page secured** (operator-only for now), fronted by **Cloudflare**.

## THE ONE ARCHITECTURAL FACT THAT SHAPES EVERYTHING
This app is **Python + FastAPI** (`automation/web/app.py`). It:
- serves a public site (`/`, `/listings`, `/listings/{lot_id}`, `/sell`, `/contact`)
  **and** an admin dashboard (`/admin`, admin JSON under `/api/*`),
- reads/writes **Supabase Postgres** (already cloud, via `BLACKWHOLE_DB_URL`),
- and the admin can spawn **Playwright/Chromium** scraping subprocesses (`run.py`).

**Cloudflare CANNOT host this app.** Workers/Pages run JS/WASM only — no Python
process, no Chromium. So Cloudflare's job is **DNS + reverse-proxy + Access
(admin auth) + Tunnel**, sitting in front of an **origin host** that actually
runs the Python app. The origin is either the operator's Mac (via Cloudflare
Tunnel) or an always-on VM/container.

Recommended v1 (fastest to live, scraping keeps working on the residential IP):
```
browser → black-whole.com → Cloudflare (DNS + proxy + Access on /admin)
                                  │  Cloudflare Tunnel (cloudflared)
                                  ▼
       FastAPI app (python -m automation.web) on the Mac → Supabase Postgres
                                  │
                                  └─ /admin scraping → Playwright/Chromium (residential IP)
```

## DECISIONS TO LOCK BEFORE STARTING (answer these in the kickoff prompt)
1. **Cloudflare access method** — pick one, the work is blocked without it:
   - (A) Operator pastes a **scoped Cloudflare API token** (Account + Zone for
     black-whole.com; permissions: `Zone:DNS:Edit`, `Account:Cloudflare Tunnel:Edit`,
     `Account:Access: Apps and Policies:Edit`, `Zone:Zone:Read`). Agent drives
     `cloudflared` + Access via API. ← recommended for hands-off.
   - (B) Connect the **Cloudflare MCP server** to the session; agent drives via MCP.
   - (C) Agent writes every command; **operator runs them** (`cloudflared` login is
     interactive anyway — see note below).
2. **Origin host** — (A) **Mac + Cloudflare Tunnel** (free, live today, up only
   when Mac awake) ← recommended for v1; (B) always-on VM/Render/Fly (24/7, more
   setup; GCP Cloud Run is currently blocked by a broken `gcloud` auth token).
3. **Domain state** — is black-whole.com already **added to Cloudflare with
   nameservers pointed**? If not, that's step 0 (add site in Cloudflare dash →
   update registrar nameservers → wait for "Active").

## PREREQUISITES TO GATHER (have these ready before the new session)
- Cloudflare API token (per decision 1A) **or** MCP connected.
- Confirm black-whole.com is Active in Cloudflare (decision 3).
- `.env` in `listing_automation/` has a working `BLACKWHOLE_DB_URL` (Supabase
  Session pooler). Optional but nice: `GEMINI_API_KEY`, `OPENAI_API_KEY`,
  `DEWATERMARK_API_KEY`, `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID`.
- `brew` available to install `cloudflared`.

## APP RUN FACTS (from the code)
- Entry: `python -m automation.web` → `automation/web/__main__.py` → `main()` in
  `automation/web/app.py`.
- Host/port env (in `app.py` `main()`): `LISTING_WEB_HOST` (default `127.0.0.1`
  — **set `0.0.0.0`** is NOT needed for a localhost Tunnel, but harmless),
  `LISTING_WEB_PORT` (default `8765`), `LISTING_WEB_RELOAD=0` for production.
- DB required: `BLACKWHOLE_DB_URL` (raises if missing). Read via `automation/config.py`
  + `automation/db.py`.
- Local hardcoded paths (fine on the Mac; matter only if moving to a container):
  `LISTING_DOWNLOAD_ROOT` (default `~/Desktop/Banquet chiars Pictures`),
  `~/.listing_automation/` (chrome_profile, logs, scratch, attachments, api_cache).
- Route map for Access scoping:
  - **Public (leave OPEN):** `/`, `/listings`, `/listings/{lot_id}`, `/sell`,
    `POST /contact`, `/static/*`, `/image/*` (listing photos — MUST stay open).
  - **Admin (PROTECT with Access):** `/admin`, **all `/api/*`** (inventory, runs,
    favorites, auctions, compare, inquiries), `/screenshot/*` (Playwright shots).

## EXECUTION PLAN — recommended path (Mac + Cloudflare Tunnel + Access)
0. (If domain not in CF) Add black-whole.com in Cloudflare dash; point registrar
   nameservers; wait until zone is **Active**.
1. **Run the app** on the Mac and smoke-test locally:
   ```bash
   cd /Users/abdelnasser/Projects/blackwhole/listing_automation
   LISTING_WEB_RELOAD=0 .venv/bin/python -m automation.web
   # → http://127.0.0.1:8765/ (public)  and  /admin (dashboard)
   ```
2. **Install cloudflared**: `brew install cloudflared`.
3. **Auth + create tunnel** (the login step is an interactive browser flow — if
   the agent can't complete it, the operator runs these two):
   ```bash
   cloudflared tunnel login            # pick the black-whole.com zone
   cloudflared tunnel create black-whole
   ```
4. **Config** `~/.cloudflared/config.yml`:
   ```yaml
   tunnel: <TUNNEL_ID_FROM_CREATE>
   credentials-file: /Users/abdelnasser/.cloudflared/<TUNNEL_ID>.json
   ingress:
     - hostname: black-whole.com
       service: http://localhost:8765
     - hostname: www.black-whole.com
       service: http://localhost:8765
     - service: http_status:404
   ```
5. **Point DNS at the tunnel**:
   ```bash
   cloudflared tunnel route dns black-whole black-whole.com
   cloudflared tunnel route dns black-whole www.black-whole.com
   ```
6. **Run the tunnel** (foreground to test, then install as a service to persist):
   ```bash
   cloudflared tunnel run black-whole
   # persist across reboots/logins:
   # sudo cloudflared service install
   ```
7. **Secure the admin** with Cloudflare Access (Zero Trust dashboard or API):
   - Create an Access **self-hosted application** for path `black-whole.com/admin`
     and one for `black-whole.com/api` (and `/screenshot`), OR a single app whose
     include paths cover all admin prefixes.
   - **Policy:** Allow • Emails • `abdel.nasser045@gmail.com` (one-time PIN). Add
     more operator emails later (Daniel/David).
   - Leave `/`, `/listings*`, `/sell`, `/contact`, `/image/*`, `/static/*` OPEN.
8. **Harden**: confirm public pages don't render admin-only data; confirm
   `LISTING_WEB_RELOAD=0`; confirm Supabase still reachable from the app.

### Always-on origin (decision 2B) — only if not using the Mac
We already proved the app's browser stack runs in a Linux container (the GovDeals
probe on `main` returns `VERDICT: PASS`). For 24/7, deploy a container of the full
app to Render/Fly/a VM, set `LISTING_WEB_HOST=0.0.0.0` + env, then point the
Cloudflare Tunnel (or a proxied DNS record) at that host instead of the Mac.
Caveat: container loses the Mac's logged-in Chrome profile + Desktop image paths;
those need volumes/secrets. Defer unless 24/7 is required now.

## VERIFICATION (done = all three pass)
- `https://black-whole.com/` loads the public landing + listings (data from Supabase).
- A listing detail page renders with photos (proves `/image/*` is open through CF).
- `https://black-whole.com/admin` triggers a Cloudflare Access login; only
  `abdel.nasser045@gmail.com` gets in; direct `https://black-whole.com/api/inventory`
  is also gated.

## STATE ALREADY DONE (don't redo)
- Branch `deploy/black-whole-cloudflare` created off `main`.
- Notion ticket created (In Progress): see link at top.
- (Separate thread, on `main`) GovDeals cloud-scrape **probe** built + proven in a
  Linux container (`VERDICT: PASS`): `probe/` dir + `probe/cloudbuild.yaml`. GCP
  execution paused on a broken `gcloud` auth token (`invalid_grant`) — unrelated
  to this deploy.

## GOTCHAS
- `cloudflared tunnel login` is interactive (browser). If driving headless, the
  operator must complete it, or use an API token + `cloudflared tunnel token`.
- Don't block `/image/*` or `/static/*` with Access or public listings lose photos.
- The app's scraping pipeline only works where Chromium + a real/residential IP
  works — keep that on the Mac (or a properly-set-up container) per the probe findings.
- Keep `.env` out of git (already gitignored). Never bake the Cloudflare token or
  `BLACKWHOLE_DB_URL` into committed files.
