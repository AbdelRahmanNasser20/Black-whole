# govauctions.app — UI map (2026-09-04)

Mapped in Chrome, page by page. Sections appended as each page was captured.

## 1. /feed (desktop 1280)

**Skeleton (dark theme by default)**
- Topbar (single row, ~44px, centered max-width container): logo `🏛 GovAuctions` · country picker `🇺🇸 US ▾` · centered search input (placeholder "Search auctions…", magnifier icon) · right nav links `Guides · Tools · Prices · About · Pro(blue)` · pill CTA `✉ Get weekly deals` (blue) · sun icon (theme) · gift icon · heart icon (favourites) · `Sign In`.
- Left sidebar (~205px, fixed, collapsible via `‹` chevron): heading "Filters".
  - **Location**: helper text; `⌖ Use my location` button; ZIP code input with `→` submit.
  - **Bids**: segmented pills `Any | No bids | ≤ 3 bids` (Any active = blue fill); helper "Shows lots from sources that report live bid counts."
  - **Price Range**: two inputs `Min – Max`.
  - Nothing else in the sidebar (it does not scroll).
- Main column:
  - Row 1: horizontal category chip strip, scrolls sideways, overflows right edge: `All (active, blue) · 🚗 Vehicles · 🏠 Real Estate · 🖥 Electronics · Military · 🔧 Tools · Equipment · 💍 Jewelry · Collectibles · Furniture · Medical · Seized · Sporting · Materials · Catering · Appliances · Art & Decor · Janitorial · Other`. Each chip = icon + label, dark pill, 1px border.
  - Row 2: sort select `Best deals ⇅` (options: Best deals / Newest / Ending soonest / Price: Low → High / Price: High → Low) · ZIP code input `→` (duplicate of sidebar) · `🗺 Show on map` toggle button · far right `🔔 Create Alert` (outline blue, needs account — not clicked).
  - Row 3: result count line `46,860 auctions across 18 sources` (count bold, rest muted).
  - Card grid: 4 columns at 1280, ~12px gutters.
  - Bottom: single `Load more (37702 remaining)` button — no page numbers, no infinite scroll.
- Floating: sparkle/AI button bottom-right (blue circle), orange plugin-ish icon mid-right (browser extension, ignore).
- Footer: none visible on /feed (grid runs to the Load more button).

**Card anatomy** (dark card, 1px border, radius ~8px)
1. Image slot 4:3-ish (~275×155), full-bleed, top corners rounded; watermark from source visible. Heart (favourite) button top-right, dark circle overlay. Image loads lazily — empty dark slot until loaded (see loading note).
2. Meta row: **price** bold `$625` · optional `Top deal` badge (small dark-blue pill, blue text) · right-aligned `⏱ 3h 47m` ending-in.
3. Title, 1–2 lines, white 13–14px.
4. Sub row (muted 11px): `GovDeals · Phoenix, AZ · 14 bids` — source name text (no logo badge), location, bids (bids in blue when >0).
5. Optional link `+4 more at this location →` (blue).
6. Optional attribute chips row: `120,842 mi`, `V8`, `Diesel`, `AWD` (tiny grey pills, right- or left-aligned).
7. Optional under-card pill `+4 more like this` (centered, dark pill) — dedupe/cluster.

**Controls**: filters = left sidebar (location/bids/price) + top chip strip (category) + inline row (sort, zip, map, alert). Search = topbar input (behaviour tested in §7). Map/list toggle = `Show on map` button (→ `?view=map`). Pagination = `Load more (N remaining)` button. Sort = native-style select.

**Loading state**: image slots render as empty dark rectangles first, then fill in (lazy images). Result count + cards were present on first screenshot → list is server-rendered/hydrated fast; no skeleton/shimmer seen for the grid (checked again in §2/§7 reload).

**Empty/error**: none visible.

## 2. /feed?view=map

**Skeleton**: same topbar + left sidebar + chip strip + control row. Map toggle button becomes `☰ Show list`. Result-count line disappears. Main area splits ~70/30:
- Left: Leaflet map (CARTO light basemap, © Leaflet | © OpenStreetMap | © CARTO attribution bottom-right), `+ / −` zoom stack top-left, rounded container ~12px, whole-world initial view. **Clusters** = blue circles with count (`46k`, `355`, `178`, `55`, `13`), white 2px ring. Tiles show "API KEY REQUIRED carto.com/basemaps/api/key" watermark → basemap key missing/expired (visible defect).
- Right: bordered panel, header `46,473 auctions in view` (bold) + "Zoom or pan the map to narrow this list" + muted note "384 more auctions have no listed location and aren't shown on the map - switch to the list to see them." Below: vertical single-column list of the same feed cards (same anatomy as §1), scrolls inside panel.

**Loading state**: on first paint the map container is an empty dark rounded box and the panel says `0 auctions in view` + "No auctions in the current map area - zoom out to see more." (this doubles as the **empty state** copy). ~2–3 s later tiles + clusters + cards fill in. No spinner, no skeleton.

**URL**: `?view=map`. Filters/sort stay identical in both views.

## 3. Detail page — /auction/2020-chevrolet-tahoe-arizona-govdeals-123-6876

URL pattern: `/auction/<slug>-<state>-<source>-<id>` (SEO slug, title-cased page `<title>` "… - Government Auction in Phoenix, AZ").

**Skeleton** (no sidebar; centered ~810px content + 300px right rail)
- Topbar unchanged. `← Back` link (blue) under it.
- Hero: full-width rounded box ~380px tall, image left (natural aspect, letterboxed on black), `⛶ View images` dark pill bottom-left (opens gallery).
- Title H1 `2020 Chevrolet Tahoe` (~22px bold) · breadcrumb-ish row `GovDeals  Vehicles  📍 Phoenix, AZ` (muted, source in white).
- Left column, stacked bordered cards (radius ~10px, header row with tinted bg):
  1. `✦ GovAuctions Summary` — AI paragraph + footnote "Estimated flip margin and comp range available with Pro. Summary may occasionally contain inaccuracies…"
  2. `Seller` — 🏛 Buckeye, AZ · `Condition: As-is`.
  3. `Pickup / shipping information` — 📍 address (blue link) + city, 📅 "Pickup within 10 business days", 🚚 as-is note, 📦 "Tap to request a 3rd party transport quote — est. $875 to $1,450 ▾" (accordion), "Grab more in one trip: 4 more lots at this pickup location." + `View all at this location →`.
  4. `Full Description` — raw seller text (spec table as plain lines).
  5. Alert CTA block "Get notified about new vehicles auctions like this" + `Create Free Alert` (not clicked).
  6. `Similar Auctions` — mini cards (title, price, `Vehicles · AZ`, attribute chips) + "See all Vehicles in Arizona / 196 more live vehicles auctions / Browse all →".
  7. Market-data links (Surplus Price Index, Market Report).
- Right rail (sticky):
  1. **Bid card**: `Current Bid  ● LIVE` (green dot) / big `$84`… note: rail shows `$84`, page text `$168`, card said `$625` — value changes on refresh (live poll, "Updated just now"). `18 bids` · `Time Left ⏱ 3h 44m` · `Ends Sep 4, 2026` · `🔔 Remind me before it ends` (blue link). `Est. all-in · incl. ~10% premium  $98 ▾`. `Bid history since Aug 27` sparkline (blue line) with `$360 → +$265 (+74%) → $625`. Primary CTA `Bid Now on GovDeals →` (blue, full-width, ~44px). Secondary row `♡ Save` · `⤴ Share`.
  2. Market demand sentence (bold numbers).
  3. **LOT ANALYST · PRO** card: score pill `99 / 100 FLIP` (green), "ESTIMATED RESALE VALUE" blurred `$4,180`, blurred margin, inner box `Current bid $625` / `Max bid for est. 50% margin (blurred)`, `Target margin 25% | 50% | 100%` segmented, `Comp final-bid range P25 · Median · P75` blurred, `Projected closing price  DEMAND` blurred, "It's a deal, but what's the right bid?" + `🔒 See the max bid with Pro →` (blue) + "$7/mo · cancel within 7 days for a full refund". **Locked-state pattern = blur + "Locked - included with Pro" alt text.**
- Footer (first seen here; /feed has none): 5 link columns — Categories · Guides · Buyer Tools · Browse · Resources — then disclaimer line "GovAuctions aggregates… not affiliated with GSA, HUD…" · Terms · Privacy · © 2026.

**Loading**: page arrives server-rendered (title/summary/price present on first screenshot); hero image right half stays black until loaded; bid value re-polls client-side.

## 4. Home — /

**Skeleton** (single centered column ~800px, no sidebar)
- Topbar unchanged.
- Hero: H1 two lines "Search every government auction." (white) / "Know what it's worth before you bid." (blue). Sub "Listings and sold comps from [rotating source name] and more." Stat line `56,520 government auctions from 31 sources · with new listings every 30 minutes  +4,536 new today` (blue). Action row: `⌖ Near me` (blue pill) · ZIP input `→` · `Browse all →` (outline).
- "Today's top-graded deals, scored by the **GovAuctions Flip Score**." → 4×2 grid of **compact cards**: image (~185×115) with optional top-left `⚡ 18 bids` blue pill; title bold 13px (2 lines); `$1,725` bold left + `1d 5h` right; muted `Baton Rouge, LA · 18 bids`; attribute chips. No heart, no source name, no Top-deal badge — a slimmer variant of the feed card.
- Featured strip: bordered wide card `● LIVE` badge over image, eyebrow `👁 UNUSUAL GOVERNMENT AUCTION`, title, `$5,000 current bid · Mountain Iron, MN`, `Share this` · `View listing →`.
- "Never Miss a Deal" newsletter block + `Get weekly deals` button.
- "What Buyers Are Hunting For" — theme tiles (`Pickup Trucks — 978 live listings — Browse 978 listings →`, 8 tiles).
- "How GovAuctions Works" — 3 numbered steps.
- Long SEO copy "What Are Government Surplus Auctions?" with inline links.
- "Popular Government Auction Categories" chip cloud (same 18 categories).
- "Research & Buyer Tools" 4 tiles · "Government Auction Guides" list · Pro upsell block "$7/mo".
- Footer = same 5-column footer as detail page.

**Loading**: server-rendered; images lazy. Source-name in hero sub-line cycles (JS ticker).

## 5. /sources

**Skeleton**: topbar · `← Back` · single centered article column (~590px) — H1 "Where GovAuctions gets its listings", 4 prose paragraphs (inline blue links), then bordered cards:
- **Coverage card**: heading "Total coverage across all markets", paragraph, 3-col table `Market | Live lots | Platforms` (row links blue, bold `All markets` total row, hairline row dividers), footnote "Live counts, updated… Last refreshed September 3, 2026."
- **Source card ×18** (one per platform, ordered by live count): header row `GovDeals` (bold, 15px) + right-aligned `25,975 live` (blue); eyebrow in small caps muted "STATE AND LOCAL GOVERNMENT SURPLUS · TIERED BUYER'S PREMIUM 7.5–12.5% (MEDIAN ~10%)"; paragraph; links `govdeals.com →` · `View live GovDeals auctions →`; quality block `100/100 · Excellent  data quality` + "No buyer ratings yet" + `Photo coverage 100% · Feed consistency 100% · Location accuracy 94%`; `Rate this source` link (needs account — not clicked). Quality label scale seen: Excellent / Strong / Limited / "— · Not enough data yet".
- "How we count" bullet list; "Federal agencies whose surplus we index" plain list; "Other markets: UK / Canadian / Australian sources →"; GeoNames attribution; footer.

**Loading**: fully server-rendered text; no images, no loading state.

## 6. Mobile (390 wide) — INFERRED, not screenshotted

`resize_window` to 390×844 reported success twice but the viewport stayed ~1384 px (Chrome window is maximized; macOS min width applies). Below is taken from the responsive classes in the served HTML + the site CSS (Tailwind v4 breakpoints sm 640 / md 768 / lg 1024 / xl 1280), so treat as design intent, not a visual check.

- **Topbar < 1024 (lg)**: search input hidden → replaced by a full-width pill button "Search auctions" (icon) that opens search; nav links (Guides/Tools/Prices/About/Pro) collapse into a hamburger `Menu` button (`lg:hidden`); country picker hidden (`hidden lg:block`); heart/favourites stays. Header is `sticky top-0 h-14`.
- **Sidebar < 768 (md)**: `#feed-filter-rail` is `hidden md:block` → gone on mobile. A blue text button `⚙ Filters` (`flex md:hidden`) appears in the control row next to the alert button and opens the filters as a sheet/drawer. `Create Alert` label shortens to `Alert` (`md:hidden` span).
- **Sidebar ≥ 768**: collapsible rail — collapsed width `--feed-rail-w: 4.25rem` (icon + 11px "Filters" label), open state persisted in `localStorage.feed_rail_open` → `html[data-feed-rail=open]`.
- **Card < 640 (sm)**: switches from vertical card to **horizontal row**: `flex sm:block`, thumbnail `w-28` (112 px) left with gradient placeholder, text block right (`p-3`, title `text-sm font-medium line-clamp-2`, price `text-sm font-bold`, meta `text-xs text-warm-gray`). ≥640: image `h-44`, ≥1280: `h-48`.
- **Grid**: `grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-4` → 1 col on phones, 2 on tablets, 3 on laptop, 4 at 1280+.
- **Category chip strip**: stays a single horizontally-scrolling row at every width.
- **Floating**: `Back to top` FAB (`fixed right-4`, 44px circle, `--fab-reserve` keeps it above a sticky bottom bar) + blue AI sparkle FAB.

## 7. URL params, search, sort, loading, empty

**URL params (observed)**
| Action | URL |
|---|---|
| Click category chip "Vehicles" | `/feed?category=vehicles` |
| Sort → Ending soonest (typed into URL, honored) | `/feed?category=vehicles&sort=ending-soonest` |
| Search "banquet chairs" + Enter | `/feed?category=vehicles&q=banquet+chairs&sort=relevance` |
| Map view | `/feed?view=map` |
| Sort option values (from `<select>`) | `best-deals` (default) · `newest` · `ending-soonest` · `price-low` · `price-high` · `relevance` (auto-set when q present, label "Best match") |
| Sitelinks search | `/feed?q={term}` |
ZIP / bids / price params not captured (sidebar forms were not submitted); `<input inputMode=numeric maxLength=5>` for ZIP, submit disabled until valid.

**Search behaviour**: typing in the topbar box swaps the right side to an `✕` clear + blue `Search` button; Enter navigates. Query keeps the current category filter and adds an **active-filter chip row** above the categories: `"banquet chairs" ×` · `Vehicles ×` · `Clear all`. Sort auto-switches to `Best match`.

**Empty state**: centered magnifier icon, H2 `No matches for "banquet chairs" in Vehicles`, sub `There are 24 matches without that filter.`, blue pill `Search everywhere` (drops the category). Control row loses the map toggle and count line.

**Loading state**: server sends an SEO fallback grid (`#feed-seo-fallback`, hidden once `html[data-hydrated]`), then the client feed renders. The client skeleton = card-shaped blocks with `.shimmer` bars (h-48 image block, h-5 w-3/4 title, h-4 w-1/2, two h-6 pills), shimmer = 1.5 s linear-gradient sweep `surface-sunken → muted → surface-sunken`. In practice the skeleton flashes too fast to catch; what is visible for ~1–2 s is cards with the **gradient image placeholder** (`bg-gradient-to-br from-accent-light to-background`, i.e. dark-blue → near-black) and occasionally a fully blank card slot. Map view: empty dark box + "0 auctions in view" until tiles load.

**Sort control**: native `<select>` styled as a dark pill with `⇅` icon; keyboard arrows on the focused select did not change it in this session (opened native popup). Changing it rewrites `?sort=`.

## 8. Component catalogue

| Name | Where | Anatomy | States |
|---|---|---|---|
| Topbar | every page, sticky | logo · country picker · search pill · nav links · `Get weekly deals` CTA · theme · gift · heart · `Sign In` | default; scrolled (border-color/shadow transition); <lg: search → icon button, nav → hamburger |
| Category chip | /feed row 1, home chip cloud | icon + label, pill, 1px border | default (dark) · active (blue fill, white text) · hover (border lightens) |
| Active-filter chip | /feed above categories, only when a filter/q set | label + `×`, plus `Clear all` link | shown/hidden |
| Sort select | /feed control row | native select in dark pill, `⇅` trailing icon | 6 options; focus ring blue |
| ZIP input | sidebar + control row | numeric input + `→` submit in one bordered pill | submit disabled until 5 digits; focus-within ring |
| `Use my location` | sidebar | pin icon + label, outline pill | default |
| Segmented pills (Bids / Vehicle Age / Target margin) | sidebar, Pro card | row of small pills | one active (blue fill) |
| Price range | sidebar | two inputs joined by `–` | empty/filled |
| Map toggle | control row | icon + `Show on map` / `Show list` | list / map |
| `Create Alert` | control row right | bell icon, blue outline→fill | needs account (not exercised); <md label `Alert` |
| Result count | above grid | `46,860 auctions across 18 sources` | number bold; hidden in map + empty states |
| Feed card | grid, map side-list | image (heart overlay) · price + `Top deal` badge + ⏱ ending · title (2-line clamp) · `Source · City, ST · N bids` · `+N more at this location →` · attribute chips | image loading (gradient placeholder) · hover (shadow-card-hover) · <sm horizontal row · optional `+N more like this` pill under card |
| Compact card | home "top-graded deals", Similar Auctions | image w/ optional `⚡ N bids` badge · title · price + ending · `City, ST · N bids` · chips | default/hover |
| Featured card | home | `● LIVE` badge, eyebrow, title, price + city, `Share this`, `View listing →` | – |
| Skeleton card | feed initial/Load more | shimmer image block + 3 shimmer lines | animating |
| Load more | grid bottom | button `Load more (N remaining)` | default; presumably loading |
| Map | /feed?view=map | Leaflet + CARTO tiles, zoom ± stack, blue cluster bubbles, attribution | loading (empty box) · loaded · basemap key error watermark |
| Map side panel | map view | header count + hint + "not shown" note; scrolling card list | 0-in-view empty copy |
| Empty state | /feed no results | icon · H2 · sub · `Search everywhere` CTA | – |
| Bid card | detail right rail | `Current Bid ● LIVE` · big price · bids · `Time Left` · ends date · `Remind me` · est. all-in accordion · sparkline · `Bid Now on <Source> →` · `Save` `Share` | live-polling ("Updated just now") |
| Lot Analyst (Pro) card | detail | score pill · blurred values with "Locked - included with Pro" · target-margin segmented · CTA `See the max bid with Pro →` | locked (blur) / unlocked (not seen) |
| Section card | detail left column, /sources | bordered rounded box, optional tinted header row, body | – |
| Coverage table | /sources | 3-col table, hairline rows, bold total | – |
| Source card | /sources | name + `N live` · small-caps eyebrow · paragraph · 2 links · quality score line · `Rate this source` | quality label: Excellent / Strong / Limited / Not enough data |
| FAB | bottom-right | 44px round: `Back to top` (border, blur) + blue AI sparkle | hidden until scrolled (back-to-top) |
| Footer | home, detail, sources (not /feed) | 5 link columns · disclaimer · Terms/Privacy · © | – |

## 9. Tokens (from `/_next/static/chunks/*.css`, `:root` + `.dark`; site defaults to dark)

| Token | Light | Dark (default seen) |
|---|---|---|
| Font | `Geist` (sans, var `--font-geist-sans`), `Geist Mono` for numbers/mono, fallback system stack | same |
| Base size | Tailwind scale: xs 12px · sm 14px · base 16px · lg 18px · xl 20px · 2xl 24px · 3xl 30px · 4xl 36px; body text mostly `text-sm` (14), meta `text-xs` (12), card price `text-lg` bold, H1 22–32px | same |
| Weights | 400 body · 500 titles · 600 labels · 700 prices/headings | same |
| `--background` | `#ffffff` | `#0e1014` |
| `--foreground` | `#1a1d23` | `#e7e9ee` |
| `--accent` (blue) | `#2563eb` | `#4b8bf5` |
| `--accent-light` | `#dbeafe` | `#1c3a63` |
| `--accent-dark` | `#1d4ed8` | `#7aa9f7` |
| `--warm-gray` / `--muted-foreground` | `#6b7280` | `#9aa3b2` |
| `--card-bg` / `--surface` | `#ffffff` | `#181b22` |
| `--surface-sunken` | `#eef0f3` | `#12151b` |
| `--muted` | `#f1f3f5` | `#1d212a` |
| `--border` | `#e5e7eb` | `#272c36` |
| `--border-strong` | `#d4d8de` | `#38404d` |
| `--urgent` | `#dc2626` | `#f47171` |
| `--success` | `#16a34a` | `#4ade80` |
| Heat scale (cold→hot, quiet) | `#3b62b0` `#2f9e86` `#e0a137` `#c0492b` `#c9ced6` | `#6a91de` `#2fa98e` `#dfa844` `#e0654a` `#3a414d` |
| Radius | sm 4px · md 6px · lg 8px (inputs, chips) · xl 12px (cards, map) · 2xl 16px · pill 9999px (chips, buttons, FAB) | same |
| `--shadow-card` | `0 1px 2px -1px #1018280f, 0 2px 6px -1px #10182812` | `0 1px 2px -1px #0006, 0 2px 6px -1px #00000073` |
| `--shadow-card-hover` | `0 4px 10px -3px #1018281a, 0 12px 24px -6px #1018281f` | `0 4px 10px -3px #00000080, 0 12px 28px -6px #0000008c` |
| `--shadow-overlay` | `0 6px 16px -4px #1018281f, 0 16px 40px -8px #1018282e` | `0 6px 16px -4px #0000008c, 0 18px 44px -8px #0009` |
| Spacing | Tailwind 4px scale: card padding 12px (`p-3`) / 16px (`p-4`); grid gap 16px; sidebar padding 16px, section gap 24px; header height 56px (`h-14`); container `max-w-6xl` (1152px) for header, feed `max-w-5xl xl:max-w-[104rem]`; sidebar rail collapsed 68px (`4.25rem`) | same |
| Breakpoints | sm 640 · md 768 · lg 1024 · xl 1280 | |
| Motion | shimmer 1.5s ease-in-out infinite; rail width .18s; `prefers-reduced-motion` disables all | |
| Misc | focus ring = `ring-2 ring-accent`; image proxy `/api/image?url=…&w=640`; theme via `.dark` class on `<html>` (toggle = sun icon) | |

## Caveats
- Mobile section is inferred from CSS/markup (window resize did not take effect).
- `javascript_tool` was blocked in this session ("Couldn't determine which page") — tokens came from fetching the site's CSS with curl instead, which is more exact anyway.
- Map basemap shows an "API KEY REQUIRED" watermark on their side (their CARTO key), not a local issue.
- Never logged in; Create Alert / Rate this source / Remind me / Pro CTAs not clicked.
