"""Offline tests for the Facebook Business catalog feed serializer (BLACKWHOLE-7).

Pure dict fixtures — no DB, no FastAPI, no network. Covers the FB column
contract, per-row eligibility (drop no-price / no-image rows), link/image/price
formatting, description fallback, the SITE_BASE_URL override, and CSV shape.
"""
import csv
import io

import pytest

from automation import catalog_feed


def _lot(**over):
    """A fully sellable, FB-eligible inventory row; override fields per test."""
    row = {
        "lot_id": "snap_06_asu_event_phoenix",
        "title": "ASU Event Chairs — Bulk Banquet / Event Chairs (Phoenix, AZ)",
        "description": "Bulk lot of used banquet/event chairs from ASU events.",
        "price_per_chair": 28.0,
        "quantity_remaining": 790,
        "status": "listed",
        "city": "Phoenix",
        "state": "AZ",
        "hero_image_url": "https://pub-4ac6bae8ec024e3aaccf3317c8873840.r2.dev/snap_06_asu_event_phoenix.jpg",
    }
    row.update(over)
    return row


BASE = "https://black-whole.com"


# ───────────────────────────── single-row mapping ────────────────────────────

def test_eligible_row_maps_all_columns():
    fr = catalog_feed.feed_row(_lot(), base_url=BASE)
    assert fr == {
        "id": "snap_06_asu_event_phoenix",
        "title": "ASU Event Chairs — Bulk Banquet / Event Chairs (Phoenix, AZ)",
        "description": "Bulk lot of used banquet/event chairs from ASU events.",
        "availability": "in stock",
        "condition": "used",
        "price": "28.00 USD",
        "link": "https://black-whole.com/listings/snap_06_asu_event_phoenix"
                f"?{catalog_feed.UTM_QUERY}",
        "image_link": "https://pub-4ac6bae8ec024e3aaccf3317c8873840.r2.dev/snap_06_asu_event_phoenix.jpg",
        "brand": "BLACKWHOLE Liquidation",
        "google_product_category": "Furniture > Chairs",
        "quantity_to_sell_on_facebook": "790",
        "product_tags[0]": "Phoenix",
        "product_tags[1]": "AZ",
    }


def test_quantity_blank_when_not_a_positive_int():
    # Meta ignores the column when blank; it rejects the item when it is 0.
    for bad in (None, 0, -3, "790"):
        assert catalog_feed.feed_row(
            _lot(quantity_remaining=bad), BASE)["quantity_to_sell_on_facebook"] == ""


def test_state_tag_is_normalized_to_two_letters():
    # A mixed "Michigan"/"CA" tag set makes a product-set filter miss lots.
    assert catalog_feed.feed_row(_lot(state="Michigan"), BASE)["product_tags[1]"] == "MI"
    assert catalog_feed.feed_row(_lot(state="idaho"), BASE)["product_tags[1]"] == "ID"
    assert catalog_feed.feed_row(_lot(state="ga"), BASE)["product_tags[1]"] == "GA"
    # Unknown values pass through rather than being dropped or guessed.
    assert catalog_feed.feed_row(_lot(state="Ontario"), BASE)["product_tags[1]"] == "Ontario"


def test_fb_product_category_is_not_emitted():
    # Guessing Meta's own taxonomy mis-files the product; it infers from Google's.
    assert "fb_product_category" not in catalog_feed.FEED_COLUMNS


def test_price_formatted_two_decimals_and_usd():
    assert catalog_feed.feed_row(_lot(price_per_chair=5), BASE)["price"] == "5.00 USD"
    assert catalog_feed.feed_row(_lot(price_per_chair="12.5"), BASE)["price"] == "12.50 USD"


def test_link_is_absolute_listings_url():
    fr = catalog_feed.feed_row(_lot(lot_id="abc123"), BASE)
    assert fr["link"] == f"https://black-whole.com/listings/abc123?{catalog_feed.UTM_QUERY}"


def test_link_carries_facebook_utm_attribution():
    fr = catalog_feed.feed_row(_lot(lot_id="abc123"), BASE)
    assert "utm_source=facebook" in fr["link"]
    assert "utm_medium=catalog" in fr["link"]


# ───────────────────────────── eligibility / drops ──────────────────────────

@pytest.mark.parametrize("bad", [None, "", 0, -5, "not-a-number"])
def test_row_without_valid_price_is_dropped(bad):
    assert catalog_feed.feed_row(_lot(price_per_chair=bad), BASE) is None


def test_row_without_image_is_dropped():
    assert catalog_feed.feed_row(_lot(hero_image_url=None), BASE) is None
    assert catalog_feed.feed_row(_lot(hero_image_url=""), BASE) is None


def test_local_image_path_is_not_accepted():
    # A relative /image/ path is useless to FB (it fetches server-side) → drop.
    assert catalog_feed.feed_row(_lot(hero_image_url="/image/folder/hero.jpg"), BASE) is None


def test_row_without_title_or_id_is_dropped():
    assert catalog_feed.feed_row(_lot(title=""), BASE) is None
    assert catalog_feed.feed_row(_lot(lot_id=None), BASE) is None


# ───────────────────────────── description fallback ─────────────────────────

def test_description_uses_lot_description_when_present():
    fr = catalog_feed.feed_row(_lot(description="  Hand written  copy. "), BASE)
    assert fr["description"] == "Hand written copy."


def test_description_synthesized_when_missing():
    fr = catalog_feed.feed_row(_lot(description=None), BASE)
    assert "790 available in Phoenix, AZ." in fr["description"]
    assert fr["description"].startswith("ASU Event Chairs")
    assert "inquire on our site" in fr["description"]


# ───────────────────────────── base URL override ────────────────────────────

def test_site_base_url_env_override(monkeypatch):
    monkeypatch.setenv("SITE_BASE_URL", "https://staging.example.com/")
    assert catalog_feed.site_base_url() == "https://staging.example.com"
    fr = catalog_feed.feed_row(_lot(lot_id="x"))  # no explicit base_url
    assert fr["link"] == f"https://staging.example.com/listings/x?{catalog_feed.UTM_QUERY}"


def test_site_base_url_default(monkeypatch):
    monkeypatch.delenv("SITE_BASE_URL", raising=False)
    assert catalog_feed.site_base_url() == "https://black-whole.com"


# ───────────────────────────── whole-CSV shape ──────────────────────────────

def test_csv_header_and_column_order():
    out = catalog_feed.rows_to_csv([], base_url=BASE)
    # Order copied from Meta's own template header (catalog_products.csv).
    assert out.splitlines()[0] == (
        "id,title,description,availability,condition,"
        "link,image_link,brand,price,"
        "google_product_category,quantity_to_sell_on_facebook,"
        "product_tags[0],product_tags[1]"
    )


def test_csv_round_trips_and_drops_incomplete_rows():
    rows = [
        _lot(lot_id="good1"),
        _lot(lot_id="noprice", price_per_chair=None),   # dropped
        _lot(lot_id="noimg", hero_image_url=None),       # dropped
        _lot(lot_id="good2", price_per_chair=10),
    ]
    out = catalog_feed.rows_to_csv(rows, base_url=BASE)
    parsed = list(csv.DictReader(io.StringIO(out)))
    ids = [r["id"] for r in parsed]
    assert ids == ["good1", "good2"]
    assert parsed[1]["price"] == "10.00 USD"
    # header columns exactly match the FB spec
    assert list(parsed[0].keys()) == catalog_feed.FEED_COLUMNS


def test_csv_quotes_fields_containing_commas():
    # Title has commas → the cell must be quoted so the CSV stays 9 columns.
    out = catalog_feed.rows_to_csv([_lot(lot_id="c")], base_url=BASE)
    parsed = list(csv.DictReader(io.StringIO(out)))
    assert "," in parsed[0]["title"]
    assert len(parsed[0]) == len(catalog_feed.FEED_COLUMNS)


def test_feed_statuses_exclude_draft():
    # The FB feed is narrower than the public storefront: no unconfirmed drafts.
    from automation import inventory
    # 'active_bid' joined 2026-08-26 (lot_channels): a lot we are bidding on is
    # already offered on Marketplace and by the CRM, so the feed carries it too.
    assert inventory.CATALOG_FEED_STATUSES == ("listed", "owned", "won_pickup", "active_bid")
    assert "draft" not in inventory.CATALOG_FEED_STATUSES
    assert "active_bid" in inventory.PUBLIC_STATUSES


# ───────────────────────────── availability by status ───────────────────────

def test_every_feed_status_is_in_stock():
    # Operator decision 2026-08-25: one availability value for every row, so the
    # feed can never disagree with the site or the Marketplace listing. A lot
    # that needs a caveat carries it in `inventory.description`, not here.
    for status in ("listed", "owned", "won_pickup"):
        assert catalog_feed.feed_row(_lot(status=status), BASE)["availability"] == "in stock"


def test_description_is_passed_through_untouched():
    desc = "Big lot. Ships on a pallet."
    fr = catalog_feed.feed_row(_lot(status="won_pickup", description=desc), BASE)
    assert desc in fr["description"]


# ───────────────────────────── dead-storage rejection ───────────────────────

_SUPABASE_URL = ("https://nihgzltpjriekyqqucbd.supabase.co/storage/v1/object/"
                 "public/listing-images/x.jpg")


def test_supabase_hero_is_dead_row_dropped():
    # Supabase Storage 402s on every public object — never ship such a URL.
    assert catalog_feed.feed_row(_lot(hero_image_url=_SUPABASE_URL), BASE) is None


def test_supabase_hero_falls_back_to_live_gallery_url():
    r2 = "https://pub-4ac6bae8ec024e3aaccf3317c8873840.r2.dev/x/01.jpg"
    fr = catalog_feed.feed_row(
        _lot(hero_image_url=_SUPABASE_URL, image_urls=[_SUPABASE_URL, r2]), BASE)
    assert fr["image_link"] == r2


def test_gallery_only_row_uses_first_gallery_url():
    r2 = "https://pub-4ac6bae8ec024e3aaccf3317c8873840.r2.dev/x/00.jpg"
    fr = catalog_feed.feed_row(_lot(hero_image_url=None, image_urls=[r2]), BASE)
    assert fr["image_link"] == r2
