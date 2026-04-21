import pytest
from pathlib import Path
from playwright.async_api import async_playwright

from automation import govdeals
from automation.llm.dom_fallback import _extract_quantity


FIXTURE = Path(__file__).parent / "fixtures" / "govdeals_sample.html"


@pytest.mark.asyncio
async def test_extract_js_against_fixture():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context()
        page = await ctx.new_page()
        await page.goto(f"file://{FIXTURE}")
        data = await page.evaluate(govdeals.EXTRACT_JS)
        await browser.close()

    assert data["title"] == "Lot of 299 Tan Metal Folding Chairs"
    assert "Nellis Air Force Base" in data["location"]
    assert data["city"] == "Nellis Air Force Base"
    assert data["quantity"] == "299"
    assert len(data["urls"]) == 3
    assert all("assets/photos" in u for u in data["urls"])


# ───────── _extract_quantity (Athens "10 vs 240" bug) ─────────

def test_quantity_prefers_description_when_dom_suspiciously_small():
    # Real bug: DOM regex caught a stray "(10)" but description has "240 Purple Chairs".
    assert _extract_quantity("10", "Lot includes 240 Purple Chairs in good condition.") == "240"


def test_quantity_keeps_dom_when_dom_is_plausible_and_larger():
    # DOM=200 is above the suspicion threshold; trust it over a smaller desc match.
    assert _extract_quantity("200", "comes with 40 chairs as backup") == "200"


def test_quantity_uses_description_when_dom_missing():
    assert _extract_quantity("", "Approx. 180 chairs available for pickup.") == "180"


def test_quantity_takes_largest_description_match():
    # Two desc mentions; pick the larger.
    assert _extract_quantity("5", "started with 60 chairs but only 320 chairs left") == "320"


def test_quantity_falls_back_to_one_when_nothing_useful():
    assert _extract_quantity("", "no quantity info here") == "1"


def test_quantity_keeps_non_digit_dom_when_no_desc_match():
    assert _extract_quantity("NA", "no chair count anywhere") == "NA"
