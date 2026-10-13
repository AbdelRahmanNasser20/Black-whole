"""Wisconsin Surplus (`bid.wisconsinsurplus.com`) — Maxanet tenant.

Everything lives in `recorder/sources/maxanet.py`; this file only binds the
host + source name. Timezone America/Chicago (platform-wide, see maxanet.py).
`www.wisconsinsurplus.com/robots.txt` publishes no Crawl-delay; the 1 s
per-host floor applies.
"""
from __future__ import annotations

from recorder.sources.maxanet import MaxanetSource

SOURCE = "wisconsin_surplus"


class WisconsinSurplusSource(MaxanetSource):
    HOST = "https://bid.wisconsinsurplus.com"
    SOURCE = SOURCE
    SITE_NAME = "Wisconsin Surplus"
