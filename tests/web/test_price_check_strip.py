"""HTTP tests for the "PRICE CHECK" strip on the public lot page.

DB-free, like test_sold_showcase: inventory + price_check.live_anchors are
monkeypatched, so these exercise the route wiring and the template. The rules:
verified numbers with outbound nofollow source links, never another seller's
photo, no strip on a sold or unpriced lot.
"""
import pytest
from fastapi.testclient import TestClient

from automation import inventory, price_check
from automation.web import auth as auth_svc
from automation.web import public_map, readcache
from automation.web.app import app

_REAL_LIVE_ANCHORS = price_check.live_anchors   # before any fixture patches it


@pytest.fixture(autouse=True)
def _no_auth(monkeypatch):
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    auth_svc.reset_caches()
    yield
    auth_svc.reset_caches()


def _live(**over):
    row = {
        "lot_id": "9006", "title": "Mauve Banquet Chairs", "subtitle": None,
        "status": "owned", "city": "Atlanta", "state": "GA", "zip_code": None,
        "chair_type": "Banquet Chairs", "quantity_original": 790,
        "quantity_remaining": 442, "price_per_chair": 25.0, "locations": None,
        "hero_image_url": "https://cdn.example.com/9006.jpg", "image_urls": [],
        "folder_name": None, "hero_image": None, "description": None,
        "dimensions": None, "facebook_url": None, "ebay_url": None,
    }
    row.update(over)
    return row


@pytest.fixture
def client(monkeypatch):
    readcache.invalidate_all()
    price_check.reset_cache()
    monkeypatch.setattr(inventory, "list_public", lambda: [_live()])
    monkeypatch.setattr(inventory, "list_sold_showcase", lambda *a, **k: [])
    monkeypatch.setattr(public_map, "nearby", lambda lot_id, **k: {"origin": None, "items": []})
    monkeypatch.setattr(price_check, "live_anchors", lambda: [])
    yield TestClient(app)
    price_check.reset_cache()


def _detail(client, monkeypatch, row):
    # get_public, not get: with a real DB + the slug column applied, the
    # route's get_public never falls back to get, and a patched get would
    # leak prod rows into these DB-free tests.
    monkeypatch.setattr(inventory, "get_public", lambda key: dict(row))
    return client.get(f"/listings/{row['lot_id']}").text


def test_strip_renders_the_verified_static_anchors(client, monkeypatch):
    html = _detail(client, monkeypatch, _live())
    assert "PRICE CHECK" in html
    assert "~$169" in html and "~$47" in html
    assert "todaysclassroom.com" in html and "webstaurantstore.com" in html
    assert 'rel="nofollow noopener"' in html
    assert "already in Atlanta, GA" in html
    # Our price row shows OUR number, not an anchor's.
    assert "$25" in html


def test_live_anchor_rows_replace_the_static_same_chair(client, monkeypatch):
    monkeypatch.setattr(price_check, "live_anchors", lambda: [{
        "title": "MTS Seating 594 Omega I Banquet Stacking Chair",
        "brand": "MTS Seating", "price_per_chair": 141.61,
        "listing_url": "https://www.example-dealer.com/mts-594/",
    }])
    html = _detail(client, monkeypatch, _live(title="MTS Omega Banquet Chairs"))
    assert "~$142" in html
    assert "example-dealer.com" in html


def test_anchor_is_text_plus_link_never_a_photo(client, monkeypatch):
    html = _detail(client, monkeypatch, _live())
    strip = html.split("PRICE CHECK")[1].split("</ul>")[0]
    assert "<img" not in strip


def test_sold_lot_has_no_price_check(client, monkeypatch):
    html = _detail(client, monkeypatch, _live(
        status="sold_out", quantity_remaining=0, lot_id="gone-3000"))
    assert "PRICE CHECK" not in html


def test_unpriced_lot_has_no_price_check(client, monkeypatch):
    html = _detail(client, monkeypatch, _live(price_per_chair=None))
    assert "PRICE CHECK" not in html


def test_lot_priced_above_every_anchor_has_no_price_check(client, monkeypatch):
    html = _detail(client, monkeypatch, _live(price_per_chair=450.0))
    assert "PRICE CHECK" not in html


def test_a_db_failure_still_renders_the_page_with_statics(client, monkeypatch):
    from automation import db

    def boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(price_check, "live_anchors", _REAL_LIVE_ANCHORS)
    monkeypatch.setattr(db, "fetch_one", boom)
    price_check.reset_cache()
    html = _detail(client, monkeypatch, _live())
    assert "PRICE CHECK" in html and "~$169" in html
