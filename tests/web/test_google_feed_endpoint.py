"""HTTP tests for GET /catalog/google.csv (multichannel Phase 2).

DB-free: `inventory.list_catalog_feed` is monkeypatched. Mirrors
tests/web/test_catalog_feed_endpoint.py — the feed must stay public so
Merchant Center's scheduled fetch works with no login.
"""
import csv
import io

import pytest
from fastapi.testclient import TestClient

from automation import google_feed, inventory
from automation.web import auth as auth_svc
from automation.web.app import app

R2 = "https://pub-4ac6bae8ec024e3aaccf3317c8873840.r2.dev"


def _lot(**over):
    row = {
        "lot_id": "31225-atl",
        "title": "Brown Convention Chairs — Atlanta, GA",
        "description": "Bulk used banquet chairs.",
        "price_per_chair": 25.0,
        "quantity_remaining": 1200,
        "status": "won_pickup",
        "city": "Atlanta",
        "state": "GA",
        "hero_image_url": f"{R2}/31225-atl/hero.jpg",
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
    r = _client().get("/catalog/google.csv")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert r.headers["cache-control"] == "public, max-age=900"
    rows = list(csv.DictReader(io.StringIO(r.text)))
    assert len(rows) == 1
    assert rows[0]["id"] == "31225-atl"
    assert rows[0]["availability"] == "in_stock"
    assert rows[0]["link"].endswith(f"/listings/31225-atl?{google_feed.UTM_QUERY}")


def test_feed_drops_ineligible_rows(monkeypatch):
    monkeypatch.setattr(inventory, "list_catalog_feed", lambda: [_lot(), _lot(lot_id="x", price_per_chair=None)])
    r = _client().get("/catalog/google.csv")
    assert [row["id"] for row in csv.DictReader(io.StringIO(r.text))] == ["31225-atl"]


def test_feed_is_public_with_admin_auth_on(monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "secret")
    auth_svc.reset_caches()
    monkeypatch.setattr(inventory, "list_catalog_feed", lambda: [_lot()])
    r = _client().get("/catalog/google.csv")
    assert r.status_code == 200


def test_feed_uses_site_base_url(monkeypatch):
    monkeypatch.setenv("SITE_BASE_URL", "https://staging.example.com")
    monkeypatch.setattr(inventory, "list_catalog_feed", lambda: [_lot()])
    r = _client().get("/catalog/google.csv")
    assert "https://staging.example.com/listings/31225-atl?" in r.text
