# BLACKWHOLE-18 — Midwest / Lockport IL (INV-002, 68 chairs)  · P2

**Ticket:** https://app.notion.com/p/3a00f6db8723813da070c0958e2b15dc
**Priority:** P2 · **Lot:** `folder:Lot_of_68_Chairs_Lockport_68`

## Inventory ground truth (Supabase `inventory`, 2026-07-17)

| Field | Value |
|---|---|
| lot_id | `folder:Lot_of_68_Chairs_Lockport_68` |
| Title / subtitle / city / desc | *(all empty — stub row)* |
| Quantity orig / remaining | 0 / **0** |
| Price/chair | *(none)* — ticket lists $1.79 **cost**, sell price TBD |
| Status | **`sold_out`** |
| Images | 2 hosted (`.../folder_Lot_of_68_Chairs_Lockport_68/00..01.jpg`) |

## ⚠️ Data reconciliation — READ FIRST
The ticket wants to list **68 Lockport chairs** ("best per-chair margin in the portfolio, $1.79/chair"). But the ledger row is a **`sold_out` stub**: quantity 0, no title/city/description, only 2 images. Either the lot already sold, or the row was never populated after intake. **Only you can tell.** I wrote listing copy + a guarded SQL block to bring the row to life *if* the 68 chairs exist.

Two other gaps:
- **Sell price not set.** $1.79 is the acquisition *cost*. I suggest **$25/chair** (portfolio norm; enormous margin) — confirm before listing.
- **Only 2 images**, and details are thin (chair color/material unknown from the DB). You'll likely want fresh photos + a quick look to describe them accurately; my copy uses neutral "padded stackable chairs" language until then.

## KS lot (BID-001 Manhattan KS, ~400 ballroom chairs)
**Status: HELD pending bid — do not list.** There is **no Manhattan KS row in the ledger**, consistent with "not yet won." Nothing to prep here beyond honoring the hold; noted in the ticket summary + checklist. Once/if the bid is won, spin a fresh listing pass like the others.

## What's prepped (this folder)
- `listing-copy.md` — FB / eBay / Craigslist copy for the 68 Lockport chairs, per-city Craigslist for Chicago / Rockford / Milwaukee / NW Indiana.
- `facebook-catalog-supplement.csv` — a ready FB catalog row **to use only after the DB row is reconciled** (a sold_out/qty-0 lot is excluded from the live feed by design).
- `checklist.md` — physical/account steps, including the KS hold.
- `../pending-db-updates-18.sql` — guarded SQL to populate + reactivate the Lockport stub. **Not applied.**

## What remains for Abdel (physical / account only)
1. **Confirm the 68 Lockport chairs exist** (vs genuinely sold out). If they exist, review + apply the guarded SQL.
2. **Set the sell price** (suggest $25/chair) and describe the chairs' actual color/material.
3. **Photograph** the lot (2 images is thin).
4. Post to FB Marketplace (Chicago), eBay, Craigslist (chicago/rockford/milwaukee/nwindiana). Copy ready.
5. **Keep BID-001 Manhattan KS on hold** — do not list until the bid is won.
