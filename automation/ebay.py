from pathlib import Path
from playwright.async_api import BrowserContext, Page

from .templates import ebay_description


SELL_URL = "https://www.ebay.com/sl/sell"


async def _fill_by_label(page: Page, label: str, value: str) -> None:
    el = page.get_by_label(label, exact=False).first
    await el.click()
    await el.fill(value)


async def create_draft(
    ctx: BrowserContext,
    title: str,
    chair_type: str,
    location: str,
    city: str,
    state: str,
    quantity: str,
    dimensions: str,
    price_each: int,
    lot_id: str,
    images: list[Path],
) -> str:
    page = await ctx.new_page()
    await page.goto(SELL_URL, wait_until="domcontentloaded")
    await page.wait_for_timeout(4000)

    full_title = f"{title} - Bulk Deal - Local Pickup"[:80]
    sku = f"BWL-{lot_id}-{city.replace(' ', '')}"
    qty_int = int(quantity) if quantity.isdigit() else 1
    bulk_price = price_each * qty_int

    description_html = ebay_description(
        title=title,
        location=location,
        chair_type=chair_type,
        quantity=quantity,
        dimensions=dimensions,
        price_each=price_each,
        bulk_price=bulk_price,
    )

    # eBay's sell flow opens a product-search first. Fall back to the classic
    # form by searching for the title and clicking "Continue without match".
    try:
        search = page.get_by_placeholder("Tell us what you're selling").first
        await search.fill(full_title)
        await page.keyboard.press("Enter")
        await page.wait_for_timeout(3000)
        no_match = page.get_by_role("button", name="Continue without match").first
        await no_match.click(timeout=5000)
        await page.wait_for_timeout(2000)
    except Exception as e:
        print(f"[eBay product search step skipped: {e}]")

    # Photos
    try:
        file_input = await page.query_selector("input[type=file][accept*='image']")
        if file_input and images:
            await file_input.set_input_files([str(p) for p in images])
            await page.wait_for_timeout(3000)
    except Exception as e:
        print(f"[eBay photo upload: {e}]")

    # Title
    try:
        await _fill_by_label(page, "Title", full_title)
    except Exception:
        pass

    # SKU / Custom label
    try:
        await _fill_by_label(page, "Custom label", sku)
    except Exception:
        pass

    # Quantity
    try:
        await _fill_by_label(page, "Quantity", str(qty_int))
    except Exception:
        pass

    # Price
    try:
        await _fill_by_label(page, "Price", f"{price_each}.00")
    except Exception:
        pass

    # Location
    try:
        await _fill_by_label(page, "Item location", f"{city}, {state}")
    except Exception:
        pass

    # Description (HTML mode)
    try:
        html_btn = page.get_by_role("button", name="HTML").first
        await html_btn.click(timeout=3000)
        desc_area = page.get_by_label("Description", exact=False).first
        await desc_area.fill(description_html)
    except Exception as e:
        print(f"[eBay HTML description fallback: {e}]")

    # Save as draft
    try:
        await page.get_by_role("button", name="Save for later").first.click()
        await page.wait_for_timeout(2000)
    except Exception:
        pass

    print(f"[eBay] draft prepared — review and click List it. URL: {page.url}")
    return page.url
