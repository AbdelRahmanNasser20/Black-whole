# Public Surplus fixtures — source

- Fetched 2026-09-04, logged-out, plain `requests`, UA `Mozilla/5.0 (BLACKWHOLE onboarding probe; contact: abdel@black-whole.com)`, 5 s between requests.
- Lane that worked: **requests** (the ps-v2 "empty `noAuctionsFound` shell" gate did not fire for this UA on this date). The adapter still falls back to Patchright if it ever does.
- `search_page.html` — `https://www.publicsurplus.com/sms/browse/search?posting=y&keyWord=banquet+chairs&page=0` (13 cards, 121 KB). Pagination grammar: `page=<0-based index>`, server-fixed 25 cards/page.
- `detail_page.html` — `https://www.publicsurplus.com/sms/auction/view?auc=4079872` (auction #4079872, "40 Black Banquet Chairs - Lot C", City of Taylor MI, 65 KB).
- `probe/` — raw artifacts from `scripts/onboard_probe.py` (robots.txt, sitemap, sample.html = the same search page, PROBE-REPORT.md).
- robots.txt: `User-agent: *` disallows only `/images/`; `Crawl-delay: 5` is declared for msnbot/Slurp/bingbot only — the adapter honors 5 s anyway.
- Known card epoch (test anchor): auc 4079872 ends at epoch-ms `1789678800000` = 2026-09-17T21:00:00Z (page shows "Sep 17, 2026 03:00 PM MDT").
