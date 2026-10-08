"""New-retail price range shown next to a lot's per-chair price.

A buyer looking at "$28 / chair" has no idea what a banquet chair costs new.
This module answers that with real, checked retail prices — never an invented
number and never a fake comparable listing. Every figure below is a public
product page a buyer can open; the storefront links them.

The range is the whole honest spread: import-grade new chairs start around
$24, hotel-grade contract brands (MTS, Shelby Williams, Mity-Lite) sell by
dealer quote and the one MTS stacker with a public price is $749. No "you
save X%" claim — the cheapest new chair can undercut a used lot. Re-check when
`CHECKED` gets old; drop a source rather than guess its price.
"""
from __future__ import annotations

CHECKED = "2026-10"

# (retailer, product, price per chair in USD, url). Multi-packs are divided out.
SOURCES: tuple[tuple[str, str, float, str], ...] = (
    ("WebstaurantStore", "Lancaster stackable banquet chair, 1in pad (import grade)", 24.49,
     "https://www.webstaurantstore.com/42535/banquet-chairs-and-stackable-chairs.html"),
    ("WebstaurantStore", "Flash Furniture Hercules trapezoidal stack chair", 35.49,
     "https://www.webstaurantstore.com/42535/banquet-chairs-and-stackable-chairs.html"),
    ("Uline", "Stackable banquet chair, fabric (H-9017BL)", 68.00,
     "https://www.uline.com/Product/Detail/H-9017BL/Reception-Chairs/Stackable-Banquet-Chairs-Black"),
    ("Barstool Comforts", "MTS Seating Luxe upholstered stacker (hotel grade, 7-11 wk lead)", 749.00,
     "https://barstoolcomforts.com/product/mts-seating-luxe-upholstered-stackable-chair-stacks-8-high/"),
)


def _is_chair(row: dict) -> bool:
    text = f"{row.get('chair_type') or ''} {row.get('title') or ''}".lower()
    return "table" not in text


def anchor(row: dict) -> dict | None:
    """The new-retail range for one lot, or None for tables / unpriced lots."""
    if not SOURCES or not _is_chair(row):
        return None
    try:
        price = float(row.get("price_per_chair") or 0)
    except (TypeError, ValueError):
        return None
    if price <= 0:
        return None
    return {
        "low": min(p for _, _, p, _ in SOURCES),
        "high": max(p for _, _, p, _ in SOURCES),
        "checked": CHECKED,
        "sources": [{"retailer": r, "product": p, "price": v, "url": u}
                    for r, p, v, u in SOURCES],
    }
