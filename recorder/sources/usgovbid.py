"""USGovBid (`bid.usgovbid.com`) — Maxanet tenant (New Jersey county surplus,
Monmouth County at probe time: 3 current auctions, 53 all-time).

Everything lives in `recorder/sources/maxanet.py`; this file binds the host,
the source name, and the crawl delay: `www.usgovbid.com/robots.txt` says
`Crawl-delay: 30`, so every request to `bid.usgovbid.com` waits ≥ 30 s
(`register_host_interval`, enforced inside `polite_get` for every caller).
A discover() here is therefore ~6 searches × 30 s ≈ 3 min wall-clock — fine
for a cron, slow at a terminal.

Timezone: America/Chicago despite the site being East-coast — the Maxanet
platform JS converts every rendered date from "America/Chicago", and the
server's own `data-nowdate` read 11:47 AM at 16:47 UTC. See maxanet.py.
"""
from __future__ import annotations

from recorder.sources.base import register_host_interval
from recorder.sources.maxanet import MaxanetSource

SOURCE = "usgovbid"
HOSTNAME = "bid.usgovbid.com"
CRAWL_DELAY_SECONDS = 30.0

register_host_interval(HOSTNAME, CRAWL_DELAY_SECONDS)


class USGovBidSource(MaxanetSource):
    HOST = f"https://{HOSTNAME}"
    SOURCE = SOURCE
    SITE_NAME = "USGovBid"
