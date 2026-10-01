"""Quantity per lot from the listing text — the value knob GovAuctions lacks.

Reuses the operator's own extractor (`auction_extractors.quantity_infer.
explicit_title_quantity`): named patterns only ("Lot of (30)", "Lot of 6",
"qty 12", "approx 16", "lot size 40"), NO blind-integer fallback (a "2009 Ford"
is not 2009 trucks). Deterministic, no LLM, no DB column — the DB is at its
free-tier ceiling (2026-09-04), so quantity is derived at read time.
"""
from __future__ import annotations

import re

from auction_extractors.quantity_infer import explicit_title_quantity

DESCRIPTION_WINDOW = 600  # chars; descriptions are TOAST blobs, don't scan them all


def lot_quantity(title: str | None, description: str | None = None) -> tuple[int, str]:
    """(quantity, source). quantity >= 1; source in {'title','description','default'}."""
    for text, source in ((title, "title"), ((description or "")[:DESCRIPTION_WINDOW], "description")):
        if not text:
            continue
        hit = explicit_title_quantity(text)
        if hit and hit[0] > 0:
            return int(hit[0]), source
    return 1, "default"


def unit_price(amount: float | None, quantity: int) -> float | None:
    if amount is None:
        return None
    q = quantity if quantity and quantity > 0 else 1
    return round(float(amount) / q, 2)


# "150 Student Chairs", "LOT of ~2500 Wire Frame Linkable CHAIRS": a count up
# to four words before the noun. Only used where the noun is known (the
# Tracking tab's chair lots), so a bare leading number can't be a model year.
_NOUN_COUNT = re.compile(
    r"(?:^|[\s(:~])(\d{1,3}(?:,\d{3})+|\d{1,5})\)?\s+(?:[a-z][\w'-]*\s+){0,4}?chairs?\b",
    re.IGNORECASE)


def chair_quantity(title: str | None) -> tuple[int | None, str]:
    """(quantity, source) for a chair lot's title; (None, 'unknown') when the
    title names no count — never a default 1, which would pass the lot total
    off as a per-chair price."""
    if not title:
        return None, "unknown"
    hit = explicit_title_quantity(title)
    if hit and hit[0] > 0:
        return int(hit[0]), "title"
    m = _NOUN_COUNT.search(title)
    if m:
        n = int(m.group(1).replace(",", ""))
        if n > 0:
            return n, "title"
    return None, "unknown"
