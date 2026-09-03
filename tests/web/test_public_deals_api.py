"""DB-free tests for the public /deals surface: routes are open without a
session, private fields never appear, excluded lots 404 for the public but
render (with photos) for the operator."""
import importlib

import pytest
from fastapi.testclient import TestClient

from automation.web import auth as auth_svc
from automation.web import public_deals as pd

# `automation.web.__init__` re-exports the FastAPI instance as `app`, shadowing
# the submodule on attribute access — go through importlib for the module.
app_mod = importlib.import_module("automation.web.app")
app = app_mod.app

PAGE = {"rows": [{"asset_id": 305, "account_id": 10340, "auction_id": 1, "title": "Lot of (30) Laptops",
                  "canonical_category": "computers_electronics", "city": "Houston", "state": "TX",
                  "bid_count": 0, "current_bid": 300.0, "end_utc": None, "outcome_complete": False,
                  "quantity": 30, "unit_bid": 10.0, "unit_landed": 11.25, "landed_cost": 337.5,
                  "govdeals_url": "https://www.govdeals.com/en/asset/305/10340",
                  "viewer_url": "/deals/305/10340/1"}],
        "total": 1, "page": 1, "per_page": 25, "pages": 1}
LOT = {"asset_id": 305, "account_id": 10340, "auction_id": 1, "title": "Lot of (30) Laptops",
       "canonical_category": "computers_electronics", "city": "Houston", "state": "TX",
       "bid_count": 0, "current_bid": 300.0, "currency_code": "USD", "end_utc": None,
       "outcome": None, "final_bid": None, "images_archived": True,
       "archived_hero_url": "https://cdn.example.com/hero.jpg", "hero_image_url": None,
       "gallery_urls": ["https://cdn.example.com/1.jpg"], "description": "d",
       "native_category_id": "29", "native_category_name": "Computers", "seller": "City",
       "opening_bid": 10.0, "has_reserve": False, "first_seen_at": None, "final_bid_count": None}


@pytest.fixture(autouse=True)
def _auth_on(monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "hunter2-but-much-longer")
    monkeypatch.setenv("SESSION_SECRET", "unit-test-secret")
    auth_svc.reset_caches()
    yield
    auth_svc.reset_caches()


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(pd, "fetch_page", lambda **kw: dict(PAGE, echo=kw))
    monkeypatch.setattr(pd, "fetch_pins", lambda **kw: {"points": [], "capped": False})
    monkeypatch.setattr(pd, "fetch_facets", lambda: {"categories": [], "states": [], "stats": {"tracked": 210932}, "cached_at": 0})
    monkeypatch.setattr(pd, "is_operator_lot", lambda a, b, c: False)
    monkeypatch.setattr(app_mod.db, "fetch_one", lambda sql, params=(): dict(LOT))
    monkeypatch.setattr("deals.tracking_store.history", lambda a, b: [])
    return TestClient(app, base_url="https://testserver")


def test_public_endpoints_need_no_session(client):
    assert client.get("/deals/api/lots?page=2&per_page=50&sort=bid").status_code == 200
    assert client.get("/deals/api/pins").status_code == 200
    assert client.get("/deals/api/facets").json()["stats"]["tracked"] == 210932
    assert client.get("/deals").status_code == 200
    assert client.get("/api/deals").status_code == 401  # admin feed still gated


def test_lots_passes_paging_and_rejects_bad_status(client):
    body = client.get("/deals/api/lots?page=2&per_page=50&sort=bid&dir=asc").json()
    assert body["echo"]["page"] == 2 and body["echo"]["per_page"] == 50 and body["echo"]["sort"] == "bid"
    assert body["rows"][0]["unit_bid"] == 10.0
    assert client.get("/deals/api/lots?status=bogus").status_code == 400


def test_viewer_hides_photos_for_public_and_shows_them_for_operator(client):
    html = client.get("/deals/305/10340/1").text
    assert "cdn.example.com/hero.jpg" not in html and "deal_card.js" not in html
    client.cookies.set(auth_svc.SESSION_COOKIE, auth_svc.issue_session_token())
    html = client.get("/deals/305/10340/1").text
    assert "cdn.example.com/hero.jpg" in html


def test_viewer_404s_excluded_lot_for_public_only(client, monkeypatch):
    monkeypatch.setattr(app_mod.db, "fetch_one",
                        lambda sql, params=(): dict(LOT, canonical_category="seating_furniture"))
    assert client.get("/deals/305/10340/1").status_code == 404
    client.cookies.set(auth_svc.SESSION_COOKIE, auth_svc.issue_session_token())
    assert client.get("/deals/305/10340/1").status_code == 200


def test_robots_disallows_deals(client):
    assert "Disallow: /deals" in client.get("/robots.txt").text
