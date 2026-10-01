"""Locations tab (13): /api/locations lists held lots with their PRIVATE storage note, gate codes masked unless
?raw=1; PUT records a note. The pane ships only a skeleton — no note renders server-side."""
import re

import pytest
from fastapi.testclient import TestClient

from automation import storage_locations as sl
from automation.web import auth as auth_svc
from automation.web import readcache
from automation.web.app import app

NOTE = "Public Storage — 10 Main St, Stanton, CA. Unit E033. Gate code 4821#. Key with Mo"
ROW = {"lot_id": "folder:Cypress_242", "title": "Crimson Chairs", "city": "Stanton", "state": "CA",
       "zip_code": "90680", "quantity_remaining": 242, "status": "owned", "fake_sold_out": False,
       "storage_note": NOTE, "updated_at": None}


@pytest.fixture(autouse=True)
def _no_auth(monkeypatch):
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    auth_svc.reset_caches()
    readcache.invalidate_all()
    yield
    auth_svc.reset_caches()
    readcache.invalidate_all()


@pytest.fixture
def fake_db(monkeypatch):
    store = {ROW["lot_id"]: dict(ROW), "gd-1-2": dict(ROW, lot_id="gd-1-2", storage_note=None)}
    calls = []

    def fetch_all(sql, params=()):
        calls.append((sql, params))
        rows = [r for r in store.values() if r["status"] in params[0]]
        if "trim(storage_note)" in sql:
            rows = [r for r in rows if not (r["storage_note"] or "").strip()]
        return [dict(r) for r in rows]

    def fetch_one(sql, params=()):
        r = store.get(params[0])
        return dict(r) if r else None

    def execute(sql, params=()):
        note, lot = params
        if lot not in store:
            return 0
        store[lot]["storage_note"] = note
        return 1

    monkeypatch.setattr(sl.db, "fetch_all", fetch_all)
    monkeypatch.setattr(sl.db, "fetch_one", fetch_one)
    monkeypatch.setattr(sl.db, "execute", execute)
    return store


def test_mask_hides_codes_keeps_address():
    m = sl.mask_codes(NOTE)
    assert "4821" not in m
    assert "10 Main St, Stanton, CA" in m and "Unit E033" in m and "Key with Mo" in m
    assert sl.mask_codes(None) == ""


def test_list_masks_codes_and_flags_missing(fake_db):
    items = TestClient(app).get("/api/locations").json()["items"]
    by_id = {r["lot_id"]: r for r in items}
    assert "4821" not in by_id[ROW["lot_id"]]["storage_note"]
    assert by_id[ROW["lot_id"]]["recorded"] is True
    assert by_id["gd-1-2"]["recorded"] is False and by_id["gd-1-2"]["storage_note"] == ""


def test_missing_filter(fake_db):
    items = TestClient(app).get("/api/locations?missing=1").json()["items"]
    assert [r["lot_id"] for r in items] == ["gd-1-2"]


def test_get_raw_returns_codes_only_on_request(fake_db):
    c = TestClient(app)
    assert "4821" not in c.get(f"/api/locations/{ROW['lot_id']}").json()["storage_note"]
    assert "4821" in c.get(f"/api/locations/{ROW['lot_id']}?raw=1").json()["storage_note"]
    assert c.get("/api/locations/nope").status_code == 404


def test_put_records_note_and_validates(fake_db):
    c = TestClient(app)
    r = c.put("/api/locations/gd-1-2", json={"storage_note": "  Warehouse — 1 Dock Rd, Las Vegas, NV  "})
    assert r.status_code == 200 and r.json()["recorded"] is True
    assert fake_db["gd-1-2"]["storage_note"] == "Warehouse — 1 Dock Rd, Las Vegas, NV"
    assert c.put("/api/locations/gd-1-2", json={"storage_note": 5}).status_code == 400
    assert c.put("/api/locations/nope", json={"storage_note": "x"}).status_code == 404
    c.put("/api/locations/gd-1-2", json={"storage_note": "  "})
    assert fake_db["gd-1-2"]["storage_note"] is None


def test_pane_ships_skeleton_and_no_private_data():
    html = TestClient(app).get("/admin").text
    m = re.search(r'<section class="panel" data-pane="locations"[^>]*>(.*?)</section>', html, re.S)
    assert m, "locations pane missing"
    pane = m.group(1)
    assert 'id="loc-list"' in pane and 'data-state="loading"' in pane
    assert "/static/admin/locations.css" in pane
    assert "storage_note" not in pane
    assert 'data-tab="locations"' in html


def test_only_this_tab_reads_the_note():
    # the Inventory tab still never touches it; the Locations tab is the one web surface
    assert "storage_note" not in open("automation/web/static/admin/inventory.js").read().replace(
        "The private storage note", "")
    assert TestClient(app).get("/static/admin/locations.js").status_code == 200
