"""Listing description templates for Facebook Marketplace and eBay.

Both templates take an LLM-generated `description_text` (3–5 sentences of plain
product prose) and surround it with our standard pickup-logistics boilerplate.
The rendered string is persisted to `inventory.description` on each run so the
public site can reuse whatever we posted on the marketplaces.
"""

FB_DESCRIPTION = """\
{description_text}

📍 Location: {location} (local pickup; delivery quotes on request)
📦 Quantity available: {quantity}{dimensions_line}

Ideal for churches, banquet halls, community centers, schools, and event venues.

To get a quote, please reply with:
  1. Quantity needed
  2. Pickup or delivery
  3. Your city / ZIP
"""


EBAY_DESCRIPTION_HTML = """\
<h2>{title}</h2>
<p>{description_text}</p>
<p><strong>Bulk Lot — Great Value!</strong></p>
<p>Selling <strong>{quantity} {chair_type}</strong>. Perfect for churches,
banquet halls, conference centers, event venues, nonprofits, schools,
and community centers.</p>
<h3>Details:</h3>
<ul>
  <li><strong>Quantity:</strong> {quantity} chairs available</li>
  <li><strong>Type:</strong> {chair_type}</li>
  <li><strong>Condition:</strong> Used — good functional condition</li>
  <li><strong>Location:</strong> {location} (pickup required)</li>
  {dimensions_li}
</ul>
<h3>Pricing:</h3>
<ul>
  <li>Individual: ${price_each} each</li>
  <li><strong>Take ALL {quantity} for ${bulk_price}
    (${bulk_price_per_chair}/chair)</strong></li>
</ul>
<p>Message us for freight shipping quotes. Local pickup available.</p>
"""


def _default_description(chair_type: str, quantity: str) -> str:
    """Used when the LLM didn't produce description_text (DOM fallback, etc.).

    One sentence about condition only — matches the new prompt contract.
    """
    return "Used, good condition with normal wear from prior service."


_STATE_ABBR: dict[str, str] = {
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
    "district of columbia": "DC", "puerto rico": "PR",
}


def state_abbr(state: str) -> str:
    """Normalize a US state name to its 2-letter abbreviation.

    Returns the input unchanged if it's already 2 letters (assumed an abbr) or
    if no match is found. Case-insensitive.
    """
    s = (state or "").strip()
    if not s:
        return ""
    if len(s) == 2:
        return s.upper()
    return _STATE_ABBR.get(s.lower(), s)


def listing_title(
    chair_type: str, city: str = "", state: str = "", fallback: str = "",
) -> str:
    """Synthesize the FB/eBay listing title.

    Format: ``"<chair_type> (<city>, <STATE>)"``. Falls back to the LLM's
    raw title when chair_type is missing. The chair_type usually already
    carries color/material (e.g. "Tan Metal Folding Chairs"), so we don't
    re-prepend a color separately.
    """
    body = (chair_type or "").strip()
    if not body:
        return fallback or "Bulk Chairs"
    parts = []
    if city:
        parts.append(city.strip())
    abbr = state_abbr(state)
    if abbr:
        parts.append(abbr)
    loc = ", ".join(p for p in parts if p)
    return f"{body} ({loc})" if loc else body


def _location_line(
    location: str, city: str = "", state: str = "", zip_code: str = "",
) -> str:
    """Compose a 'City, State ZIP' style line, falling back to the raw location.

    When city/state/zip are explicitly provided we prefer them over the
    LLM-formatted `location` string so the ZIP can be appended cleanly.
    """
    parts: list[str] = []
    if city:
        parts.append(city.strip())
    if state:
        parts.append(state.strip())
    composed = ", ".join(p for p in parts if p)
    if zip_code:
        composed = f"{composed} {zip_code.strip()}".strip()
    return composed or location


def fb_description(
    location: str,
    chair_type: str,
    quantity: str,
    dimensions: str,
    description_text: str = "",
    city: str = "",
    state: str = "",
    zip_code: str = "",
) -> str:
    dims = dimensions.strip()
    text = description_text.strip() or _default_description(chair_type, quantity)
    dims_line = f"\n📏 Dimensions: {dims}" if dims else ""
    loc = _location_line(location, city, state, zip_code)
    return FB_DESCRIPTION.format(
        description_text=text,
        location=loc,
        quantity=quantity,
        dimensions_line=dims_line,
    )


def ebay_description(
    title: str, location: str, chair_type: str, quantity: str,
    dimensions: str, price_each: int, bulk_price: int,
    description_text: str = "",
    city: str = "",
    state: str = "",
    zip_code: str = "",
) -> str:
    dims_li = (
        f"<li><strong>Dimensions:</strong> {dimensions}</li>" if dimensions.strip() else ""
    )
    qty_int = int(quantity) if quantity.isdigit() else 1
    per_chair = round(bulk_price / max(qty_int, 1), 2)
    text = description_text.strip() or _default_description(chair_type, quantity)
    loc = _location_line(location, city, state, zip_code)
    return EBAY_DESCRIPTION_HTML.format(
        title=title,
        description_text=text,
        location=loc,
        chair_type=chair_type,
        quantity=quantity,
        dimensions_li=dims_li,
        price_each=price_each,
        bulk_price=bulk_price,
        bulk_price_per_chair=per_chair,
    )
