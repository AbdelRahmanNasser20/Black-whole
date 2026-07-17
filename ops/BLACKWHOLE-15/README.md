# BLACKWHOLE-15 — PHX / ASU chairs (BID-003)

**Ticket:** https://app.notion.com/p/3a00f6db872381f98d2fd6b41d395041
**Priority:** P1 · **Lot:** `9006` (Phoenix)

## Inventory ground truth (read live from Supabase `inventory`, 2026-07-17)

| Field | Value |
|---|---|
| lot_id | 9006 |
| Title | Mauve Banquet Chairs |
| Subtitle | Light-purple diamond-pattern seats, black frames |
| City | Phoenix, AZ *(zip blank)* |
| Quantity original / remaining | 790 / **442** |
| Sell price/chair | $28.00 |
| Status | **`owned`** |
| Images | 8 hosted on R2 (`.../9006/00..07.jpg`) |
| Description | "Mauve event banquet chairs … Previously used at Arizona State University event spaces … Stackable." |

## ⚠️ Data reconciliation — READ FIRST
The ticket frames this as **"win the ASU West Valley bid (BID-003), active bid, then list 790."** The live ledger tells a different story:
- The Phoenix 790-chair lot (`9006`) is already **`owned`**, not an open bid.
- **quantity_remaining is 442, not 790** — 348 appear already sold.
- The DB has no row tagged `BID-003` and no `active_bid` lot in Phoenix.

**Two possibilities only you can resolve:**
1. BID-003 was already won and partially sold → the real task is "list the remaining **442**," not win a bid. In that case this lot is ready to list today.
2. BID-003 is a *separate, newer* ASU lot not yet in the ledger → you still need to resolve the bid, and `9006` is a different (older) Phoenix lot.

I built the listing artifacts against `9006` (the only Phoenix lot that exists), quantities framed as **442 remaining**. If BID-003 is a new lot, re-point the copy once it's in the ledger.

## What's prepped (this folder)
- `listing-copy.md` — FB Marketplace, eBay (local pickup), Craigslist (PHX / Tucson / Las Vegas). Local pickup = zero freight, matches the "highest margin" thesis.
- `facebook-catalog-supplement.csv` — 9-column FB catalog feed row for lot 9006.
- `checklist.md` — physical/account steps.
- `../pending-db-updates-15.sql` — staged SQL to add the Phoenix zip (SEO/JSON-LD) and an *optional, commented* quantity reconcile. **Not applied.**

## What remains for Abdel (physical / account only)
1. **Resolve the BID-003 question above** — this is the one true blocker and only you can (auction account + your records).
2. If listing 442 (case 1): the lot is `owned` with images + description already present → post to FB (Phoenix), eBay (local pickup), Craigslist (PHX/Tucson/Las Vegas). Copy ready.
3. Confirm/stage the correct `quantity_remaining` (442 per DB vs 790 per ticket).
4. Apply `pending-db-updates-15.sql` (adds zip) after review — improves local search ranking.
