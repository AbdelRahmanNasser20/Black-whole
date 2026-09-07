# UI Rebuild — GovAuctions structure, BLACKWHOLE skin, loading states everywhere

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. **Each workstream (A–G) is one fresh session on its own branch; §10 has the paste-ready dispatch prompt for each.**

**Goal:** Rebuild `automation/web/` UI so every page shares one layout skeleton cloned from govauctions.app (topbar+search, category chips, filter rail, 4→1-col card grid with a filter drawer, "Load more (N remaining)", list/map toggle, detail page with sticky bid rail, sources page), painted in our existing dark palette, with a loading/empty/error/stale system that every one of the 68 fetch call sites uses.

**Architecture:** Same process, same stack — FastAPI + Jinja2 + vanilla JS + plain CSS custom properties, no build step. One new base template (`_base.html`) and one primitive layer (`static/ui/`: tokens, components, skeletons, `state.js`) that every page mounts on. `/deals`, the lot viewer, `/sources`, the map view, and the admin console are each re-templated onto the base; `app.js` is cut into ES modules (`<script type="module">`) per admin tab so the tab-by-tab migration can run as parallel agents with disjoint file ownership. Server-side policy (public exclusions, status gates, image resolver, `storage_note` privacy) is untouched.

**Tech Stack:** Python 3.11, FastAPI, Jinja2, psycopg via `automation/db.py`, vanilla ES modules, CSS custom properties, Leaflet via `static/admin_map.js`, pytest (`.venv/bin/python -m pytest`), Playwright (already in `.venv`, chromium installed), Node 25 (syntax check only, no bundling).

**Spec:** `docs/research/2026-09-04/govauctions-ui-map.md` (his structure, tokens, states) + `docs/research/2026-09-04/blackwhole-ui-inventory.md` (our routes, palette, 68 fetch sites, hard rules) + `docs/research/2026-09-04/web-hang-diagnosis.md` (Task 0 fix) + `listing_automation/CLAUDE.md`. Operator's ask verbatim: *"map out ben wallace site and lets rebuild my ui structure with all the loading ui for my site as well please, setup multiple agents create a plan for rebuilding sort of a clone copying his UI structure with my color scheme."*

## Global Constraints (apply to every task)

- **Stack stays.** No Tailwind, no React, no bundler, no npm `build`. Node is used only for `node --check`.
- **Public `/deals` policy lives only in `automation/web/public_deals.py`.** Never filter, hide, or exclude lots in a template or in JS. Public JSON stays under `/deals/api/`, never `/api/`. Public rows never render photos, verdicts, `distance_mi`, `seller`, `description` in list views. SQL regexes use `\y`.
- **`inventory.storage_note` never renders anywhere, on any page, in any state.** Not in a card, not in a drawer, not in a skeleton fixture.
- **Relaunch after every `app.py` / `templates/` / `static/` change:** kill the process on :8765 and run `python -m automation.web` (from `listing_automation/`, venv active). No hot reload of templates.
- **`_tracking_loop` stays in the web process** (`app.py:2517`). Do not move it, do not touch it.
- **`app.py` edits are capped at the lines named in each workstream** (route context, one `include_router`, one Jinja global). No other app.py changes. Rebase on `main` before opening a PR.
- **DB rule:** everything through `automation/db.py`; `%s` placeholders; every DB call inside an `async def` route is wrapped in `await asyncio.to_thread(...)` (this is the Task 0 lesson — never block the loop on the pooler). No schema changes anywhere in this plan.
- **Loading rule (new):** every network read goes through `UI.load(...)`, every mutation through `UI.pending(...)` (both in `static/ui/state.js`). A test (`tests/web/test_ui_primitives.py::test_no_raw_fetch_in_new_modules`) fails on any raw `fetch(` under `static/ui/`, `static/deals/`, `static/admin/`, `static/site/`. Legacy `app.js` / `deals_public.js` / `public.js` / `deal_card.js` are exempt only until the workstream that deletes them lands.
- **Tests:** `.venv/bin/python -m pytest tests/web/ tests/deals/ -q` (baseline 2026-09-04: **370 passed, 10.9 s**) must stay green; plus the smoke in §8. There is no `pytest` console script.
- **Answer shape** in anything written to docs: summary first, details nested.
- **Commit after every green cycle.** One workstream = one branch = one PR. Worktrees: `git worktree add .claude/worktrees/<name> -b <branch> main` (repo convention — see `git worktree list`).
- Fonts: JetBrains Mono, Instrument Serif, IBM Plex Sans (Google Fonts, already loaded by `index.html`). No new font families.

---

## 1. Design decisions (read before building anything)

### 1.1 What "clone his structure" means
Copy the **skeleton and behaviours** in `govauctions-ui-map.md` §1–§8, not the skin:

| His | Ours (decision) |
|---|---|
| Topbar 56 px sticky: logo · country · search · nav · CTA · icons | Topbar 56 px sticky: brand · search (grows, hidden < 1024 → icon button that opens the rail drawer with search on top) · nav `Deals · Sources · Admin` · connection dot + clock (admin only). **No** country picker, theme toggle, gift, heart, sign-in. |
| Left rail ~205 px, collapsible, `localStorage.feed_rail_open` | Left rail 240 px ≥ 768, collapsible to 56 px icon rail (`localStorage.ui.rail`), `< 768` = drawer opened by a `Filters` button in the control row. |
| Category chip strip, single scrolling row | Same. Chips = our canonical categories (`deals/classify.py:22`): `All · General · Vehicles · Collectibles & jewelry · Computers & electronics · Other`. (`seating_furniture` never appears publicly — the policy filters it server-side and the facets endpoint returns none.) |
| Control row: sort select · ZIP · map toggle · Create Alert | sort select · state select · `Map / List` toggle · result count `9,791 lots · 44 states`. **No** ZIP, **no** alerts. |
| Active-filter chip row + `Clear all` | Same. |
| 4/3/2/1-col grid at xl/lg/sm/base, gap 16 | Same breakpoints (640 / 1024 / 1280), gap 16. |
| `Load more (N remaining)` | Same. `per_page` fixed at 24 (divisible by 4/3/2), `page` increments; URL keeps `page` so refresh restores depth. |
| Card: image · price + badge + ⏱ · title · source · city · bids · chips | **No image (hard rule).** See §1.4. |
| Map: 70/30 split, cluster bubbles, side list `N in view` | Same split, side list reuses the feed card. |
| Detail: hero · H1 · breadcrumb · section cards · sticky bid rail | Same, minus hero for public (gallery only for operator session). |
| Sources: prose · coverage table · source cards · how we count | Same, one source (GovDeals), coverage by state. |
| Skeleton: shimmer card (image block + 3 lines), 1.5 s sweep | Skeleton card = band + 4 lines; shimmer 1.5 s; disabled under `prefers-reduced-motion`. |
| Empty: icon · H2 `No matches for "q" in Cat` · sub `N matches without that filter` · `Search everywhere` | Same copy pattern, CTA drops the category. |

### 1.2 Radius: **0 px everywhere.** One line: it is the brand already shipped on both the admin (`--radius: 0` "hard corners") and the storefront (`public.css` radius 0), we are cloning his layout not his skin, and hard corners are the one thing that stops our grid from reading as a GovAuctions reskin. Map container and drawers included. Delete the 2/3/4/8/10/12 px drift as each component migrates.

### 1.3 Tokens (`static/ui/tokens.css`) — values copied verbatim from `app.css:6-28`, names normalised
```css
:root {
  --bg: #0c0c0e;  --surface: #131316;  --surface-2: #1a1a1f;
  --border: #25252c;  --border-strong: #3a3a44;
  --text: #ece8dd;  --muted: #9e988a;  --dim: #5a5650;
  --accent: #ffb547;  --accent-soft: rgba(255,181,71,.10);  --accent-2: #c2f7c4;
  --danger: #ff6c4d;  --success: #6cd47e;  --info: #7ab9ff;  --warn: #ffb547;
  --sk-a: #16161a;  --sk-b: #202026;                 /* skeleton shimmer stops */
  --mono: "JetBrains Mono", ui-monospace, "SF Mono", Menlo, monospace;
  --display: "Instrument Serif", "New York", Georgia, serif;
  --sans: "IBM Plex Sans", system-ui, sans-serif;
  --radius: 0px;  --gap: 16px;  --pad: 16px;
  --topbar-h: 56px;  --rail-w: 240px;  --rail-w-collapsed: 56px;
  --shadow-overlay: 0 8px 24px rgba(0,0,0,.5);
  --fs-xs: 11px; --fs-sm: 12px; --fs-base: 14px; --fs-lg: 18px; --fs-xl: 22px; --fs-2xl: 28px;
  --bp-sm: 640px; --bp-md: 768px; --bp-lg: 1024px; --bp-xl: 1280px;  /* documentation only — media queries can't read vars */
}
/* legacy aliases so app.css keeps working until E/F delete it */
:root { --bg-elev: var(--surface); --bg-elev-2: var(--surface-2); --line: var(--border);
        --line-bold: var(--border-strong); --ink: var(--text); --ink-mute: var(--muted);
        --ink-dim: var(--dim); --ok: var(--success); --err: var(--danger); }
```
Type roles: `--sans` 14 px body; `--display` for the single page H1 only (28 px, weight 400, italic allowed); `--mono` for prices, counts, timers, IDs, chips (tabular-nums). Weights: 400 body · 500 titles · 700 prices. Hard-coded hex outside `tokens.css` is a review reject.

### 1.4 Feed card anatomy (ours — no photo, so the price block leads)
```
┌──────────────────────────────────────┐
│ COMPUTERS · TX                ⏱ 3h 47m│  .card-band  surface-2, mono 11px; timer → --danger under 1 h, "closed" when outcome_complete
│ $625            30 × $20.83 /unit     │  .card-price mono 18/700 · .card-unit mono 12 muted (omitted when quantity_source == 'default')
│ Lot of (30) Dell Latitude 5420 lap…   │  .card-title sans 14/500, 2-line clamp
│ Houston, TX · 14 bids                 │  .card-meta sans 12 muted; bids in --accent when > 0
│ [landed $703] [no bids yet]           │  .card-chips optional mono chips
└──────────────────────────────────────┘
```
Whole card = `<a class="card" href="{viewer_url}">`; a nested `<a class="card-ext" href="{govdeals_url}" target="_blank" rel="noopener">GovDeals ↗</a>` in the band with `stopPropagation`. Hover = `border-color: var(--border-strong)` only (no shadow, no lift). `< 640`: same vertical card in a 1-col grid (his horizontal row exists for thumbnails we don't have). The skeleton twin `.sk-card` = band + 4 lines (40 % / 100 % / 70 % / 50 %) so the swap does not reflow.

### 1.5 Page layouts (ASCII, desktop ≥ 1280)
```
/deals                                          /deals?view=map
┌ topbar ─────────────────────────────────────┐ ┌ topbar ────────────────────────────────┐
│ ◣◢ BLACK WHOLE  [search…]  Deals Sources Adm│ │ same                                   │
├ rail 240 ┬ main ────────────────────────────┤ ├ rail ┬ main ───────────────────────────┤
│ Filters  │ [All][General][Vehicles][…]      │ │      │ chips · control row (List)     │
│ Bids     │ sort ▾  state ▾  [Map]  9,791 lots│ │      │ ┌ map 70% ─────┐┌ panel 30% ─┐ │
│ any|0|≤3 │ "laptop" × · Vehicles × · Clear   │ │      │ │ clusters      ││ 412 in view│ │
│ Ending   │ ┌card┐┌card┐┌card┐┌card┐          │ │      │ │               ││ card       │ │
│ Status   │ ┌card┐┌card┐┌card┐┌card┐          │ │      │ │               ││ card       │ │
│ Price    │        [Load more (9,767 remaining)]│ │      │ └───────────────┘└────────────┘ │
│ min–max  │                                   │ └──────┴────────────────────────────────┘
│ Clear all│                                   │
└──────────┴───────────────────────────────────┘
/deals/{a}/{b}/{c}                              /sources
┌ topbar ─────────────────────────────────────┐ ┌ topbar ───────────────────────────────┐
│ ← Back to deals                              │ │ ← Back                                │
│ Lot of (30) Dell Latitude 5420   (display H1)│ │ Where the lots come from   (H1)       │
│ GovDeals · Computers · Houston, TX           │ │ prose (640 px column)                 │
├ left 1fr ───────────────────┬ rail 300 sticky┤ │ ┌ Coverage: State | Live lots ┐       │
│ [gallery — operator only]   │ Current bid ●  │ │ └─────────────────────────────┘       │
│ Seller & pickup             │ $625  14 bids  │ │ ┌ GovDeals · 9,791 live ───────┐      │
│ Full description            │ Time left 3h47m│ │ │ 12.5 % premium · links       │      │
│ Bid history (observed)      │ Ends Sep 4 21:00│ │ └──────────────────────────────┘      │
│ Similar lots (4 cards)      │ Est. all-in $703│ │ How we count                          │
│ Lot facts                   │ [Bid on GovDeals]│ └───────────────────────────────────────┘
└─────────────────────────────┴────────────────┘
/admin?tab=deals
┌ topbar: brand · search(cut) · ● 21:14:07 ──────────────────────────────────┐
├ rail: 01 Launcher 02 Drafts 03 Auctions 04 Inventory … 10 Tracking ┬ panel ┤
│ (vertical tab nav; per-tab filters render inside the panel, not the rail)   │
└─────────────────────────────────────────────────────────────────────────────┘
```
< 1024: detail rail stacks above the left column (bid card first). < 768: rail → drawer; topbar search → icon.

### 1.6 The loading system (the actual deliverable the operator asked for)
Five states, one owner, one attribute:

| State | Who sets it | What the user sees |
|---|---|---|
| `loading` (first paint) | `UI.load()` before the fetch | skeleton twin of the final layout (`skeleton('card', 8)` etc.), container `aria-busy="true"` |
| `loading` (refetch, `keepOld:true`) | `UI.load()` | old content stays, dimmed to 55 % + `pointer-events:none`, a `Refreshing…` stale badge top-right, `aria-busy="true"` |
| `ready` | `UI.load()` after `render()` | content |
| `empty` | `UI.load()` when `isEmpty(data)` | `.ui-empty`: glyph · H2 · one sentence · one CTA (`Search everywhere` / `Clear filters` / `Run a pipeline`) |
| `error` | `UI.load()` on throw/timeout/!ok | `.ui-error`: what failed, in the interface's voice ("Couldn't load lots. The database didn't answer in 15 s.") + `Retry` button that re-runs the same load |
| `stale` | `UI.markStale(el, {since})` after a poll fails | small `stale · 3 min` badge; content stays interactive |
| pending (mutation) | `UI.pending(btn, 'Saving…', fn)` | button disabled + label swapped + `.is-pending` spinner glyph; restores on finally |

`el.dataset.state` carries the state (`loading|ready|empty|error`) — the Playwright smoke waits for `[data-state]` to leave `loading`. Never write "Loading…" text into `innerHTML` again.

---

## 2. Fetch call-site register (68 sites → workstream)

Line numbers = `static/app.js` on `main` at 9d60c23 unless a file is named. "none" = shows nothing while pending today.

| # | Line | Call | Today | Workstream → primitive |
|---|---|---|---|---|
| 1 | 286 | `POST /api/runs/start` | button | E-launcher → `UI.pending` |
| 2 | 317 | `POST /api/runs/cancel` | button | E-launcher → `UI.pending` |
| 3 | 328 | `POST /api/runs/queue/clear` | button | E-launcher → `UI.pending` |
| 4 | 345 | `POST /api/runs/stdin` | button | E-launcher → `UI.pending` |
| 5 | 402 | `GET /api/runs/state` (boot) | none | E-shell → `UI.load(keepOld)` on the phase grid |
| 6 | 404 | `GET /api/scrape/state` (boot) | none | E-shell → `UI.load(keepOld)` on the scrape strip |
| 7 | 412 | `GET /api/drafts` | text | E-drafts → `UI.load(skeleton:'row',6)` |
| 8 | 527 | `GET /api/profiles` | none | E-auctions → `UI.load(skeleton:'pill',4)` |
| 9 | 568 | `POST /api/profiles` | button | E-auctions → `UI.pending` |
| 10 | 581 | `DELETE /api/profiles/{slug}` | none | E-auctions → `UI.pending` |
| 11 | 600 | `POST /api/auctions/refresh` | button | E-auctions → `UI.pending` |
| 12 | 646 | `POST /api/scrape/start` | button | E-auctions → `UI.pending` |
| 13 | 660 | `POST /api/scrape/cancel` | button | E-auctions → `UI.pending` |
| 14 | 713 | `GET /api/scrape/state` (post-SSE) | none | E-auctions → `UI.load(keepOld)` |
| 15 | 820 | `GET /api/auctions/cache-stats` | none | E-auctions → `UI.load(skeleton:'line',2)` |
| 16 | 857 | `GET /api/auctions?…` | spinner | E-auctions → `UI.load(skeleton:'card',8)` |
| 17 | 1121 | `GET /api/auctions/favorites` (30 s poll) | none | E-auctions → `UI.load(keepOld)` + `markStale` on failure |
| 18 | 1137 | `DELETE …/favorites/{id}` | none | E-auctions → `UI.pending` on the star |
| 19 | 1142 | `POST …/favorites` | none | E-auctions → `UI.pending` on the star |
| 20 | 1224 | `POST …/favorites/test-telegram` | button | E-auctions → `UI.pending` |
| 21 | 1299 | `POST /api/runs/queue` | button | E-shared (`admin/shared.js`) → `UI.pending` |
| 22 | 1305 | `GET /api/runs/state` (poll) | none | E-launcher → `UI.load(keepOld)` + `markStale` |
| 23 | 1320 | `GET /api/inventory?with_stats=1` | "Loading…" row | E-inventory → `UI.load(skeleton:'tr',10)` |
| 24 | 1442 | `PATCH /api/inventory/{lot}` (inline edit) | none | E-inventory → `UI.pending` on the cell's row (`tr.is-pending`) |
| 25 | 1472 | `POST /api/lots/{lot}/remove` | button | E-inventory → `UI.pending` |
| 26 | 1486 | `DELETE /api/inventory/{lot}` | button | E-inventory → `UI.pending` |
| 27 | 1525 | `PATCH /api/inventory/{lot}` | button | E-inventory → `UI.pending` |
| 28 | 1550 | `POST …/buyer-cert` | button | E-inventory → `UI.pending` |
| 29 | 1569 | `DELETE …/buyer-cert` | button | E-inventory → `UI.pending` |
| 30 | 1593 | `POST /api/runs/start` (from inventory) | button | E-inventory → `UI.pending` |
| 31 | 1623 | `POST …/platform` (set) | button | E-inventory → `UI.pending` |
| 32 | 1645 | `POST …/platform` (clear) | button | E-inventory → `UI.pending` |
| 33 | 1671 | `POST /api/inventory/backfill` | button | E-inventory → `UI.pending` |
| 34 | 1704 | `POST /api/inventory` | button | E-inventory → `UI.pending` |
| 35 | 1735 | `GET /api/inquiries` | "Loading…" | E-inquiries → `UI.load(skeleton:'row',5)` |
| 36 | 1784 | `PATCH /api/inquiries/{id}` | none | E-inquiries → `UI.pending` |
| 37 | 1800 | `DELETE /api/inquiries/{id}` | none | E-inquiries → `UI.pending` |
| 38 | 1839 | `GET /api/subscribers` | "Loading…" | E-subscribers → `UI.load(skeleton:'row',5)` |
| 39 | 1895 | `PATCH /api/subscribers/{id}` | none | E-subscribers → `UI.pending` |
| 40 | 1911 | `DELETE /api/subscribers/{id}` | none | E-subscribers → `UI.pending` |
| 41 | 1966 | `GET /api/listings?…` | spinner | E-listings-db → `UI.load(skeleton:'tr',10)` |
| 42 | 2154 | `GET /api/test-scrape?…` | spinner | E-test-scrape → `UI.load(skeleton:'card',4)` |
| 43 | 2324 | `GET /api/deals/tree` | none | E-deals → `UI.load(keepOld, skeleton:'line',6)` |
| 44 | 2384 | `GET /api/deals?…` | none (stale rows) | E-deals → `UI.load(keepOld:true, skeleton:'tr',12)` |
| 45 | 2578 | `GET /api/profiles/{slug}/outcomes` | none | E-deals → `UI.load(skeleton:'line',3)` |
| 46 | 2713 | `GET /api/deals/geo?…` | none | D (shared `map.js`) → `UI.load(keepOld)` on the map panel |
| 47 | 2779 | `GET /api/geo/zip?zip=` | none | E-deals → `UI.pending` on the ZIP `→` button |
| 48 | 2798 | `GET /api/deals/lists|tags|searches` | none | E-deals → `UI.load(skeleton:'pill',3)` |
| 49 | 2910 | `DELETE /api/deals/searches/{id}` | none | E-deals → `UI.pending` |
| 50 | 2964 | `PUT|DELETE …/lists/{id}/items/{key}` | none | E-deals → `UI.pending` on the heart |
| 51 | 2980 | `POST /api/deals/lists` | none | E-deals → `UI.pending` |
| 52 | 2986 | `PUT …/lists/{id}/items/{key}` | none | E-deals → `UI.pending` |
| 53 | 3037 | `DELETE …/tags/{key}/{tag}` | none | E-deals → `UI.pending` on the chip |
| 54 | 3051 | `PUT …/tags/{key}/{tag}` | none | E-deals → `UI.pending` |
| 55 | 3088 | `POST /api/deals/searches` | none | E-deals → `UI.pending` |
| 56 | 3144 | `GET /api/tracking` | first-paint text | E-tracking → `UI.load(skeleton:'tr',6)` |
| 57 | 3229 | `GET /api/tracking/{key}/history` | none | E-tracking → `UI.load(skeleton:'line',8)` in the drawer |
| 58 | 3240 | `DELETE /api/tracking/{key}` | none | E-tracking → `UI.pending` |
| 59 | 3252 | `PATCH /api/tracking/{key}` | none | E-tracking → `UI.pending` |
| 60 | 3329 | `POST /api/tracking` | none | E-tracking → `UI.pending` |
| 61 | 3346 | `POST /api/tracking/sync` | button | E-tracking → `UI.pending` |
| 62 | 361 | `EventSource /api/runs/stream` | — | E-launcher → `markStale` on `onerror`, clear on `onopen` |
| 63 | 703 | `EventSource /api/scrape/stream` | — | E-auctions → same |
| 64 | `deals_public.js:72` | `GET /deals/api/facets` | silent | B → `UI.load(skeleton:'pill',6)` on the chip strip |
| 65 | `deals_public.js:137` | `GET /deals/api/lots` | "Loading…" row | B → `UI.load(skeleton:'card',8)` / `keepOld` for Load more |
| 66 | `deals_public.js:163` | `GET /deals/api/pins` | note text | D → `UI.load` on the map panel |
| 67 | `deal_card.js:166` | `GET /api/deals/{a}/{b}/{c}` | "loading lot…" | C → `UI.load(skeleton:'line',6)` inside the overlay |
| 68 | `public.js:34` | `POST /subscribe|/contact` | button relabel | G → `UI.pending` |
| + | C-new | `GET /deals/api/lots?category=&state=&per_page=4` (Similar lots) | — | C → `UI.load(skeleton:'card',4)` |
| + | `login.html:71` inline | `POST /api/auth/login` | none | E-shell → `UI.pending` |

Template placeholders to delete as each tab migrates (`index.html`): 170, 279, 363, 398, 432, 486, 498, 555, 612, 720 (all "Loading…" text / spinners); `deals_public.html:113`.

---

## 3. Workstreams, ownership, merge order

| WS | Branch | Owns (may create/edit) | Must NOT touch | Depends on |
|---|---|---|---|---|
| **0** | `fix/web-event-loop` | `app.py` (`list_favorites` 2320-2330, `_alerts_tick` 2407-2490, `deal_listing` 553-575), `automation/db.py:53`, `scripts/web_health_probe.sh` | everything else | — |
| **A** | `ui/a-tokens-primitives` | `static/ui/{tokens,components,skeleton}.css`, `static/ui/state.js`, `static/ui/card.js`, `templates/_base.html`, `templates/_ui/macros.html`, `templates/ui_preview.html`, `automation/web/ui_preview.py`, `scripts/ui_smoke.py`, `tests/web/test_ui_primitives.py`, `app.py` (**one** `include_router` line + **one** Jinja global after line 83) | `index.html`, `app.js`, `deals_public.*`, `public.*`, `deal_listing.html` | 0 |
| **B** | `ui/b-deals-feed` | `templates/deals_public.html`, `static/deals/feed.js`, `static/deals/feed.css`, `tests/web/test_public_deals_page.py`, deletes `static/deals_public.{js,css}` | `public_deals.py`, `app.py`, `static/ui/*`, `admin_map.js` | A |
| **C** | `ui/c-lot-detail-sources` | `templates/deal_listing.html`, `templates/sources.html`, `static/deals/detail.js`, `static/deals/detail.css`, `static/deal_card.js`, `tests/web/test_sources_page.py`, `tests/web/test_public_deals_api.py` (viewer assertions only), `app.py` (`deal_listing` context lines + new `GET /sources` route placed directly after `public_deals_facets` ~line 866) | `deals_public.html`, `static/deals/feed.*`, `public_deals.py` | A |
| **D** | `ui/d-map-view` | `static/deals/map.js`, `static/deals/map.css`, `static/admin_map.js` | `deals_public.html` (B owns; D uses the ids in §5 contract), `feed.js` | A (parallel with B via the §5 event contract) |
| **F** | `ui/f-admin-modules` | `static/admin/*.js` (new), `static/app.js` (shrinks to a shim, then deleted), `templates/index.html` lines 787-788 only (script tags), `tests/web/test_admin_modules.py` | `index.html` markup, `app.css`, `static/ui/*` | A |
| **E1** | `ui/e-admin-shell` | `templates/index.html` (shell → `_base.html`, tab nav → rail, URL-param state), `templates/login.html`, `static/admin/shell.js`, `static/admin/shell.css`, `tests/web/test_admin_tabs.py` | `static/admin/<tab>.js` bodies (E2+), `app.css` component rules | A, F |
| **E2…E10** | `ui/e-<tab>` (one branch per tab: launcher, drafts, auctions, inventory, inquiries, subscribers, listings-db, test-scrape, deals, tracking) | `static/admin/<tab>.js`, `static/admin/<tab>.css`, the matching `<section data-pane="<tab>">` block in `index.html`, the matching section of `app.css` (moved into `<tab>.css`, then deleted from `app.css`) | any other tab's files, `shell.js` | E1 |
| **G** (optional) | `ui/g-storefront-light` | `templates/_public_base.html`, `templates/{landing,listings,listing_detail,sell}.html`, `static/site/site.{css,js}`, `static/ui/tokens.css` (light block only) | everything else | A–E merged |

**Merge order:** `0 → A → {B, C, D, F} (parallel) → E1 → {E2…E10} (parallel) → G`. B/C/D/F branch from `main` after A merges. E2+ branch from `main` after E1 merges. Any PR that touches `app.py` rebases first; conflicts there are ≤ 5 lines by construction.

**Time box:** each row ≤ one session (≤ a day). If a workstream can't finish, ship what is green behind the old code path and list the rest under "cut" in the PR body — never leave a page half-migrated with both old and new loaders running.

---

## 4. Task 0 — event-loop fix (prerequisite; skeletons cannot fix a server that does not answer)

**Files:**
- Modify: `automation/web/app.py:2320-2330` (`list_favorites`), `2407-2490` (`_alerts_tick` → `_alerts_collect_due` + `to_thread`), `553-575` (`deal_listing`)
- Modify: `automation/db.py:53` (`connect_timeout`)
- Create: `scripts/web_health_probe.sh`

**Interfaces:** Produces nothing new — same routes, same JSON. Every later workstream relies on `/api/health` answering in < 50 ms while ticks run.

- [ ] **Step 1: Apply the diagnosed diff.** It is in `docs/research/2026-09-04/web-hang-diagnosis.md` §Fix (also `docs/research/2026-09-04/web-hang-fix.diff`). Re-create it by hand if the file is gone — the three hunks are quoted verbatim in the diagnosis. `git apply --check` first.
- [ ] **Step 2: Also un-block the lot viewer** (`app.py:558`), which B/C build on:
```python
    row = await asyncio.to_thread(
        db.fetch_one,
        """SELECT * FROM deal_lots
        WHERE asset_id=%s AND account_id=%s AND auction_id=%s""",
        (asset_id, account_id, auction_id))
```
and `history = await asyncio.to_thread(tracking_store.history, asset_id, account_id)`.
- [ ] **Step 3: Write the probe** `scripts/web_health_probe.sh`:
```bash
#!/usr/bin/env bash
# 4 minutes of /api/health probes = 8 scheduler ticks. Every line must be "200" under 0.05 s.
BASE="${UI_BASE:-http://127.0.0.1:8765}"; bad=0
for i in $(seq 1 120); do
  out=$(curl -m 5 -s -o /dev/null -w "%{http_code} %{time_total}" "$BASE/api/health")
  echo "$out"; [[ "$out" == 200* ]] || bad=$((bad+1)); sleep 2
done
echo "failures: $bad"; [[ $bad -eq 0 ]]
```
- [ ] **Step 4: Run the existing suite** — `.venv/bin/python -m pytest tests/web/ tests/deals/ -q` → expect `370 passed`.
- [ ] **Step 5: Relaunch and probe.** Kill :8765 (`lsof -ti :8765 | xargs kill`), `python -m automation.web`, then `bash scripts/web_health_probe.sh` → `failures: 0`. Before the fix this printed intermittent `000 5.0`.
- [ ] **Step 6: Commit** `fix(web): run favorites/alerts DB work off the event loop; connect_timeout=10`. PR → merge to `main` before A starts.

Follow-up (NOT this task, listed so nobody re-diagnoses): ~60 other `async def` handlers still call sync psycopg on the loop. Rule for every route any workstream in this plan touches: wrap the DB call in `asyncio.to_thread`. The wholesale conversion is its own ticket.

---

## 5. Workstream A — tokens, base template, loading primitives (lands first)

**Files:**
- Create: `automation/web/static/ui/tokens.css`, `components.css`, `skeleton.css`, `state.js`, `card.js`
- Create: `automation/web/templates/_base.html`, `templates/_ui/macros.html`, `templates/ui_preview.html`
- Create: `automation/web/ui_preview.py` (APIRouter, `GET /admin/ui` — auth-gated by the `/admin` prefix)
- Create: `scripts/ui_smoke.py`
- Create: `tests/web/test_ui_primitives.py`
- Modify: `automation/web/app.py` — after line 83 add exactly:
  ```python
  templates.env.globals["asset_v"] = str(int(time.time()))  # cache-bust per process start
  from automation.web.ui_preview import router as _ui_preview_router  # noqa: E402
  app.include_router(_ui_preview_router)
  ```

**Interfaces (produced — every other workstream consumes these exact names):**

`templates/_base.html` blocks: `title`, `head`, `topbar_center`, `topbar_nav`, `topbar_right`, `rail`, `content`, `footer`, `scripts`. Body gets class `has-rail` when the `rail` block renders non-empty (Jinja `{% set rail %}{% block rail %}{% endblock %}{% endset %}`). Loads fonts, `tokens.css`, `components.css`, `skeleton.css`, and `<script type="module" src="/static/ui/state.js?v={{ asset_v }}">`.

`static/ui/state.js` exports (and also assigns `window.UI = {…same…}` for the legacy non-module files during migration):
```js
export function esc(s)                                   // HTML-escape → string
export const fmt = { money(v), int(v), endsIn(iso, nowMs=Date.now()), ago(iso), date(iso) }
export function toast(message, kind = 'info', ttlMs = 4000)          // kind: info|ok|err
export async function api(url, opts)                     // fetch; throws Error{status, detail} on !ok; json or text
export function skeleton(kind, count = 1)                // 'card'|'row'|'tr'|'line'|'pill' → HTML string
export function setBusy(el, busy)                        // aria-busy + .is-stale dimming of existing children
export function renderEmpty(el, {glyph='◌', title, body='', cta=null})   // cta: {label, href} | {label, onClick}
export function renderError(el, {message, retry})        // retry: () => void
export function markStale(el, {since = Date.now(), label = 'stale'} = {})  // badge; clearStale(el) removes it
export function clearStale(el)
export async function pending(btn, label, fn)            // disable + relabel + .is-pending; always restores; returns fn()
export async function load(el, fetcher, opts = {})       // THE read primitive — see contract below
```
`load(el, fetcher, opts)` contract:
- `fetcher({signal}) → Promise<data>`; aborted after `opts.timeoutMs` (default 15000).
- `opts.skeleton` (`'card'|'row'|'tr'|'line'|'pill'`, default `'line'`), `opts.count` (default 6), `opts.keepOld` (default `false`), `opts.render(data) → string | Node`, `opts.isEmpty(data) → bool` (default: `Array.isArray(data) ? !data.length : false`), `opts.empty` (args for `renderEmpty`), `opts.onError(err)`, `opts.errorMessage(err) → string` (default `"Couldn't load this. " + (err.status ? 'The server said ' + err.status + '.' : 'The server didn't answer in ' + timeoutMs/1000 + ' s.')`).
- Sets `el.dataset.state` = `loading` → `ready|empty|error`, toggles `aria-busy`, never throws, returns `data` or `undefined`. On error renders `renderError` with `retry: () => load(el, fetcher, opts)`. Concurrent calls on the same `el` abort the previous one (`el.__uiAbort`).

`static/ui/card.js`: `export function card(row, {compact=false} = {})` → HTML string for §1.4 given a `/deals/api/lots` row (`title, canonical_category, city, state, bid_count, current_bid, final_bid, outcome, outcome_complete, end_utc, quantity, quantity_source, unit_bid, landed_cost, govdeals_url, viewer_url`); `export function cardSkeleton(n)` → `skeleton('card', n)`; `export function tickTimers(root=document)` → updates every `[data-ends]` text via `fmt.endsIn` (call on a 30 s interval).

`templates/_ui/macros.html`: `{% macro chip(label, href, active=False, count=None) %}`, `{% macro section(title) %}…{{ caller() }}{% endmacro %}`, `{% macro empty(title, body, cta_label=None, cta_href=None) %}`, `{% macro skeleton_cards(n) %}` (server-side twin of `skeleton('card', n)` so a shell can ship the skeleton in the HTML and JS replaces it — no flash of empty grid).

CSS component classes in `components.css` (names are the contract; B–G use them and add page-specific ones in their own files): `.topbar .brand .topbar-search .topbar-nav .topbar-right` · `.shell .rail .rail-toggle .rail-group .rail-title .drawer-scrim` · `.chips .chip .chip.is-active .chip-count` · `.controls .select .seg .seg-btn .seg-btn.is-active .result-count` · `.active-filters .filter-chip .filter-chip-x .clear-all` · `.grid` (1/2/3/4 cols at 640/1024/1280) · `.card .card-band .card-ext .card-price .card-unit .card-title .card-meta .card-chips` · `.btn .btn-primary .btn-ghost .btn-small .btn.is-pending` · `.load-more` · `.table .table-wrap .tr.is-pending` · `.section .section-head .section-body` · `.rail-card` (sticky detail rail) · `.badge .badge-live .badge-closed .badge-stale` · `.drawer .drawer-head .drawer-body` · `.ui-empty .ui-error .ui-stale-badge` · `.toast-container .toast .toast-in .toast-out .toast-info .toast-ok .toast-err` · `.footer .footer-cols` · `.visually-hidden` · `[hidden]{display:none!important}`.

- [ ] **Step 1: Failing test file** `tests/web/test_ui_primitives.py`:
```python
"""Design-system contract: primitives exist, are served, and new modules never call fetch() directly."""
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from automation.web import auth as auth_svc
from automation.web.app import app

STATIC = Path("automation/web/static")
NEW_DIRS = ("ui", "deals", "admin", "site")


@pytest.fixture(autouse=True)
def _no_auth(monkeypatch):
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    auth_svc.reset_caches()
    yield
    auth_svc.reset_caches()


def test_ui_static_files_are_served():
    c = TestClient(app)
    for name in ("tokens.css", "components.css", "skeleton.css", "state.js", "card.js"):
        assert c.get(f"/static/ui/{name}").status_code == 200, name


def test_tokens_hold_the_operator_palette():
    css = (STATIC / "ui/tokens.css").read_text()
    for token, value in {"--bg": "#0c0c0e", "--surface": "#131316", "--border": "#25252c",
                         "--text": "#ece8dd", "--muted": "#9e988a", "--accent": "#ffb547",
                         "--danger": "#ff6c4d", "--success": "#6cd47e", "--radius": "0px"}.items():
        assert re.search(rf"{re.escape(token)}:\s*{re.escape(value)}", css), token


def test_no_hardcoded_hex_outside_tokens():
    for d in NEW_DIRS:
        for f in (STATIC / d).glob("*.css"):
            if f.name == "tokens.css":
                continue
            body = re.sub(r"/\*.*?\*/", "", f.read_text(), flags=re.S)
            assert not re.search(r"#[0-9a-fA-F]{3,8}\b", body), f"{f}: use a token"


def test_no_raw_fetch_in_new_modules():
    for d in NEW_DIRS:
        for f in (STATIC / d).glob("*.js"):
            if f == STATIC / "ui/state.js":
                continue
            assert "fetch(" not in f.read_text(), f"{f}: use UI.load / UI.pending / UI.api"


def test_preview_page_renders_every_state():
    html = TestClient(app).get("/admin/ui").text
    for state_id in ("ex-skeleton-card", "ex-skeleton-tr", "ex-empty", "ex-error", "ex-stale", "ex-pending", "ex-card"):
        assert f'id="{state_id}"' in html, state_id
    assert "storage_note" not in html


def test_state_js_exports_the_contract():
    src = (STATIC / "ui/state.js").read_text()
    for name in ("esc", "fmt", "toast", "api", "skeleton", "setBusy", "renderEmpty",
                 "renderError", "markStale", "clearStale", "pending", "load"):
        assert re.search(rf"export (async )?(function|const) {name}\b", src), name
    assert "window.UI" in src
```
- [ ] **Step 2: Run** `.venv/bin/python -m pytest tests/web/test_ui_primitives.py -q` → FAIL (404s / missing files).
- [ ] **Step 3: Write `tokens.css`** exactly as §1.3.
- [ ] **Step 4: Write `skeleton.css`:**
```css
/* static/ui/skeleton.css — loading/empty/error/stale primitives. Every fetch site uses these via state.js. */
.sk { position: relative; overflow: hidden; background: var(--sk-a); border-radius: var(--radius); }
.sk::after { content: ""; position: absolute; inset: 0; transform: translateX(-100%);
  background: linear-gradient(90deg, transparent 0, var(--sk-b) 50%, transparent 100%);
  animation: sk-sweep 1.5s ease-in-out infinite; }
@keyframes sk-sweep { to { transform: translateX(100%); } }
@media (prefers-reduced-motion: reduce) { .sk::after { animation: none; } }
.sk-line { height: 12px; width: var(--w, 100%); margin: 6px 0; }
.sk-pill { display: inline-block; height: 26px; width: var(--w, 88px); margin: 0 6px 6px 0; }
.sk-band { height: 28px; margin: -1px -1px 8px; }
.sk-card { border: 1px solid var(--border); background: var(--surface); padding: 0 var(--pad) var(--pad); min-height: 150px; }
.sk-row { display: flex; gap: 12px; padding: 10px 0; border-bottom: 1px solid var(--border); }
.sk-row .sk-cell { flex: 1; height: 14px; }
.sk-tr td { padding: 10px 8px; }
[aria-busy="true"] > :not(.ui-stale-badge):not(.sk):not(.sk-card):not(.sk-row):not(.sk-tr) { opacity: .55; pointer-events: none; transition: opacity .15s; }
.ui-empty, .ui-error { display: grid; justify-items: center; text-align: center; gap: 8px; padding: 48px 16px; border: 1px dashed var(--border); color: var(--muted); }
.ui-empty .glyph { font-family: var(--mono); font-size: 28px; color: var(--dim); }
.ui-empty h2, .ui-error h2 { margin: 0; font: 400 var(--fs-xl)/1.2 var(--display); color: var(--text); }
.ui-empty p, .ui-error p { margin: 0; max-width: 46ch; }
.ui-error { border-color: var(--danger); }
.ui-stale-badge { position: absolute; top: 8px; right: 8px; z-index: 2; font: 500 var(--fs-xs)/1 var(--mono); letter-spacing: .06em;
  padding: 4px 8px; background: var(--surface-2); color: var(--warn); border: 1px solid var(--border-strong); }
[data-state] { position: relative; }
.btn.is-pending { opacity: .7; cursor: progress; }
.btn.is-pending::before { content: "◌"; display: inline-block; margin-right: 6px; animation: sk-spin 1s linear infinite; }
@keyframes sk-spin { to { transform: rotate(360deg); } }
@media (prefers-reduced-motion: reduce) { .btn.is-pending::before { animation: none; } }
.tr.is-pending, tr.is-pending { opacity: .55; pointer-events: none; }
```
- [ ] **Step 5: Write `state.js`** (full implementation; this is the load-bearing file):
```js
// static/ui/state.js — the one way to fetch and show state. ES module; also window.UI for legacy scripts.
export function esc(s) {
  return String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}
export const fmt = {
  money(v) { return v == null ? '—' : '$' + Number(v).toLocaleString(undefined, {maximumFractionDigits: 2}); },
  int(v) { return v == null ? '—' : Number(v).toLocaleString(); },
  endsIn(iso, nowMs = Date.now()) {
    if (!iso) return '—';
    const s = Math.floor((new Date(iso).getTime() - nowMs) / 1000);
    if (s <= 0) return 'ended';
    const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
    return d ? `${d}d ${h}h` : h ? `${h}h ${m}m` : `${m}m`;
  },
  ago(iso) { const m = Math.round((Date.now() - new Date(iso).getTime()) / 60000); return m < 1 ? 'just now' : m < 60 ? `${m} min` : `${Math.round(m / 60)} h`; },
  date(iso) { return iso ? new Date(iso).toLocaleString(undefined, {month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit'}) : '—'; },
};
export function toast(message, kind = 'info', ttlMs = 4000) {
  let host = document.getElementById('toast-container');
  if (!host) { host = document.createElement('div'); host.id = 'toast-container'; host.className = 'toast-container'; host.setAttribute('aria-live', 'polite'); document.body.appendChild(host); }
  const el = document.createElement('div');
  el.className = `toast toast-${kind}`; el.setAttribute('role', kind === 'err' ? 'alert' : 'status'); el.textContent = message;
  host.appendChild(el); requestAnimationFrame(() => el.classList.add('toast-in'));
  const dismiss = () => { el.classList.remove('toast-in'); el.classList.add('toast-out'); el.addEventListener('transitionend', () => el.remove(), {once: true}); };
  el.addEventListener('click', dismiss); setTimeout(dismiss, ttlMs);
}
export async function api(url, opts) {
  const res = await fetch(url, opts);
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch {}
    const err = new Error(detail); err.status = res.status; throw err;
  }
  const ct = res.headers.get('content-type') || '';
  return ct.includes('application/json') ? res.json() : res.text();
}
const SK = {
  line: '<div class="sk sk-line" aria-hidden="true"></div>',
  pill: '<span class="sk sk-pill" aria-hidden="true"></span>',
  row: '<div class="sk-row" aria-hidden="true">' + '<div class="sk sk-cell"></div>'.repeat(5) + '</div>',
  tr: '<tr class="sk-tr" aria-hidden="true"><td colspan="99"><div class="sk sk-line"></div></td></tr>',
  card: '<div class="sk-card" aria-hidden="true"><div class="sk sk-band"></div><div class="sk sk-line" style="--w:40%"></div>'
      + '<div class="sk sk-line"></div><div class="sk sk-line" style="--w:70%"></div><div class="sk sk-line" style="--w:50%"></div></div>',
};
export function skeleton(kind, count = 1) { return (SK[kind] || SK.line).repeat(count); }
export function setBusy(el, busy) { if (busy) el.setAttribute('aria-busy', 'true'); else el.removeAttribute('aria-busy'); }
export function renderEmpty(el, {glyph = '◌', title, body = '', cta = null} = {}) {
  el.innerHTML = `<div class="ui-empty"><div class="glyph">${esc(glyph)}</div><h2>${esc(title)}</h2>${body ? `<p>${esc(body)}</p>` : ''}`
    + (cta ? (cta.href ? `<a class="btn btn-primary" href="${esc(cta.href)}">${esc(cta.label)}</a>` : `<button class="btn btn-primary" type="button" data-cta>${esc(cta.label)}</button>`) : '') + '</div>';
  if (cta && cta.onClick) el.querySelector('[data-cta]').addEventListener('click', cta.onClick);
  el.dataset.state = 'empty';
}
export function renderError(el, {message, retry}) {
  el.innerHTML = `<div class="ui-error" role="alert"><h2>Something didn't load</h2><p>${esc(message)}</p><button class="btn" type="button" data-retry>Retry</button></div>`;
  if (retry) el.querySelector('[data-retry]').addEventListener('click', retry);
  el.dataset.state = 'error';
}
export function markStale(el, {since = Date.now(), label = 'stale'} = {}) {
  clearStale(el);
  const b = document.createElement('span'); b.className = 'ui-stale-badge'; b.dataset.since = String(since);
  b.textContent = `${label} · ${fmt.ago(new Date(since).toISOString())}`; el.appendChild(b);
}
export function clearStale(el) { el.querySelectorAll(':scope > .ui-stale-badge').forEach(b => b.remove()); }
export async function pending(btn, label, fn) {
  if (!btn) return fn();
  const orig = btn.textContent, wasDisabled = btn.disabled;
  btn.disabled = true; if (label) btn.textContent = label; btn.classList.add('is-pending');
  try { return await fn(); }
  finally { btn.disabled = wasDisabled; btn.textContent = orig; btn.classList.remove('is-pending'); }
}
export async function load(el, fetcher, opts = {}) {
  const {skeleton: kind = 'line', count = 6, keepOld = false, timeoutMs = 15000, render, isEmpty, empty, onError, errorMessage} = opts;
  if (el.__uiAbort) el.__uiAbort.abort();
  const ac = new AbortController(); el.__uiAbort = ac;
  const timer = setTimeout(() => ac.abort(), timeoutMs);
  el.dataset.state = 'loading'; setBusy(el, true);
  if (!keepOld || !el.children.length) el.innerHTML = skeleton(kind, count);
  else markStale(el, {label: 'refreshing'});
  try {
    const data = await fetcher({signal: ac.signal});
    if (ac.signal.aborted && el.__uiAbort !== ac) return undefined;   // superseded
    clearStale(el);
    const emptyNow = isEmpty ? isEmpty(data) : (Array.isArray(data) ? !data.length : false);
    if (emptyNow) { renderEmpty(el, empty || {title: 'Nothing here yet'}); return data; }
    if (render) { const out = render(data); if (typeof out === 'string') el.innerHTML = out; else if (out) { el.innerHTML = ''; el.appendChild(out); } }
    el.dataset.state = 'ready';
    return data;
  } catch (err) {
    if (el.__uiAbort !== ac) return undefined;
    const msg = errorMessage ? errorMessage(err)
      : ac.signal.aborted ? `The server didn't answer in ${Math.round(timeoutMs / 1000)} s.`
      : err.status ? `The server said ${err.status}${err.message ? ': ' + err.message : ''}.` : (err.message || 'Network error.');
    renderError(el, {message: msg, retry: () => load(el, fetcher, opts)});
    if (onError) onError(err);
    return undefined;
  } finally { clearTimeout(timer); setBusy(el, false); if (el.__uiAbort === ac) el.__uiAbort = null; }
}
window.UI = {esc, fmt, toast, api, skeleton, setBusy, renderEmpty, renderError, markStale, clearStale, pending, load};
```
- [ ] **Step 6: Write `card.js`** per §1.4 (uses `esc`, `fmt`; band shows `canonical_category` (underscore → space, upper) · state; timer span `<span data-ends="${iso}">`; `closed · no bid` when `outcome_complete`; `tickTimers`).
- [ ] **Step 7: Write `components.css`** — the class list above. Topbar `position:sticky; top:0; height:var(--topbar-h); border-bottom:1px solid var(--border); background:var(--bg)`. `.shell{display:grid;grid-template-columns:var(--rail-w) 1fr}`; `html[data-rail="closed"] .shell{grid-template-columns:var(--rail-w-collapsed) 1fr}`; `@media (max-width:767px){.shell{grid-template-columns:1fr}.rail{position:fixed;inset:var(--topbar-h) auto 0 0;width:min(320px,90vw);transform:translateX(-100%);transition:transform .18s}.rail.is-open{transform:none}}`. `.grid{display:grid;gap:var(--gap);grid-template-columns:1fr}@media(min-width:640px){.grid{grid-template-columns:repeat(2,1fr)}}@media(min-width:1024px){.grid{grid-template-columns:repeat(3,1fr)}}@media(min-width:1280px){.grid{grid-template-columns:repeat(4,1fr)}}`. Focus: `:focus-visible{outline:2px solid var(--accent);outline-offset:2px}`. `.chip.is-active{background:var(--accent);color:var(--bg);border-color:var(--accent)}`. Keep the body dot-grid from `app.css:47-52`.
- [ ] **Step 8: Write `_base.html`** with the block contract, the rail toggle button (`.rail-toggle`, persists `localStorage.ui.rail` → `html[data-rail]`), the mobile `drawer-scrim`, and `<script type="module" src="/static/ui/state.js?v={{ asset_v }}">` before `{% block scripts %}`. Then `_ui/macros.html`.
- [ ] **Step 9: Write `ui_preview.py` + `ui_preview.html`** — `router = APIRouter()`; `@router.get("/admin/ui", response_class=HTMLResponse)` renders every component with fixture rows (fixture rows must not contain `storage_note`), each example wrapped in `<div id="ex-…">`; a `<script type="module">` that demos `UI.load` against `Promise` fixtures (resolve / reject / timeout) with buttons. **This is the design review page: open it, screenshot it, fix what looks wrong before writing B–G.**
- [ ] **Step 10: Add the three `app.py` lines** after line 83.
- [ ] **Step 11: Write `scripts/ui_smoke.py`** (Playwright, against a running server):
```python
"""Playwright smoke: for each URL, wait until no [data-state="loading"] remains, assert no console errors,
no aria-busy left behind, screenshot to docs/superpowers/screenshots/<branch>/. Usage:
  .venv/bin/python scripts/ui_smoke.py /deals "/deals?view=map" /sources
Env: UI_BASE (default http://127.0.0.1:8765), UI_WIDTHS (default 1280,390)."""
import os, subprocess, sys
from pathlib import Path
from playwright.sync_api import sync_playwright

BASE = os.getenv("UI_BASE", "http://127.0.0.1:8765")
WIDTHS = [int(w) for w in os.getenv("UI_WIDTHS", "1280,390").split(",")]
branch = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True).stdout.strip() or "wip"
out = Path("docs/superpowers/screenshots") / branch; out.mkdir(parents=True, exist_ok=True)
failures = []
with sync_playwright() as p:
    b = p.chromium.launch()
    for url in sys.argv[1:]:
        for w in WIDTHS:
            pg = b.new_page(viewport={"width": w, "height": 900}); errors = []
            pg.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
            pg.on("pageerror", lambda e: errors.append(str(e)))
            pg.goto(BASE + url, wait_until="domcontentloaded")
            try:
                pg.wait_for_function("!document.querySelector('[data-state=\"loading\"]')", timeout=20000)
            except Exception:
                failures.append(f"{url}@{w}: still loading after 20 s")
            if pg.locator("[aria-busy='true']").count():
                failures.append(f"{url}@{w}: aria-busy left behind")
            if errors:
                failures.append(f"{url}@{w}: console errors: {errors[:3]}")
            pg.screenshot(path=str(out / (url.strip('/').replace('/', '_').replace('?', '_') or 'root') + f"_{w}.png"), full_page=True)
            pg.close()
    b.close()
print("\n".join(failures) or f"smoke ok → {out}"); sys.exit(1 if failures else 0)
```
- [ ] **Step 12: Run** `.venv/bin/python -m pytest tests/web/ tests/deals/ -q` → all green (370 + 6). `node --check --input-type=module < automation/web/static/ui/state.js` → no output.
- [ ] **Step 13: Relaunch, open `http://127.0.0.1:8765/admin/ui` at 1280 and 390, run `scripts/ui_smoke.py /admin/ui`.** Review against §1: one memorable element (the mono price + live timer), everything else quiet; no rounded corners; no hard-coded hex. Fix, re-screenshot.
- [ ] **Step 14: Commit** `feat(ui): tokens, base template, loading primitives (state.js), preview page, smoke script`. PR. **Done when:** tests green, `/admin/ui` shows all 7 states at both widths, smoke passes, no page other than `/admin/ui` changed.

---

## 6. Workstream B — public `/deals` feed (list, filters, chips, load-more, skeletons)

**Files:**
- Rewrite: `templates/deals_public.html` (extends `_base.html`)
- Create: `static/deals/feed.js` (module), `static/deals/feed.css`
- Delete: `static/deals_public.js`, `static/deals_public.css`
- Modify: `tests/web/test_public_deals_page.py`
- Do not touch: `public_deals.py`, `app.py`, `static/ui/*`, `admin_map.js`

**Interfaces:**
- Consumes `UI.load`, `UI.skeleton`, `card()`, `cardSkeleton()`, `tickTimers()`, `_base.html` blocks, `.grid .chips .controls .rail .load-more .active-filters` classes; API `/deals/api/lots|facets` unchanged (`{rows, total, page, per_page, pages}`).
- Produces (the **§5 contract D relies on**) — element ids in `deals_public.html`: `feed-q` (topbar search), `feed-chips`, `feed-controls`, `feed-sort`, `feed-state`, `feed-view-toggle`, `feed-count`, `feed-filters` (active chips), `feed-grid` (`data-state` owner), `feed-more`, `feed-rail`, `feed-map` (hidden until D), `feed-map-panel` (hidden until D); and two events on `document`: `feed:params` (`detail = {params: URLSearchParams, total}` dispatched after every successful lots load) and listener for `feed:bbox` (`detail = {bbox: 'S,W,N,E' | ''}` → sets `st.bbox`, reloads with `page=1`). `view=map` in the URL: feed.js only toggles `body.dataset.view` and the toggle label; D does the rest.
- URL is the state: keys `q, category, state, max_bids, ending_within, status, min_price, max_price, sort, dir, page, view`; `bbox` transient (never written to the URL — same rule as today, `deals_public.js:24`).

- [ ] **Step 1: Update the page test** (`test_public_deals_page.py::test_deals_page_shell`) to the new ids: `id="feed-q"`, `id="feed-grid"`, `id="feed-rail"`, `id="feed-more"`, `id="feed-map"`, `/static/deals/feed.js`, `/static/ui/state.js`, keep `noindex`, keep `"<img" not in html`, keep `"Sell Your Chairs" not in html`; add `assert 'class="sk-card"' in html` (server-shipped skeleton) and `assert "storage_note" not in html`. Run → FAIL.
- [ ] **Step 2: Rewrite `deals_public.html`:** `{% extends "_base.html" %}`; `title` = `Surplus Radar — live government surplus lots`; `head` = `<meta name="robots" content="noindex,nofollow">` + `feed.css`; `topbar_center` = search form `#feed-q` (placeholder `Search lots — laptops, forklift, fryer…`); `topbar_nav` = `Deals · Sources`; `rail` = groups **Bids** (`.seg`: Any / No bids / ≤ 3), **Ending** (select: any / < 6 h / < 24 h / < 48 h / < 7 d), **Status** (`.seg`: Live / Closed / All), **Price** (min–max), `LANDED = bid × 1.125` note, `Clear all filters`; `content` = `#feed-chips` (with `{{ skeleton_pills(6) }}`), `#feed-controls` (sort select: `Ending soonest` (ends) / `Newest` (newest) / `Bid: low → high` (bid) / `Bid: high → low` (bid:desc) / `Most bids` (bids:desc); state select; `#feed-view-toggle` `Map`; `#feed-count`), `#feed-filters`, `<div id="feed-map" hidden></div><aside id="feed-map-panel" hidden></aside>`, `<div id="feed-grid" class="grid" data-state="loading">{{ skeleton_cards(8) }}</div>`, `<button id="feed-more" class="btn load-more" hidden>`; `footer` = disclaimer line `Surplus Radar aggregates public GovDeals listings and is not affiliated with GovDeals or any agency.` + `© 2026 Black Whole`; `scripts` = `<script type="module" src="/static/deals/feed.js?v={{ asset_v }}">`. **No `<img>` anywhere.** Keep the portfolio "what this is" block as a collapsed `<details>` at the top of `content` (test asserts `id="sr-about"` → rename to `feed-about` and update the test).
- [ ] **Step 3: Write `feed.js`:**
  - state from `location.search` (copy the `KEYS/DEFAULTS/qs/pushUrl/set` pattern from `deals_public.js:11-33`, `per_page` fixed 24, add `view`).
  - `loadChips()` → `UI.load($('#feed-chips'), ({signal}) => UI.api('/deals/api/facets', {signal}), {skeleton:'pill', count:6, render: f => chipsHtml(f.categories)})` — chip = `All` + one per facet category with count; also fills `#feed-state` options and `#feed-count` suffix `· N states`.
  - `loadLots({append=false})` → target `#feed-grid`; `keepOld: append`; fetcher hits `/deals/api/lots?…&page=N`; `render`: append → `grid.insertAdjacentHTML('beforeend', rows.map(card).join(''))` and return `null`; first page → `rows.map(card).join('')`; `isEmpty: b => !b.total`; `empty`: `{glyph:'⌕', title: q ? \`No matches for "${q}"${cat ? ' in ' + catLabel : ''}\` : 'No lots match these filters', body: 'Try fewer filters, or search everywhere.', cta: {label: cat || q ? 'Search everywhere' : 'Clear filters', onClick: () => set({category:'', state:'', max_bids:'', ending_within:'', min_price:'', max_price:''})}}`. After render: `#feed-count` = `${fmt.int(total)} lots`, `#feed-more` label `Load more (${fmt.int(total - page*per_page)} remaining)` / hidden when none, `tickTimers()`, dispatch `feed:params`.
  - `#feed-more` click → `UI.pending(btn, 'Loading…', () => { st.page = String(+st.page + 1); pushUrl(); return loadLots({append:true}); })`.
  - Active-filter chips: one per non-default key with `×`, plus `Clear all`.
  - `< 768`: `Filters` button in `#feed-controls` toggles `.rail.is-open` (`_base.html` provides `.drawer-scrim`).
  - `feed:bbox` listener; `view` toggle sets `document.body.dataset.view` and label `List`/`Map` and updates the URL; `setInterval(tickTimers, 30000)`.
- [ ] **Step 4: Write `feed.css`** — only feed-specific rules (`.feed-about`, rail group spacing, `.load-more` centering, `body[data-view="map"] #feed-grid, body[data-view="map"] #feed-more {display:none}`). No hex.
- [ ] **Step 5: Delete `deals_public.js` / `deals_public.css`**, grep the repo for references (`grep -rn "deals_public\.\(js\|css\)" automation tests docs`) and fix.
- [ ] **Step 6: Tests** `.venv/bin/python -m pytest tests/web/ tests/deals/ -q` → green. `node --check --input-type=module < automation/web/static/deals/feed.js`.
- [ ] **Step 7: Relaunch; smoke** `scripts/ui_smoke.py /deals "/deals?q=laptop" "/deals?category=vehicles&sort=bid&dir=desc" "/deals?q=zzzznomatch"` at 1280/390 → passes; screenshots reviewed: skeleton → cards without reflow, empty state on the nonsense query, Load more label counts down, refresh restores `page`.
- [ ] **Step 8: Commit** `feat(deals): /deals feed on the shared shell — chips, rail, load-more, skeleton/empty/error states`. **Done when:** every one of #64/#65 goes through `UI.load`, no `Loading…` string in the template or JS, hard rules hold (no img, no exclusions in JS).

---

## 7. Workstream C — lot detail page + `/sources`

**Files:**
- Rewrite: `templates/deal_listing.html` (extends `_base.html`), `static/deal_card.js` (overlay gallery, operator-only; swap its raw fetch for `UI.load`, restyle with tokens)
- Create: `templates/sources.html`, `static/deals/detail.js`, `static/deals/detail.css`, `tests/web/test_sources_page.py`
- Modify: `app.py` `deal_listing` (lines 553-575): enrich the row and pass the fee — replace the `TemplateResponse` context with
  ```python
  from deals.fees import fee_model_from_env
  fees = fee_model_from_env()
  lot = deals_query.enrich(dict(row), fees)
  return templates.TemplateResponse(request, "deal_listing.html", {
      "lot": lot, "history": history, "bidders": tracking.bidder_summary(history),
      "show_images": operator, "premium_pct": fees.buyer_premium_pct})
  ```
  and add, directly after `public_deals_facets` (~line 866):
  ```python
  @app.get("/sources", response_class=HTMLResponse)
  async def sources_page(request: Request):
      try:
          facets = await asyncio.to_thread(public_deals.fetch_facets)
      except Exception as e:
          raise HTTPException(503, f"facets query failed: {e!r}")
      from deals.fees import fee_model_from_env
      return templates.TemplateResponse(request, "sources.html", {
          "facets": facets, "premium_pct": fee_model_from_env().buyer_premium_pct,
          "base_url": PUBLIC_BASE_URL})
  ```
- Modify: `tests/web/test_public_deals_api.py` — keep every existing assertion (photos hidden for public, shown for operator, excluded lot 404s); add `assert 'id="bid-rail"' in html` and `assert "storage_note" not in html`.

**Interfaces:** consumes `UI.load`, `card()`, `.section .rail-card .badge-live .badge-closed`; the enriched lot row (`quantity, quantity_source, unit_bid, unit_landed, landed_cost, govdeals_url, viewer_url` + raw columns). Produces `/sources` (linked from the topbar nav and the feed footer).

- [ ] **Step 1: Failing tests.** `tests/web/test_sources_page.py`:
```python
import importlib
import pytest
from fastapi.testclient import TestClient
from automation.web import auth as auth_svc
from automation.web import public_deals as pd
app_mod = importlib.import_module("automation.web.app"); app = app_mod.app

FACETS = {"categories": [{"value": "vehicles", "count": 3}], "states": [{"value": "TX", "count": 120}, {"value": "AZ", "count": 80}],
          "stats": {"tracked": 210932, "active": 9791, "closed": 200000, "no_bid": 40000, "states": 44, "since": None}, "cached_at": 0}

@pytest.fixture(autouse=True)
def _no_auth(monkeypatch):
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False); auth_svc.reset_caches(); yield; auth_svc.reset_caches()

def test_sources_page_is_public_and_server_rendered(monkeypatch):
    monkeypatch.setattr(pd, "fetch_facets", lambda: FACETS)
    html = TestClient(app).get("/sources").text
    assert "Where the lots come from" in html and "GovDeals" in html
    assert "9,791" in html and "TX" in html and "12.5" in html
    assert "<img" not in html and "storage_note" not in html
    assert 'data-state="loading"' not in html  # nothing to fetch client-side

def test_sources_503_when_facets_fail(monkeypatch):
    monkeypatch.setattr(pd, "fetch_facets", lambda: (_ for _ in ()).throw(RuntimeError("db")))
    assert TestClient(app).get("/sources").status_code == 503
```
Run → FAIL (404).
- [ ] **Step 2: `sources.html`** — `_base.html`, `topbar_nav` same as feed, `content` = 640 px article column: `← Back`, H1 `Where the lots come from` (display), 3 short paragraphs (what GovDeals is, what we store — title/bid/close/outcome text only, no photos — and why seating lots are absent: "chairs are our own trade; we don't publish them here"), **Coverage** `.section` with a `<table class="table">` `State | Live lots` for the top 15 states from `facets.states` + `All states` total row (`facets.stats.active`), **Source card** `.section`: `GovDeals` · `{{ '{:,}'.format(facets.stats.active) }} live` · eyebrow `State and local government surplus · buyer's premium {{ (premium_pct*100)|round(1) }} %` · paragraph · links `govdeals.com ↗` and `View live lots →` (`/deals`) · data line `Titles, bids, close times and outcomes sampled every 20 min; counts cached 5 min` (from `deals.md:63` — `deals-watch` every 20 min, `CACHE_TTL = 300`), **How we count** bullets (live = not closed and `end_utc` in the future; closed with no bids = `outcome = 'no_bid'`; states = distinct `state` on live lots), footer.
- [ ] **Step 3: `deal_listing.html`** on `_base.html` — `title` `{{ lot.title }} · Surplus Radar`; `head` = noindex + `detail.css`; content per §1.5: `← Back to deals` (`history.length > 1 ? back : /deals` in `detail.js`), H1 display, breadcrumb row `GovDeals · {{ category }} · {{ city }}, {{ state }}`, two-column `.detail` (`grid-template-columns: 1fr 300px`, stacks < 1024 with the rail first). Left: `{% if show_images and (hero or gallery) %}` gallery (existing markup, `.section`) `{% elif not show_images %}` one-line note; `.section` **Seller & pickup** (`lot.seller` only when operator — public rows never carry seller; city/state; `Pickup terms are on the GovDeals listing`); **Full description** `<pre>`; **Bid history** (observed changes list + bidders, existing loop, inside `.section`); **Similar lots** `<div id="similar" class="grid" data-state="loading">{{ skeleton_cards(4) }}</div>`; **Lot facts** key/value grid (lot key, opening bid, reserve, first seen, native category, final bids if closed). Right `.rail-card#bid-rail`: `Current bid` + `.badge-live` (or `Final bid` + `.badge-closed`), price mono 32 px, `N bids`, `Time left <span data-ends="{{ lot.end_utc.isoformat() }}">`, `Ends {{ … }}`, `Est. all-in {{ landed_cost }} · incl. {{ premium }} % premium`, `qty {{ quantity }} · {{ unit_landed }}/unit landed` (hidden when `quantity_source == 'default'`), primary `<a class="btn btn-primary" href="{{ govdeals_url }}" target="_blank" rel="noopener">Bid on GovDeals ↗</a>`, secondary `Copy link`. **Never** render `storage_note`, `distance_mi`, verdict fields, `high_bidder`, `raw`.
- [ ] **Step 4: `detail.js`** — `tickTimers()` + 30 s interval; Similar lots: `UI.load($('#similar'), ({signal}) => UI.api(\`/deals/api/lots?category=${cat}&state=${st}&per_page=4&status=active\`, {signal}), {skeleton:'card', count:4, render: b => b.rows.filter(r => r.asset_id !== self).slice(0,4).map(card).join(''), isEmpty: b => b.rows.length <= 1, empty: {title:'No similar live lots right now', cta:{label:'Browse all', href:'/deals'}}})` (category/state/self read from `data-*` on `#similar`; the endpoint is exclusion-filtered server-side, so nothing to hide here); `Copy link` via `UI.pending`.
- [ ] **Step 5: `deal_card.js`** — replace the raw fetch (line 166) with `UI.load(overlay.querySelector('.dcard'), ({signal}) => UI.api(url, {signal}), {skeleton:'line', count:6, render: d => …})`; move its inline colours to tokens. It is only loaded for operators (`show_images`), keep that.
- [ ] **Step 6: Tests** → green (`test_public_deals_api.py` viewer tests must still pass unchanged in meaning). `node --check` on `detail.js`.
- [ ] **Step 7: Relaunch; smoke** `scripts/ui_smoke.py /sources "/deals/<a>/<b>/<c>"` (pick a live key from `/deals/api/lots?per_page=1`), 1280 + 390. Check: rail stacks first on mobile, similar-lots skeleton → cards, timer ticks.
- [ ] **Step 8: Commit** `feat(deals): lot detail on the shared shell (bid rail, similar lots) + /sources`. **Done when:** both pages render on `_base.html`, #67 and the new similar-lots fetch use `UI.load`, tests green, no private field in public HTML.

---

## 8. Workstream D — map view (`/deals?view=map`)

**Files:**
- Create: `static/deals/map.js` (module), `static/deals/map.css`
- Modify: `static/admin_map.js` — cluster/pin colours to tokens (`.marker-cluster` background `var(--accent-soft)`, border `var(--accent)`, count text `var(--text)`, mono), radius 0 on the container, popup styled with tokens; keep the API (`mount → {setPoints, fit, onViewport, bboxParam, invalidateSize, count, visibleCount, leaflet}`) unchanged because the admin Auctions/Deals tabs use it.
- Do not touch: `deals_public.html`, `feed.js` (use the §6 contract: ids `feed-map`, `feed-map-panel`, `feed-view-toggle`; events `feed:params` / `feed:bbox`).

**Interfaces:** consumes `/deals/api/pins?…` (`{points:[{lat,lng,title,city,state,current_bid,bid_count,end_utc,govdeals_url,asset_id,account_id,auction_id}], capped}`), `/deals/api/lots?…&bbox=` (via feed.js after `feed:bbox`), `card()`, `UI.load`. Produces nothing new server-side.

- [ ] **Step 1: `map.js`:** on `DOMContentLoaded`, if `body.dataset.view === 'map'` or the toggle is clicked → `showMap()`: un-hide `#feed-map` + `#feed-map-panel`, `UI.load($('#feed-map-panel'), …)` is **not** used for the map box itself (Leaflet owns it) — instead the map box gets `skeleton('line')`-free treatment: add class `map-loading` (a `.sk` shimmer background) until `AdminMap.mount()` resolves and the first `setPoints` runs; the **panel** is the `data-state` owner: `UI.load(panel, ({signal}) => UI.api('/deals/api/pins?' + params, {signal}), {skeleton:'card', count:3, render: d => panelHtml(d), isEmpty: d => !d.points.length, empty:{glyph:'⌖', title:'No lots in this area', body:'Zoom out or clear a filter.', cta:{label:'Show list', onClick: showList}}})`. `panelHtml` = header `${visibleCount} lots in view` + hint `Pan or zoom to narrow the list` + note `${capped ? 'Showing the first 5,000 — narrow the filters' : ''}` + `<div class="grid grid-1">` of `card(row)` for the rows the feed loaded with the current bbox (listen to `feed:params` and re-render the panel list from the `rows` the feed passes: extend the event to `detail = {params, total, rows}` — **one-line addition B must include: `rows` in `feed:params` detail**; put it in the §6 contract so B ships it).
  - `map.onViewport(debounce(400, () => document.dispatchEvent(new CustomEvent('feed:bbox', {detail:{bbox: map.bboxParam()}}))))`.
  - `showList()` → hide both, dispatch `feed:bbox` with `''`, set `view=''`.
  - Re-fetch pins on every `feed:params` whose filter keys changed (ignore `page`).
- [ ] **Step 2: `map.css`:** `body[data-view="map"] .feed-main{display:grid;grid-template-columns:7fr 3fr;gap:var(--gap)}`, `#feed-map{min-height:calc(100vh - var(--topbar-h) - 160px)}`, `< 1024` → stacked with the map 50 vh, `.map-loading` shimmer.
- [ ] **Step 3: `admin_map.js`** colour swap to tokens; radius 0; verify the admin Auctions tab map still mounts (open `/admin`, Auctions, toggle map).
- [ ] **Step 4: Tests** → green (no server change; `test_no_hardcoded_hex_outside_tokens` covers `static/deals/map.css`). `node --check` on `map.js`.
- [ ] **Step 5: Relaunch; smoke** `scripts/ui_smoke.py "/deals?view=map" "/deals?view=map&state=AZ"` 1280/390. Check: shimmer box → tiles + amber clusters; panel skeleton → `N lots in view`; panning re-filters the grid; `Show list` restores.
- [ ] **Step 6: Commit** `feat(deals): map view on the shared shell — panel states, token-coloured clusters`. **Done when:** #46 / #66 use `UI.load`, admin maps unaffected, smoke green.

---

## 9. Workstream F — `app.js` → ES modules per tab (mechanical, behaviour-preserving)

**Files:**
- Create: `static/admin/shared.js` (`$`, `$$`, `esc`/`escapeHtml`/`escapeAttr` → re-export `esc` from `../ui/state.js`, `queueRuns`, `shortUrl`, `applyState`, `row()`, `PLATFORM_LABELS`, `SUB_LABELS`, `SOURCE_NAMES`), `static/admin/launcher.js` (app.js 131-406), `drafts.js` (407-467), `auctions.js` (468-1116 + favorites 1117-1308), `inventory.js` (1309-1726), `inquiries.js` (1727-1822), `subscribers.js` (1823-1933), `listings_db.js` (1934-2118), `test_scrape.js` (2119-2233), `deals.js` (2234-3111), `tracking.js` (3112-3357), `static/admin/main.js` (imports all, owns `panels`, `activateTab`, `restoreLastTab`, clock — app.js 62-130; it stays `localStorage`-based here; E1 replaces it with URL state).
- Modify: `static/app.js` → deleted at the end of this workstream; `templates/index.html:787-788` → `<script src="/static/admin_map.js?v={{ asset_v }}"></script><script type="module" src="/static/admin/main.js?v={{ asset_v }}"></script>`.
- Create: `tests/web/test_admin_modules.py`.
- Do not touch: `index.html` markup (only the two script lines), `app.css`, `static/ui/*`.

**Interfaces:** each tab module exports `export function mount(panelEl)` (binds its listeners once — the code currently at module top-level that does `$('#…').addEventListener`) and `export async function load()` (the existing `loadX` loader). `main.js`: `const TABS = {launcher, drafts, auctions, inventory, inquiries, subscribers, 'listings-db': listingsDb, 'test-scrape': testScrape, deals, tracking}`; `activateTab(name)` calls `TABS[name].load?.()`. Cross-tab calls resolved through `shared.js` (never import one tab from another). `toast`/`withButtonLoading`/`apiFetch` → `import {toast, pending as withButtonLoading, api as apiFetch} from '../ui/state.js'` so the bodies stay byte-identical.

- [ ] **Step 1: Failing test** `tests/web/test_admin_modules.py`:
```python
import re, subprocess
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from automation.web import auth as auth_svc
from automation.web.app import app

ADMIN = Path("automation/web/static/admin")
TABS = ["launcher", "drafts", "auctions", "inventory", "inquiries", "subscribers", "listings_db", "test_scrape", "deals", "tracking"]

@pytest.fixture(autouse=True)
def _no_auth(monkeypatch):
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False); auth_svc.reset_caches(); yield; auth_svc.reset_caches()

def test_admin_shell_loads_modules_not_app_js():
    html = TestClient(app).get("/admin").text
    assert 'type="module" src="/static/admin/main.js' in html
    assert "/static/app.js" not in html
    assert not Path("automation/web/static/app.js").exists()

@pytest.mark.parametrize("tab", TABS)
def test_tab_module_exports_mount_and_load(tab):
    src = (ADMIN / f"{tab}.js").read_text()
    assert re.search(r"export (async )?function mount\(", src), tab
    assert re.search(r"export (async )?function load\(", src), tab
    assert not re.search(r"from '\./(?!shared)", src), f"{tab}: tabs import only shared.js / ../ui"

@pytest.mark.parametrize("f", sorted(ADMIN.glob("*.js")) if ADMIN.exists() else [])
def test_module_syntax(f):
    r = subprocess.run(["node", "--check", "--input-type=module"], input=f.read_text(), capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
```
Run → FAIL.
- [ ] **Step 2: Cut by section markers** (`app.js` `// ───` comments at 131, 407, 468, 1117, 1309, 1727, 1823, 1934, 2108, 2119, 2234, 3112). Move code verbatim; for every top-level `const X = …addEventListener` / `$('#id').addEventListener` block, wrap it in `export function mount()`. Guard `mount` with a module-level `let mounted = false`.
- [ ] **Step 3: Resolve cross-references** by `grep -n "<name>(" static/admin/*.js` for every function defined in one module and used in another → move to `shared.js`. Known ones: `queueRuns` (auctions, inventory), `startRun`/`applyState` (launcher, inventory), `esc`/`escapeHtml`/`escapeAttr` (all), `row` (drafts, inventory), `_fmtRemaining` (auctions only), `toast/apiFetch/withButtonLoading` (all → `../ui/state.js`).
- [ ] **Step 4: `main.js`** — imports, `panels`, `activateTab`, `restoreLastTab` (keep the TDZ note from `app.js:104-115` irrelevant now: call `mount()` for every tab first, then `restoreLastTab()`), clock, `connectStream()`/`connectScrapeStream()` boot calls, the two boot fetches (#5/#6 stay as-is here; E-shell migrates them).
- [ ] **Step 5: Delete `app.js`**; swap the script tags. `git grep -n "app\.js"` → fix any doc pointers under `docs/claude-reference/repo-layout.md` (one line).
- [ ] **Step 6: Tests** → green. Then **Relaunch and the real check:** `scripts/ui_smoke.py /admin` plus a manual pass: click all 10 tabs, open the Auctions map, star a lot, run a Deals search, open a Tracking drawer — **zero console errors** (`ReferenceError` = a missed cross-reference). Record the checklist result in the PR body.
- [ ] **Step 7: Commit** `refactor(admin): split app.js into ES modules per tab (no behaviour change)`. **Done when:** `app.js` gone, every tab works as before, tests + smoke green. Do **not** add skeletons here — that is E.

---

## 10. Workstream E — admin shell + tab-by-tab migration

### E1 — shell (`ui/e-admin-shell`)
**Files:** `templates/index.html` (extends `_base.html`; the 10 `<section class="panel" data-pane>` blocks are moved verbatim into `content`), `templates/login.html` (extends `_base.html`, `UI.pending` on the login button, drop its inline palette), `static/admin/shell.js` (replaces `main.js`: URL-param state), `static/admin/shell.css` (rail nav rules, moved from `app.css:59-130`), `tests/web/test_admin_tabs.py` (update the tab-count regex to the rail markup: `<a class="rail-tab(?: is-active)?" data-tab="` == 10).
**Interfaces:** rail nav `<a class="rail-tab" data-tab="deals" href="?tab=deals">`; `shell.js` exports `getParams()`/`setParams(patch, {replace=true})` that per-tab modules use for their own keys (each tab owns its key names; the shell only owns `tab`). `?tab=` replaces `localStorage.admin.lastTab` (migration: read it once, write it into the URL, delete the key). `popstate` → `activateTab`. Boot fetches #5/#6 → `UI.load(keepOld)` on `#phase-grid` / `#scrape-strip`. Topbar right = `#conn-dot` + `#clock`. `< 768` rail → drawer via `_base.html`.
- [ ] Failing test → shell → login → tests → relaunch → `ui_smoke.py "/admin?tab=deals" "/admin?tab=tracking" /admin/login` at 1280/390 → commit `feat(admin): shell on _base.html, rail nav, URL-param tab state`.

### E2–E10 — one branch per tab (`ui/e-launcher`, `ui/e-drafts`, `ui/e-auctions`, `ui/e-inventory`, `ui/e-inquiries`, `ui/e-subscribers`, `ui/e-listings-db`, `ui/e-test-scrape`, `ui/e-deals`, `ui/e-tracking`)
Each tab agent does the same five things in its own files only:
1. **Register the tab's fetch sites** from §2 (rows tagged `E-<tab>`) and convert each: reads → `UI.load(target, fetcher, {skeleton, count, keepOld, render, isEmpty, empty})`, mutations → `UI.pending(btn, label, fn)`, polls → `keepOld:true` + `UI.markStale` on failure, SSE → `markStale`/`clearStale` on `onerror`/`onopen`.
2. **Delete the tab's `Loading…` placeholder(s)** from `index.html` (§2 list) and ship the server-side skeleton twin (`{{ skeleton_cards(n) }}` / `skeleton_rows(n)` macro) inside `<div … data-state="loading">`.
3. **Move the tab's CSS** section out of `app.css` (line pointers in `blackwhole-ui-inventory.md` §4) into `static/admin/<tab>.css`, replacing per-tab prefixed classes with the shared ones where a 1:1 exists (`.inv-table/.ldb-table/.trk-table/#deal-table` → `.table`; `.deal-chip/.queue-chip/.cat-pill` → `.chip`; `.ldb-pager/.deal-pager` → `.pager` markup `Load more (N remaining)`; `.fav-card/.auction-card/.inq-card` → `.card` + a tab modifier). Delete hard-coded hex (14 off-token colours listed in the inventory §3a).
4. **URL-param state** for the tab's filters via `shell.js` `getParams/setParams` (Deals: `q, category, state, max_bids, ending, status, sort, page, profile`; Auctions: `source, q, profile, map`; Inventory: `status, q`; Tracking: `label`; Inquiries/Subscribers: `status`; Listings DB: `q, source, offset`) — replaces the `localStorage.admin.aucMapOn/dealMapOn` toggles with `?map=1`.
5. **Test + smoke:** `tests/web/test_admin_tab_<tab>.py` asserting the panel ships `data-state="loading"` with a skeleton and no `Loading…` text; `.venv/bin/python -m pytest tests/web/ tests/deals/ -q`; relaunch; `scripts/ui_smoke.py "/admin?tab=<tab>"`; a manual pass of every button in the tab. Commit `feat(admin/<tab>): loading/empty/error states + URL params`.

Tab-specific notes: **Deals** (#43–#55, biggest) keeps the tree/drawer/popovers; `loadDeals` uses `keepOld:true` so rows stay visible dimmed while re-querying (today they silently swap). **Auctions** favorites poll (#17) is the one that wedged the server — poll interval stays 30 s, but `markStale` replaces silent failure. **Inventory** inline edits (#24) mark the `<tr>` `is-pending`. **Launcher** keeps SSE; `applyState` unchanged. **Listings DB / Test Scrape**: skeletons + empty/error only, no redesign.

---

## 11. Workstream G — storefront light theme (optional, last, cuttable)

**Files:** `static/ui/tokens.css` (add `[data-theme="light"]` block: `--bg #F4F1EC; --surface #fff; --surface-2 #ECE7DD; --border #1A1A1A; --text #0B0B0B; --muted #6B6257; --accent #FF4A1C; --danger #B00020; --success #1f7a3a` — the storefront's own values from `public.css:7-19`, so nothing changes visually), `templates/_public_base.html` → extends `_base.html` with `<html data-theme="light">`, `static/site/site.css` (from `public.css`), `static/site/site.js` (from `public.js`, #68 → `UI.pending`), keep Archivo Black / Fraunces as `--display`/`--sans` overrides inside the light block. Storefront pages keep their markup. Tests: `tests/test_seo.py`, `tests/web/test_sold_showcase.py`, `test_catalog_feed_endpoint.py` stay green; smoke `/ /listings /sell`. **Do not start G until A–E are on `main`.** If time is short, G is the first cut.

---

## 12. Cuts (explicit — do not build)

- No accounts, alerts, "Remind me", newsletter, Pro/Lot Analyst, AI summary, flip score, blur-locked values, `Rate this source`, country picker, theme toggle on dark pages, gift/heart icons.
- No photos on any public row or card (hard rule) → no image proxy, no lazy-image placeholder, no horizontal mobile card.
- No `+N more at this location` / `+N more like this` clustering; no attribute chips beyond qty/unit/landed/status.
- No search beyond title `ILIKE` (existing); no "Best match" relevance sort; no ZIP / "near me" on the public feed (home distance is a hard-rule exclusion anyway).
- No infinite scroll; no page-number pager on `/deals` (Load more only). Admin tables keep their pagers but restyled.
- No sparkline on the detail page (text history list only); no third-party transport quote; no "Similar" beyond 4 cards.
- No `/` deals home page — `/` stays the chair storefront; `/deals` is the feed root.
- No wholesale `async def` → threadpool conversion (Task 0 follow-up ticket); no connection pooling; no RLS; no schema changes.
- No admin global search, keyboard shortcuts, or dark/light toggle; Listings DB and Test Scrape tabs get states only, no redesign.
- No SSR hydration fallback grid (`#feed-seo-fallback`) — the page is `noindex`.

---

## 13. Dispatch prompts (paste into a fresh `claude` session in `~/Projects/blackwhole/listing_automation`)

Common preamble for every prompt (copy it verbatim at the top):

> Start with `@../CLAUDE.md @CLAUDE.md` in context. Read `docs/superpowers/plans/2026-09-04-ui-rebuild-govauctions-clone.md` fully — §Global Constraints, §1 design decisions, §2 fetch register, §3 ownership, and your workstream section — then `docs/research/2026-09-04/govauctions-ui-map.md` and `docs/research/2026-09-04/blackwhole-ui-inventory.md`. Use superpowers:executing-plans. Work in a worktree: `git worktree add .claude/worktrees/<name> -b <branch> main && cd .claude/worktrees/<name>`. Only edit the files your section owns; if you need a change in a file you don't own, stop and report it. After every `app.py`/`templates/`/`static/` change: kill :8765 (`lsof -ti :8765 | xargs kill`) and relaunch `python -m automation.web` before testing in a browser. Gate: `.venv/bin/python -m pytest tests/web/ tests/deals/ -q` green (baseline 370) + `scripts/ui_smoke.py <your urls>` green + `node --check --input-type=module < <each new js>`. Never render `inventory.storage_note`; never add a public-deals exclusion outside `automation/web/public_deals.py`; never show photos/verdicts/distance on public rows; do not touch `_tracking_loop`. Commit after each green step; open a PR titled with the branch; report back: PR link, what shipped, what was cut, smoke screenshot dir.

**Task 0** — `fix/web-event-loop` — "…preamble… Do §4 Task 0 exactly: apply the diff from `docs/research/2026-09-04/web-hang-diagnosis.md`, add the `deal_listing` `to_thread` change, write `scripts/web_health_probe.sh`, relaunch, run the probe for the full 4 minutes and paste the `failures:` line in the PR. Do not diagnose anything — the diagnosis is done."

**A** — `ui/a-tokens-primitives` — "…preamble… Do §5 Workstream A, steps 1–14, in order. Tokens are verbatim from §1.3; `state.js` is verbatim from step 5 (extend, don't rewrite). Build `/admin/ui` as the design-review page and screenshot it at 1280 and 390 before you call it done; check it against §1 (radius 0, one bold element = mono price + timer, no hard-coded hex, empty/error copy in the interface's voice). Your `app.py` change is exactly the 3 lines in the file list."

**B** — `ui/b-deals-feed` — "…preamble… Prereq: A is on `main`. Do §6 Workstream B. Ship the §6 'Produces' contract exactly (ids, `feed:params` with `{params,total,rows}`, `feed:bbox` listener) — D is building the map against it in parallel. Delete `deals_public.js/.css`. Smoke `/deals`, `/deals?q=laptop`, `/deals?category=vehicles&sort=bid&dir=desc`, `/deals?q=zzzznomatch` at both widths."

**C** — `ui/c-lot-detail-sources` — "…preamble… Prereq: A on `main`. Do §7 Workstream C. `app.py` edits are limited to the `deal_listing` context and the new `/sources` route shown in the file list; every DB call in them is `await asyncio.to_thread(...)`. Keep every existing assertion in `tests/web/test_public_deals_api.py` passing. Smoke `/sources` and one live lot URL."

**D** — `ui/d-map-view` — "…preamble… Prereq: A on `main` (B may still be in flight — code against the §6 contract; if `deals_public.html` on your branch base lacks the `feed-map`/`feed-map-panel` ids, add a local stub only in your worktree and do not commit it). Do §8 Workstream D. `admin_map.js` API must not change — the admin Auctions/Deals tabs use it; verify them by hand. Smoke `/deals?view=map` and `/deals?view=map&state=AZ`."

**F** — `ui/f-admin-modules` — "…preamble… Prereq: A on `main`. Do §9 Workstream F. This is a mechanical split: bodies byte-identical, only `import/export/mount` wrappers added. No skeletons, no CSS, no markup changes beyond the two script tags. The gate is the manual 10-tab console-error pass in step 6 — paste the checklist in the PR."

**E1** — `ui/e-admin-shell` — "…preamble… Prereq: A and F on `main`. Do §10 E1. `?tab=` replaces localStorage; per-tab modules only get `getParams/setParams` from `shell.js` — do not migrate any tab's fetches (that's E2–E10)."

**E2–E10** — `ui/e-<tab>` — "…preamble… Prereq: E1 on `main`. Do §10 for the **<tab>** tab only: the §2 rows tagged `E-<tab>`, the tab's `index.html` panel block, `static/admin/<tab>.js` and new `<tab>.css` (moved out of `app.css`). Touch nothing else. Every read through `UI.load`, every mutation through `UI.pending`, polls `keepOld` + `markStale`. Smoke `/admin?tab=<tab>` and click every button in the tab."

**G** — `ui/g-storefront-light` — "…preamble… Prereq: A–E on `main`. Do §11. Visual output must be pixel-equivalent to today's storefront (same values, now as tokens). If `tests/test_seo.py` or the catalog feed tests move, stop."

---

## 14. Self-review (done while writing; re-run before dispatch)

- Spec coverage: topbar+search ✔ (A/B), category chips ✔ (B), side rail + mobile drawer ✔ (A/B), 4→1 grid ✔ (A), Load more (N remaining) ✔ (B), list/map toggle ✔ (B/D), card anatomy ✔ (§1.4, A), detail layout ✔ (C), sources ✔ (C), skeleton/aria-busy/pending/empty/error/stale ✔ (A §1.6), every fetch site assigned ✔ (§2, 68 + 2 new), workstreams with ownership/done-when/test/branch/prompt ✔ (§3, §5–§13), merge order ✔ (§3), Task 0 ✔ (§4), hard rules ✔ (Global Constraints), radius decided ✔ (§1.2), cuts ✔ (§12), one-session scope ✔ (E split per tab because 63 sites in one file is not a day).
- Placeholder scan: no TBD/TODO; every code step has the code or the exact rule.
- Name consistency: `UI.load/pending/api/skeleton/markStale/clearStale/renderEmpty/renderError/setBusy/toast/esc/fmt` (A) are the names used in B–G and in the tests; `card()/cardSkeleton()/tickTimers()` (A) used by B/C/D; ids `feed-*` (B) used by D; `mount()/load()` (F) used by E; `getParams/setParams` (E1) used by E2+.
