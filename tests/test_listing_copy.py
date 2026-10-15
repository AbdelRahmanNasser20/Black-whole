"""No bulk / tiered / volume prices in any public listing copy (operator HARD
RULE, 2026-10-15). The guard itself, then every channel writer that uses it."""
import pytest

from automation import catalog_feed, google_feed, lot_channels
from automation import craigslist
from automation.listing_copy import find_bulk_pricing, has_bulk_pricing, strip_bulk_pricing
from automation.llm.base import EXTRACTION_PROMPT
from automation.templates import ebay_description, fb_description
from automation.web import seo_copy


BULK = [
    "Pickup or delivery; $25 per chair, with bulk pricing on 100+.",
    "Asking $25 per chair — bulk discounts on the full lot.",
    "Text or email the quantity you're after — bulk discounts on larger orders.",
    "$22 for 500+",
    "$22 each for 100 or more chairs.",
    "500+ chairs at $20 each.",
    "Bulk: 25+ → $22 · 100+ → $20 · whole lot → best offer.",
    "Bulk tiers available.",
    "Take ALL 300 for $6,000 ($20/chair).",
    "Whole lot: $6,000 before freight.",
    "Make an offer on the whole lot.",
    "$25/chair or best offer on all 68.",
    "Volume pricing available for venues.",
    "Ask about quantity discounts.",
    "Price breaks for churches buying 200 or more.",
    "Better pricing for larger orders.",
    "The more you buy, the more you save.",
    "Priced per chair; bulk discounts available.",
]

CLEAN = [
    "Asking $25 per chair.",
    "$25 each, local pickup in Atlanta.",
    "About 2,500 MTS Seating 600-series padded banquet stacking chairs in brown.",
    "Bulk lot of white folding chairs — molded poly seat and back.",
    "Sold in bulk; palletized for freight or local pickup.",
    "442 available — buy a few or the whole lot.",
    "Quantity available: 1,200",
    "$25/chair or best offer.",
    "Ideal for churches, banquet halls, schools, and event venues.",
    "Pickup is free. Freight quoted to your ZIP.",
]


@pytest.mark.parametrize("text", BULK)
def test_guard_catches_bulk_pricing(text):
    assert has_bulk_pricing(text), text
    assert find_bulk_pricing(text)
    assert not has_bulk_pricing(strip_bulk_pricing(text))


@pytest.mark.parametrize("text", CLEAN)
def test_guard_leaves_plain_copy_alone(text):
    assert not has_bulk_pricing(text), text
    assert strip_bulk_pricing(text) == text


def test_strip_keeps_the_rest_of_the_line_and_the_layout():
    text = ("About 2,500 brown chairs. Used, good condition. "
            "Pickup or delivery; $25 per chair, with bulk pricing on 100+.\n\n"
            "Bulk discounts on larger orders!\n\nIdeal for churches.")
    out = strip_bulk_pricing(text)
    assert out == "About 2,500 brown chairs. Used, good condition.\n\nIdeal for churches."


def test_strip_handles_empty():
    assert strip_bulk_pricing(None) == ""
    assert strip_bulk_pricing("") == ""
    assert find_bulk_pricing(None) == []


# ── channel writers ──

DIRTY_BLURB = "Brown padded banquet chairs. $25 per chair, with bulk pricing on 100+."


def test_fb_marketplace_body_has_no_bulk_price():
    body = fb_description("Atlanta, GA", "Banquet Chairs", "300", "", DIRTY_BLURB)
    assert not has_bulk_pricing(body) and "Brown padded banquet chairs." in body


def test_ebay_body_is_one_per_chair_price():
    html = ebay_description(title="300 Banquet Chairs", location="Atlanta, GA",
                            chair_type="Banquet Chairs", quantity="300", dimensions="",
                            price_each=25, description_text=DIRTY_BLURB)
    assert "$25 per chair" in html
    assert "Take ALL" not in html and "$7500" not in html
    assert not has_bulk_pricing(html)


def test_relist_plan_body_has_no_bulk_price():
    body = lot_channels.fb_description(blurb=DIRTY_BLURB, city="Atlanta", state="GA",
                                       quantity=300, lot_id="31225", unit="chair",
                                       profile_id=None)
    assert not has_bulk_pricing(body) and body.startswith("Brown padded banquet chairs.")


def _lot(**over):
    row = {"lot_id": "31225", "title": "Brown Banquet Chairs (Boise, ID)",
           "description": DIRTY_BLURB, "price_per_chair": 25.0,
           "quantity_remaining": 2500, "status": "won_pickup", "city": "Boise", "state": "ID",
           "hero_image_url": "https://pub-4ac6bae8ec024e3aaccf3317c8873840.r2.dev/x.jpg"}
    row.update(over)
    return row


def test_fb_and_google_feeds_have_no_bulk_price():
    fb = catalog_feed.feed_row(_lot(), base_url="https://black-whole.com")
    gg = google_feed.feed_row(_lot(), base_url="https://black-whole.com")
    for row in (fb, gg):
        assert row is not None
        assert not any(has_bulk_pricing(str(v)) for v in row.values())
        assert row["description"].startswith("Brown padded banquet chairs.")


def test_craigslist_copy_has_no_bulk_price_in_any_city():
    listing = craigslist.CraigslistListing(chair_type="Banquet Chairs", quantity="300",
                                           price=25, description_text=DIRTY_BLURB)
    for sub in ("atlanta", "phoenix", "boise", "nashville", "chicago", "losangeles"):
        _, body = craigslist.deterministic_varier(listing, sub, sub.title())
        assert not has_bulk_pricing(body), (sub, body)
        assert "Asking $25 per chair." in body


def test_storefront_has_no_whole_lot_price():
    facts = dict(seo_copy.freight_facts(_lot(chair_frame="Steel"), sold=False))
    assert "Whole lot" not in facts


def test_storefront_decorate_strips_description():
    import importlib
    web_app = importlib.import_module("automation.web.app")
    row = web_app._decorate(_lot())
    assert not has_bulk_pricing(row["description"])


def test_llm_prompt_forbids_bulk_pricing():
    assert "never bulk, volume, tiered or whole-lot pricing" in EXTRACTION_PROMPT
    assert "down if very large" not in EXTRACTION_PROMPT
