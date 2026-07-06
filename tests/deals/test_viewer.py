from unittest.mock import patch
from fastapi.testclient import TestClient
from automation.web.app import app

def test_viewer_renders_archived_lot_from_store():
    # full column set as the route's SELECT * returns it (absent values are
    # None in real rows, never missing keys)
    row = dict(asset_id=984, account_id=6466, auction_id=2, title="9 chairs",
               description="Nine stackable banquet chairs", current_bid=10.0, bid_count=0,
               currency_code="USD", city="Warren", state="ME", outcome="no_bid",
               hero_image_url="https://store/govdeals/984_6466_2/00.jpg",
               native_category_name="Furniture and Furnishings",
               native_category_id="372", canonical_category="seating_furniture",
               seller=None, end_utc=None, opening_bid=5.0, has_reserve=False,
               final_bid=None, final_bid_count=0, first_seen_at=None,
               images_archived=True, archived_hero_url=None, gallery_urls=None)
    with patch("automation.web.app.db.fetch_one", return_value=row):
        r = TestClient(app).get("/deals/984/6466/2")
    assert r.status_code == 200
    assert "9 chairs" in r.text and "Nine stackable banquet chairs" in r.text
    assert "no_bid" in r.text

def test_viewer_404_when_absent():
    with patch("automation.web.app.db.fetch_one", return_value=None):
        r = TestClient(app).get("/deals/1/1/1")
    assert r.status_code == 404

def _json_row(**over):
    row = dict(asset_id=984, account_id=6466, auction_id=2, title="9 chairs",
               description="d", native_category_name="Furniture", canonical_category="seating_furniture",
               city="Warren", state="ME", seller=None, bid_count=0, current_bid=10.0,
               opening_bid=5.0, currency_code="USD", end_utc=None, outcome=None,
               final_bid=None, final_bid_count=None, images_archived=True,
               archived_hero_url="https://store/hero.webp",
               gallery_urls=["https://store/g1.webp", "https://store/g2.webp"],
               hero_image_url="https://cdn/hero.jpg")
    row.update(over)
    return row

def test_deal_json_prefers_archived_images():
    with patch("automation.web.app.db.fetch_one", return_value=_json_row()):
        r = TestClient(app).get("/api/deals/984/6466/2")
    assert r.status_code == 200
    d = r.json()
    assert d["images"] == ["https://store/hero.webp", "https://store/g1.webp", "https://store/g2.webp"]
    assert d["image_source"] == "archived"
    assert "archived_hero_url" not in d          # flattened into images[]

def test_deal_json_falls_back_to_cdn_hero():
    row = _json_row(images_archived=False, archived_hero_url=None, gallery_urls=None)
    with patch("automation.web.app.db.fetch_one", return_value=row):
        d = TestClient(app).get("/api/deals/984/6466/2").json()
    assert d["images"] == ["https://cdn/hero.jpg"]
    assert d["image_source"] == "cdn"
