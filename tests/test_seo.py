"""SEO surface (BLACKWHOLE-11): robots.txt, sitemap.xml, per-page meta, JSON-LD.

DB-free: inventory functions are monkeypatched. TestClient is used without a
context manager on purpose so the favorites scheduler startup hook never runs.
"""

import json
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

import importlib

# `automation.web.__init__` re-exports the FastAPI instance as `app`,
# shadowing the app *module* — import the module explicitly.
web_app = importlib.import_module("automation.web.app")


ROW = {
    "lot_id": "10340",
    "title": "Burgundy Banquet Chairs",
    "description": "Stackable padded banquet chairs from a conference center.",
    "city": "Athens",
    "state": "GA",
    "zip_code": "30601",
    "chair_type": "banquet",
    "quantity_remaining": 100,
    "quantity_original": 120,
    "price_per_chair": 12.0,
    "status": "listed",
    "hero_image_url": "https://cdn.example.com/10340.jpg",
    "image_urls": ["https://cdn.example.com/10340/01.jpg"],
    "folder_name": None,
    "hero_image": None,
    "subtitle": None,
    "dimensions": None,
    "facebook_url": None,
    "ebay_url": None,
    "updated_at": datetime(2026, 7, 1, tzinfo=timezone.utc),
}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(web_app.inventory, "list_public", lambda **kw: [dict(ROW)])
    monkeypatch.setattr(web_app.inventory, "get", lambda lot_id: dict(ROW) if lot_id == "10340" else None)
    monkeypatch.setattr(web_app.inventory, "stats", lambda: {"lots": 1, "chairs": 100, "cities": 1})
    return TestClient(web_app.app)


def test_robots_txt(client):
    r = client.get("/robots.txt")
    assert r.status_code == 200
    assert "Disallow: /admin" in r.text
    assert "Disallow: /api/" in r.text
    assert "Sitemap: https://black-whole.com/sitemap.xml" in r.text


def test_sitemap_lists_static_pages_and_live_lots(client):
    r = client.get("/sitemap.xml")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/xml")
    for loc in ("https://black-whole.com/",
                "https://black-whole.com/listings",
                "https://black-whole.com/sell",
                "https://black-whole.com/listings/10340"):
        assert f"<loc>{loc}</loc>" in r.text
    assert "<lastmod>2026-07-01</lastmod>" in r.text


def test_detail_page_meta_and_jsonld(client):
    r = client.get("/listings/10340")
    assert r.status_code == 200
    html = r.text
    assert "<title>100× Burgundy Banquet Chairs — Athens, GA | Black Whole Liquidation</title>" in html
    assert 'rel="canonical" href="https://black-whole.com/listings/10340"' in html
    assert 'property="og:image" content="https://cdn.example.com/10340.jpg"' in html

    start = html.index('"@type": "Product"')
    block_start = html.rindex('<script type="application/ld+json">', 0, start) + len('<script type="application/ld+json">')
    block_end = html.index("</script>", block_start)
    product = json.loads(html[block_start:block_end].replace("<\\/", "</"))
    assert product["sku"] == "10340"
    offer = product["offers"]
    assert offer["price"] == "12.00"
    assert offer["availability"] == "https://schema.org/InStock"
    addr = offer["availableAtOrFrom"]["address"]
    assert addr["addressLocality"] == "Athens"
    assert addr["addressRegion"] == "GA"
    assert addr["postalCode"] == "30601"


def test_detail_page_degrades_without_optional_fields(client, monkeypatch):
    bare = dict(ROW, city=None, state=None, zip_code=None,
                price_per_chair=None, quantity_remaining=0,
                hero_image_url=None, image_urls=None, description=None)
    monkeypatch.setattr(web_app.inventory, "get", lambda lot_id: bare)
    r = client.get("/listings/10340")
    assert r.status_code == 200
    html = r.text
    assert "None" not in html.split("<title>")[1].split("</title>")[0]
    start = html.index('"@type": "Product"')
    block_start = html.rindex('<script type="application/ld+json">', 0, start) + len('<script type="application/ld+json">')
    block_end = html.index("</script>", block_start)
    product = json.loads(html[block_start:block_end].replace("<\\/", "</"))
    offer = product["offers"]
    assert offer["availability"] == "https://schema.org/SoldOut"
    assert "price" not in offer
    assert "availableAtOrFrom" not in offer
    assert "image" not in product


def test_listings_page_meta(client):
    r = client.get("/listings")
    assert r.status_code == 200
    assert "pickup in Athens" in r.text
    assert 'rel="canonical" href="https://black-whole.com/listings"' in r.text


def test_google_verification_meta_absent_when_unset(client):
    r = client.get("/")
    assert "google-site-verification" not in r.text
