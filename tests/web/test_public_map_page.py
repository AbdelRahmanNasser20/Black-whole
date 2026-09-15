"""Public /map page + /map/api/points: ids the JS builds against, no auth, allow-list on the wire."""
import importlib
import pytest
from fastapi.testclient import TestClient

from automation.web import auth as auth_svc
from automation.web.app import app

app_mod = importlib.import_module("automation.web.app")
POINT = {"id": "gd-1-2#0", "kind": "lot", "lot_id": "gd-1-2", "title": "500 chairs", "bucket": "available",
         "quantity": 500, "unit": "CHAIR", "price_per_chair": 25.0, "city": "Boise", "state": "ID",
         "lat": 43.6, "lng": -116.2, "precision": "city", "hero": None, "url": "/listings/gd-1-2"}


@pytest.fixture(autouse=True)
def _no_auth(monkeypatch):
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    auth_svc.reset_caches()
    monkeypatch.setattr(app_mod.visits, "track", lambda *a, **k: None)
    yield
    auth_svc.reset_caches()


@pytest.fixture
def client(monkeypatch):
    seen = {}
    def fake_fetch(*, statuses=None, near=None, radius_mi=None):
        seen.update(statuses=statuses, near=near, radius_mi=radius_mi)
        return {"points": [POINT], "counts": {"available": 1, "incoming": 0, "sold": 0}, "near": None}
    monkeypatch.setattr(app_mod.public_map, "fetch_points", fake_fetch)
    c = TestClient(app)
    c.seen = seen
    return c


def test_map_page_shell(client):
    html = client.get("/map").text
    assert "<title>" in html and "Where the chairs are" in html
    for el_id in ("map-near", "map-status", "map-radius", "map-count", "site-map", "map-list"):
        assert f'id="{el_id}"' in html, el_id
    assert "/static/site/map.js" in html and "/static/site/map.css" in html
    assert 'href="/map"' in html          # nav link from _public_base
    assert "storage_note" not in html


def test_points_default_statuses_and_params(client):
    r = client.get("/map/api/points")
    assert r.status_code == 200
    assert client.seen["statuses"] == {"available", "incoming"}
    body = r.json()
    assert body["points"][0]["lot_id"] == "gd-1-2" and "counts" in body
    client.get("/map/api/points?status=available,sold&near=Boise%2C%20ID&radius=200")
    assert client.seen == {"statuses": {"available", "sold"}, "near": "Boise, ID", "radius_mi": 200.0}


def test_points_rejects_unknown_bucket(client):
    assert client.get("/map/api/points?status=secret").status_code == 400


def test_points_is_public_even_with_auth_on(monkeypatch, client):
    monkeypatch.setenv("ADMIN_PASSWORD", "x"); monkeypatch.setenv("SESSION_SECRET", "y")
    auth_svc.reset_caches()
    assert client.get("/map/api/points").status_code == 200
    assert client.get("/map").status_code == 200


def test_sitemap_lists_map(monkeypatch):
    monkeypatch.setattr(app_mod.inventory, "list_public", lambda: [])
    monkeypatch.setattr(app_mod.inventory, "list_sold_showcase", lambda: [])
    assert "/map</loc>" in TestClient(app).get("/sitemap.xml").text


def test_points_failure_does_not_leak_the_exception(monkeypatch, client, caplog):
    """A DB error repr carries the DSN and local paths. /map/api/points is public
    and unauthenticated, so the detail is logged, never returned."""
    def boom(**kw):
        raise RuntimeError("secret dsn postgres://x")
    monkeypatch.setattr(app_mod.public_map, "fetch_points", boom)
    r = client.get("/map/api/points")
    assert r.status_code == 503
    assert "postgres://" not in r.text and "secret" not in r.text
    assert "postgres://x" in caplog.text  # operator still sees it in the server log


def test_home_has_map_band(monkeypatch):
    monkeypatch.setattr(app_mod, "_landing_data", lambda: {"counts": {"lots": 1, "chairs": 500, "cities": 1, "moved": 0}, "featured": []})
    html = TestClient(app).get("/").text
    assert 'id="home-map"' in html and 'data-points-url="/map/api/points"' in html
    assert 'action="/map"' in html and 'name="near"' in html
    assert 'id="home-map-sold"' in html
    assert "/static/site/map.js" in html


def test_listing_detail_shows_nearby(monkeypatch):
    row = {"lot_id": "gd-1-2", "title": "500 chairs", "status": "owned", "quantity_remaining": 500,
           "city": "Boise", "state": "ID", "hero_image_url": None, "image_urls": [], "locations": None,
           "price_per_chair": 25, "storage_note": "gate 4321"}
    monkeypatch.setattr(app_mod.inventory, "get", lambda lot_id: dict(row))
    monkeypatch.setattr(app_mod.public_map, "nearby", lambda lot_id, **k: {
        "origin": {"lat": 43.6, "lng": -116.2, "precision": "city"},
        "items": [{**POINT, "lot_id": "gd-3-4", "url": "/listings/gd-3-4", "title": "200 chairs", "distance_mi": 42.0}]})
    html = TestClient(app).get("/listings/gd-1-2").text
    assert 'id="lot-map"' in html and 'data-focus-lat="43.6"' in html
    assert "1 other lot within 200 mi" in html and 'href="/listings/gd-3-4"' in html
    assert "gate 4321" not in html and "storage_note" not in html
