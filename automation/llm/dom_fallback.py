import re
from pathlib import Path

from .base import Extraction


CHAIR_TYPE_HINTS = [
    ("metal folding", 15),
    ("padded", 25),
    ("banquet", 25),
    ("stacking", 20),
    ("plastic", 12),
    ("wood folding", 18),
    ("wooden", 22),
    ("executive", 30),
    ("office", 30),
    ("upholstered", 25),
    ("cafeteria", 12),
]


def _guess_chair_type(title: str, description: str) -> str:
    text = f"{title} {description}".lower()
    for hint, _ in CHAIR_TYPE_HINTS:
        if hint in text:
            return hint.title() + " Chairs"
    return "Chairs"


def _guess_price(chair_type: str) -> int:
    t = chair_type.lower()
    for hint, price in CHAIR_TYPE_HINTS:
        if hint in t:
            return price
    return 20


# When DOM-scraped quantity is below this, we trust a description-derived
# quantity over the DOM. Real lots have run aground on DOM=10 (a stray paren
# match) while the description clearly said "240 Purple Chairs".
DOM_QUANTITY_SUSPICION_THRESHOLD = 20


def _extract_quantity(dom_qty: str, description: str) -> str:
    counts = [
        int(m) for m in
        re.findall(r"(\d{2,5})\s+(?:[A-Za-z]+\s+)?chairs?", description, re.I)
    ]
    desc_max = max(counts) if counts else None
    dom_n = int(dom_qty) if dom_qty and dom_qty.isdigit() else None

    if desc_max is not None and (dom_n is None or dom_n < DOM_QUANTITY_SUSPICION_THRESHOLD):
        return str(desc_max)
    if dom_n is not None:
        return str(dom_n)
    return dom_qty or "1"


class DomFallbackExtractor:
    """Used when no real LLM is reachable.

    Maps DOM-scraped values directly into an Extraction with light inference
    (chair type from keywords, price from chair type, quantity sniffed from
    the description if the DOM regex missed). The dashboard's price-confirm
    step lets the user override anything that's wrong.
    """

    name = "dom_fallback"

    async def extract(self, dom_hint: dict, screenshots: dict[str, Path]) -> Extraction:
        description = dom_hint.get("description_head", "") or ""
        chair_type = _guess_chair_type(dom_hint.get("title", ""), description)
        price = _guess_price(chair_type)
        quantity = _extract_quantity(str(dom_hint.get("quantity", "")), description)
        return Extraction(
            title=dom_hint.get("title", "Unknown"),
            location=dom_hint.get("location", "Unknown"),
            city=dom_hint.get("city", ""),
            state=dom_hint.get("state", ""),
            quantity=quantity,
            chair_type=chair_type,
            dimensions="",
            suggested_price_per_chair=price,
            style_suffix="Bulk Lot - Local Pickup",
            source=self.name,
        )
