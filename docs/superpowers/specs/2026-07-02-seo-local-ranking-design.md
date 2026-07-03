# BLACKWHOLE-11 — SEO foundation with local-ranking focus

**Ticket:** BLACKWHOLE-11 · GH issue #34 · Branch `BLACKWHOLE-11-feature-seo-local-ranking`
**Date:** 2026-07-02

## Goal

Make black-whole.com legible to Google so lot pages can rank in organic
search — especially location-flavored queries in the cities where chair
lots physically sit ("banquet chairs for sale Athens GA"). Today the site
has one static `<title>`, no meta descriptions, no sitemap, no robots.txt,
and no structured data.

## Non-goals

- Per-city landing pages ("chairs in Boise" hub pages). Premature at
  current inventory size; thin-content risk. Revisit when the ledger has
  enough lots per city.
- Google Business Profile setup — requires the operator's real address +
  phone and Google's manual verification. Documented as an optional
  operator step in `docs/seo.md`, not code.
- Changing 404 behavior for sold-out lots. Sitemap only lists live lots;
  dead URLs drop out naturally.
- RLS or any DB change. This is all read-path.

## Design

### 1. Config (`automation/config.py`)

- `PUBLIC_BASE_URL` — canonical public origin, default
  `https://black-whole.com`, env-overridable. Used for canonical links,
  absolute OG-image URLs, and sitemap entries.
- `GOOGLE_SITE_VERIFICATION` — optional; when set, renders
  `<meta name="google-site-verification" content="...">` on public pages.

### 2. Base template (`_public_base.html`)

New blocks + site-wide tags in `<head>`:

- `{% block meta_description %}` → `<meta name="description">`, with a
  brand-level default.
- Canonical: `<link rel="canonical" href="{{ base_url }}{{ request.url.path }}">`.
- Open Graph + Twitter card tags: `og:title` (mirrors title block),
  `og:description`, `og:url`, `og:type`, `og:image` via
  `{% block og_image %}` (default: none → tag omitted), `og:site_name`,
  `twitter:card = summary_large_image`.
- Organization JSON-LD (name, url, logo-less, sameAs → Facebook business
  page when configured).
- `{% block head_extra %}` for page-specific JSON-LD.
- Google verification meta when configured.

`_public_ctx()` gains `base_url` (and `google_site_verification`). Jinja
templates already receive `request` from Starlette's TemplateResponse.

### 3. Per-page meta

- **Landing `/`** — title "Bulk Banquet & Stacking Chairs for Sale |
  Black Whole Liquidation"; description mentions wholesale lots, pickup
  cities, nationwide freight.
- **`/listings`** — title "Current Chair Lots for Sale — Bulk &
  Wholesale"; description dynamically lists the distinct cities currently
  in inventory (the route already computes `cities`).
- **`/listings/{lot_id}`** — the local-SEO workhorse:
  - Title: `{qty}× {title} — {city}, {state} | Black Whole Liquidation`
    (qty/city/state segments omitted when missing).
  - Description: price + quantity + location + first ~150 chars of the
    lot description.
  - `og:image`: hero image, made absolute (Supabase URLs already are;
    local `/image/...` fallback gets `base_url` prefixed).
  - **Product JSON-LD**: `name`, `image[]` (absolute), `description`,
    `sku` (lot_id), `itemCondition: UsedCondition`, `offers` → `Offer`
    with `priceCurrency: USD`, `price` (omit Offer price field when
    unset), `availability` (`InStock` when quantity_remaining > 0 else
    `SoldOut`), and `availableAtOrFrom` → `Place` with a `PostalAddress`
    carrying `addressLocality` (city), `addressRegion` (state),
    `postalCode` (zip), `addressCountry: US`. This is the
    machine-readable "where the chairs are".
- **`/sell`** — title "Sell Your Used Chairs in Bulk — We Buy Chair
  Lots"; matching description.

### 4. Crawl surface (`automation/web/app.py`)

- `GET /sitemap.xml` — generated per-request from `inventory.list_public()`
  (same visibility rules as the site): static pages (`/`, `/listings`,
  `/sell`) + one `<url>` per live lot with `<lastmod>` from `updated_at`.
  Served as `application/xml`. No caching layer — the query is the same
  one the listings page already runs.
- `GET /robots.txt` — allow all, `Disallow: /admin` + `/api/`, and the
  `Sitemap:` line pointing at `{PUBLIC_BASE_URL}/sitemap.xml`.

### 5. Operator guide (`docs/seo.md`)

Step-by-step: Google Search Console verification (via the
`GOOGLE_SITE_VERIFICATION` env var on Render), sitemap submission,
optional Google Business Profile, what to expect (weeks-months, not
days), and how per-lot local ranking works.

## Error handling

- Missing city/state/price/hero on a row → tags/fields degrade by
  omission, never render empty strings like "None, None".
- Sitemap DB failure → 500 on `/sitemap.xml` only; public pages already
  guard their own queries.

## Testing

`tests/test_seo.py` using Starlette `TestClient` with `inventory`
functions monkeypatched (no DB): robots.txt content, sitemap XML includes
static + lot URLs and skips nothing public, detail-page title/meta/JSON-LD
render with full data, and degrade correctly with missing city/price.

## Rollout

Merge → Render auto-deploy → operator sets `GOOGLE_SITE_VERIFICATION`,
verifies in Search Console, submits sitemap (documented in docs/seo.md).
