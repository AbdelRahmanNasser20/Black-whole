from pathlib import Path
from playwright.async_api import BrowserContext, Page

from .config import (
    FB_CATEGORY, FB_CONDITION, FB_PACKAGE_WEIGHT_LB, FB_PACKAGE_WEIGHT_OZ,
    FB_SHIPPING_CARRIER,
)
from .templates import fb_description, listing_title


CREATE_URL = "https://www.facebook.com/marketplace/create/item"


async def _fill_by_label(page: Page, label: str, value: str) -> None:
    el = page.get_by_label(label, exact=False).first
    await el.click()
    await el.fill(value)


async def _click_text(page: Page, text: str, timeout: int = 5000) -> None:
    await page.get_by_text(text, exact=False).first.click(timeout=timeout)


async def _ensure_hide_from_friends_on(page: Page, timeout: int = 5000) -> None:
    """Find the 'Hide from friends' switch and turn it ON if it isn't already.

    FB renders this as `<label>Hide from friends</label>` with a sibling switch
    (`role="switch"`, aria-checked="true|false"). We scroll the label into view,
    locate the nearest switch element, read its state, and click only when OFF.
    """
    label = page.get_by_text("Hide from friends", exact=False).first
    await label.wait_for(state="visible", timeout=timeout)
    await label.scroll_into_view_if_needed()

    # The switch is in the same form-row container. Walk up the DOM until we
    # find a container that has a role=switch descendant, then grab that switch.
    switch = await label.evaluate_handle(
        """el => {
          let cur = el;
          for (let i = 0; i < 6 && cur; i++) {
            const sw = cur.querySelector?.('[role="switch"]');
            if (sw) return sw;
            cur = cur.parentElement;
          }
          // Fallback: nearest switch by document order after the label.
          const all = Array.from(document.querySelectorAll('[role="switch"]'));
          return all[0] || null;
        }"""
    )
    sw_el = switch.as_element()
    if sw_el is None:
        raise RuntimeError("hide-from-friends switch element not found")

    state = await sw_el.get_attribute("aria-checked")
    if state == "true":
        print("[FB] hide-from-friends: already ON")
        return
    await sw_el.click()
    # Confirm the click landed.
    await page.wait_for_timeout(400)
    new_state = await sw_el.get_attribute("aria-checked")
    if new_state == "true":
        print("[FB] hide-from-friends: turned ON")
    else:
        print(f"[FB] hide-from-friends: click did not flip switch (state={new_state!r})")


async def create_draft(
    ctx: BrowserContext,
    title: str,
    price_per_chair: int,
    chair_type: str,
    location: str,
    quantity: str,
    dimensions: str,
    style_suffix: str,
    images: list[Path],
    description_text: str = "",
    city: str = "",
    state: str = "",
    zip_code: str = "",
    sku: str = "",
) -> tuple[str, str]:
    """Fill the FB Marketplace draft. Returns (draft_url, rendered_description).

    The rendered description is returned so the caller can persist exactly the
    copy we posted onto the marketplace (rather than re-rendering it later and
    possibly drifting).
    """
    page = await ctx.new_page()
    await page.goto(CREATE_URL, wait_until="domcontentloaded")
    await page.wait_for_timeout(4000)

    # Title format: "<chair_type> (<city>, <STATE>)" — e.g. "Banquet Chairs
    # (Fresno, CA)". Fall back to the LLM's raw title if chair_type is empty.
    # `style_suffix` ("Bulk Lot - Local Pickup" etc.) is now redundant and
    # dropped — the FB category + body description already convey that.
    full_title = listing_title(chair_type, city=city, state=state, fallback=title)
    description = fb_description(
        location, chair_type, quantity, dimensions, description_text,
        city=city, state=state, zip_code=zip_code,
    )

    # Photos — FB accepts set_input_files on its hidden <input type=file>
    file_input = await page.query_selector("input[type=file][accept*='image']")
    if file_input and images:
        await file_input.set_input_files([str(p) for p in images])
        await page.wait_for_timeout(2000)

    await _fill_by_label(page, "Title", full_title)
    await _fill_by_label(page, "Price", str(price_per_chair))

    # Category dropdown — type the target category itself so the dropdown
    # filters straight to it. (Previously typed "Furniture" and then tried to
    # click a sub-category, which only worked for compound names like "Dining
    # Furniture Sets" and missed standalone leaves like "Chairs".)
    try:
        await _click_text(page, "Category")
        await page.keyboard.type(FB_CATEGORY, delay=50)
        await page.wait_for_timeout(500)
        await _click_text(page, FB_CATEGORY)
    except Exception as e:
        print(f"[FB category fill fallback: {e}]")

    # Condition dropdown
    try:
        await _click_text(page, "Condition")
        await _click_text(page, FB_CONDITION)
    except Exception as e:
        print(f"[FB condition fill fallback: {e}]")

    await _fill_by_label(page, "Description", description)

    # SKU field — only shown on Marketplace Business / commerce-enabled
    # accounts. Best-effort: try the labels FB has used, fail silently if
    # the field isn't on this form.
    if sku:
        for label in ("SKU", "Inventory ID", "Product ID"):
            try:
                await _fill_by_label(page, label, sku)
                print(f"[FB] SKU filled into '{label}': {sku}")
                break
            except Exception:
                continue

    # Hide from friends toggle — must end up ON. The switch is a sibling/parent
    # of the "Hide from friends" label, so we locate the label first then walk
    # to the nearest [role=switch]. We only click when currently OFF; clicking
    # an already-ON switch would turn it OFF, defeating the purpose.
    try:
        await _ensure_hide_from_friends_on(page)
    except Exception as e:
        print(f"[FB hide-from-friends fallback: {e}]")

    # Save draft
    try:
        await page.get_by_role("button", name="Save draft").first.click()
        await page.wait_for_timeout(1500)
    except Exception:
        pass

    # Next -> shipping page
    try:
        await page.get_by_role("button", name="Next").first.click()
        await page.wait_for_timeout(2000)
    except Exception:
        pass

    # Shipping: weight + carrier (best-effort; leave defaults if selectors shift)
    try:
        await _fill_by_label(page, "Pounds", str(FB_PACKAGE_WEIGHT_LB))
        await _fill_by_label(page, "Ounces", str(FB_PACKAGE_WEIGHT_OZ))
    except Exception:
        pass

    # Save draft + next
    try:
        await page.get_by_role("button", name="Save draft").first.click()
        await page.wait_for_timeout(1000)
        await page.get_by_role("button", name="Next").first.click()
        await page.wait_for_timeout(1500)
    except Exception:
        pass

    # Offers page: negotiation OFF (toggle if currently on)
    try:
        offers = page.get_by_text("Let buyers", exact=False).first
        await offers.click()
    except Exception:
        pass

    print(f"[FB] draft prepared — review and click Publish. URL: {page.url}")
    return page.url, description
