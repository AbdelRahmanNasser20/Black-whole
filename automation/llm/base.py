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
    # Short marketable label used for folder names and listing title body.
    chair_title: str = ""
    # 3–5 sentence product description embedded in FB/eBay templates.
    description_text: str = ""
    # 5-digit US ZIP for the pickup address (or "ZIP-EXT4"). Empty if not shown.
    zip_code: str = ""
    # Seller / facility contact pulled from the asset page. Used only on the
    # admin side (Auctions tab + Inventory tab) — never rendered on the public
    # listing page.
    contact_name: str = ""
    contact_email: str = ""
    contact_phone: str = ""

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
  "zip_code": "5-digit US ZIP from the pickup address, e.g. '89191'. Empty string if not shown. May appear as 'ZIP-1234' (5+4) — keep the full form if present.",
  "quantity": "integer as string, e.g. '299'",
  "chair_type": "short human phrase, e.g. 'Tan Metal Folding Chairs'",
  "chair_title": "short marketable product label, 2–5 words, e.g. 'Tan Metal Folding Chairs'. Used as BOTH the listing folder name and the listing body title — keep it concise and human. No quantity numbers.",
  "description_text": "Faithfully summarize what the GovDeals page's product description actually says about the chairs (material, construction, color, dimensions, model, etc. — whatever the source mentions), AND include the condition the source describes. 2–4 short sentences max. Pull condition language directly from the source when present (e.g. 'used, good condition', 'shows visible wear', 'minor scuffs', 'like new'); if the source is silent on condition, write 'Used, good condition with normal wear from prior service.' Do NOT invent material/construction details that the source doesn't mention. Hard rules: no auction/bidding language ('bidders', 'place a bid', 'lot', 'as-is sale'); no meta-commentary ('details not available', 'inspect prior to bidding'); no marketing fluff; no emojis; no hedge words like 'typically' / 'generally'.",
  "dimensions": "e.g. '20\\" wide, 21\\" deep, 33\\" tall' or '' if not shown",
  "suggested_price_per_chair": 20,
  "style_suffix": "short marketing suffix for FB title, e.g. 'Bulk Lot - Local Pickup'",
  "contact_name": "name of the GovDeals seller/contact person if visible (e.g. 'John Smith' or a department like 'DRMO Nellis'). Empty string if not shown.",
  "contact_email": "contact email from the seller/contact panel, e.g. 'jane.doe@example.gov'. Empty string if not shown — do not invent.",
  "contact_phone": "contact phone in the format shown on the page, e.g. '(702) 652-1234'. Empty string if not shown — do not invent."
}

Location guidance:
- Prefer the **pickup address** shown in the auction details, not the seller
  agency name. City should be a real city name, not a seller code.
- If only a state/region is shown, leave city blank rather than guessing.
- ZIP comes from the same pickup address line. Leave blank rather than guessing
  if no ZIP is visible — do not infer it from the city.

Contact guidance:
- The "Contact Information" / "Seller Information" panel typically lists a
  contact name, phone, and email. Capture them verbatim. If only some fields
  are present, leave the missing ones as empty strings.

Pricing guidance (USD, per chair, typical resale):
- banquet (any kind — padded, metal, fabric): 25
- stackable / stacking (any kind, including plastic and cushioned): 25
- metal folding: 15
- wood folding: 18
- executive / office: 30
Adjust up if quantity is small (<50), down if very large (>500).
"""
