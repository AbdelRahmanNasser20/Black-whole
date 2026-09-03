# GovAuctions → Admin feature map

We explored https://govauctions.app/feed and rebuilt its core features in the admin (Deals + Auctions tabs).
- Built: maps with cluster pins, viewport filtering, ZIP centering, price/bids filters, chips, alerts.
- Skipped on purpose: location/similarity grouping, weekly email digest.

## Feature inventory

| # | GovAuctions feature | Behavior observed | Ours |
|---|---|---|---|
| 1 | Split map view (?view=map) | "Show on map" toggles map + result list | Built: 🗺 toggle on Deals + Auctions tabs |
| 2 | Cluster pins | Leaflet + markercluster count bubbles, click zooms | Built: static/admin_map.js (Leaflet 1.9.4 + markercluster 1.5.3, CARTO dark) |
| 3 | Viewport-bound list ("N auctions in view") | pan/zoom re-filters the list instantly | Built: Deals = bbox pushed into SQL (/api/deals?bbox=s,w,n,e); Auctions = client-side card filter |
| 4 | Pin popup (title, price, location, view link) | | Built |
| 5 | "X auctions have no listed location" note | unmapped stay in list | Built (map notes; unmapped lots never hidden) |
| 6 | Live search + removable filter chips + clear all | typing refilters, URL updates | Built: existing debounced #deal-q + new active-filter chip row |
| 7 | Category chips | one-click pills | Built: top canonical categories pill row on Deals |
| 8 | Sort (Best deals / Best match / price / ending) | | Already had (table column sorts) |
| 9 | ZIP code + "use my location" | centers search | Built: ZIP → /api/geo/zip (pgeocode) → map centers; browser geolocation skipped |
| 10 | Bids filter (Any / No bids / ≤3) | pills | Built: seg control replacing the 0-bids checkbox |
| 11 | Price range min/max | | Built: min_price/max_price on current_bid |
| 12 | Create Alert 🔔 | save filters, get notified of new matches | Built on existing saved_searches.alert + hourly Telegram sweep; now also captures price + map-viewport bbox |
| 13 | Favorites ♥ | | Already had (deal lists ♥, auction ★ w/ Telegram countdowns) |
| 14 | "+N more at this location" grouping | | Skipped v1 |
| 15 | "+2 more like this" similarity | | Skipped |
| 16 | Multi-source aggregation ("18 sources") | | Partially had (gd/ps/bs) |
| 17 | Weekly email digest / Pro tier | SaaS | Skipped |
| 18 | (they lack) quantity per lot | — | Built: `deals/quantity.py` → Qty + $/unit + landed/unit columns, admin + public |
| 19 | (they lack) closed-auction outcomes | — | Already had (`outcome`, `final_bid`); now public on /deals with status=closed |
| 20 | Public site | govauctions.app/feed | Built: `/deals` (2026-09-04), exclusion-filtered, paged 25/50/100, noindex |

## How their reactivity works (and our mirror)

GovAuctions sends the whole dataset to the browser once; pan/zoom filters and clusters client-side with zero API calls (verified via network inspection).
- Our mirror: unpaged `/api/deals/geo` pin feed — ~21k active mapped lots, 91% coverage of 23,870 active — feeds the map; the paged table stays server-side via SQL bbox (`/api/deals?bbox=s,w,n,e`).
- Auctions tab coords: zip-first geocoding (pgeocode), state-centroid fallback, amber "approx" pins for the fallback.
