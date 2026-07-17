# BLACKWHOLE-15 — Abdel action checklist (physical / account only)

- [ ] **RESOLVE BID-003 (the real blocker).** Check your auction account + records: is BID-003 already won (and lot 9006 is it, 442 left)? Or is BID-003 a separate ASU lot not yet in the ledger? Document the outcome on the ticket either way (acceptance criterion: "bid resolved and documented, won or lost").
- [ ] **If the bid was lost / no lot to sell:** record the outcome, close the ticket — the listing artifacts here become moot.
- [ ] **If listing lot 9006 (442):** post to FB Marketplace (Phoenix), eBay (local pickup), Craigslist (phoenix / tucson / lasvegas). Copy + images ready in `listing-copy.md`.
- [ ] **Confirm the sellable quantity** — DB says 442 remaining; ticket said 790. Set the correct number before/while listing.
- [ ] **Apply `../pending-db-updates-15.sql`** (adds Phoenix zip for local-search ranking; optional qty reconcile is commented out) after review.
- [ ] **Stamp the ledger** after posting via the admin Inventory tab / `POST /api/inventory/9006/platform`.

**Not doing (per guardrails):** no live posts, no messages, no DB writes — staged only.
