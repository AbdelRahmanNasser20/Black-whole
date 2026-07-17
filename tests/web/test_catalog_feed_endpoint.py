"""HTTP tests for GET /catalog/facebook.csv (BLACKWHOLE-7).

DB-free: `inventory.list_catalog_feed` is monkeypatched to return fixture rows,
so these exercise the route/serialization/content-type without a database.
Also asserts the feed stays public even when admin auth is enabled (it must,
so Facebook's scheduled crawler can fetch it with no login).
"""
import csv
import io

import pytest
from fastapi.testclient import TestClient

from automation import inventory
from automation.web import app as app_mod
from automation.web import auth as auth_svc
from automation.web.app import app


def _lot(**over):
    row = {
        "lot_id": "snap_06_asu_event_phoenix",
        "title": "ASU Event Chairs (Phoenix, AZ)",
        "description": "Bulk used banquet chairs.",
        "price_per_chair": 28.0,
        "quantity_remaining": 790,
        "status": "listed",
        "city": "Phoenix",
        "state": "AZ",
        "hero_image_url": "https://cdn.example.com/hero.jpg",
    }
    row.update(over)
    return row


@pytest.fixture(autouse=True)
def _clean_auth(monkeypatch):
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("SITE_BASE_URL", raising=False)
    auth_svc.reset_caches()
    yield
    auth_svc.reset_caches()


def _client():
    return TestClient(app, base_url="https://testserver")


def test_feed_returns_csv_rows(monkeypatch):
    monkeypatch.setattr(inventory, "list_catalog_feed", lambda: [_lot()])
    r = _client().get("/catalog/facebook.csv")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    parsed = list(csv.DictReader(io.StringIO(r.text)))
    assert len(parsed) == 1
    assert parsed[0]["id"] == "snap_06_asu_event_phoenix"
    assert parsed[0]["price"] == "28.00 USD"
    assert parsed[0]["link"].endswith("/listings/snap_06_asu_event_phoenix")
    assert parsed[0]["brand"] == "BLACKWHOLE Liquidation"


def test_feed_header_present_when_empty(monkeypatch):
    monkeypatch.setattr(inventory, "list_catalog_feed", lambda: [])
    r = _client().get("/catalog/facebook.csv")
    assert r.status_code == 200
    assert r.text.splitlines()[0] == (
        "id,title,description,availability,condition,price,link,image_link,brand"
    )


def test_feed_drops_incomplete_rows(monkeypatch):
    rows = [_lot(lot_id="ok"), _lot(lot_id="noimg", hero_image_url=None)]
    monkeypatch.setattr(inventory, "list_catalog_feed", lambda: rows)
    r = _client().get("/catalog/facebook.csv")
    ids = [row["id"] for row in csv.DictReader(io.StringIO(r.text))]
    assert ids == ["ok"]


def test_feed_honors_site_base_url_env(monkeypatch):
    monkeypatch.setenv("SITE_BASE_URL", "https://staging.example.com")
    monkeypatch.setattr(inventory, "list_catalog_feed", lambda: [_lot(lot_id="x")])
    r = _client().get("/catalog/facebook.csv")
    row = next(csv.DictReader(io.StringIO(r.text)))
    assert row["link"] == "https://staging.example.com/listings/x"


def test_feed_public_even_with_admin_auth_enabled(monkeypatch):
    # FB's crawler has no cookie — the feed must not sit behind the auth gate.
    monkeypatch.setenv("ADMIN_PASSWORD", "a-very-long-admin-password")
    monkeypatch.setenv("SESSION_SECRET", "unit-test-secret")
    auth_svc.reset_caches()
    monkeypatch.setattr(inventory, "list_catalog_feed", lambda: [_lot()])
    r = _client().get("/catalog/facebook.csv")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
