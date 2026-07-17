# BLACKWHOLE-16 — GA / Atlanta warehouse (588 chairs)  · P0

**Ticket:** https://app.notion.com/p/3a00f6db8723811e9818c56385512bf0
**Priority:** P0 · **Lots:** `folder:ATL_Grey_blueish_chairs_399` + `folder:ATL_Blue_banquet_silver_frame_189`
**Warehouse:** 1387 Northside Dr NW, Atlanta, GA 30318 (private gate code is in the DB `storage_note` — deliberately NOT copied into any committed file)

## Inventory ground truth (Supabase `inventory`, 2026-07-17)

| lot_id | title (DB) | city | qty orig / remaining | $/chair | status | imgs | description |
|---|---|---|---|---|---|---|---|
| `folder:ATL_Grey_blueish_chairs_399` | Blue Banquet Chairs (denim-blue on silver frames) | Atlanta, GA 30318 | 399 / **100** | $25 | `owned` | 8 | *(empty)* |
| `folder:ATL_Blue_banquet_silver_frame_189` *(note trailing space in lot_id)* | *(stub — none)* | *(blank)* | 0 / **0** | *(none)* | **`sold_out`** | 5 | *(empty)* |

## ⚠️ Data reconciliation — READ FIRST
The ticket says "photograph the 588 chairs (189 blue/silver + 399 light grey) and list them." The ledger doesn't match:
- **The 399 "light grey" lot is titled "Blue Banquet Chairs" / "denim-blue on silver frames"** in the DB and has only **100 remaining**, not 399. I wrote copy for the denim-blue/grey chairs actually described; confirm quantity (100 vs 399).
- **The 189 blue/silver lot is a `sold_out` stub** — quantity 0, no title, no city, no price, no description (just 5 images). Its `lot_id` also has a **trailing space** (data-hygiene bug). Either it genuinely sold out, or the row was never populated after intake. **Only you know which.**
- Net: the ledger currently shows ~**100 sellable** ATL chairs, not 588.

## Fresh-photo requirement (physical — you only)
Acceptance criterion demands **current, accurate photos, not stock/GovDeals images.** Both lots do have R2-hosted images (8 + 5) usable for an interim listing, but to satisfy the ticket you need to shoot the chairs as they sit in the warehouse. That's a physical step I can't do.

## What's prepped (this folder)
- `listing-copy.md` — FB / eBay / Craigslist copy for **both** lots, with per-city Craigslist variants for Atlanta / Macon / Athens / Augusta / Chattanooga TN.
- `facebook-catalog-supplement.csv` — FB catalog feed row for the **grey/blue 399 lot only** (the one that's `owned` with stock). The 189 lot is intentionally excluded until its DB row is real — see SQL.
- `checklist.md` — physical/account steps.
- `../pending-db-updates-16.sql` — staged SQL: (a) backfill the 399 lot's empty description; (b) a **guarded** block to populate + reactivate the 189 stub *if* you confirm those chairs exist in the warehouse. **Not applied.**

## What remains for Abdel (physical / account only)
1. **Resolve the 189 blue/silver lot:** is it actually sold out, or an un-populated intake? If the chairs exist, review + apply the guarded SQL block to bring the row back.
2. **Confirm the 399 lot's true remaining quantity** (100 in DB vs 399 in ticket).
3. **Photograph both lots** in the warehouse (fresh images) and upload.
4. Post to FB Marketplace (Atlanta), eBay, Craigslist (Atlanta/Macon/Athens/Augusta/Chattanooga). Copy ready.
5. Apply `pending-db-updates-16.sql` (description backfill + optional 189 revive) after review.
