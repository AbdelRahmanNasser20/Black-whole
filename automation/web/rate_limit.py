"""A small in-process rate limiter for public, unauthenticated endpoints.

The freight estimator is free to call and cheap to run, but it is not free to
*abuse*: every call writes a `freight_quotes` row, and a scraper hammering it
would drown the operator's lane analytics in junk and burn pooler connections.

**Deliberately in-process and deliberately dumb.** No Redis, no dependency, no
shared state. The site runs as a single Render web service, so one dict is the
whole truth; if it ever scales to N instances the effective limit becomes N×,
which is still a ceiling and still cheaper than standing up Redis for it. The
window is fixed (not sliding), so a caller can in theory get 2× the limit
across a boundary — again, fine for a "stop the bot, never the buyer" control.
Restarting the process forgets everything, which is the right failure mode for
a limiter that must never lock out a real customer.

Counters live only for keys that are being hit; old windows are pruned on
write, so the dict stays proportional to *active* callers, not to all callers
ever seen.
"""
from __future__ import annotations

import time

from . import cloudflare_ips

# Per-IP: generous enough that a buyer comparing several lots and quantities
# never notices, tight enough that a scraper does.
FREIGHT_PER_IP_LIMIT = 20
# Global: the circuit breaker for a distributed hammering (or one bad bot on a
# rotating proxy). Real storefront traffic is nowhere near this.
FREIGHT_GLOBAL_LIMIT = 300

# {key: (window_end_epoch, hits_in_window)}. Storing the *end* rather than a
# window index keeps each key's window size independent — the per-IP and global
# buckets never have to agree on one period.
_HITS: dict[str, tuple[float, int]] = {}


def client_ip(request) -> str:
    """Best guess at the caller's IP, for rate limiting only (never authorization).

    The site sits behind Cloudflare, then Render's proxy, so `request.client`
    is a load-balancer address. Render appends the IP that connected to it as
    the LAST entry of `X-Forwarded-For`; that hop cannot be forged by the
    caller. When that hop is a Cloudflare edge (cloudflare_ips), the request
    really came through Cloudflare and `CF-Connecting-IP` is the client. When
    it is anything else, the caller reached the origin directly and may have
    written `CF-Connecting-IP` itself — so the hop IS the client. A loopback /
    private / test-client peer (dev box, pytest) still honours the header, so
    local tests can name an IP.
    """
    headers = getattr(request, "headers", {}) or {}
    cf = (headers.get("cf-connecting-ip") or "").strip()
    xff = [p.strip() for p in (headers.get("x-forwarded-for") or "").split(",") if p.strip()]
    client = getattr(request, "client", None)
    peer = xff[-1] if xff else (getattr(client, "host", None) or "")
    if cf and (cloudflare_ips.is_cloudflare(peer) or cloudflare_ips.is_local(peer)):
        return cf
    if peer:
        return peer
    return "unknown"


def allow(key: str, *, limit: int, window_s: int = 3600) -> bool:
    """Count one hit against `key`; True if it's within `limit` for this window.

    Calling this IS the hit — there's no separate `record()`. A rejected call
    still counts, so a caller that keeps hammering stays rejected for the rest
    of the window instead of being handed a free retry each time.
    """
    if limit <= 0:
        return False
    now = time.time()
    window_s = max(1, int(window_s))
    end, count = _HITS.get(key, (0.0, 0))
    if now >= end:
        # Aligned to the wall clock so every key rolls over together — makes
        # "you're limited until the top of the hour" true and explainable.
        end = (now // window_s + 1) * window_s
        count = 0
    count += 1
    _HITS[key] = (end, count)
    _prune(now)
    return count <= limit


def _prune(now: float) -> None:
    """Drop counters whose window has already closed."""
    for key in [k for k, (end, _) in _HITS.items() if end <= now]:
        _HITS.pop(key, None)


def reset() -> None:
    """Forget every counter. For tests — and a usable manual escape hatch."""
    _HITS.clear()
