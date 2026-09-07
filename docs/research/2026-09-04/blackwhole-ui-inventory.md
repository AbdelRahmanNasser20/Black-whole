# BLACKWHOLE web UI inventory — 2026-09-04 (read-only)

Repo: `listing_automation/automation/web/` (FastAPI + Jinja2 + vanilla JS). Written incrementally; sections appended per step.

## Summary
- 10 HTML routes + ~85 JSON endpoints, all in one 3,062-line `app.py`; admin `/admin` = 790-line Jinja shell filled by a 3,357-line `app.js` (63 call sites, 104 functions); public storefront (`/`, `/listings`, `/sell`) is server-rendered; `/deals` is a client-fetch page over `/deals/api/*`.
- Three palettes coexist: admin dark "industrial terminal" (`app.css` `--bg #0c0c0e`, `--ink #ece8dd`, `--accent #ffb547`, JetBrains Mono / Instrument Serif / IBM Plex Sans, radius 0) — **this is the scheme to keep**; storefront light paper/orange (`public.css`) stays separate; `deals_public.css` + `login.html` are drifted copies to fold in.
- No shared primitives for card/chip/table/pager/pill/modal — each tab has its own prefixed set (`inv-`, `ldb-`, `deal-`, `trk-`, `inq-`, `fav-`); only `.btn .tab .seg .toast .dropdown .spinner .status-dot` are shared.
- Loading/empty/error all funnel through one `.drafts-empty` placeholder; ~60% of fetches show no pending state; no skeletons, no `aria-busy`, no URL-param state on `/admin` (only localStorage tab + map toggles); `/deals` does use URL params.
- Server on :8765 is wedged (17h uptime, every URL incl. `/api/health` times out at 30s) — timings unmeasurable until relaunched.
- Rebuild constraints: `/deals` exclusions live only in `public_deals.py`; relaunch required after any web change; `_tracking_loop` lives in the web process; status gates + image resolver + `storage_note` privacy stay server-side; RLS still off.

## 1. Routes

### HTML routes (route → template → render mode)
| Route | Handler line (app.py) | Template | Context passed | Mode |
|---|---|---|---|---|
| `GET /admin` | 294 | `index.html` | `phases`, `now` | shell only; everything client-fetched by `static/app.js` |
| `GET /admin/login` | 307 | `login.html` | `{}` | server shell + fetch to `/api/auth/login` |
| `GET /` | 485 | `landing.html` | `stats`, `featured` (+ `_public_ctx`) | server-rendered |
| `GET /listings` | 517 | `listings.html` | `items`, `sold_items`, `cities`, `chair_types` | server-rendered (JS filters client-side) |
| `GET /listings/{lot_id}` | 534 | `listing_detail.html` | `item`, `hero`, `images` | server-rendered |
| `GET /deals/{asset_id}/{account_id}/{auction_id}` | 553 | `deal_listing.html` | `lot`, `history`, `bidders`, `show_images` (operator only) | server-rendered |
| `GET /deals` | 815 | `deals_public.html` | `base_url`, `now`, `per_page_choices` | shell only; `static/deals_public.js` fetches `/deals/api/*` |
| `GET /sell` | 1242 | `sell.html` | `_public_ctx({})` | server-rendered |
| `GET|POST /alerts/unsubscribe` | 1436/1441 | inline HTML via `_do_unsubscribe` | token | server-rendered string |
| `GET /robots.txt`, `/sitemap.xml`, `/catalog/facebook.csv` | 1249/1269/1288 | none | – | plain text / XML / CSV |

### JSON API list (grouped)
- **Auth:** `GET /api/auth/status` (315), `POST /api/auth/login` (326), `POST /api/auth/logout` (356)
- **Deals (admin JSON):** `GET /api/deals` (658), `GET /api/deals/geo` (738), `GET /api/geo/zip` (795), `GET /api/deals/tree` (869), `GET /api/deals/{asset}/{acct}/{auction}` (1223)
- **Deals (public JSON, lives under `/deals/api/`):** `GET /deals/api/lots` (822), `GET /deals/api/pins` (844), `GET /deals/api/facets` (861)
- **Deal lists:** `GET|POST /api/deals/lists` (921/930), `DELETE /api/deals/lists/{id}` (943), `PUT|DELETE /api/deals/lists/{id}/items/{asset}/{acct}/{auction}` (951/962)
- **Deal tags:** `GET /api/deals/tags` (975), `PUT|DELETE /api/deals/tags/{asset}/{acct}/{auction}/{tag}` (983/996)
- **Saved searches:** `GET|POST /api/deals/searches` (1008/1016), `DELETE /api/deals/searches/{id}` (1034)
- **Research profiles:** `GET|POST /api/profiles` (1044/1051), `DELETE /api/profiles/{slug}` (1067), `GET /api/profiles/{slug}/outcomes` (1081)
- **Tracking:** `GET|POST /api/tracking` (1145/1152), `PATCH|DELETE /api/tracking/{asset}/{acct}` (1169/1182), `GET .../history` (1190), `POST /api/tracking/sync` (1203)
- **Public forms:** `POST /contact` (1327), `POST /subscribe` (1375)
- **Pipeline runs:** `GET /api/runs/state` (1448), `POST /api/runs/start|queue` (1506/1507), `POST /api/lots/{lot_id}/remove` (1541), `GET /api/lots/status` (1555), `POST /api/runs/queue/clear` (1561), `POST /api/runs/cancel` (1570), `POST /api/runs/stdin` (1581), `GET /api/runs/stream` (SSE, 1592), `GET /api/drafts` (1678)
- **Images:** `GET /image/{folder}/{name}` (1709), `GET /screenshot/{folder}/{name}` (1720)
- **Scrape (auction_extractors):** `POST /api/scrape/start|cancel` (2020/2038), `GET /api/scrape/state` (2050), `GET /api/scrape/stream` (SSE, 2055), `GET /api/test-scrape` (2275)
- **Auctions:** `GET /api/auctions` (2137), `POST /api/auctions/refresh` (2203), `GET|POST /api/auctions/favorites` (2318/2331), `DELETE /api/auctions/favorites/{asset_id:path}` (2356), `POST /api/auctions/favorites/test-telegram` (2364), `GET /api/auctions/cache-stats` (2552)
- **Listings/health:** `GET /api/listings` (2561), `GET /api/health` (2662, plain text)
- **Inventory ledger:** `GET|POST /api/inventory` (2695/2729), `GET|PATCH|DELETE /api/inventory/{lot_id}` (2707/2715/2756), `POST .../platform` (2767), `GET|POST|DELETE .../buyer-cert` (2809/2795/2823), `GET /api/inventory-stats` (2831), `GET /api/site-config` (2836), `POST /api/inventory/seed-snapshot` (2841), `POST /api/inventory/backfill` (2886)
- **Inquiries / subscribers / alerts:** `GET /api/inquiries` (2956), `PATCH|DELETE /api/inquiries/{id}` (2961/2979), `GET /api/subscribers` (2991), `PATCH|DELETE /api/subscribers/{id}` (2996/3012), `POST /api/alerts/blast/{lot_id}/preview` (3027), `POST /api/alerts/blast/{lot_id}` (3036)

Count: 10 HTML routes, ~85 JSON/other endpoints, all in one 3,062-line `app.py` (no `APIRouter` split).

## 2. Templates

| Template | Lines | `fetch(` in file | `id="` | `class="` | Base | Scripts/CSS loaded | Render mode |
|---|---|---|---|---|---|---|---|
| `index.html` (admin) | 790 | 0 (all in app.js) | 150 | 416 | none (standalone) | `app.css`, `app.js`, `admin_map.js` | **client-fetch**; Jinja only injects `phases`/`now` |
| `deals_public.html` | 130 | 0 (in deals_public.js) | 22 | 67 | none (standalone, own `<head>`) | `deals_public.css`, `deals_public.js`, `admin_map.js` | **client-fetch** (`/deals/api/facets`, `/deals/api/lots`, `/deals/api/pins`) |
| `landing.html` (`/`) | 193 | 0 | 5 | 83 | `_public_base.html` | `public.js` | server-rendered |
| `listings.html` | 178 | 0 | 8 | 74 | `_public_base.html` | `public.js` | server-rendered; client-side filter chips only |
| `listing_detail.html` | 152 | 0 | 3 | 39 | `_public_base.html` | `public.js` | server-rendered |
| `sell.html` | 59 | 0 | 1 | 26 | `_public_base.html` | `public.js` | server-rendered |
| `deal_listing.html` | 108 | 0 (in deal_card.js) | 1 | 16 | none (standalone, 1 inline `<style>`) | `deal_card.js` | server-rendered + 1 fetch (`/api/deals/{a}/{b}/{c}`) |
| `login.html` | 116 | 1 (`/api/auth/login`) | 6 | 7 | none (standalone, inline `<style>` w/ own dark palette) | – | shell + fetch |
| `_public_base.html` | 102 | 0 | 0 | 29 | (base) | `public.css` | layout: header/nav/footer |
| `_subscribe_form.html` | 101 | 0 | 2 | 18 | partial | – | included by landing/listings; `public.js` posts `/subscribe` |

Static JS/CSS sizes: `app.js` 3,357 · `app.css` 1,430 · `public.css` 724 · `deals_public.js` 192 · `deal_card.js` 179 · `admin_map.js` 174 · `public.js` 139 · `deals_public.css` 115.

Three separate visual systems coexist:
1. **Admin** (`index.html` + `app.css`) — dark dashboard.
2. **Public storefront** (`_public_base.html` + `public.css`) — light "paper/ink" brand (`--paper #F4F1EC`, `--ink #0B0B0B`, `--accent #FF4A1C`, Archivo Black / Fraunces / JetBrains Mono).
3. **`/deals` public + `login.html`** — each with its own small palette (`deals_public.css`; login inline: `--bg #0c0c0e`, `--accent #ffb547`, IBM Plex Sans).

## 3. Palette (the scheme the rebuild must KEEP)

### 3a. Admin console — `static/app.css` lines 1-28 (header comment: "industrial terminal. Off-black canvas, paper-cream ink, hairline grids, monospace IDs. No gradients, no glassmorphism.")
| Role | Token | Value |
|---|---|---|
| background | `--bg` | `#0c0c0e` |
| surface | `--bg-elev` / `--bg-elev-2` | `#131316` / `#1a1a1f` |
| border | `--line` / `--line-bold` | `#25252c` / `#3a3a44` |
| text | `--ink` | `#ece8dd` (warm cream) |
| muted | `--ink-mute` / `--ink-dim` | `#9e988a` / `#5a5650` |
| accent | `--accent` | `#ffb547` (burnt amber, brand); `--accent-2` `#c2f7c4` |
| danger | `--err` | `#ff6c4d` |
| success | `--ok` | `#6cd47e` |
| warn / info | `--warn` / `--info` | `#ffb547` / `#7ab9ff` |
| fonts | `--mono` / `--display` / `--sans` | JetBrains Mono, ui-monospace, SF Mono, Menlo / Instrument Serif, New York, Georgia / IBM Plex Sans, system-ui |
| spacing/radius | `--pad` / `--radius` | `28px` / `0px` ("hard corners") |

- Radii actually used (drift from `--radius:0`): 2px, 3px (chips/thumbs), 4px (inputs, selects, cards l.694/852/1173), 8px (tracking cards l.1286/1294), 10px (map l.1400), 12px (drawers/modals l.1304/1413).
- Shadows: `0 8px 24px rgba(0,0,0,.4/.5)` (dropdown/modal l.544/1316), `0 4px 16px rgba(0,0,0,.35)` (l.1103), `-12px 0 32px rgba(0,0,0,.5)` (side drawer l.1337), `inset 0 -2px 0 var(--accent)` (active tab l.104/593), status-dot glow `0 0 8px var(--ok)`.
- Body font: `--sans`; nearly every label/ID/meta uses `--mono` at 10-13px with letter-spacing; headings use `--display`.
- Top hard-coded colors (count): `#ffcf3d` ×5 (yellow highlight, off-token), `#000` ×3, `#ffb547` ×2, `#f85149` ×2, `#4ade80` ×2, `#1a1408` ×2, then singletons `#fff #ffbd2e #facc15 #f87171 #58a6ff #161616 #0e0e0e #0a0a0a #08080a`; ~20 `rgba()` overlays (`rgba(255,181,71,0.04/0.05)` accent tints, `rgba(255,255,255,.04-.14)` hairlines, `rgba(12,12,14,0.85)` scrim). Total ~27 distinct hex → 14 are off-token drift (GitHub-ish `#58a6ff/#f85149/#4ade80/#facc15` from the deals/tracking additions).

### 3b. Public storefront — `static/public.css` lines 7-19 (light "paper/ink" brand)
`--ink #0B0B0B` · `--paper #F4F1EC` · `--paper-2 #ECE7DD` · `--rule #1A1A1A` · `--accent #FF4A1C` (orange) · `--accent-ink #0B0B0B` · `--muted #6B6257` · `--stamp-bg #FFE9DF` · `--shadow 0 14px 30px -22px rgba(0,0,0,.6)`.
Fonts: `--f-display` Archivo Black · `--f-body` Fraunces (serif) · `--f-mono` JetBrains Mono (Google Fonts loaded in `_public_base.html` l.28). Radius 0 everywhere; hover = hard offset shadow `3px 3px 0 0 var(--ink)` / `5px 5px 0 0` (l.182/297). Hard-coded: `#3a3731` ×4, `#aaa299` ×3, `#B00020` ×2 (error), `#6a655c` ×2.

### 3c. Public `/deals` — `static/deals_public.css` lines 5-10 (third, separate dark palette)
`--bg #0f1216` · `--bg-2 #161a21` · `--bg-3 #1d222b` · `--line #2a303b` · `--ink #e8e9ec` · `--ink-2 #a6adba` · `--ink-3 #6f7784` · `--accent #ffb020` · `--ok #3fb950` · `--warn #ff5c5c`. Fonts: Archivo Black / IBM Plex Sans / JetBrains Mono. `deals_public.html` itself has no colors.

### 3d. `login.html` inline `<style>` l.10-14 — copies the admin tokens (`--bg #0c0c0e … --accent #ffb547`, Plex Sans + JetBrains Mono).

**Verdict:** the admin palette (3a) is the canonical dark scheme; 3c is a near-duplicate with drifted values (`#ffb020` vs `#ffb547`, cooler greys). The storefront (3b) is deliberately different (light, orange) and should stay so. A rebuild should collapse 3a/3c/3d into one token file.

## 4. Components in `static/app.css` (line pointers)

Section map (comment headers): topbar l.59 · layout l.123 · launcher form l.171 · phase grid l.235 · price prompt l.303 · console l.324 · drafts l.362 · footer l.456 · queue strip l.469 · scrape strip l.493 · dropdown l.535 · auctions l.560 · star button l.683 · favorites strip l.701 · responsive l.816 · Listings DB (tab 07) l.1008 · toast + feedback primitives l.1087 · cache header/staleness banner l.1115 · test scrape (08) l.1167 · Deals tab l.1193 · category tree l.1211 · deal browser (verdicts/lists/tags/searches) l.1257 · popover l.1312 · comps drawer l.1332 · launcher copy fields l.1356 · Tracking (11) l.1365 · map view l.1397 · category pills l.1408 · active-filter chips l.1421.

| Asked-for component | What exists | Lines |
|---|---|---|
| `.card` | **no generic `.card`**. Per-feature: `.auction-card` (606), `.fav-card` (744, 10 rules), `.inq-card` (956), `.phase` cards (252-300) | 606 / 744 / 956 / 252 |
| `.chip` | **no generic `.chip`**. `.queue-chip` (486), `.deal-chip` + `.deal-chip-x` (1284/1288), `.deal-search-chip` (1301), `.deal-active-chip` (1422), `.deal-tag` (1282) | 486 / 1282-1301 / 1422 |
| `.tab` | `.tab`, `.tab:hover`, `.tab.active` (inset amber underline), `.tab-num`, `.tab-label`; `.panel` fade-in (130) | 93-110, 130 |
| `.pager` | **no generic**. `.ldb-pager` (1070), `.deal-pager` + `.deal-pager select` (1206/1224-1225) | 1070 / 1206 |
| `.table` | **no generic**. `.diff-table` (823), `.inv-table` (878, 9 rules incl. inline `input`/`select` 914-915), `.ldb-table` (1010), `#deal-table` (1208), `.trk-table` (1369-1370) | 823 / 878 / 1010 / 1208 / 1370 |
| `.modal` | **no modal class**. Overlay patterns: `.dropdown-menu` (539-555), shared floating popover (1312-1330, `.deal-pop-*`), `.deal-drawer` fixed-right comps drawer (1333, 9 rules), `.trk-drawer` (1383) | 539 / 1312 / 1333 / 1383 |
| `.toast` | `.toast-container` (1089), `.toast`, `.toast-in/out`, `.toast-info/ok/err` (left-border colour) | 1089-1110 |
| `.badge` | **none by that name**. Pills: `.scrape-pill[data-status]` (504-509), `.inq-status-pill` (984), `.src-pill` (1060), `.ts-source-pill`/`.ts-miss-pill` (1178/1187), `.cat-pill` (1409, 6 rules) | see line list |
| `.seg` | `.seg`, `.seg-btn`, `.seg-btn.active` (segmented control) | 582-593 |
| `.pill` | see `.badge` row; no base `.pill` | – |
| buttons | `.btn`, `.btn-primary`, `.btn-small`, `.btn-ghost`, `.btn-glyph`, `:disabled` opacity .4, `.btn.is-loading` (opacity .7 + `cursor:progress`) | 213-233, 1113 |
| status | `.status-dot`, `.status-dot.live` (glow), `.stat`/`.auction-stat`/`.inq-stat`/`.phase-status[data-status]` | 116-120, 285, 596, 989 |
| loading/empty | `.spinner` (376), `.drafts-empty` + `.drafts-empty.loading` (368/373), `.deal-pop-empty` (1325), `.deal-thumb-empty` (1201) | 368-376 |
| strips | `.queue-strip` (471), `.scrape-strip` (495), `.fav-strip` (702), `.inv-strip`/`.strip` (858/863) | – |
| inputs | `.url-field` (181), `.opt`/`.opts` (205-207), `.inv-add-form/-grid` (838-850), `.ts-input` (1170), `.deal-controls input/select` (1194) | – |

**Takeaway:** zero shared primitives for card / chip / table / pager / pill / modal — every tab re-declares its own (`inv-`, `ldb-`, `deal-`, `trk-`, `inq-`, `fav-`, `ts-` prefixes). Only `.btn`, `.tab`, `.seg`, `.toast`, `.dropdown`, `.spinner`, `.status-dot` are truly shared.

## 5. Loading / empty / error states

**Primitives (app.js top):** `toast(msg, kind)` (l.~10-27, `.toast-info/ok/err`, auto-dismiss) · `withButtonLoading(btn, text, fn)` (l.31-45: disables + relabels + `.is-loading`) · `apiFetch(url, opts)` (l.49-60: throws on `!ok` with `detail`). One CSS placeholder class does all the work: `.drafts-empty` (+ `.loading` + `<span class="spinner">`), reused by every tab as loading text, empty text AND error text.

**Templates ship initial placeholders (index.html):** "Loading drafts…" l.170 · spinner "fetching listings from cache…" l.279 · "Loading inventory…" l.363 · "Loading inquiries…" l.398 · "Loading subscribers…" l.432 · "Loading tracked lots…" l.486 · deal-stats "Loading…" l.498 · tree "Loading…" l.555 · "Loading deals…" l.612 · spinner "querying listings.db…" l.720. `deals_public.html` l.113 "Loading…" in `.sr-empty`. No skeletons, no `aria-busy` anywhere.

**Empty-state copy (app.js):** "No listing folders yet. Run a pipeline." 415 · "Cache is empty for {source}. Hit ⟳ scrape now" 918 · "No rows. Click ↓ backfill…" 1343 · "No inquiries yet. Share /listings…" 1746 · "No alert signups yet…" 1850 · "No rows match these filters." 2002 · "0 listings for “q”" 2180 · tree "no data" 2291. Errors are inline text in the same placeholder: "Error loading auctions" 868, "Load failed" 1327/1739/1843, "Query failed" 1979, "tree error" 2330, "deals API error" 2388.

**Fetch call sites.** `grep -c "fetch("` = **24 raw `fetch(`** + **39 `apiFetch(`** = 63 network call sites in `app.js` (plus 5 across `deals_public.js`/`deal_card.js`/`public.js`).

Raw `fetch(` sites (line → URL → pending state shown?):
| L | URL | Pending state |
|---|---|---|
| 50 | (apiFetch wrapper) | – |
| 286 | `POST /api/runs/start` | button via withButtonLoading (caller) |
| 402 | `GET /api/runs/state` (boot) | none |
| 404 | `GET /api/scrape/state` (boot) | none |
| 412 | `GET /api/drafts` | text "Scanning ~/Desktop/…" l.411 |
| 713 | `GET /api/scrape/state` (after SSE) | none |
| 1305 | `GET /api/runs/state` (poll, 1293 setInterval) | none |
| 1320 | `GET /api/inventory?with_stats=1` | "Loading…" row l.1316 |
| 1442 | `PATCH /api/inventory/{lot}` (inline edit) | none (cell just updates or toast err) |
| 1525 | `PATCH /api/inventory/{lot}` | button loading l.1523 |
| 1550, 1569 | inventory buyer-cert POST/DELETE | button loading |
| 1735 | `GET /api/inquiries` | "Loading…" l.1733 |
| 1839 | `GET /api/subscribers` | "Loading…" l.1837 |
| 2324 | `GET /api/deals/tree` | none (stale tree stays until replaced) |
| 2384 | `GET /api/deals?…` | **none** — table keeps old rows until swap (only template's initial "Loading deals…") |
| 2798 | `GET /api/deals/lists`, `/tags`, `/searches` (loadDealMeta) | none |
| 2910 | `DELETE /api/deals/searches/{id}` | none |
| 2964 | `PUT/DELETE /api/deals/lists/{id}/items/{key}` | none (optimistic-ish) |
| 2980, 2986 | `POST /api/deals/lists` + `PUT …/items` | none |
| 3037, 3051 | `DELETE/PUT /api/deals/tags/{key}/{tag}` | none |
| 3088 | `POST /api/deals/searches` | none |

`apiFetch(` sites (39): runs cancel/clear/stdin 317/328/345 (button) · profiles GET/POST/DELETE 527/568/581 (POST via button) · auctions refresh 600 (button) · scrape start/cancel 646/660 (button) · cache-stats 820 (none) · `GET /api/auctions` 857 (spinner l.842 + `auc.loading` guard) · favorites GET/DELETE/POST/test-telegram 1121/1137/1142/1224 (none / button) · runs queue 1299 (button) · lots remove 1472, inventory DELETE 1486, runs start 1593, platform 1623/1645, backfill 1671, inventory POST 1704 (all button) · inquiries PATCH/DELETE 1784/1800, subscribers 1895/1911 (none) · `GET /api/listings` 1966 (spinner l.1963) · test-scrape 2154 (spinner l.2148) · profile outcomes 2578 (none) · deals geo 2713, geo zip 2779 (none) · tracking GET/history/DELETE/PATCH/POST/sync 3144/3229/3240/3252/3329/3346 (sync via button; list = "Loading tracked lots…" only on first paint).

**Net:** ~60% of call sites show no pending state at all; mutations rely on `withButtonLoading` when triggered by a button, and on toasts for errors. Reads after the first paint replace content silently (stale-while-loading with no indicator). Streaming: two `EventSource`s (`/api/runs/stream` l.361, `/api/scrape/stream` l.703) + two `setInterval` polls (l.126 clock, l.1293 runs state).

Other JS: `deals_public.js` — facets 72 (silent fallback), lots 137 ("Loading…" row + error row), pins 163 (map note "map data unavailable"). `deal_card.js` 166 — overlay "loading lot …" + error state. `public.js` 34 — `POST /subscribe|/contact` form.

## 6. Rendering pattern + where state lives, per page

| Page | Rendering | State location |
|---|---|---|
| `/admin` (`index.html` + `app.js`) | Jinja shell (11 tab panels pre-rendered with placeholders) → every panel filled by `fetch` + `innerHTML` template strings (`renderAuctionCard`, `renderInvTable`, `renderInquiries`, `renderSubscribers`, `renderTrackingRows`, `loadDeals`…). Two SSE streams + 2 polls. | **Module-level globals**: `panels` l.64, `auc` l.470, `_ldb` l.1936, `_ts` l.2124, `deal` l.2234 (~20 fields), `_dealTree` l.2287, `trk` l.3117, `PLATFORM_LABELS`/`SUB_LABELS`. **localStorage**: `admin.lastTab` (l.77/97/118), `admin.aucMapOn` (1002/1009), `admin.dealMapOn` (2761/2768). **URL params: none** — no `history.*`, no `location.hash`; deal filters/paging/sort are not shareable or refresh-safe. `URLSearchParams` used only to build API query strings (845/1318/1945/2362/2696). |
| `/deals` (`deals_public.html` + `deals_public.js`) | Jinja shell → `fetch` `/deals/api/facets|lots|pins` → `innerHTML` rows; map via shared `admin_map.js`. | **URL params are the state** (`KEYS` l.15 read from `location.search`, written back with `history.replaceState` l.26). localStorage: `sr.about` (open/closed, l.187). |
| `/`, `/listings`, `/listings/{id}`, `/sell` | Fully Jinja server-rendered off `_public_base.html`; `public.js` handles subscribe/contact POST + client-side chip filter on `/listings`. | none (server) |
| `/deals/{a}/{b}/{c}` (`deal_listing.html`) | Jinja server-rendered; `deal_card.js` opens an overlay that fetches `/api/deals/{a}/{b}/{c}` on demand. | none |
| `/admin/login` | Jinja shell + inline fetch to `/api/auth/login`. | cookie session (server) |

Escaping: three separate HTML-escape helpers (`esc`, `escapeHtml`, `_dealEsc` l.2793) — inconsistent; most renderers interpolate into template strings.

## 7. Sizes

| File | Lines |
|---|---|
| `static/app.js` | 3,357 (104 top-level functions) |
| `static/app.css` | 1,430 |
| `templates/index.html` | 790 (150 ids, 416 class attrs) |
| `templates/deals_public.html` | 130 |
| `app.py` | 3,062 (all routes in one module) |
| `static/public.css` / `deals_public.css` | 724 / 115 |

Biggest functions in `app.js` (start line, approx span to next top-level def):
1. `onInvAction` l.1459 — 154 lines (inventory row action dispatcher: queue/remove/delete/edit/platform/cert)
2. `shortUrl` l.253 — 106 (block includes `startRun` and SSE wiring beneath it)
3. `renderAuctionCard` l.1015 — 104
4. `loadDeals` l.2358 — 99
5. `renderSubscribers` l.1847 — 97
6. `renderProfileSeg` l.541 — 93
7. `renderInquiries` l.1743 — 92
8. `renderTrackingRows` l.3186 — 79
(then `renderInvTable` 1340/77, `flashRow` 1658/73, `openDealDrawer` 3000/70, `applyDealSearch` 2854/70)

## 8. Timing (2026-09-04, `curl --max-time 30`)

| URL | HTTP | time |
|---|---|---|
| `/` | 000 | 30.0s timeout |
| `/deals` | 000 | 30.0s timeout |
| `/admin` | 000 | 30.0s timeout |
| `/deals/api/lots?per_page=2` | 000 | 30.0s timeout |
| `/api/profiles` | 000 | 30.0s timeout |
| `/listings`, `/api/health` | 000 | 30.0s timeout |

**The running server is wedged**, not slow: PIDs 84800/84817 listening on `127.0.0.1:8765`, up 17h09m, 0% CPU, state `SN`; even `/api/health` (plain text) never answers. Consistent with the whole event loop blocked (likely a sync DB call against Supabase at the 500 MB read-only line, or the in-process `_tracking_loop`). Not restarted (read-only task). Re-measure after `kill` + `python -m automation.web`.

## 9. Hard rules from CLAUDE.md that constrain a rebuild

- **Public `/deals` policy (verbatim):** "Public `/deals` never shows auction photos, verdicts, home distance, seating lots, or any lot in `tracked_lots`/`auction_favorites`/`deal_list_items`. The policy is `automation/web/public_deals.py` — add exclusions there (env `PUBLIC_DEALS_EXCLUDE_*`), never in a template. Public JSON lives under `/deals/api/`, not `/api/`. SQL regexes use `\y` word boundaries (Postgres reads `\b` as backspace)."
- **Relaunch rule (verbatim):** "Changes to `automation/web/app.py`, `templates/`, or `static/` require killing and relaunching `python -m automation.web`." (No hot reload; explains the 17h-stale process above.)
- **No template-side exclusions:** exclusions are server-side in `public_deals.py` only; a rebuilt frontend must never filter/hide protected lots in JS or Jinja.
- **Tracking runs in the web process:** "`tracked_lots` polling runs **in the web process** (`_tracking_loop`), not a Render cron" — a rebuild that swaps the server (e.g. to a separate SPA + API) must keep or relocate this loop.
- **Status gating stays server-side:** "The FB catalog feed and CRM recommendations are status-gated (`CATALOG_FEED_STATUSES` / `ROUTABLE_STATUSES`)… Keep it that way." Sold showcase rule: `status IN ('sold_out','lost_sold_out') AND quantity_original > 0 AND has a photo`.
- **Ledger edits only via the API:** "Delete/edit via the admin Inventory tab or `DELETE /api/inventory/{lot_id}`, don't hack the DB directly." Re-upsert preserves `quantity_remaining`, `status`, `price_per_chair`, `hero_image`, platform URLs.
- **Images:** resolve only through `automation/lot_images.py` (`durable DB URLs → local disk → nothing`); never write Supabase Storage URLs; `storage_note` is private and must never render.
- **Launch button** on Auctions cards enabled only for GovDeals URLs.
- **RLS is disabled** on all Supabase tables — "Must add `service_role`-only policies before any storefront launch or multi-user dashboard."
- **Sends:** anything writing to FB respects `MAX_SENDS_PER_DAY` / `MIN_SECONDS_BETWEEN_SENDS`.
- **DB at the 500 MB read-only line**; batch UPDATEs only; new blob columns need an archival path first.
