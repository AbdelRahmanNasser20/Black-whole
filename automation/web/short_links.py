"""Per-channel short links: `black-whole.com/cl` → `/?utm_source=craigslist…`.

Craigslist, Marketplace and phone calls can't carry a clickable tagged link —
buyers type the address. A short path they can type still lands in
`site_visits` with a source, because the redirect target carries the UTM tags.

`/{code}` → landing page, `/{code}/{lot_id}` → that lot's page. Add a channel
by adding a row to `CHANNELS`; `short_url()` builds the text to paste into a post.
"""
from __future__ import annotations

from urllib.parse import quote, urlencode

from ..config import PUBLIC_BASE_URL

# code → (utm_source, utm_medium). Codes are short on purpose: people type them.
CHANNELS: dict[str, tuple[str, str]] = {
    "cl": ("craigslist", "listing"),
    "fb": ("facebook", "marketplace"),
    "ou": ("offerup", "listing"),
    "nd": ("nextdoor", "post"),
    "ig": ("instagram", "profile"),
    "call": ("phone", "call"),
    "card": ("print", "card"),
}


def target(code: str, lot_id: str | None = None) -> str | None:
    """Redirect target for a code, or None for an unknown code."""
    ch = CHANNELS.get(code)
    if ch is None:
        return None
    source, medium = ch
    qs = urlencode({"utm_source": source, "utm_medium": medium, "utm_campaign": f"short_{code}"})
    path = f"/listings/{quote(lot_id, safe='')}" if lot_id else "/"
    return f"{path}?{qs}"


def short_url(code: str, lot_id: str | None = None, *, scheme: bool = False) -> str:
    """Text to paste into a post: `black-whole.com/cl` (no scheme by default —
    it is typed, not clicked)."""
    if code not in CHANNELS:
        raise KeyError(code)
    base = PUBLIC_BASE_URL if scheme else PUBLIC_BASE_URL.split("://", 1)[-1]
    return f"{base}/{code}" + (f"/{lot_id}" if lot_id else "")
