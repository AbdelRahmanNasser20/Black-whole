"""Edge caching for the anonymous public feed.

Every visitor used to hit Postgres: the public JSON answered with no
Cache-Control, so Cloudflare marked it DYNAMIC and passed it through. Now the
user-independent public reads carry a public Cache-Control (the same shape
GovAuctions.app uses on its feed files): the browser keeps a copy for a
minute, Cloudflare for five, and serves a stale copy for a day while it
refetches in the background — a slow query never makes a visitor wait twice.

Rules:
* only the paths in PATHS: read-only, user-independent, no session involved;
* never when the request carries a platform or admin session cookie — the
  signed-in payload differs (Flip Score, saved) and must not be shared;
* never over a Cache-Control the route already set (`private, no-store` wins);
* 200 only.

Cloudflare still needs a Cache Rule to cache JSON at all (it caches by file
extension by default): "Eligible for cache" on these paths, bypass when the
cookie `sb-access-token` or the admin session cookie is present. The origin
header then sets the TTL. Without the rule nothing breaks; the browser cache
alone still helps.
"""
from __future__ import annotations

import os

PATHS = frozenset({
    "/sources",
    "/platform/api/sources", "/platform/api/sites", "/platform/api/auctions",
    "/deals/api/lots", "/deals/api/pins", "/deals/api/sold", "/deals/api/facets",
})

HEADER = "public, max-age=60, s-maxage=300, stale-while-revalidate=86400"


def enabled() -> bool:
    return os.getenv("EDGE_CACHE_ENABLED", "1").strip() not in ("0", "false", "no")


def cacheable(path: str) -> bool:
    return path in PATHS
