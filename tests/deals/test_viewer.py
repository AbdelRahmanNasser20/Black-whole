from unittest.mock import patch
from fastapi.testclient import TestClient
from automation.web.app import app

def test_viewer_renders_archived_lot_from_store():
    row = dict(asset_id=984, account_id=6466, auction_id=2, title="9 chairs",
               description="Nine stackable banquet chairs", current_bid=10.0, bid_count=0,
               currency_code="USD", city="Warren", state="ME", outcome="no_bid",
               hero_image_url="https://store/govdeals/984_6466_2/00.jpg",
               native_category_name="Furniture and Furnishings")
    with patch("automation.web.app.db.fetch_one", return_value=row):
        r = TestClient(app).get("/deals/984/6466/2")
    assert r.status_code == 200
    assert "9 chairs" in r.text and "Nine stackable banquet chairs" in r.text
    assert "no_bid" in r.text

def test_viewer_404_when_absent():
    with patch("automation.web.app.db.fetch_one", return_value=None):
        r = TestClient(app).get("/deals/1/1/1")
    assert r.status_code == 404
