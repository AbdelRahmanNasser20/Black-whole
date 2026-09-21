"""Offline tests for the Google Merchant Center feed serializer (multichannel Phase 2).

Pure dict fixtures — no DB, no FastAPI, no network. Mirrors tests/test_catalog_feed.py.
"""
import csv
import io

import pytest

from automation import google_feed

R2 = "https://pub-4ac6bae8ec024e3aaccf3317c8873840.r2.dev"
BASE = "https://black-whole.com"


def _lot(**over):
    row = {
        "lot_id": "31225-atl",
        "title": "Brown Convention Chairs — Atlanta, GA",
        "description": "1,200 stackable banquet chairs, brown fabric, metal frame.",
        "price_per_chair": 25.0,
        "quantity_remaining": 1200,
        "status": "won_pickup",
        "city": "Atlanta",
        "state": "Georgia",
        "hero_image_url": f"{R2}/31225-atl/hero.jpg",
        "image_urls": [f"{R2}/31225-atl/hero.jpg", f"{R2}/31225-atl/2.jpg", f"{R2}/31225-atl/3.jpg"],
        "storage_note": "Unit 12, gate code 4455",
    }
    row.update(over)
    return row


def test_columns_match_google_spec():
    assert google_feed.FEED_COLUMNS == [
        "id", "title", "description", "link", "image_link", "additional_image_link",
        "availability", "price", "condition", "brand", "identifier_exists",
        "google_product_category", "product_type", "custom_label_0", "custom_label_1",
    ]


def test_eligible_row_maps_all_columns():
    fr = google_feed.feed_row(_lot(), base_url=BASE)
    assert fr == {
        "id": "31225-atl",
        "title": "Brown Convention Chairs — Atlanta, GA",
        "description": "1,200 stackable banquet chairs, brown fabric, metal frame.",
        "link": f"{BASE}/listings/31225-atl?{google_feed.UTM_QUERY}",
        "image_link": f"{R2}/31225-atl/hero.jpg",
        "additional_image_link": f"{R2}/31225-atl/2.jpg,{R2}/31225-atl/3.jpg",
        "availability": "in_stock",
        "price": "25.00 USD",
        "condition": "used",
        "brand": "BLACKWHOLE Liquidation",
        "identifier_exists": "no",
        "google_product_category": "Furniture > Chairs",
        "product_type": "Seating > Banquet Chairs",
        "custom_label_0": "GA",
        "custom_label_1": "Atlanta",
    }


def test_utm_marks_google_feed():
    assert google_feed.UTM_QUERY == "utm_source=google&utm_medium=feed&utm_campaign=merchant"


@pytest.mark.parametrize("over", [
    {"price_per_chair": None},
    {"price_per_chair": 0},
    {"hero_image_url": None, "image_urls": []},
    {"hero_image_url": "https://nihgzltpjriekyqqucbd.supabase.co/storage/v1/object/public/x.jpg", "image_urls": []},
    {"title": ""},
])
def test_incomplete_rows_are_dropped(over):
    assert google_feed.feed_row(_lot(**over), base_url=BASE) is None


def test_additional_images_cap_at_ten_and_exclude_hero():
    urls = [f"{R2}/31225-atl/{i}.jpg" for i in range(14)]
    fr = google_feed.feed_row(_lot(hero_image_url=urls[0], image_urls=urls), base_url=BASE)
    extra = fr["additional_image_link"].split(",")
    assert len(extra) == 10
    assert urls[0] not in extra


def test_tables_get_table_product_type():
    fr = google_feed.feed_row(_lot(title="Round Banquet Tables (Augusta, GA)", chair_type="table"), base_url=BASE)
    assert fr["google_product_category"] == google_feed.catalog_feed.GOOGLE_CATEGORY_TABLES
    assert fr["product_type"] == "Tables > Banquet Tables"


def test_title_truncated_to_150():
    fr = google_feed.feed_row(_lot(title="x" * 200), base_url=BASE)
    assert len(fr["title"]) == 150


def test_storage_note_never_appears():
    csv_text = google_feed.rows_to_csv([_lot()], base_url=BASE)
    assert "gate code" not in csv_text
    assert "Unit 12" not in csv_text


def test_csv_has_header_and_eligible_rows_only():
    text = google_feed.rows_to_csv([_lot(), _lot(lot_id="bad", price_per_chair=None)], base_url=BASE)
    rows = list(csv.DictReader(io.StringIO(text)))
    assert [r["id"] for r in rows] == ["31225-atl"]
    assert text.splitlines()[0] == ",".join(google_feed.FEED_COLUMNS)


def test_site_base_url_env_override(monkeypatch):
    monkeypatch.setenv("SITE_BASE_URL", "https://staging.example.com/")
    fr = google_feed.feed_row(_lot())
    assert fr["link"].startswith("https://staging.example.com/listings/31225-atl?")
