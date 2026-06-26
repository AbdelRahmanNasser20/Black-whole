"""Normalized, enriched listing content — the stable seam every platform driver
fills from.

Source of truth is the `inventory` ledger (same data that powers black-whole.com
/listings/<lot>). We *enrich* it by parsing the free-text `subtitle`, `title`,
`chair_type`, and `description` into structured chair attributes (color, frame
material, seat material, frame color, style) so each platform's detail fields —
e.g. eBay item specifics — can be filled as completely as possible.

Pure module: no browser, no network beyond the ledger read. Unit-testable.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import inventory
from .templates import listing_title, state_abbr

WEBSITE_BASE = os.getenv("LISTING_WEBSITE_BASE", "https://black-whole.com")
PIX_BASE = Path.home() / "Desktop" / "Banquet chiars Pictures"
_IMG_EXT = (".png", ".jpg", ".jpeg")

# ---- attribute vocabularies (keyword -> eBay-friendly canonical value) -------
# Order matters: earlier, more-specific keys win.
_COLORS = [
    ("charcoal", "Gray"), ("maroon", "Red"), ("burgundy", "Red"),
    ("burgandy", "Red"), ("wine", "Red"), ("crimson", "Red"),
    ("red", "Red"), ("mauve", "Purple"), ("purple", "Purple"),
    ("pink", "Pink"), ("tan", "Beige"), ("beige", "Beige"),
    ("ivory", "Cream"), ("cream", "Cream"), ("saffron", "Yellow"),
    ("gold", "Gold"), ("yellow", "Yellow"), ("orange", "Orange"),
    ("blue", "Blue"), ("denim", "Blue"), ("navy", "Blue"),
    ("green", "Green"), ("grey", "Gray"), ("gray", "Gray"),
    ("silver", "Silver"), ("black", "Black"), ("brown", "Brown"),
    ("natural", "Brown"), ("wood", "Brown"),
]
_FRAME_MATERIAL = [
    ("chrome", "Metal"), ("steel", "Metal"), ("metal", "Metal"),
    ("aluminum", "Metal"), ("wood", "Wood"), ("wooden", "Wood"),
    ("plastic", "Plastic"), ("resin", "Plastic"),
]
_SEAT_MATERIAL = [
    ("vinyl", "Vinyl"), ("leather", "Faux Leather"), ("fabric", "Fabric"),
    ("upholster", "Fabric"), ("cushion", "Fabric"), ("padded", "Fabric"),
    ("foam", "Foam"), ("plastic", "Plastic"), ("wood", "Wood"),
    ("mesh", "Mesh"),
]
_FRAME_COLOR = [
    ("chrome", "Silver"), ("silver", "Silver"), ("gold", "Gold"),
    ("bronze", "Bronze"), ("black", "Black"), ("brown", "Brown"),
    ("grey", "Gray"), ("gray", "Gray"), ("white", "White"),
]
_STYLES = [("stackable", "Stackable"), ("stacking", "Stackable"),
           ("folding", "Folding"), ("foldable", "Folding")]


def _first_match(text: str, table) -> str | None:
    low = text.lower()
    for kw, val in table:
        # word-boundary match so "red" doesn't fire inside "hundred"/"covered"
        if re.search(rf"\b{re.escape(kw)}\b", low):
            return val
    return None


@dataclass
class ChairAttributes:
    color: str | None = None
    frame_material: str | None = None
    seat_material: str | None = None
    frame_color: str | None = None
    style: str | None = None          # Stackable / Folding
    brand: str = "Unbranded"
    type: str = "Banquet Chair"

    def as_ebay_specifics(self) -> dict[str, str]:
        """Map to eBay item-specific labels, omitting unknowns."""
        out = {"Brand": self.brand, "Type": self.type}
        if self.color:          out["Color"] = self.color
        if self.frame_material: out["Frame Material"] = self.frame_material
        if self.seat_material:  out["Seat Material"] = self.seat_material
        if self.frame_color:    out["Frame Color"] = self.frame_color
        if self.style:          out["Features"] = self.style
        return out


def parse_attributes(row: dict) -> ChairAttributes:
    """Derive structured chair attributes from a lot's free-text fields."""
    blob = " ".join(str(row.get(k) or "") for k in
                    ("title", "subtitle", "chair_type", "description"))
    style = _first_match(blob, _STYLES)
    ctype = row.get("chair_type") or ""
    type_ = "Banquet Chair"
    if re.search(r"dining", ctype, re.I):
        type_ = "Dining Chair"
    elif re.search(r"folding", blob, re.I):
        type_ = "Folding Chair"
    return ChairAttributes(
        color=_first_match(blob, _COLORS),
        frame_material=_first_match(blob, _FRAME_MATERIAL),
        seat_material=_first_match(blob, _SEAT_MATERIAL),
        frame_color=_first_match(blob, _FRAME_COLOR),
        style=style,
        type=type_,
    )


@dataclass
class ListingContent:
    lot_id: str | None
    title: str
    price: int                      # per-chair price
    quantity: int                   # listing quantity (lead-gen default = 1)
    condition: str                  # canonical: "Used - Good"
    description: str                # body + website backlink, already composed
    photos: list[Path]
    city: str
    state: str
    zip_code: str
    website_url: str
    attributes: ChairAttributes = field(default_factory=ChairAttributes)


def _photos_for(row: dict, limit: int = 12) -> list[Path]:
    folder = row.get("folder_path")
    d = Path(folder) if folder else (
        PIX_BASE / row["folder_name"] if row.get("folder_name") else None)
    if not d or not d.is_dir():
        return []
    files = sorted(p for p in d.glob("*")
                   if p.suffix.lower() in _IMG_EXT and p.is_file())
    return files[:limit]


def website_url_for(lot_id: str | None) -> str:
    return f"{WEBSITE_BASE}/listings/{lot_id}" if lot_id else WEBSITE_BASE


def _compose_description(row: dict, attrs: ChairAttributes, website: str,
                         lead_gen: bool) -> str:
    qty = row.get("quantity_remaining") or row.get("quantity_original") or ""
    city = row.get("city") or ""
    state = row.get("state") or ""
    loc = ", ".join(p for p in (city, state) if p)
    base = (row.get("description") or "").strip()

    # Detail line built from enriched attributes.
    bits = []
    if attrs.color:          bits.append(f"Color: {attrs.color}")
    if attrs.seat_material:  bits.append(f"Seat: {attrs.seat_material}")
    if attrs.frame_material: bits.append(f"Frame: {attrs.frame_material}")
    if attrs.frame_color:    bits.append(f"Frame color: {attrs.frame_color}")
    if attrs.style:          bits.append(attrs.style)
    detail = " · ".join(bits)

    lines = []
    if base:
        lines.append(base)
    if detail:
        lines.append(detail)
    if qty and loc:
        lines.append(f"Approximately {qty} available in {loc}. "
                     "Used, good condition with normal wear from prior service.")
    lines.append("Local pickup; freight delivery quotes available on request. "
                 "Ideal for churches, banquet halls, schools, event venues and "
                 "rental companies.")
    if lead_gen:
        lines.append("We supply bulk chair lots nationwide — message us for "
                     "quantity and freight quotes.")
    lines.append(f"More photos, full details and our other lots: {website}")
    return "\n\n".join(lines)


def from_lot(lot_id: str, *, lead_gen: bool = True,
             quantity: int | None = None) -> ListingContent:
    """Build enriched, platform-agnostic content for a lot from the ledger."""
    row = inventory.get(lot_id)
    if row is None:
        raise ValueError(f"lot not found in ledger: {lot_id}")
    attrs = parse_attributes(row)
    website = website_url_for(lot_id)
    title = listing_title(row.get("chair_type") or "", city=row.get("city") or "",
                          state=row.get("state") or "",
                          fallback=row.get("title") or "Bulk Banquet Chairs")
    return ListingContent(
        lot_id=str(lot_id),
        title=title,
        price=int(float(row.get("price_per_chair") or 25)),
        quantity=quantity if quantity is not None else (1 if lead_gen else
                  int(row.get("quantity_remaining") or 1)),
        condition="Used - Good",
        description=_compose_description(row, attrs, website, lead_gen),
        photos=_photos_for(row),
        city=row.get("city") or "",
        state=state_abbr(row.get("state") or ""),
        zip_code=row.get("zip_code") or "",
        website_url=website,
        attributes=attrs,
    )
