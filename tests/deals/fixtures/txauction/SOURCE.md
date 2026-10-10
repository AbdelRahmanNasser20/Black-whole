# TXAuction fixtures — source

- Fetched 2026-10-10, logged-out, plain `requests`/curl, UA `Mozilla/5.0 (BLACKWHOLE deal tracker; contact: abdel@black-whole.com)`, 3 s between requests. Every page answered 200.
- Site: txauction.com (Gaston & Sheehan Auctioneers, Pflugerville TX) — React SSR on AuctioneerSoftware. Data = the inline `window.__APOLLO_STATE__ = {...}` script.
- **Trimmed**: each file keeps only that script, re-serialised compact. `ROOT_QUERY` keeps the `lots(` / `auctions(` / `auction(` / `lot(` keys only (site settings + menus dropped); `Auction.*` entities have their long HTML fields (`description`, `terms`, `removal_times`, `preview`, `_shipping_info*`, `highlights`, `documents`, `featured_attachments`) set to null; `AuctionLot.*` drop `nextLot`/`prevLot`/`full_auction`. Every field the adapter reads is untouched.
- `search_page.html` — `https://www.txauction.com/search?search=chair` (1 live hit: an artwork lot whose description mentions a chair — search is full-text).
- `search_page_seating.html` — `https://www.txauction.com/search?search=seating` (2 live hits, both artwork).
- `auctions_index.html` — `https://www.txauction.com/auctions` (20 live/upcoming auctions with `auction_location`; total 20, pageSize 50).
- `catalog_page.html` — `https://www.txauction.com/auctions/31431` (City of Austin Convention Center, closed 2026-08-14: 11 "(N) MTS Seating Omega Stacker event chairs" lots, 50–500 chairs, $350–$5,705).
- `lot_page.html` — `https://www.txauction.com/auctions/31431/lot/57702-lot-11` ("(500) MTS Seating …", 682 bids, $5,645, ended 2026-08-14T16:58Z, winner `f****r`, 2 images).
- `probe/` — raw artifacts from `scripts/onboard_probe.py` (robots.txt, sitemap.xml, sample.html = the lot page, PROBE-REPORT.md).
- robots.txt: `User-agent: *` disallows `/admin/`, `/api/`, `/asset/` only. `Crawl-delay: 10` is declared for SemrushBot/SemrushBot-SA/PetalBot only — the probe's regex reads it as site-wide (`legal: crawl-delay=10`); the adapter paces 3 s for everyone and never touches the disallowed paths.
- Pagination grammar checked live: `/search?search=gold&page=2` → `pagination:{page:2,pageSize:25}`, different lots, same total.
