<!-- Moved verbatim from ../../CLAUDE.md on 2026-08-28 (trim to <=8 KB). Original kept as ../../CLAUDE.md.pre-trim-2026-08-28 -->

## Known TODOs (ordered roughly by blast-radius)

1. **eBay flow doesn't land on a real draft.** Final URL was `ebay.com/sh/lst/active?sku=...` (seller hub search), not `sell.ebay.com/sell/form/...`. Selectors in `automation/ebay.py` need end-to-end verification. First step: open the URL the script lands on, compare against what a manual "List an item" flow produces, update `SELL_URL` and the subsequent form-fill selectors.
2. `facebook.py` and `ebay.py` selectors are best-effort. They print `[fallback]` warnings instead of crashing — check the dashboard log for which ones are firing on real runs.
3. Quantity parsing in `govdeals.py` JS uses `\((\d{1,5})\)` — brittle. The `dom_fallback` description-priority logic (added 2026-04-17) compensates, but the JS regex is still worth tightening at the source.
4. **Dashboard cost-tracking tile.** `dewatermark_usage.jsonl` exists; surface `today: N calls / cache: M hashes` somewhere on the A/B tab.

## Done (recently completed — kept here briefly so future-Claude knows what changed)

- GovAuctions-style maps + filter parity on the admin — 2026-08-31. 🗺 map toggles on Deals + Auctions tabs (`static/admin_map.js`: lazy Leaflet + markercluster, CARTO dark); new unpaged `/api/deals/geo` pin feed; `bbox=s,w,n,e` filter on `/api/deals` (viewport pan → SQL); `/api/geo/zip` ZIP→center via pgeocode; Deals gains min/max price + bids seg control (Any/0/≤3), removable filter chips, category pills, and a Create Alert 🔔 button; the saved-search Telegram sweep now honors price + bbox; new `deals.cli saved-search-alerts` verb. Feature-by-feature comparison: `docs/govauctions-feature-map.md`.
- Watermark idempotency + API cost log + budget caps + offline-by-default — 2026-04-17. See "Dewatermark behavior" above.
- `_extract_quantity` in `dom_fallback.py` now prefers description match over DOM when DOM quantity is `< 20` (the Athens-style bug). Tunable via `DOM_QUANTITY_SUSPICION_THRESHOLD`.
- `folder_name` is now built **after** the LLM finalizes quantity. `govdeals.scrape()` writes screenshots to `~/.listing_automation/scratch/<lot>_<ts>/` and returns metadata; `run.py` calls `govdeals.finalize_folder(meta, primary.quantity)` post-LLM, which mkdir's the real folder and moves screenshots in.
- Dashboard price-confirm UI now actually surfaces: `run.py` emits `progress.emit("price", suggested=N, confirmed=None)` BEFORE blocking, then again with `confirmed=N` after. In no-TTY mode it waits up to `LISTING_PRICE_CONFIRM_TIMEOUT` seconds (dashboard sets this to 120s) for stdin from `/api/runs/stdin`, then auto-accepts on timeout.
