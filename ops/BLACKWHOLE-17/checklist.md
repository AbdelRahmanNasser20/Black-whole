# BLACKWHOLE-17 — Abdel action checklist (physical / account only)

Everything a computer could do is in this folder. These steps need your hands, your accounts, or your judgment.

- [ ] **Verify images are clean.** Open 2-3 of the 13 hosted images and confirm no readable `govdeals.com` watermark ghosting. URLs in `listing-copy.md`.
- [ ] **(Optional) Apply DB description backfill.** Review `../pending-db-updates-17.sql`, then apply so the lot page + live FB feed aren't blank. Once applied, this lot auto-appears in `catalog/facebook.csv` (it's already `owned` with 242 remaining).
- [ ] **FB Marketplace** — post in Los Angeles, then duplicate to Orange County, San Diego, Inland Empire. Paste title/description from `listing-copy.md`, attach the 13 images, set price $25.
- [ ] **eBay** — create a local-pickup listing (Long Beach 90806, no shipping). Title/description ready.
- [ ] **Craigslist** — post the four per-city variants: losangeles, orangecounty, sandiego, ventura (furniture - by dealer).
- [ ] **Stamp the ledger** after posting: `POST /api/inventory/folder:Orange_Red_Banquet_Chairs_Cypress_242/platform` with the FB/eBay URLs (or use the admin Inventory tab) so dedup + status promotion work.

**Not doing (per guardrails):** no live posts, no messages sent, no DB writes — all staged only.
