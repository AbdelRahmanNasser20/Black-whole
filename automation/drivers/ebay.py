"""eBay listing driver — codifies the Sell-flow funnel proven on lot 31225.

Funnel: prelist/home → type → Search → pick "Banquet Chairs" category →
Continue without match → condition (Used) → editor. Then fill the editor from a
`ListingContent` (title, photos, price, qty, UPC, description-in-iframe, item
specifics from enriched attributes, brand) and either publish ("List it") or
save a draft ("Save for later").

Lead-gen oriented: quantity defaults to 1, the description carries bulk details
and the black-whole.com backlink, and item specifics are filled as completely as
the parsed attributes allow.
"""
from __future__ import annotations

import re
from pathlib import Path

from ..listing_content import ListingContent

NAME = "ebay"
PRELIST = "https://www.ebay.com/sl/prelist/home"
BANQUET_CAT = "Restaurant Chairs & Seating > Banquet Chairs"
_SEARCH_SEED = "Banquet Chairs bulk lot used"

_ITM_RE = re.compile(r"/itm/(\d{9,})")
_ID_TEXT_RE = re.compile(r"ID[:\s#]*(\d{9,})")
# Text eBay shows once a listing goes live — used to confirm a publish even when
# the item id can't be scraped from the confirmation modal.
_LIVE_MARKERS = (
    "is now live", "listing is live", "your listing is live",
    "you're all set", "listing was submitted",
)


def extract_item_id(*sources: str | None) -> str | None:
    """Pull a 9+ digit eBay item id from any of the given strings (an href, the
    page URL, page text). Pure — unit-testable without a browser."""
    for s in sources:
        if not s:
            continue
        m = _ITM_RE.search(s) or _ID_TEXT_RE.search(s)
        if m:
            return m.group(1)
    return None


def looks_published(page_text: str | None, url: str | None) -> bool:
    """True when the page shows an eBay 'listing is live' success state.

    Decoupled from item-id capture so a publish that succeeded but whose id we
    couldn't scrape is still reported ``published=True`` (fixes the Maroon
    false-negative). Pure — unit-testable."""
    text = (page_text or "").lower()
    if any(marker in text for marker in _LIVE_MARKERS):
        return True
    return bool(_ITM_RE.search(url or ""))


async def _shoot(page, screenshot_dir, name):
    if screenshot_dir:
        try:
            await page.screenshot(path=str(Path(screenshot_dir) / f"ebay_{name}.png"),
                                  full_page=True)
        except Exception:
            pass


async def _set_text(page, name, value):
    el = page.locator(f'input[name="{name}"]').first
    await el.click()
    await el.press("ControlOrMeta+a")
    await el.press("Delete")
    await el.type(str(value), delay=12)


async def _set_specific(page, label, value):
    """Best-effort fill of an eBay item-specific combobox."""
    try:
        cb = page.get_by_role("combobox", name=label).first
        if not await cb.count():
            cb = page.locator(f'[aria-label*="{label}" i]').first
        if not await cb.count():
            return False
        await cb.click(timeout=4000)
        await page.wait_for_timeout(400)
        await page.keyboard.type(value, delay=25)
        await page.wait_for_timeout(500)
        opt = page.get_by_role("option", name=value, exact=False).first
        if await opt.count():
            await opt.click(timeout=2500)
        else:
            await page.keyboard.press("Enter")
        return True
    except Exception:
        return False


async def _reach_editor(page):
    await page.goto(PRELIST, wait_until="domcontentloaded")
    await page.wait_for_timeout(3500)
    box = await page.query_selector('input[type=text]')
    await box.click()
    await box.type(_SEARCH_SEED, delay=18)
    await page.wait_for_timeout(1000)
    await page.get_by_role("button", name="Search", exact=True).first.click(timeout=6000)
    await page.wait_for_timeout(4000)
    await page.get_by_text(BANQUET_CAT, exact=False).first.click(timeout=6000)
    await page.wait_for_timeout(3500)
    await page.get_by_role("button", name="Continue without match",
                           exact=False).first.click(timeout=6000)
    await page.wait_for_timeout(5000)
    # condition radios: New, New-Open box, Seller refurbished, Used, For parts
    await page.locator('input[type=radio]').nth(3).check(force=True)
    await page.wait_for_timeout(1200)
    await page.get_by_role("button", name="Continue to listing",
                           exact=False).first.click(timeout=8000)
    await page.wait_for_timeout(10000)


async def _fill_editor(page, content: ListingContent):
    await _set_text(page, "title", content.title[:80])
    await _set_text(page, "price", f"{content.price}.00")
    try:
        await _set_text(page, "quantity", str(content.quantity))
    except Exception:
        pass
    try:
        await _set_text(page, "universalProductCode", "Does not apply")
    except Exception:
        pass

    # photos
    fi = await page.query_selector('input[type=file]')
    if fi and content.photos:
        await fi.set_input_files([str(p) for p in content.photos])
        await page.wait_for_timeout(8000)

    # description (rich-text iframe)
    try:
        body = page.frame_locator('iframe#se-rte-frame__summary').locator('body')
        await body.click()
        await body.press("ControlOrMeta+a")
        await body.press("Delete")
        await body.type(content.description, delay=3)
    except Exception as e:
        print(f"[ebay] description fill fallback: {str(e)[:80]}")

    # item specifics from enriched attributes
    for label, value in content.attributes.as_ebay_specifics().items():
        await _set_specific(page, label, value)


async def _confirm_publish(page) -> tuple[bool, str | None]:
    """After clicking "List it", read the confirmation state.

    Returns ``(published, item_id)``. ``published`` reflects whether eBay showed
    a live-listing success state — it is *not* gated on scraping the id, so a
    listing that went live but whose id we couldn't read still reports
    ``published=True``. The item id is best-effort from the "View listing" link,
    the "ID: <num>" text, or the URL.
    """
    # Give the "Your listing is now live" confirmation a chance to render.
    try:
        await page.get_by_text("is now live", exact=False).first.wait_for(timeout=9000)
    except Exception:
        pass

    href = await page.evaluate(
        "() => { const a=[...document.querySelectorAll('a')]"
        ".find(x=>/view listing/i.test(x.innerText||'')); return a?a.href:null; }")
    try:
        body_text = await page.inner_text("body")
    except Exception:
        body_text = ""

    item_id = extract_item_id(href, page.url, body_text)
    published = looks_published(body_text, page.url) or bool(item_id)
    return published, item_id


async def create(ctx, content: ListingContent, *, publish: bool = False,
                 screenshot_dir: str | None = None) -> dict:
    """Create an eBay listing from `content`. Returns {item_id, url, published}."""
    page = await ctx.new_page()
    try:
        await _reach_editor(page)
        await _fill_editor(page, content)
        await page.wait_for_timeout(1500)
        await _shoot(page, screenshot_dir, f"{content.lot_id}_ready")

        btn = "List it" if publish else "Save for later"
        await page.get_by_role("button", name=btn, exact=True).first.click(timeout=10000)
        await page.wait_for_timeout(7000)
        await _shoot(page, screenshot_dir, f"{content.lot_id}_done")

        published, item_id = (await _confirm_publish(page)) if publish else (False, None)
        url = f"https://www.ebay.com/itm/{item_id}" if item_id else page.url
        return {"item_id": item_id, "url": url, "published": published}
    finally:
        await page.close()
