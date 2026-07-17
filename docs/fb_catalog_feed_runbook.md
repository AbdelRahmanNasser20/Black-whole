# Facebook Business Catalog Feed — Operator Runbook (BLACKWHOLE-7)

Lists our inventory on the **Facebook Business Page Shop** via the sanctioned
**Commerce Manager / Catalog** path, with every product linking back to
`https://black-whole.com/listings/{lot_id}`. This is *not* the scraped personal-
Marketplace path (`facebook_url`) — it's a supported product data feed FB pulls
on a schedule.

## What the code provides

- **`GET /catalog/facebook.csv`** — a Facebook catalog feed. One row per sellable
  lot, columns: `id, title, description, availability, condition, price, link,
  image_link, brand`.
  - Included: `status IN ('listed','owned','won_pickup')` **and**
    `quantity_remaining > 0`. Sold-out / hidden / lost / draft are excluded.
    Rows with no price or no image are dropped (FB rejects incomplete rows).
  - `link` = `https://black-whole.com/listings/{lot_id}`
  - `image_link` = the lot's absolute Supabase Storage hero image
  - `price` = `"<amount> USD"`, `condition` = `used`, `availability` = `in stock`,
    `brand` = `BLACKWHOLE Liquidation`
- Site base URL is configurable: env **`SITE_BASE_URL`** (default
  `https://black-whole.com`). Set it on the Render `black-whole-web` service if
  the domain ever changes.
- **Stamping**: once a lot is live in the FB shop, record it with
  `POST /api/inventory/{lot_id}/platform` `{"platform":"fb_business","url":"<page-shop-or-product-url>"}`.
  This sets `inventory.fb_business_url` + `fb_business_published_at`. (Posting an
  `fb_business` URL does **not** promote status to `listed` — it's promotional,
  not a marketplace listing.)

Feed URL (live): **`https://black-whole.com/catalog/facebook.csv`**

## One-time Commerce Manager setup

1. **Business Page + Commerce Manager.** Go to
   <https://business.facebook.com/commerce> with the account that owns the
   BLACKWHOLE Business Page. If no catalog exists yet:
   *Commerce Manager → Add catalog → Items type: **Other / e-commerce** → name it
   `BLACKWHOLE Inventory` → owned by your Business account → Create.*
2. **Add the scheduled feed.**
   *Catalog → Data sources → Add items → **Use a data feed (scheduled)** →
   Enter feed URL: `https://black-whole.com/catalog/facebook.csv` →
   (no login required; the URL is public) → set currency **USD** → schedule
   **Daily** (or Hourly) → Upload/Start.*
3. **Resolve validation.** FB validates on first import. If it flags a column,
   compare against the header above — every required field is emitted. Re-run the
   import after any fix.
4. **Connect the catalog to the Page Shop.**
   *Commerce Manager → Settings → Business assets → connect the catalog to the
   BLACKWHOLE **Facebook Page**.* If a Shop isn't set up yet:
   *Commerce Manager → add a Shop → checkout method **Checkout on another website**
   (sends buyers to our links) → select this catalog → submit for review.*
5. **Verify link-back.** Open a product in the Page Shop and confirm it clicks
   through to the correct `https://black-whole.com/listings/{lot_id}`.
6. **Stamp it.** Copy the Page-Shop product (or shop) URL and run the
   `POST /api/inventory/{lot_id}/platform` call above so the ledger records that
   the lot is published to the FB business shop.

## Refresh behavior

After the initial setup there's nothing manual to repeat — FB re-pulls
`facebook.csv` on the schedule you picked. As lots sell out
(`quantity_remaining → 0`) or change status they drop out of the feed
automatically; new sellable lots appear on the next pull.

## Notes

- The feed is read-only against `inventory` except the `fb_business_*` stamp.
- It does not touch `facebook_url` / `facebook_published_at` (separate scraped
  path).
- No tokens or secrets are involved — the feed URL is public by design.
- v2 (out of scope here): programmatic Graph API product create/update instead
  of a scheduled file pull.
