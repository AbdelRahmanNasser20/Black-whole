"""Google Merchant Center product feed serialization (multichannel Phase 2, D7).

Pure, DB-free rendering of `inventory` rows into the CSV Merchant Center
fetches on a schedule. The SQL that selects sellable lots is the same one the
Facebook feed uses (`inventory.list_catalog_feed()`); this module only decides
the Google column shape and per-row eligibility. Price/image/description
helpers are imported from `catalog_feed` so the two feeds can never disagree
about a lot's price or photo.

Column names follow the Merchant Center product data specification. Google
maps by header name. `availability` is `in_stock` (underscore) — Meta's feed
uses "in stock" with a space; the two are not interchangeable.

Deliberately NOT emitted (no trustworthy value in the ledger, and a guessed
value puts a wrong fact in front of a buyer): gtin, mpn, shipping,
shipping_weight, product_dimensions. `identifier_exists=no` tells Google we
have no GTIN/MPN, which is expected for used liquidation stock.

Operator setup: docs/google_merchant_runbook.md.
"""
from __future__ import annotations

import csv
import io
from collections.abc import Iterable

from . import catalog_feed, lot_images

FEED_COLUMNS = [
    "id", "title", "description", "link", "image_link", "additional_image_link",
    "availability", "price", "condition", "brand", "identifier_exists",
    "google_product_category", "product_type", "custom_label_0", "custom_label_1",
]

UTM_QUERY = "utm_source=google&utm_medium=feed&utm_campaign=merchant"
AVAILABILITY = "in_stock"
CONDITION = "used"
IDENTIFIER_EXISTS = "no"
PRODUCT_TYPE_CHAIRS = "Seating > Banquet Chairs"
PRODUCT_TYPE_TABLES = "Tables > Banquet Tables"

_TITLE_MAX = 150          # Merchant Center hard limit
_EXTRA_IMAGES_MAX = 10    # additional_image_link limit


def _product_type(category: str) -> str:
    return PRODUCT_TYPE_TABLES if category == catalog_feed.GOOGLE_CATEGORY_TABLES else PRODUCT_TYPE_CHAIRS


def _additional_images(row: dict, hero: str) -> str:
    """Up to 10 durable gallery URLs after the hero, comma-joined (Google's format)."""
    resolved = lot_images.resolve(row)
    extra: list[str] = []
    for url in resolved.urls:
        if not url or url == hero or lot_images.storage_backend(url) == "supabase":
            continue
        if url not in extra:
            extra.append(url)
        if len(extra) == _EXTRA_IMAGES_MAX:
            break
    return ",".join(extra)


def feed_row(row: dict, base_url: str | None = None) -> dict | None:
    """Map one inventory row to a Merchant Center row, or None if Google would reject it."""
    base = (base_url or catalog_feed.site_base_url()).rstrip("/")
    lot_id = catalog_feed._clean(row.get("lot_id"))
    title = catalog_feed._clean(row.get("title"))[:_TITLE_MAX]
    price = catalog_feed._price(row)
    image = catalog_feed._image_link(row)
    if not (lot_id and title and price and image):
        return None
    category = catalog_feed.google_category(row)
    return {
        "id": lot_id,
        "title": title,
        "description": catalog_feed._description(row, title),
        "link": f"{base}/listings/{lot_id}?{UTM_QUERY}",
        "image_link": image,
        "additional_image_link": _additional_images(row, image),
        "availability": AVAILABILITY,
        "price": price,
        "condition": CONDITION,
        "brand": catalog_feed.BRAND,
        "identifier_exists": IDENTIFIER_EXISTS,
        "google_product_category": category,
        "product_type": _product_type(category),
        "custom_label_0": catalog_feed.state_code(row.get("state")),
        "custom_label_1": catalog_feed._clean(row.get("city")),
    }


def build_feed_rows(rows: Iterable[dict], base_url: str | None = None) -> list[dict]:
    base = (base_url or catalog_feed.site_base_url()).rstrip("/")
    return [fr for r in rows if (fr := feed_row(r, base))]


def rows_to_csv(rows: Iterable[dict], base_url: str | None = None) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=FEED_COLUMNS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(build_feed_rows(rows, base_url))
    return buf.getvalue()
