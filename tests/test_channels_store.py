"""Phase 1.3 — `listing_channels` store. Offline: every `automation.db` call is
monkeypatched, nothing opens a connection. The properties defended:

  1. Vocabulary is validated BEFORE any SQL — a typo'd channel or state is a
     ValueError, never a CHECK violation surfaced as a 500.
  2. `upsert` is one idempotent statement keyed on (lot_id, channel); nullable
     fields COALESCE so a state-only write never wipes a known url.
  3. `matrix()` is total: every lot × every channel has a cell, `off` when no
     row exists, so the admin grid never has holes.
  4. Parameterized SQL only (`%s`), no lot ids interpolated into strings.
"""
import pytest

from automation import channels
from automation.channels import store


# ─────────────────────────── validation ───────────────────────────

def test_upsert_rejects_unknown_channel():
    with pytest.raises(ValueError):
        store.upsert("gd-1-2", "myspace", state="live")


def test_upsert_rejects_unknown_state():
    with pytest.raises(ValueError):
        store.upsert("gd-1-2", "ebay", state="maybe")


def test_set_state_validates_before_sql(monkeypatch):
    monkeypatch.setattr(store.db, "fetch_one", lambda *a, **k: pytest.fail("must validate first"))
    with pytest.raises(ValueError):
        store.set_state("gd-1-2", "ebay", "maybe")
    with pytest.raises(ValueError):
        store.set_state("gd-1-2", "nope", "live")


def test_list_by_state_validates(monkeypatch):
    monkeypatch.setattr(store.db, "fetch_all", lambda *a, **k: pytest.fail("must validate first"))
    with pytest.raises(ValueError):
        store.list_by_state("ebay", "maybe")


# ─────────────────────────── upsert / set_state ───────────────────────────

def test_upsert_issues_on_conflict_sql(monkeypatch):
    seen = {}

    def fake_fetch_one(sql, params=None):
        seen["sql"], seen["params"] = sql, params
        return {"lot_id": "gd-1-2", "channel": "ebay", "state": "live"}

    monkeypatch.setattr(store.db, "fetch_one", fake_fetch_one)
    row = store.upsert("gd-1-2", "ebay", state="live", url="https://ebay.com/itm/1")
    assert "ON CONFLICT (lot_id, channel)" in seen["sql"]
    assert "RETURNING" in seen["sql"]
    assert "gd-1-2" not in seen["sql"], "lot ids are parameters, never interpolated"
    assert seen["params"][:3] == ("gd-1-2", "ebay", "live")
    assert "https://ebay.com/itm/1" in seen["params"]
    assert row["state"] == "live"


def test_upsert_coalesces_nullable_fields(monkeypatch):
    seen = {}
    monkeypatch.setattr(store.db, "fetch_one", lambda sql, params=None: seen.setdefault("sql", sql) and {})
    store.upsert("gd-1-2", "ebay", state="delisted")
    for col in ("external_id", "url", "payload_hash"):
        assert f"{col} = COALESCE(EXCLUDED.{col}, listing_channels.{col})" in seen["sql"], col
    assert "last_error = EXCLUDED.last_error" in seen["sql"]   # errors are NOT sticky
    assert "last_synced_at = now()" in seen["sql"]


def test_set_state_creates_the_row_when_missing(monkeypatch):
    """remove_lot / restore_lot call set_state on lots that never had a row —
    that must land a row, not silently no-op."""
    seen = {}

    def fake_fetch_one(sql, params=None):
        seen["sql"], seen["params"] = sql, params
        return {"lot_id": "31225", "channel": "fb_marketplace", "state": "pending_approval"}

    monkeypatch.setattr(store.db, "fetch_one", fake_fetch_one)
    row = store.set_state("31225", "fb_marketplace", "pending_approval")
    assert "INSERT INTO listing_channels" in seen["sql"]
    assert "ON CONFLICT (lot_id, channel)" in seen["sql"]
    assert "last_synced_at" not in seen["sql"], "a state flip is not a sync"
    assert row["state"] == "pending_approval"


def test_set_state_passes_last_error(monkeypatch):
    seen = {}
    monkeypatch.setattr(store.db, "fetch_one", lambda sql, params=None: seen.setdefault("params", params) and {})
    store.set_state("31225", "ebay", "error", last_error="boom")
    assert "boom" in seen["params"]


# ─────────────────────────── reads ───────────────────────────

def test_get_is_parameterized(monkeypatch):
    seen = {}

    def fake_fetch_one(sql, params=None):
        seen["sql"], seen["params"] = sql, params
        return None

    monkeypatch.setattr(store.db, "fetch_one", fake_fetch_one)
    assert store.get("gd-1-2", "site") is None
    assert "%s" in seen["sql"] and seen["params"] == ("gd-1-2", "site")


def test_matrix_shape(monkeypatch):
    monkeypatch.setattr(store.inventory, "list_all", lambda: [{"lot_id": "a", "title": "t", "status": "owned", "quantity_remaining": 5}])
    monkeypatch.setattr(store.db, "fetch_all", lambda sql, params=None: [{"lot_id": "a", "channel": "ebay", "state": "live", "url": "u"}])
    m = store.matrix()
    assert m[0]["channels"]["ebay"] == {"state": "live", "url": "u"}
    assert m[0]["channels"]["site"] == {"state": "off", "url": None}
    assert set(m[0]["channels"]) == set(channels.CHANNELS)
    assert m[0]["lot_id"] == "a" and m[0]["title"] == "t" and m[0]["status"] == "owned" and m[0]["qty"] == 5


def test_matrix_never_leaks_storage_note(monkeypatch):
    monkeypatch.setattr(store.inventory, "list_all", lambda: [
        {"lot_id": "a", "title": "t", "status": "owned", "quantity_remaining": 5,
         "storage_note": "gate code 1234"}])
    monkeypatch.setattr(store.db, "fetch_all", lambda sql, params=None: [])
    m = store.matrix()
    assert "storage_note" not in m[0]
    assert "1234" not in repr(m)


def test_matrix_error_cell_carries_last_error(monkeypatch):
    monkeypatch.setattr(store.inventory, "list_all", lambda: [{"lot_id": "a", "title": "t", "status": "owned", "quantity_remaining": 5}])
    monkeypatch.setattr(store.db, "fetch_all", lambda sql, params=None: [
        {"lot_id": "a", "channel": "ebay", "state": "error", "url": None, "last_error": "token expired"}])
    cell = store.matrix()[0]["channels"]["ebay"]
    assert cell["state"] == "error" and cell["last_error"] == "token expired"


def test_queue_is_pending_approval_newest_first(monkeypatch):
    seen = {}

    def fake_fetch_all(sql, params=None):
        seen["sql"], seen["params"] = sql, params
        return [{"id": 3, "lot_id": "a", "channel": "fb_marketplace", "state": "pending_approval", "title": "t"}]

    monkeypatch.setattr(store.db, "fetch_all", fake_fetch_all)
    q = store.queue()
    assert q[0]["id"] == 3
    assert "pending_approval" in seen["params"]
    assert "ORDER BY" in seen["sql"] and "DESC" in seen["sql"]
    assert "storage_note" not in seen["sql"]


def test_current_map_is_keyed_by_lot_and_channel(monkeypatch):
    monkeypatch.setattr(store.db, "fetch_all", lambda sql, params=None: [
        {"lot_id": "a", "channel": "ebay", "state": "live"},
        {"lot_id": "a", "channel": "site", "state": "live"},
    ])
    cur = store.current_map()
    assert set(cur) == {("a", "ebay"), ("a", "site")}


# ─────────────────────────── approve / reject ───────────────────────────

def test_approve_sets_queued_and_approved_at(monkeypatch):
    seen = {}

    def fake_fetch_one(sql, params=None):
        seen["sql"], seen["params"] = sql, params
        return {"id": 7, "state": "queued"}

    monkeypatch.setattr(store.db, "fetch_one", fake_fetch_one)
    row = store.approve(7)
    assert row["state"] == "queued"
    assert "approved_at = now()" in seen["sql"]
    assert seen["params"] == ("queued", 7)
    assert "pending_approval" in seen["sql"], "approve only moves rows that are waiting"


def test_reject_sets_off(monkeypatch):
    seen = {}
    monkeypatch.setattr(store.db, "fetch_one", lambda sql, params=None: seen.setdefault("params", params) and {"id": 7, "state": "off"})
    assert store.reject(7)["state"] == "off"
    assert seen["params"][0] == "off"
