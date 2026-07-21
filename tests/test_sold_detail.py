"""A sold lot's detail page returns 200, emits SoldOut, and shows the label.
inventory is monkeypatched (pattern from tests/test_seo.py — TestClient without
a context manager so the favorites scheduler startup hook never runs). Run:
  .venv/bin/python -m pytest tests/test_sold_detail.py -v
"""
import importlib
from fastapi.testclient import TestClient

web_app = importlib.import_module("automation.web.app")

SOLD_ROW = {
    "lot_id": "5003", "title": "Burgundy Vinyl Banquet Chairs",
    "description": "Lost at auction.", "city": "Fresno", "state": "CA",
    "zip_code": "93650", "chair_type": "banquet", "quantity_remaining": 700,
    "quantity_original": 700, "price_per_chair": 12.0, "status": "lost_sold_out",
    "folder_name": None, "hero_image": None, "hero_image_url": None,
    "image_urls": None, "facebook_url": None, "ebay_url": None,
    "subtitle": None, "dimensions": None,
}

AVAIL_ROW = {**SOLD_ROW, "lot_id": "334", "status": "listed"}


def test_sold_lot_detail_is_soldout(monkeypatch):
    monkeypatch.setattr(web_app.inventory, "get", lambda lid: dict(SOLD_ROW))
    client = TestClient(web_app.app)
    resp = client.get("/listings/5003")
    assert resp.status_code == 200
    assert "schema.org/SoldOut" in resp.text
    assert "InStock" not in resp.text
    assert "SOLD OUT" in resp.text


def test_available_lot_detail_is_instock(monkeypatch):
    monkeypatch.setattr(web_app.inventory, "get", lambda lid: dict(AVAIL_ROW))
    client = TestClient(web_app.app)
    resp = client.get("/listings/334")
    assert resp.status_code == 200
    assert "schema.org/InStock" in resp.text
    assert "SoldOut" not in resp.text
