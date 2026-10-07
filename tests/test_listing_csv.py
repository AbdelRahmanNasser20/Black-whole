"""Offline tests for the eBay bulk-upload CSV exporter (BLACKWHOLE-9).

Pure dict fixtures — no DB, no Playwright, no network. Covers the File Exchange
column contract, Add-vs-Revise selection, price/photo eligibility (drop rows
without either), Supabase-only PicURL, item-specific enrichment, the backlink,
the category / Business Policy env knobs, and CSV shape.
"""
import csv
import io

import pytest

from automation import listing_csv


def _lot(**over):
    """A fully sellable, eBay-eligible inventory row; override per test."""
    row = {
        "lot_id": "2807",
        "title": "Maroon Banquet Chairs — Bulk Lot (Cypress, TX)",
        "chair_type": "Maroon Fabric Banquet Chairs",
        "subtitle": "stackable steel frame",
        "description": "Bulk lot of used maroon banquet chairs.",
        "price_per_chair": 25.0,
        "quantity_remaining": 300,
        "status": "listed",
        "city": "Cypress",
        "state": "TX",
        "zip_code": "77429",
        "hero_image_url": "https://sb.example.co/storage/v1/object/public/listing-images/2807-hero.jpg",
        "image_urls": ["https://sb.example.co/storage/v1/object/public/listing-images/2807-1.jpg"],
        "ebay_url": None,
    }
    row.update(over)
    return row


BASE = "https://black-whole.com"


# ───────────────────────────── single-row mapping ────────────────────────────

def test_add_row_core_columns():
    r = listing_csv.csv_row(_lot(), base_url=BASE)
    assert r["Action"] == "Add"
    assert r["ItemID"] == ""
    assert r["CustomLabel"] == "2807"          # SKU = lot id
    assert r["ConditionID"] == "3000"          # Used
    assert r["Format"] == "FixedPrice"
    assert r["Duration"] == "GTC"
    assert r["StartPrice"] == "25.00"
    assert r["Quantity"] == "1"                 # lead-gen default
    assert r["Location"] == "Cypress, TX"
    assert r["PostalCode"] == "77429"


def test_title_capped_at_80_and_has_location():
    r = listing_csv.csv_row(_lot(chair_type="X" * 100), base_url=BASE)
    assert len(r["Title"]) <= 80


def test_backlink_in_description():
    r = listing_csv.csv_row(_lot(lot_id="2807"), base_url=BASE)
    assert "https://black-whole.com/listings/2807" in r["Description"]


def test_item_specifics_enriched_from_freetext():
    r = listing_csv.csv_row(_lot(), base_url=BASE)
    assert r["C:Brand"] == "Unbranded"
    assert r["C:Type"] == "Banquet Chair"
    assert r["C:Color"] == "Red"               # "maroon" → Red
    assert r["C:Frame Material"] == "Metal"    # "steel" → Metal
    assert r["C:Seat Material"] == "Fabric"    # "fabric" → Fabric
    assert r["C:Features"] == "Stackable"      # "stackable" → Stackable


# ───────────────────────────── Add vs Revise ────────────────────────────────

def test_existing_ebay_url_becomes_revise():
    r = listing_csv.csv_row(_lot(ebay_url="https://www.ebay.com/itm/336648980712"), BASE)
    assert r["Action"] == "Revise"
    assert r["ItemID"] == "336648980712"


def test_existing_item_id_field_used():
    r = listing_csv.csv_row(_lot(ebay_item_id="336544966627"), BASE)
    assert r["Action"] == "Revise"
    assert r["ItemID"] == "336544966627"


def test_no_ebay_url_is_add():
    assert listing_csv.csv_row(_lot(ebay_url=""), BASE)["Action"] == "Add"


# ───────────────────────────── eligibility / drops ──────────────────────────

@pytest.mark.parametrize("bad", [None, "", 0, -5, "not-a-number"])
def test_row_without_valid_price_is_dropped(bad):
    assert listing_csv.csv_row(_lot(price_per_chair=bad), BASE) is None


def test_row_without_any_photo_is_dropped():
    assert listing_csv.csv_row(_lot(hero_image_url=None, image_urls=None), BASE) is None
    assert listing_csv.csv_row(_lot(hero_image_url="", image_urls=[]), BASE) is None


def test_local_image_path_is_not_accepted():
    r = listing_csv.csv_row(_lot(hero_image_url="/image/2807/hero.jpg", image_urls=None), BASE)
    assert r is None


# ───────────────────────────── photos / PicURL ──────────────────────────────

def test_picurl_hero_first_then_extras_pipe_joined():
    r = listing_csv.csv_row(_lot(
        hero_image_url="https://x/h.jpg",
        image_urls=["https://x/a.jpg", "https://x/b.jpg"]), BASE)
    assert r["PicURL"] == "https://x/h.jpg|https://x/a.jpg|https://x/b.jpg"


def test_picurl_dedupes_and_drops_non_http():
    r = listing_csv.csv_row(_lot(
        hero_image_url="https://x/h.jpg",
        image_urls=["https://x/h.jpg", "/local/rel.jpg", "https://x/a.jpg"]), BASE)
    assert r["PicURL"] == "https://x/h.jpg|https://x/a.jpg"


def test_image_urls_accepts_json_string():
    r = listing_csv.csv_row(_lot(
        hero_image_url=None,
        image_urls='["https://x/a.jpg", "https://x/b.jpg"]'), BASE)
    assert r["PicURL"] == "https://x/a.jpg|https://x/b.jpg"


# ───────────────────────────── env knobs ────────────────────────────────────

def test_category_from_env(monkeypatch):
    monkeypatch.setenv("EBAY_BANQUET_CATEGORY_ID", "29508")
    assert listing_csv.csv_row(_lot(), BASE)["Category"] == "29508"


def test_category_blank_without_env(monkeypatch):
    monkeypatch.delenv("EBAY_BANQUET_CATEGORY_ID", raising=False)
    assert listing_csv.csv_row(_lot(), BASE)["Category"] == ""


def test_business_policy_columns_appended_when_set(monkeypatch):
    monkeypatch.setenv("EBAY_SHIPPING_PROFILE", "Freight LTL")
    monkeypatch.setenv("EBAY_RETURN_PROFILE", "No returns")
    cols = listing_csv.columns()
    assert cols[-2:] == ["ShippingProfileName", "ReturnProfileName"]
    r = listing_csv.csv_row(_lot(), BASE)
    assert r["ShippingProfileName"] == "Freight LTL"


def test_no_policy_columns_without_env(monkeypatch):
    for e in ("EBAY_SHIPPING_PROFILE", "EBAY_RETURN_PROFILE", "EBAY_PAYMENT_PROFILE"):
        monkeypatch.delenv(e, raising=False)
    assert listing_csv.columns() == listing_csv.CSV_COLUMNS


# ───────────────────────────── whole-CSV shape ──────────────────────────────

def test_csv_header_is_file_exchange_order(monkeypatch):
    for e in ("EBAY_SHIPPING_PROFILE", "EBAY_RETURN_PROFILE", "EBAY_PAYMENT_PROFILE"):
        monkeypatch.delenv(e, raising=False)
    header = listing_csv.rows_to_csv([], base_url=BASE).splitlines()[0]
    assert header.split(",")[:6] == [
        "Action", "CustomLabel", "Category", "Title", "ConditionID", "PicURL"]
    assert header.endswith("ItemID")


def test_csv_round_trips_and_drops_incomplete_rows():
    rows = [
        _lot(lot_id="good1"),
        _lot(lot_id="noprice", price_per_chair=None),                 # dropped
        _lot(lot_id="nophoto", hero_image_url=None, image_urls=None),  # dropped
        _lot(lot_id="good2", price_per_chair=10,
             ebay_url="https://www.ebay.com/itm/336648980712"),        # Revise
    ]
    out = listing_csv.rows_to_csv(rows, base_url=BASE)
    parsed = list(csv.DictReader(io.StringIO(out)))
    assert [r["CustomLabel"] for r in parsed] == ["good1", "good2"]
    assert parsed[1]["Action"] == "Revise"
    assert parsed[1]["ItemID"] == "336648980712"
    assert list(parsed[0].keys()) == listing_csv.columns()


def test_csv_quotes_fields_containing_commas():
    out = listing_csv.rows_to_csv([_lot(lot_id="c")], base_url=BASE)
    parsed = list(csv.DictReader(io.StringIO(out)))
    assert "," in parsed[0]["Title"]
    assert len(parsed[0]) == len(listing_csv.columns())
