# BLACKWHOLE-16 — Abdel action checklist (physical / account only)  · P0

- [ ] **Resolve Lot B (189 blue/silver).** Confirm whether those chairs physically exist in the Atlanta warehouse. If yes: review + apply the guarded block in `../pending-db-updates-16.sql` to repopulate + reactivate the `sold_out` stub row. If no: leave it sold_out and note it on the ticket.
- [ ] **Confirm Lot A quantity** — DB shows 100 remaining, ticket said 399. Set the right number.
- [ ] **Photograph both lots** in the warehouse — fresh, current photos (acceptance criterion says not stock/GovDeals images). Upload; they replace the interim R2 images referenced in `listing-copy.md`.
- [ ] **FB Marketplace (Atlanta)** — post both lots. Copy in `listing-copy.md`.
- [ ] **eBay** — local-pickup listing for each lot (Atlanta 30318, no shipping).
- [ ] **Craigslist** — post per-city: atlanta, macon, athens, augusta, chattanooga (TN), both lots.
- [ ] **Apply `../pending-db-updates-16.sql`** (Lot A description backfill + optional Lot B revive) after review.
- [ ] **Stamp the ledger** after posting (admin Inventory tab / `POST /api/inventory/{lot_id}/platform`).
- [ ] **Data hygiene:** Lot B's `lot_id` has a trailing space (`folder:ATL_Blue_banquet_silver_frame_189 `). Worth cleaning up, but changing a PK touches referencing rows — do it deliberately, not as a side effect.

**Not doing (per guardrails):** no live posts, no messages, no DB writes — staged only. Gate code stays in the DB `storage_note`, never in these files.
