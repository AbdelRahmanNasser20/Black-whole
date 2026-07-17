# BLACKWHOLE-17 — LA / INV-001 (242 orange-red banquet chairs)

**Ticket:** https://app.notion.com/p/3a00f6db8723810da19fd6897e2b718a
**Priority:** P1 · **Lot:** `folder:Orange_Red_Banquet_Chairs_Cypress_242`

## Inventory ground truth (read live from Supabase `inventory`, 2026-07-17)

| Field | Value |
|---|---|
| Title | Crimson Patterned Banquet Chairs |
| Subtitle | Ornate orange-red damask on gold frames |
| City | Long Beach, CA 90806-2709 |
| Quantity original / remaining | 242 / **242** |
| Sell price/chair | $25.00 |
| Acquisition cost/chair | $4.76 (per ticket) |
| Status | `owned` |
| Images | **13 hosted** on R2 (`pub-4ac6bae8ec024e3aaccf3317c8873840.r2.dev/folder_Orange_Red_Banquet_Chairs_Cypress_242/00..12.jpg`) |
| Description in DB | *(empty — filled by staged SQL below)* |

This is the cleanest of the four lots: `owned`, full 242 remaining, price set, 13 images already hosted on the production image CDN.

## Reconciliation note — images already scraped + hosted
The ticket asks to "scrape GovDeals images → dewatermark → list." **13 images are already hosted on the production R2 bucket** (the same bucket public listings serve from), which means the scrape + dewatermark step for INV-001 appears already done. Abdel should eyeball a couple of them for any residual `govdeals.com` ghosting before going live (see checklist) — I could not visually verify watermark removal, only that the files exist and are referenced by the ledger.

## What's prepped (this folder)
- `listing-copy.md` — FB Marketplace, eBay (local pickup), and Craigslist copy, including per-city Craigslist variants for LA / OC / SD / Ventura.
- `facebook-catalog-supplement.csv` — one row in the exact 9-column FB Business Catalog feed schema (`id,title,description,availability,condition,price,link,image_link,brand`), ready to append to `catalog/facebook.csv` or bulk-upload in Commerce Manager.
- `checklist.md` — the physical/account steps only Abdel can do.
- `../pending-db-updates-17.sql` — staged SQL to backfill the empty `description` so the live feed + lot page read well. **Not applied.**

## What remains for Abdel (physical / account only)
1. Verify a couple of the 13 hosted images have no readable `govdeals.com` watermark ghosting.
2. Post to FB Marketplace (LA + OC + SD + IE), eBay (local pickup), Craigslist (LA / OC / SD / Ventura) — or run the automation pipeline. Copy is ready to paste.
3. (Optional) Apply `pending-db-updates-17.sql` after review so the lot page/feed description isn't blank.
4. This lot is already `owned` + `quantity_remaining > 0`, so once the description is filled it will **auto-appear in the live `catalog/facebook.csv` feed** with no further action.
