"""Cloudflare's published edge ranges (https://www.cloudflare.com/ips-v4 and
/ips-v6, copied 2026-11-19). A request whose connecting peer is one of these
came through Cloudflare, so its `CF-Connecting-IP` header can be believed.
Any other peer wrote the header itself.

Refresh: replace the two tuples from the two URLs above; nothing else to touch."""
from __future__ import annotations

import ipaddress

_V4 = (
    "173.245.48.0/20", "103.21.244.0/22", "103.22.200.0/22", "103.31.4.0/22",
    "141.101.64.0/18", "108.162.192.0/18", "190.93.240.0/20", "188.114.96.0/20",
    "197.234.240.0/22", "198.41.128.0/17", "162.158.0.0/15", "104.16.0.0/13",
    "104.24.0.0/14", "172.64.0.0/13", "131.0.72.0/22",
)
_V6 = (
    "2400:cb00::/32", "2606:4700::/32", "2803:f800::/32", "2405:b500::/32",
    "2405:8100::/32", "2a06:98c0::/29", "2c0f:f248::/32",
)
NETWORKS = tuple(ipaddress.ip_network(n) for n in (*_V4, *_V6))


def is_cloudflare(ip: str) -> bool:
    """True when `ip` is inside a published Cloudflare range. Junk → False."""
    try:
        addr = ipaddress.ip_address((ip or "").strip())
    except ValueError:
        return False
    return any(addr in net for net in NETWORKS)


def is_local(ip: str) -> bool:
    """Loopback / private / the test client: a dev box or a test, never the internet."""
    if ip == "testclient":
        return True
    try:
        addr = ipaddress.ip_address((ip or "").strip())
    except ValueError:
        return False
    return addr.is_loopback or addr.is_private
