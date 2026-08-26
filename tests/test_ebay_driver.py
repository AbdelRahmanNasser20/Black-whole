"""Unit tests for the pure helpers in the eBay listing driver (BLACKWHOLE-9).

The item-id scrape and the publish-confirmation logic are factored out of the
async browser flow so they can be tested without Playwright. The key regression:
a listing that went live but whose id we couldn't scrape must still report
``published=True`` (the Maroon false-negative — lot 2807 published but returned
``published=False``).
"""
from automation.drivers import ebay


# ───────────────────────────── extract_item_id ──────────────────────────────

def test_extract_from_itm_href():
    assert ebay.extract_item_id("https://www.ebay.com/itm/336648980712") == "336648980712"


def test_extract_from_id_text():
    assert ebay.extract_item_id("Your item ID: 336544966627") == "336544966627"


def test_extract_prefers_first_source():
    assert ebay.extract_item_id(
        None, "", "https://www.ebay.com/itm/123456789", "ID 999999999") == "123456789"


def test_extract_ignores_short_numbers():
    # A price or count is not a 9+ digit item id.
    assert ebay.extract_item_id("Quantity 300, price 25") is None


def test_extract_returns_none_when_absent():
    assert ebay.extract_item_id(None, "", "no id here") is None


# ───────────────────────────── looks_published ──────────────────────────────

def test_published_from_live_marker_without_id():
    # The Maroon case: success banner present, no /itm/ id in URL yet.
    assert ebay.looks_published("Your listing is now live!", "https://www.ebay.com/sl/lstng") is True


def test_published_from_itm_url():
    assert ebay.looks_published("", "https://www.ebay.com/itm/336648980712") is True


def test_not_published_on_editor_page():
    assert ebay.looks_published("Save for later  List it", "https://www.ebay.com/sl/list") is False


def test_published_marker_is_case_insensitive():
    assert ebay.looks_published("YOUR LISTING IS LIVE", "") is True
