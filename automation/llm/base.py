from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Protocol


@dataclass
class Extraction:
    title: str
    location: str
    city: str
    state: str
    quantity: str
    chair_type: str
    dimensions: str
    suggested_price_per_chair: int
    style_suffix: str
    source: str

    def to_dict(self) -> dict:
        return asdict(self)


class Extractor(Protocol):
    name: str

    async def extract(
        self,
        dom_hint: dict,
        screenshots: dict[str, Path],
    ) -> Extraction: ...


EXTRACTION_PROMPT = """\
You are extracting structured listing data from a GovDeals auction page so it can
be turned into a Facebook Marketplace + eBay listing for used chairs.

You have:
- A screenshot of the listing page (title area, details, images)
- The DOM-scraped hint values (may be wrong)

Return EXACTLY this JSON (no prose, no markdown fences):
{
  "title": "clean human title, e.g. 'Lot of 299 Tan Metal Folding Chairs'",
  "location": "full location string, e.g. 'Nellis Air Force Base, Nevada, USA'",
  "city": "city only",
  "state": "state name only, e.g. 'Nevada'",
  "quantity": "integer as string, e.g. '299'",
  "chair_type": "short human phrase, e.g. 'Tan Metal Folding Chairs'",
  "dimensions": "e.g. '20\\" wide, 21\\" deep, 33\\" tall' or '' if not shown",
  "suggested_price_per_chair": 20,
  "style_suffix": "short marketing suffix for FB title, e.g. 'Bulk Lot - Local Pickup'"
}

Pricing guidance (USD, per chair, typical resale):
- metal folding: 15
- padded banquet / stackable cushioned: 25
- plastic stacking: 12
- wood folding: 18
- executive / office: 30
Adjust up if quantity is small (<50), down if very large (>500).
"""
