FB_DESCRIPTION = """\
Location: {location}

Large Quantity of {chair_type} Available
{dimensions_line}

Update: About {quantity} chairs left in current inventory

Great for churches, banquet halls, community centers, and large events.

Please include in your message:
1. Chair type / photo number
2. Quantity needed
3. Pickup or delivery
4. Your city / zip code for delivery quotes

Delivery is available for a fee based on distance and order size.

Bulk orders welcome. First come, first served.
"""


EBAY_DESCRIPTION_HTML = """\
<h2>{title}</h2>
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
<p><strong>Black Whole Liquidation — Bulk Chairs at Wholesale Prices</strong></p>
"""


def fb_description(location: str, chair_type: str, quantity: str, dimensions: str) -> str:
    dims = dimensions.strip()
    return FB_DESCRIPTION.format(
        location=location,
        chair_type=chair_type,
        quantity=quantity,
        dimensions_line=dims if dims else "",
    )


def ebay_description(
    title: str, location: str, chair_type: str, quantity: str,
    dimensions: str, price_each: int, bulk_price: int,
) -> str:
    dims_li = (
        f"<li><strong>Dimensions:</strong> {dimensions}</li>" if dimensions.strip() else ""
    )
    qty_int = int(quantity) if quantity.isdigit() else 1
    per_chair = round(bulk_price / max(qty_int, 1), 2)
    return EBAY_DESCRIPTION_HTML.format(
        title=title,
        location=location,
        chair_type=chair_type,
        quantity=quantity,
        dimensions_li=dims_li,
        price_each=price_each,
        bulk_price=bulk_price,
        bulk_price_per_chair=per_chair,
    )
