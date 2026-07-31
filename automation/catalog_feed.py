"""Facebook Business catalog feed serialization (BLACKWHOLE-7).

Pure, DB-free rendering of `inventory` rows into the CSV that Facebook Commerce
Manager ingests as a scheduled product-catalog feed. The SQL that *selects*
sellable lots lives in `inventory.list_catalog_feed()`; this module turns those
rows into the exact FB columns and decides per-row eligibility — FB rejects a
row with no price or no fetchable image, so we drop those here rather than let
the whole import fail validation.

Keeping this separate from the web layer means the feed format is unit-testable
with plain dict fixtures — no database, no FastAPI, no network.

FB catalog spec columns (in order):
    id, title, description, availability, condition, price, link, image_link, brand

See docs/fb_business_catalog_HOWTO.md and docs/fb_catalog_feed_runbook.md.
"""
from __future__ import annotations

import csv
import io
import os
from collections.abc import Iterable

from .config import PUBLIC_BASE_URL

# Facebook catalog feed column order.
FEED_COLUMNS = [
    "id", "title", "description", "availability",
    "condition", "price", "link", "image_link", "brand",
]

# Fixed values for a liquidation catalog — identical on every row.
AVAILABILITY = "in stock"
CONDITION = "used"
BRAND = "BLACKWHOLE Liquidation"
# Attribution for clicks arriving from the FB catalog (Apollo/analytics read these).
UTM_QUERY = "utm_source=facebook&utm_medium=catalog&utm_campaign=fb_shop"
CURRENCY = "USD"

# Stay comfortably under FB's field limits (title 200, description 9999).
_TITLE_MAX = 200
_DESC_MAX = 5000


def site_base_url() -> str:
    """Base URL for product link-backs.

    Honors the ``SITE_BASE_URL`` env var (the knob the runbook tells the
    operator to set on Render) and otherwise falls back to
    ``config.PUBLIC_BASE_URL`` — both default to ``https://black-whole.com``.
    """
    return (os.getenv("SITE_BASE_URL") or PUBLIC_BASE_URL).rstrip("/")


def _clean(text: object) -> str:
    """Collapse whitespace/newlines so a value is safe inside one CSV cell."""
    return " ".join(str(text).split()) if text not in (None, "") else ""


def _price(row: dict) -> str | None:
    """`"<amount> USD"` for a positive price, else None (row will be dropped)."""
    val = row.get("price_per_chair")
    if val in (None, ""):
        return None
    try:
        amount = float(val)
    except (TypeError, ValueError):
        return None
    if amount <= 0:
        return None
    return f"{amount:.2f} {CURRENCY}"


def _image_link(row: dict) -> str | None:
    """Absolute public image URL FB can fetch server-side, else None.

    Only a durable http(s) URL (the Supabase Storage hero image) works: FB
    pulls the image itself, so a relative ``/image/...`` local path is useless
    and the row is dropped instead of shipped broken.
    """
    url = (row.get("hero_image_url") or "").strip()
    return url if url.startswith(("http://", "https://")) else None


def _description(row: dict, title: str) -> str:
    """The lot's own description, else a clean fallback from structured fields."""
    existing = _clean(row.get("description"))
    if existing:
        return existing[:_DESC_MAX]
    loc = ", ".join(p for p in (_clean(row.get("city")), _clean(row.get("state"))) if p)
    qty = row.get("quantity_remaining")
    bits = [title if title.endswith(".") else f"{title}."]
    if qty:
        bits.append(f"{int(qty)} available{f' in {loc}' if loc else ''}.")
    elif loc:
        bits.append(f"Available in {loc}.")
    bits.append("Tap to view full details and inquire on our site.")
    return _clean(" ".join(bits))[:_DESC_MAX]


def feed_row(row: dict, base_url: str | None = None) -> dict | None:
    """Map one inventory row to an FB feed row, or None if FB would reject it.

    Requires: lot_id, title, a positive price, and an absolute image URL.
    """
    base = (base_url or site_base_url()).rstrip("/")
    lot_id = _clean(row.get("lot_id"))
    title = _clean(row.get("title"))[:_TITLE_MAX]
    price = _price(row)
    image = _image_link(row)
    if not (lot_id and title and price and image):
        return None
    return {
        "id": lot_id,
        "title": title,
        "description": _description(row, title),
        "availability": AVAILABILITY,
        "condition": CONDITION,
        "price": price,
        "link": f"{base}/listings/{lot_id}?{UTM_QUERY}",
        "image_link": image,
        "brand": BRAND,
    }


def build_feed_rows(rows: Iterable[dict], base_url: str | None = None) -> list[dict]:
    """Eligible FB feed rows for the given inventory rows (drops incomplete)."""
    base = (base_url or site_base_url()).rstrip("/")
    return [fr for r in rows if (fr := feed_row(r, base))]


def rows_to_csv(rows: Iterable[dict], base_url: str | None = None) -> str:
    """Render inventory rows to the FB catalog CSV (header + eligible rows)."""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=FEED_COLUMNS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(build_feed_rows(rows, base_url))
    return buf.getvalue()
