"""Scrape guard for the PUBLIC auction feed (the data we collect and others want).

Four layers, all in-process like `rate_limit` (one Render web service = one
truth; a restart forgets everything, which can never lock out a real visitor):

1. **Rate limit** per IP on the feed's JSON and its server-rendered pages:
   `PER_MIN` a minute and `PER_HOUR` an hour, plus a global ceiling for a
   scraper on rotating addresses.
2. **Trap link.** Every public feed page carries one hidden, nofollow link to
   `TRAP_PATH`, which robots.txt disallows. A person never sees it and a
   well-behaved crawler never fetches it, so a hit is a scraper: its IP loses
   the feed for `BAN_HOURS`. The storefront keeps working for that IP.
3. **Depth cap.** A visitor with no account reads the first `ANON_MAX_ROWS`
   rows of any list. Deeper pages need a signed-in platform user, so the whole
   feed cannot be paged out anonymously.
4. **Crawler rules.** Feed JSON answers `X-Robots-Tag: noindex, nofollow,
   noarchive`; robots.txt disallows the JSON prefixes.

The operator's admin session is exempt from all of it. The IP comes from
`rate_limit.client_ip` (Cloudflare's header) and is spoofable at the origin,
so it feeds these limits only — never an authorization decision.

Switch off with `SCRAPE_GUARD_ENABLED=0`. This raises the cost of a scrape; it
does not make one impossible. Cloudflare bot rules are the outer layer.
"""
from __future__ import annotations

import os
import re
import time

from . import rate_limit


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


PER_MIN = _int("SCRAPE_GUARD_PER_MIN", 90)          # a filter-happy person makes ~20
PER_HOUR = _int("SCRAPE_GUARD_PER_HOUR", 1200)
GLOBAL_PER_MIN = _int("SCRAPE_GUARD_GLOBAL_PER_MIN", 3000)
ANON_MAX_ROWS = _int("SCRAPE_GUARD_ANON_MAX_ROWS", 1000)
BAN_HOURS = _int("SCRAPE_GUARD_BAN_HOURS", 12)

TRAP_PATH = "/deals/api/export-all.json"
ROBOTS_TAG = "noindex, nofollow, noarchive"
JSON_PREFIXES = ("/deals/api/", "/platform/api/", "/map/api/", "/distress/api/")
_LOT_PAGE = re.compile(r"^/deals/\d+/\d+/\d+$")
_PAGES = ("/deals/sold",)
# list endpoints a caller can page through → (default page size, largest the endpoint serves).
# The endpoints clamp an oversized per_page themselves, so the depth is counted on the clamped value.
_PAGED = {"/deals/api/lots": (25, 100), "/deals/api/sold": (50, 50), "/deals/sold": (50, 50),
          "/platform/api/auctions": (50, 100)}

_BANS: dict[str, float] = {}


def enabled() -> bool:
    return os.environ.get("SCRAPE_GUARD_ENABLED", "1").strip() != "0"


def is_json(path: str) -> bool:
    return path.startswith(JSON_PREFIXES)


def is_guarded(path: str) -> bool:
    return is_json(path) or path in _PAGES or bool(_LOT_PAGE.match(path))


def ban(ip: str) -> None:
    """Take the feed away from `ip` for BAN_HOURS. An unknown address is never
    banned: that key is every caller whose IP we could not read."""
    if ip and ip != "unknown":
        _BANS[ip] = time.time() + BAN_HOURS * 3600


def is_banned(ip: str) -> bool:
    return ban_left(ip) > 0


def ban_left(ip: str) -> int:
    until = _BANS.get(ip)
    if not until:
        return 0
    left = int(until - time.time())
    if left <= 0:
        _BANS.pop(ip, None)
        return 0
    return left


def over_limit(ip: str) -> int:
    """Count one request. 0 = allowed, else the seconds to wait."""
    now = time.time()
    if not rate_limit.allow(f"sg:m:{ip}", limit=PER_MIN, window_s=60):
        return max(1, int(60 - now % 60))
    if not rate_limit.allow(f"sg:h:{ip}", limit=PER_HOUR, window_s=3600):
        return max(1, int(3600 - now % 3600))
    if not rate_limit.allow("sg:global", limit=GLOBAL_PER_MIN, window_s=60):
        return max(1, int(60 - now % 60))
    return 0


def _pos(value, default: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return n if n > 0 else default


def depth(path: str, query) -> int:
    """Rows a request reaches into a list: page × per_page. 0 = not a paged list."""
    if path not in _PAGED:
        return 0
    size, largest = _PAGED[path]
    return _pos(query.get("page"), 1) * min(_pos(query.get("per_page"), size), largest)


def reset() -> None:
    """Forget every ban. For tests, and a manual way out."""
    _BANS.clear()
