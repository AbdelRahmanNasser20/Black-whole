# Ops prep — BLACKWHOLE-15 / 16 / 17 / 18

Ready-to-use listing artifacts for four Black-whole liquidation ops tickets, grounded in the live Supabase `inventory` ledger (read-only) and the repo's FB catalog feed conventions (`docs/fb_catalog_feed_runbook.md`, `docs/seo.md`).

**Guardrails honored:** no live marketplace posts, no messages/emails sent, no live DB writes. All DB changes are staged as `pending-db-updates-*.sql` (not applied). No application code touched. `/Users/abdelnasser/Projects/blackwhole/` was read-only reference only. No secrets (gate codes, GovDeals creds) copied into any file.

## Per ticket
| Ticket | Lot(s) | Ledger reality | Prepped |
|---|---|---|---|
| **15** PHX (P1) | `9006` Mauve, $28 | `owned`, **442 remaining** (not an open bid, not 790) | copy · CSV · checklist · SQL(zip) |
| **16** GA (P0) | `...Grey_blueish_399` + `...Blue_silver_189` | grey `owned`/100 left, empty desc · blue/silver is a **sold_out stub** | copy(both) · CSV(grey) · checklist · SQL(desc + guarded revive) |
| **17** LA (P1) | `...Orange_Red_...242` | **clean** — `owned`, 242, $25, 13 images | copy · CSV · checklist · SQL(desc) |
| **18** Midwest (P2) | `...Lockport_68` | **sold_out stub**; KS held (no row) | copy · CSV(gated) · checklist · SQL(guarded revive) |

## The recurring theme (why so much is left to Abdel)
Three of the four lots don't match their tickets in the ledger: PHX is `owned` not an open bid, and the 189-blue/silver + 68-Lockport lots are `sold_out` stubs with no metadata. Everything a computer can do without inventing facts is done (copy, CSVs, cross-post text, checklists, staged SQL). What's left is genuinely physical/account work: **resolving bids (BID-003, BID-001), confirming which stub lots are real, taking fresh photos, and the live posting itself** — plus reviewing/applying the staged SQL.

## Files per ticket folder
- `README.md` — ground truth + reconciliation notes + what's left for Abdel
- `listing-copy.md` — FB Marketplace / eBay / Craigslist copy incl. per-city variants
- `facebook-catalog-supplement.csv` — 9-col FB catalog feed row(s)
- `checklist.md` — physical/account-only steps
- `../pending-db-updates-<n>.sql` — staged, un-applied DB changes
