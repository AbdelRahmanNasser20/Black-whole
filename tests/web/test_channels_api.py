"""Offline tests for /api/channels* (Phase 1.5). Same shape as
``tests/web/test_deposits_admin.py``: a ``TestClient`` built WITHOUT a ``with``
block so the lifespan (and the sync loop) never runs, and every DB call site
monkeypatched — nothing here opens a connection.

Properties defended:
  1. Every route is behind the /api/ auth wall.
  2. The switches PATCH only reaches `channel*` / `browser_channel*` keys —
     the deposit rule cannot be edited through the Channels tab by accident.
  3. Approving a queued FB Marketplace post on a DISABLED channel is a 409, so
     the approval queue can never be a back door past the switch.
  4. The sync loop is registered like the other pollers and can be turned off
     with CHANNEL_SYNC_SEC=0.
"""
from __future__ import annotations

import asyncio
import importlib

import pytest
from fastapi.testclient import TestClient

from automation import site_settings
from automation.channels import store, sync
from automation.web import auth as auth_svc
from automation.web import readcache
from automation.web.app import app

app_mod = importlib.import_module("automation.web.app")

PASSWORD = "hunter2-but-much-longer"
_AUTH_ENV = ("ADMIN_PASSWORD", "SESSION_SECRET", "TOTP_SECRET")


@pytest.fixture(autouse=True)
def _no_auth(monkeypatch):
    for var in _AUTH_ENV:
        monkeypatch.delenv(var, raising=False)
    auth_svc.reset_caches()
    readcache.invalidate_all()
    yield
    auth_svc.reset_caches()
    readcache.invalidate_all()


def _enable_auth(monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", PASSWORD)
    monkeypatch.setenv("SESSION_SECRET", "unit-test-secret")
    auth_svc.reset_caches()


def _client():
    return TestClient(app, base_url="https://testserver")


def _login(client):
    r = client.post("/api/auth/login", json={"password": PASSWORD})
    assert r.status_code == 200, r.text
    return client


QUEUED_ROW = {"id": 7, "lot_id": "31225", "channel": "fb_marketplace", "state": "pending_approval",
              "title": "Brown chairs", "status": "owned", "quantity_remaining": 1200}


@pytest.fixture
def world(monkeypatch):
    """In-memory store + settings. `calls` records what the routes asked for."""
    calls = {"approve": [], "reject": [], "sync": [], "set_many": []}
    settings = {**site_settings.defaults()}

    monkeypatch.setattr(store, "matrix", lambda: [{"lot_id": "31225", "title": "Brown chairs", "status": "owned",
                                                    "qty": 1200, "channels": {"site": {"state": "live", "url": "u"}}}])
    monkeypatch.setattr(store, "queue", lambda: [QUEUED_ROW])
    monkeypatch.setattr(store, "get_by_id", lambda i: QUEUED_ROW if i == 7 else None)

    def fake_approve(i):
        calls["approve"].append(i)
        return {**QUEUED_ROW, "state": "queued"} if i == 7 else None

    def fake_reject(i):
        calls["reject"].append(i)
        return {**QUEUED_ROW, "state": "off"} if i == 7 else None

    monkeypatch.setattr(store, "approve", fake_approve)
    monkeypatch.setattr(store, "reject", fake_reject)
    monkeypatch.setattr(site_settings, "get_all", lambda: dict(settings))

    def fake_set_many(values):
        calls["set_many"].append(dict(values))
        for k, v in values.items():
            if k not in site_settings.SPEC:
                raise ValueError(f"unknown setting: {k}")
            settings[k] = site_settings._coerce(k, v)
        return dict(settings)

    monkeypatch.setattr(site_settings, "set_many", fake_set_many)

    def fake_run_once(**kw):
        calls["sync"].append(kw)
        return {"planned": 2, "applied": 1, "errors": 0, "skipped": {"disabled": 1}}

    monkeypatch.setattr(sync, "run_once", fake_run_once)
    return {"calls": calls, "settings": settings}


# ─────────────────────────────── auth gate ───────────────────────────────

def test_channel_routes_require_auth(monkeypatch, world):
    _enable_auth(monkeypatch)
    client = _client()
    for method, path in (
        ("get", "/api/channels"),
        ("patch", "/api/channels/switches"),
        ("post", "/api/channels/queue/7/approve"),
        ("post", "/api/channels/queue/7/reject"),
        ("post", "/api/channels/sync"),
    ):
        r = getattr(client, method)(path, **({"json": {}} if method == "patch" else {}))
        assert r.status_code == 401, f"{method} {path} -> {r.status_code}"
    assert world["calls"] == {"approve": [], "reject": [], "sync": [], "set_many": []}


def test_channels_readable_once_logged_in(monkeypatch, world):
    _enable_auth(monkeypatch)
    r = _login(_client()).get("/api/channels")
    assert r.status_code == 200


# ─────────────────────────────── GET ───────────────────────────────

def test_get_shape(world):
    body = _client().get("/api/channels").json()
    assert set(body) == {"switches", "matrix", "queue", "channels"}
    assert body["switches"]["channel_fb_marketplace_enabled"] == 0
    assert body["switches"]["channels_master_enabled"] == 1
    assert "deposit_pct" not in body["switches"], "only channel keys ride along"
    assert body["matrix"][0]["channels"]["site"]["state"] == "live"
    assert body["queue"][0]["id"] == 7
    assert body["channels"]["approval"] == ["fb_marketplace"]
    assert "storage_note" not in r_text(body)


def r_text(obj) -> str:
    import json
    return json.dumps(obj)


# ─────────────────────────────── PATCH switches ───────────────────────────────

def test_patch_switch_writes_channel_keys(world):
    r = _client().patch("/api/channels/switches", json={"channel_ebay_enabled": 1, "browser_channel_daily_cap": 2})
    assert r.status_code == 200
    assert r.json()["channel_ebay_enabled"] == 1
    assert world["settings"]["browser_channel_daily_cap"] == 2


def test_patch_switch_rejects_deposit_pct(world):
    r = _client().patch("/api/channels/switches", json={"deposit_pct": 0.5})
    assert r.status_code == 400
    assert "deposit_pct" in r.json()["detail"]
    assert world["calls"]["set_many"] == []   # never reached the store


def test_patch_switch_rejects_out_of_range(world):
    r = _client().patch("/api/channels/switches", json={"channel_site_enabled": 2})
    assert r.status_code == 400


def test_patch_switch_empty_body_is_400(world):
    assert _client().patch("/api/channels/switches", json={}).status_code == 400


def test_fb_marketplace_default_is_off_and_nothing_here_flips_it(world):
    """Only an explicit operator PATCH can turn FB Marketplace on; GET / sync never do."""
    c = _client()
    c.get("/api/channels")
    c.post("/api/channels/sync")
    assert world["settings"]["channel_fb_marketplace_enabled"] == 0


# ─────────────────────────────── queue ───────────────────────────────

def test_approve_on_disabled_channel_is_409(world):
    world["settings"]["channel_fb_marketplace_enabled"] = 0
    r = _client().post("/api/channels/queue/7/approve")
    assert r.status_code == 409
    assert r.json()["detail"] == {"reason": "channel disabled"}
    assert world["calls"]["approve"] == []


def test_approve_on_enabled_channel_queues(world):
    world["settings"]["channel_fb_marketplace_enabled"] = 1
    r = _client().post("/api/channels/queue/7/approve")
    assert r.status_code == 200
    assert r.json()["state"] == "queued"
    assert world["calls"]["approve"] == [7]


def test_approve_respects_master_switch(world):
    world["settings"]["channel_fb_marketplace_enabled"] = 1
    world["settings"]["channels_master_enabled"] = 0
    assert _client().post("/api/channels/queue/7/approve").status_code == 409


def test_approve_missing_row_is_404(world):
    world["settings"]["channel_fb_marketplace_enabled"] = 1
    assert _client().post("/api/channels/queue/999/approve").status_code == 404


def test_reject_sets_off_even_when_channel_disabled(world):
    r = _client().post("/api/channels/queue/7/reject")
    assert r.status_code == 200
    assert r.json()["state"] == "off"
    assert world["calls"]["reject"] == [7]


def test_reject_missing_row_is_404(world):
    assert _client().post("/api/channels/queue/999/reject").status_code == 404


# ─────────────────────────────── sync now ───────────────────────────────

def test_sync_now_returns_report(world):
    r = _client().post("/api/channels/sync")
    assert r.status_code == 200
    assert r.json() == {"planned": 2, "applied": 1, "errors": 0, "skipped": {"disabled": 1}}
    assert world["calls"]["sync"] == [{}]


def test_sync_failure_is_502_not_500(world, monkeypatch):
    def boom(**kw):
        raise RuntimeError("db down")
    monkeypatch.setattr(sync, "run_once", boom)
    r = _client().post("/api/channels/sync")
    assert r.status_code == 502
    assert "db down" in r.json()["detail"]


# ─────────────────────────────── background loop ───────────────────────────────

def test_sync_interval_env_default_and_off(monkeypatch):
    monkeypatch.delenv("CHANNEL_SYNC_SEC", raising=False)
    assert app_mod._channel_sync_interval() == 300.0
    monkeypatch.setenv("CHANNEL_SYNC_SEC", "0")
    assert app_mod._channel_sync_interval() == 0.0
    monkeypatch.setenv("CHANNEL_SYNC_SEC", "junk")
    assert app_mod._channel_sync_interval() == 300.0


@pytest.mark.asyncio
async def test_sync_tick_never_raises(monkeypatch):
    def boom(**kw):
        raise RuntimeError("db down")
    monkeypatch.setattr(sync, "run_once", boom)
    await app_mod._channel_sync_tick()   # must not raise


@pytest.mark.asyncio
async def test_startup_registers_the_loop_and_zero_disables_it(monkeypatch):
    monkeypatch.delenv("BLACKWHOLE_DB_URL", raising=False)
    monkeypatch.setattr(app_mod, "_alerts_loop", lambda: asyncio.sleep(0))
    monkeypatch.setattr(app_mod, "_tracking_loop", lambda: asyncio.sleep(0))
    monkeypatch.setattr(app_mod, "_channel_sync_loop", lambda: asyncio.sleep(0))
    from automation.alerts import geo
    monkeypatch.setattr(geo, "_pgeocode_us", lambda: None)
    for name in ("_alerts_task", "_tracking_task", "_geo_warm_task", "_channel_sync_task"):
        monkeypatch.setattr(app_mod, name, None)
    try:
        monkeypatch.setenv("CHANNEL_SYNC_SEC", "0")
        await app_mod._start_alerts_loop()
        assert app_mod._channel_sync_task is None, "CHANNEL_SYNC_SEC=0 must not start the loop"

        monkeypatch.setenv("CHANNEL_SYNC_SEC", "300")
        await app_mod._start_alerts_loop()
        assert isinstance(app_mod._channel_sync_task, asyncio.Task)
    finally:
        for name in ("_alerts_task", "_tracking_task", "_geo_warm_task", "_channel_sync_task"):
            task = getattr(app_mod, name, None)
            if isinstance(task, asyncio.Task):
                task.cancel()
