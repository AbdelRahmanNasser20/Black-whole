# FB Business Catalog — How It Was Done & How To Automate (BLACKWHOLE-7)

Goal: list inventory on the Facebook **Business Page shop** via the sanctioned
Commerce Manager **Catalog** path, with every product linking back to
`https://black-whole.com/listings/{lot_id}`.

This doc records (a) exactly what was done to get the first Phoenix lot live, and
(b) the path to full automation so you never hand-upload again.

## Current state (done)

- **Business / Commerce account:** `BW Rentals` (business_id `1730228417959412`).
- **Catalog:** `Chairs` (catalog ID `1284591566477615`) — already existed; we
  reused it instead of making a duplicate.
- **Data source created:** `BLACKWHOLE Inventory Feed` (type: Data file / Manual,
  currency USD).
- **Product live:** `snap_06_asu_event_phoenix` — "ASU Event Chairs — Bulk
  Banquet / Event Chairs (Phoenix, AZ)", $28.00, In stock, status **Eligible**.
  Image was fetched by Facebook from the Supabase URL; import reported
  **1 added, 0 failed, 0 issues**.
- **Link-back:** product `link` = `https://black-whole.com/listings/snap_06_asu_event_phoenix`.
- **Ledger stamped:** `inventory.fb_business_url` + `fb_business_published_at`
  set on the Phoenix row.

### Still to do (needs you — account-level)
- **Connect a Page Shop.** Commerce Manager → Shops → *Go to Shops* → create a
  shop for the Black Whole Page, checkout method **"Checkout on another website"**
  (sends buyers to our links), select the `Chairs` catalog. This requires
  accepting Meta's **Seller Agreement** and may go through a short Meta review —
  that's why it's left to you rather than auto-clicked. Once live, replace the
  stamped `fb_business_url` with the public Page-Shop product URL.

## The product file (what Facebook ingests)

File: `facebook_catalog_phoenix.csv` (in the repo root). Facebook catalog spec
columns:

```
id,title,description,availability,condition,price,link,image_link,brand
```

- `id` = lot_id, `price` = `"28.00 USD"`, `availability` = `in stock`,
  `condition` = `used`, `brand` = `BLACKWHOLE Liquidation`
- `link` = `https://black-whole.com/listings/{lot_id}` (the link-back)
- `image_link` = absolute Supabase Storage URL (FB fetches it server-side, so no
  local image upload needed)

## Manual path that was used (one-time, exact clicks)

Commerce Manager → catalog `Chairs` → **Catalog ▸ Data sources** →
**Add items** → **Data File** → **Next** → **Upload from your computer** →
attach `facebook_catalog_phoenix.csv` → **Next** → name = `BLACKWHOLE Inventory
Feed`, currency = **USD** → **Upload**. Result: product appears under
**Catalog ▸ Products** as Eligible.

> Implementation note for re-runs: Facebook's "choose file" button only creates
> the `<input type=file>` on click, which opens a native OS dialog automation
> can't drive. The working trick was to synthesize the file in-page and dispatch
> a `drop` event on FB's drop zone (a `File` built from the CSV text +
> `DataTransfer` + `DragEvent`). That's brittle and FB-DOM-dependent — prefer the
> scheduled-URL path below for real automation.

## How to automate it fully (recommended end state)

The repo already has the code for this — it just needs to be deployed:

1. **Endpoint:** `GET /catalog/facebook.csv` (in `automation/web/app.py`) emits
   the same feed for **all** sellable lots, live from the `inventory` table:
   `status IN ('listed','owned','won_pickup')` AND `quantity_remaining > 0`.
   Base URL is configurable via env `SITE_BASE_URL` (default
   `https://black-whole.com`).
2. **Deploy** the app to Render (`black-whole-web`, autoDeploy on). After deploy,
   confirm `https://black-whole.com/catalog/facebook.csv` returns rows.
3. **Point Facebook at the URL once:** Commerce Manager → Data sources →
   (the `BLACKWHOLE Inventory Feed` source) → switch to / add **"Use a URL or
   Google Sheets"** = `https://black-whole.com/catalog/facebook.csv` → schedule
   **Daily** (or Hourly).
4. From then on it's hands-off: as lots sell out (`quantity_remaining → 0`) or
   change status they drop from the feed automatically; new sellable lots appear
   on the next pull. No more file uploads.

### Adding the rest of the chairs
Once the scheduled URL feed is live, **nothing manual** is needed — every
sellable lot in `inventory` is already in the feed. Until then, regenerate the
CSV for additional lots and re-upload via the same Data File path. (The endpoint
and `inventory.list_catalog_feed()` already produce the multi-row version.)

## Stamping publication state
- Helper: `inventory.set_platform_url(lot_id, "fb_business", url)` →
  sets `fb_business_url` + `fb_business_published_at`. Does **not** flip status to
  `listed` (a business-catalog listing is promotional, not a marketplace listing).
- Admin API: `POST /api/inventory/{lot_id}/platform`
  `{"platform":"fb_business","url":"<page-shop-or-product-url>"}`.

## Guardrails (unchanged)
- Read-only against `inventory` except the `fb_business_*` stamp.
- Does **not** touch `facebook_url` / `facebook_published_at` (the separate
  scraped personal-Marketplace path).
- No tokens or secrets — the feed URL is public by design.

## IDs for reference
- business_id: `1730228417959412`
- catalog (`Chairs`) ID: `1284591566477615`
- data source: `BLACKWHOLE Inventory Feed`
- first product content ID: `snap_06_asu_event_phoenix`
