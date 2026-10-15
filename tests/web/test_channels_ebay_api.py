"""GET /api/channels/ebay — the read-only eBay section of the Channels tab. eBay and the
DB are both mocked (ebay_ads.snapshot + inventory.list_all); nothing hits the network.

Properties defended:
  1. Behind the /api/ auth wall.
  2. NOT ELIGIBLE comes through with eBay's reason, and each listing is joined to its lot
     by the item id in inventory.ebay_url.
  3. The handler is a plain `def` (threadpool), memoised by readcache, and only
     `refresh=1` forces a fresh eBay read.
  4. The pane ships the eBay section (badge, reason, table) and channels.js reads it.
"""
from __future__ import annotations

import importlib
import inspect
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from automation import ebay_ads, inventory
from automation.web import auth as auth_svc
from automation.web import readcache
from automation.web.app import app

app_mod = importlib.import_module("automation.web.app")

SNAP = {
    "state": "not_eligible",
    "reason": "To gain access to Promoted Listings, you must be in good standing with recent sales activity.",
    "cap": 15.0,
    "campaign": {"id": "164302777016", "name": "Campaign", "status": "SYSTEM_PAUSED",
                 "model": "COST_PER_SALE", "rate": 9.0},
    "campaigns": [],
    "rows": [
        {"id": "336648966527", "title": "Banquet Chairs Boise", "price": "25.0", "qty": "100",
         "now": None, "suggested": 14.0, "want": 14.0},
        {"id": "336534200205", "title": "Burgandy chairs", "price": "20.0", "qty": "399",
         "now": None, "suggested": 16.0, "want": 15.0},
    ],
    "fetched_at": 1760000000.0,
}


@pytest.fixture(autouse=True)
def _setup(monkeypatch):
    for var in ("ADMIN_PASSWORD", "SESSION_SECRET", "TOTP_SECRET"):
        monkeypatch.delenv(var, raising=False)
    auth_svc.reset_caches()
    readcache.invalidate_all()
    calls = []

    def fake_snapshot(force=False, ttl=None):
        calls.append(force)
        return SNAP

    monkeypatch.setattr(ebay_ads, "snapshot", fake_snapshot)
    monkeypatch.setattr(inventory, "list_all", lambda status=None: [
        {"lot_id": "gd-123", "title": "Boise lot", "status": "listed",
         "ebay_url": "https://www.ebay.com/itm/336648966527"},
        {"lot_id": "gd-999", "title": "no ebay", "status": "listed", "ebay_url": None},
    ])
    yield calls
    auth_svc.reset_caches()
    readcache.invalidate_all()


def test_requires_auth(monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "hunter2-but-much-longer")
    monkeypatch.setenv("SESSION_SECRET", "unit-test-secret")
    auth_svc.reset_caches()
    r = TestClient(app, base_url="https://testserver").get("/api/channels/ebay")
    assert r.status_code == 401


def test_not_eligible_with_reason_and_lot_join(_setup):
    r = TestClient(app).get("/api/channels/ebay")
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["state"] == "not_eligible"
    assert "recent sales activity" in b["reason"]
    assert b["campaign"]["status"] == "SYSTEM_PAUSED"
    rows = {x["item_id"]: x for x in b["listings"]}
    assert rows["336648966527"]["lot"] == {"lot_id": "gd-123", "title": "Boise lot", "status": "listed"}
    assert rows["336648966527"]["suggested"] == 14.0 and rows["336648966527"]["current"] is None
    assert rows["336534200205"]["lot"] is None and rows["336534200205"]["want"] == 15.0


def test_memoised_and_refresh_forces(_setup):
    c = TestClient(app)
    c.get("/api/channels/ebay")
    c.get("/api/channels/ebay")
    assert _setup == [False]
    c.get("/api/channels/ebay?refresh=1")
    assert _setup == [False, True]


def test_handler_is_sync_def():
    assert not inspect.iscoroutinefunction(inspect.unwrap(app_mod.channels_ebay))


def test_pane_ships_ebay_section_and_js_reads_it():
    html = TestClient(app).get("/admin").text
    m = re.search(r'<section class="panel" data-pane="channels".*?</section>', html, flags=re.S)
    assert m
    pane = m.group(0)
    for el in ('id="ch-ebay-badge"', 'id="ch-ebay-reason"', 'id="ch-ebay"', 'id="ch-ebay-refresh"'):
        assert el in pane
    js = Path("automation/web/static/admin/channels.js").read_text()
    assert "/api/channels/ebay" in js and "NOT ELIGIBLE" in js
    assert "method: 'POST'" not in js.split("// ───────── eBay Promoted Listings")[1].split("// ───────── load")[0]
