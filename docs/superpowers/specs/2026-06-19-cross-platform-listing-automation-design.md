# Cross-platform listing automation — design

**Date:** 2026-06-19
**Status:** Implemented (eBay) — see Implementation outcome below
**Author:** Claude (brainstormed with Abdel)

## Implementation outcome (2026-06-20)

- **OfferUp dropped:** web item-posting is app-only (every post route → `/getapp`);
  `business.offerup.com` is a paid/sales-gated ad product, not self-serve. Not
  browser-automatable. Pivoted Phase 1 to **eBay**.
- **eBay is the proven driver.** Funnel: prelist → title → Banquet Chairs
  category → Continue without match → condition (Used) → editor. Lot **31225
  (Boise) published live** (item `336544966627`); lot **2807 (Maroon) verified as
  a draft** through the skill.
- **Lead-gen model** (per user): eBay quantity = 1, description carries bulk qty +
  freight + "message for quotes" + the `black-whole.com/listings/<lot>` backlink.
  Promoted Listings (ad rate) for extra reach is a separate, still-manual step.
- **Enrichment added** (per user "fill up as much detail… colors and what not"):
  `listing_content.parse_attributes()` derives Color / Frame Material / Seat
  Material / Frame Color / Style from each lot's free-text fields and fills eBay
  item specifics + a detail line in the description.
- **Login reality:** the established CfT `chrome_profile` is reused for all
  marketplace work — a fresh Playwright/MCP profile gets bot-blocked at eBay/
  OfferUp login (reCAPTCHA). `LISTING_CHROME_PROFILE` points at it.
- Shipped: `automation/listing_content.py`, `automation/drivers/ebay.py`,
  `automation/drivers/__init__.py`, `scripts/list_item.py`,
  `.claude/skills/list-item/SKILL.md`. Ledger `set_platform_url` already supports
  `ebay`; `offerup`/`craigslist` columns remain unused.

## Problem

Chair lots live in the `inventory` ledger and are posted to Facebook Marketplace
today via `automation/facebook.py` (and eBay via `automation/ebay.py`). We want
to:

1. Duplicate three existing FB listings onto **OfferUp** (immediate).
2. Build a **reusable, consistent** way to create/edit a listing for any lot on
   any platform (Facebook, eBay, OfferUp, Craigslist) — surfaced as a Claude
   Code **skill**.
3. Have every listing **link back to black-whole.com** to drive traffic.

The current FB/eBay drivers are one-off modules with bespoke flows; there is no
shared content model, no OfferUp/Craigslist support, and the ledger's
`set_platform_url` only knows `facebook`/`ebay`/`fb_business`/`ad`.

## Goals

- Get `General`, `Idaho (lot 31225)`, and `Maroon (lot 2807)` live on OfferUp.
- A stable seam — `ListingContent` + a per-platform driver interface — so each
  platform's selector fragility is isolated to one file.
- Website backlink in every listing description (deep link per lot, homepage for
  the general umbrella).
- A skill that lets the operator say "post lot 2807 to offerup" / "edit the
  Idaho listing on facebook" and have it run end-to-end with verification.

## Non-goals

- No new headless/cron posting. Listings are operator-initiated and visually
  verified (screenshots) — marketplaces flag unattended bulk posting.
- No true "duplicate" button reliance (FB removed "Sell similar" for this
  account); we rebuild from `ListingContent`.
- Craigslist and eBay drivers are scaffolded by the interface but only OfferUp
  is implemented + proven in Phase 1. FB driver is refactored to the interface;
  eBay/Craigslist can follow.

## Approach (chosen): OfferUp tracer bullet, then generalize

Phase 1 proves the end-to-end flow on OfferUp with real listings; Phase 2
extracts the working pieces into the reusable architecture. Lower risk, real
listings sooner, and the OfferUp driver becomes the reference implementation for
the driver interface.

## Architecture

### Browser
Reuse `automation/browser.py` `persistent_context()` against the logged-in
Chrome-for-Testing profile at
`…/facebook_scraper_Claude/chrome_profile` (set via `LISTING_CHROME_PROFILE`).
The operator logs into OfferUp there once; the session persists. CfT-engine =>
cookies decrypt under Playwright's bundled Chromium (real-Chrome profiles are
macOS-keychain-encrypted and do not — see session notes).

### `automation/listing_content.py` (new) — the stable seam
```
@dataclass
class ListingContent:
    title: str
    price: int                 # per-chair price (matches inventory.price_per_chair + existing FB listings)
    condition: str             # canonical: "used_good" etc. (driver maps to platform label)
    description: str           # body + website backlink already injected
    photos: list[Path]
    city: str
    state: str
    zip_code: str
    website_url: str           # black-whole.com/listings/<lot> or homepage
    lot_id: str | None         # None for the hard-coded general umbrella

def from_lot(lot_id: str, *, platform: str) -> ListingContent: ...
def general_umbrella(*, platform: str) -> ListingContent:   # hard-coded
```
- `condition` is canonical; each driver maps to its platform's exact option
  label (e.g. OfferUp/FB "Used - Good", eBay "Used").
- Website backlink injected here so it appears identically on every platform.
  Deep link `https://black-whole.com/listings/<lot_id>` when `lot_id` is set,
  else `https://black-whole.com`. (Optionally UTM-tagged per platform later.)
- The **General Listing** is a hard-coded umbrella entry (no DB row) per the
  design decision: umbrella copy + representative photos + homepage link.

### `automation/drivers/` (new package) — per-platform drivers
Common interface (Protocol):
```
class ListingDriver(Protocol):
    name: str
    async def create(self, ctx, content: ListingContent) -> str: ...   # returns listing URL
    async def edit(self, ctx, url: str, content: ListingContent) -> str: ...
```
- `drivers/offerup.py` — new, built + proven in Phase 1.
- `drivers/facebook.py` — refactor of existing `automation/facebook.py` to the
  interface (keeps current selectors/flow; create returns the listing URL).
- `drivers/ebay.py`, `drivers/craigslist.py` — scaffolded; eBay wraps existing
  `automation/ebay.py`. Craigslist stub raises `NotImplementedError` until built.
- `drivers/__init__.py` — `REGISTRY: dict[str, ListingDriver]` resolving a
  platform name to its driver.

### Ledger
Extend `automation/inventory.py` `_PLATFORM_COLUMNS` with
`offerup` → (`offerup_url`, *needs `offerup_published_at`*) and
`craigslist` → (`craigslist_url`, *needs `craigslist_published_at`*). The
`offerup_url`/`craigslist_url` columns already exist; add the two
`*_published_at` columns via Supabase migration. Add `offerup`/`craigslist` to
`_MARKETPLACE_PLATFORMS` so a successful post promotes `draft → listed`.

### The skill — `.claude/skills/list-item/`
`SKILL.md` documents invocation patterns the operator uses in chat:
- "post lot 2807 to offerup", "post the general listing to offerup"
- "edit the Idaho listing on facebook"

The skill's procedure:
1. Resolve lot(s) + platform(s) + create|edit from the request.
2. Ensure the logged-in browser profile (prompt the operator to log into the
   platform if the session is missing).
3. Build `ListingContent` (with website backlink).
4. Run the platform driver's `create`/`edit`; screenshot each major step.
5. Persist the returned URL via `inventory.set_platform_url(lot, platform, url)`.
6. Report the live URL + verification screenshot.

A thin CLI (`scripts/list_item.py` or extend `run.py`) backs the skill so it is
runnable headless-of-chat too: `python scripts/list_item.py <lot|general>
<platform> [--edit URL]`.

## Data flow
```
inventory row ─┐
               ├─> listing_content.from_lot() ─> ListingContent ─> driver.create(ctx, content) ─> URL
general umbrella┘                       (backlink injected)            │
                                                                       └─> inventory.set_platform_url(lot, platform, URL)
```

## Error handling
- Login guard: each run checks the platform is logged in (look for the create
  form / absence of a login wall) and aborts with a clear message + screenshot
  if not, rather than posting garbage.
- Selector fragility is per-driver; a driver step that can't find an element
  logs `[<platform> <field> fallback]` and screenshots, mirroring the existing
  FB driver's best-effort pattern. Publishing only proceeds when required
  fields verified (read back title/price before advancing).
- Ledger update is best-effort and only fires when a real listing URL (platform
  id present) is captured.

## Testing / verification
- Phase 1 success = three live OfferUp listing URLs, each with the website
  backlink in the description, screenshots of the published state, and
  `offerup_url` populated in the ledger.
- `listing_content` is pure and unit-testable (title/price/condition/backlink
  rendering) without a browser.
- Driver flows are verified by screenshots at fill / pre-publish / done, not by
  unit tests (DOM-dependent, marketplace-side).

## Open questions / future
- UTM tagging per platform (deferred; link form chosen = deep link + homepage).
- eBay driver currently lands on the seller-hub search page, not a real draft
  (pre-existing TODO); not addressed here.
- Craigslist posting (email/phone verification, category geography) — stub now,
  build later.
