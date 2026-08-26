"""Facebook Business catalog feed serialization (BLACKWHOLE-7).

Pure, DB-free rendering of `inventory` rows into the CSV that Facebook Commerce
Manager ingests as a scheduled product-catalog feed. The SQL that *selects*
sellable lots lives in `inventory.list_catalog_feed()`; this module turns those
rows into the exact FB columns and decides per-row eligibility — FB rejects a
row with no price or no fetchable image, so we drop those here rather than let
the whole import fail validation.

Keeping this separate from the web layer means the feed format is unit-testable
with plain dict fixtures — no database, no FastAPI, no network.

Column order and names match Meta's own downloadable template
(`catalog_products.csv`, Commerce Manager -> Data sources -> Add -> template),
checked 2026-08-25. Meta maps by header name, not position, but matching the
template exactly makes a diff against it trivial when a validation error lands.

Required: id, title, description, availability, condition, link, image_link, brand
Optional we fill: price, google_product_category, quantity_to_sell_on_facebook,
                  product_tags[0] (city), product_tags[1] (state)
Optional deliberately left blank: color, material, size, shipping_weight,
    dimensions — the ledger has no trustworthy value for these (weight_lb and
    dim_in are NULL on every sellable lot as of 2026-08-25). A blank optional
    column costs nothing; a guessed one puts a wrong fact in front of a buyer.

See docs/fb_business_catalog_HOWTO.md and docs/fb_catalog_feed_runbook.md.
"""
from __future__ import annotations

import csv
import io
import os
from collections.abc import Iterable

from . import lot_images
from .config import PUBLIC_BASE_URL

# Column order copied from Meta's template header, 2026-08-25.
FEED_COLUMNS = [
    "id", "title", "description", "availability", "condition",
    "link", "image_link", "brand", "price",
    "google_product_category", "quantity_to_sell_on_facebook",
    "product_tags[0]", "product_tags[1]",
]

# `inventory.state` is entered by hand and is inconsistent — "Michigan" on one
# lot, "CA" on the next. Product tags are what Meta filters product sets on, so
# a mixed format means "state = GA" silently misses the Michigan lots. Normalize
# to the 2-letter code; anything unrecognized passes through untouched rather
# than being dropped or guessed at.
_STATE_CODES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN",
    "mississippi": "MS", "missouri": "MO", "montana": "MT", "nebraska": "NE",
    "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ",
    "new mexico": "NM", "new york": "NY", "north carolina": "NC",
    "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR",
    "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
    "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
    "district of columbia": "DC",
}


def state_code(value: str | None) -> str:
    """"Michigan" -> "MI"; "CA" -> "CA"; unknown -> unchanged."""
    raw = _clean(value)
    return _STATE_CODES.get(raw.lower(), raw.upper() if len(raw) == 2 else raw)


# Google product taxonomy. Every lot we sell is a chair, so this is a
# classification we can assert, not a product fact we would be inventing.
GOOGLE_CATEGORY = "Furniture > Chairs"
# The one non-chair we list (Augusta round banquet tables, 2026-08-26). Decided
# off the row's own words so a table can never be filed under Chairs.
GOOGLE_CATEGORY_TABLES = "Furniture > Tables"


def google_category(row: dict) -> str:
    text = f"{row.get('chair_type') or ''} {row.get('title') or ''}".lower()
    return GOOGLE_CATEGORY_TABLES if "table" in text else GOOGLE_CATEGORY
# `fb_product_category` is deliberately NOT emitted: Meta's own taxonomy needs
# an exact ID/path and a wrong one silently mis-files the product. Meta infers
# it from google_product_category, so leaving it blank is the safer default.

# Fixed values for a liquidation catalog — identical on every row.
# Operator decision 2026-08-25: every lot ships as "in stock", consistent with
# how the listings have always been worded. Availability caveats, when a lot
# needs one, live in `inventory.description` — one field, so the site, the
# Marketplace listing, and this feed can never disagree.
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

    Resolution goes through ``lot_images.resolve`` (the one resolver — hero
    column first, then the gallery), and any Supabase Storage URL is treated as
    dead: that backend is egress-restricted and 402s on every public object, so
    shipping such a URL means FB imports a product with a broken photo. FB pulls
    the image itself, so a relative ``/image/...`` local path is equally useless
    — rows with no live durable URL are dropped instead of shipped broken.
    """
    resolved = lot_images.resolve(row)
    candidates = ([resolved.hero] if resolved.hero else []) + resolved.urls
    for url in candidates:
        if url and lot_images.storage_backend(url) != "supabase":
            return url
    return None


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
    qty = row.get("quantity_remaining")
    return {
        "id": lot_id,
        "title": title,
        "description": _description(row, title),
        "availability": AVAILABILITY,
        "condition": CONDITION,
        "link": f"{base}/listings/{lot_id}?{UTM_QUERY}",
        "image_link": image,
        "brand": BRAND,
        "price": price,
        "google_product_category": google_category(row),
        # Meta requires >= 1 or the item is not buyable. Feed rows only exist
        # for lots with stock left, so this is the real remaining count.
        "quantity_to_sell_on_facebook": str(qty) if isinstance(qty, int) and qty > 0 else "",
        # Tags drive product sets. City/state is the split that matters for us:
        # a buyer in Atlanta should be shown the Atlanta lot, not the Boise one.
        "product_tags[0]": _clean(row.get("city")),
        "product_tags[1]": state_code(row.get("state")),
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
