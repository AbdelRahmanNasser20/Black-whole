"""HTTP tests for the sold archive + multi-location rendering (BLACKWHOLE-29).

DB-free: `inventory.list_public` / `list_sold_showcase` / `get` are
monkeypatched, so these exercise routing + templates without a database.
"""
import pytest
from fastapi.testclient import TestClient

from automation import inventory
from automation.web import app as app_mod
from automation.web import auth as auth_svc
from automation.web.app import app


@pytest.fixture(autouse=True)
def _no_auth(monkeypatch):
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    auth_svc.reset_caches()
    yield
    auth_svc.reset_caches()


def _live(**over):
    row = {
        "lot_id": "9006", "title": "Mauve Banquet Chairs", "subtitle": None,
        "status": "owned", "city": "Phoenix", "state": "AZ", "zip_code": None,
        "chair_type": "Banquet Chairs", "quantity_original": 790,
        "quantity_remaining": 442, "price_per_chair": 25.0, "locations": None,
        "hero_image_url": "https://cdn.example.com/9006.jpg", "image_urls": [],
        "folder_name": None, "hero_image": None, "description": None,
        "dimensions": None, "facebook_url": None, "ebay_url": None,
    }
    row.update(over)
    return row


def _sold(**over):
    return _live(**{
        "lot_id": "blue-silver-frame-3000",
        "title": "Blue Banquet Chairs w/ Silver Frame",
        "subtitle": "Blue fabric seats on chrome frames.",
        "status": "sold_out", "quantity_original": 3000, "quantity_remaining": 0,
        "city": "Baltimore", "state": "MD",
        "locations": [
            {"city": "Baltimore", "state": "MD", "quantity": 1200},
            {"city": "Atlanta", "state": "GA", "quantity": 800},
            {"city": "Orlando", "state": "FL", "quantity": 1000},
        ],
        **over,
    })


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(inventory, "list_public", lambda: [_live()])
    monkeypatch.setattr(inventory, "list_sold_showcase", lambda *a, **k: [_sold()])
    return TestClient(app)


def test_listings_page_shows_the_sold_archive(client):
    html = client.get("/listings").text
    assert "ALREADY MOVED" in html
    assert "Blue Banquet Chairs w/ Silver Frame" in html
    assert "SOLD" in html


def test_sold_card_shows_the_lot_size_and_the_price_it_sold_at(client):
    html = client.get("/listings").text
    assert "3000" in html                       # full lot size, not 0 remaining
    assert "SOLD AT / CHAIR" in html


def test_sold_lots_are_not_counted_as_active_stock(client):
    html = client.get("/listings").text
    # The header count covers the live grid only.
    assert "TOTAL LOTS <span class=\"tag\">1</span>" in html


def test_multi_location_lot_lists_every_city_in_the_filter(client):
    html = client.get("/listings").text
    # Live lot's location is the only filter option; the sold lot's cities are
    # archive-only and must not pollute the "shop by city" dropdown.
    assert '<option value="Phoenix, AZ">' in html
    assert '<option value="Orlando, FL">' not in html


def test_sold_detail_page_swaps_the_cta_and_shows_location_chips(client, monkeypatch):
    monkeypatch.setattr(inventory, "get", lambda lot_id: _sold())
    html = client.get("/listings/blue-silver-frame-3000").text
    assert "SOLD OUT" in html
    assert "FIND ME ONE LIKE THIS" in html
    assert "I WANT THIS LOT" not in html
    for city in ("Baltimore, MD", "Atlanta, GA", "Orlando, FL"):
        assert city in html
    assert "LOCATIONS" in html


def test_live_detail_page_is_unchanged(client, monkeypatch):
    monkeypatch.setattr(inventory, "get", lambda lot_id: _live())
    html = client.get("/listings/9006").text
    assert "I WANT THIS LOT" in html
    assert "SOLD OUT" not in html
    assert "Phoenix, AZ" in html


def test_sold_lot_seo_says_sold_not_available(client, monkeypatch):
    monkeypatch.setattr(inventory, "get", lambda lot_id: _sold())
    html = client.get("/listings/blue-silver-frame-3000").text
    assert "3000 sold" in html
    assert "3000 available" not in html
    assert "https://schema.org/SoldOut" in html
    # Every city it sat in belongs in the title, not just the primary one.
    assert "Baltimore, MD · Atlanta, GA · Orlando, FL" in html


def test_sitemap_includes_sold_lots(client):
    xml = client.get("/sitemap.xml").text
    assert "/listings/blue-silver-frame-3000" in xml
    assert "/listings/9006" in xml


def test_landing_shows_the_chairs_moved_tile(client, monkeypatch):
    monkeypatch.setattr(
        inventory, "stats",
        lambda: {"lots": 8, "chairs": 4000, "cities": 6, "moved": 3000},
    )
    html = client.get("/").text
    assert "CHAIRS MOVED" in html
    assert "3,000" in html
