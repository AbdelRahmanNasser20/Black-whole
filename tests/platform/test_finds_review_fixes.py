"""PR #175 review fixes: honest error codes, held alerts before 037, alert needs a real
filter, create dedupe, control characters, capped star import. DB-free."""
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from automation import db
from automation.web import platform_auth, platform_finds, platform_routes

ME = "11111111-1111-1111-1111-111111111111"
WEB = Path(platform_finds.__file__).parent
NEW_COLS = ("name", "category", "min_price", "max_bids", "ending_within")


class FakeDB:
    def __init__(self, migrated):
        self.rows, self.next_id, self.migrated, self.calls = [], 1, migrated, []

    def fetch_one(self, sql, params=None):
        self.calls.append((sql, params))
        if "information_schema" in sql:
            return {"?column?": 1} if self.migrated else None
        if "IS NOT DISTINCT FROM" in sql:                      # the create dedupe lookup
            assert sql.count("%s") == len(params)
            if not self.migrated:
                assert not any(c in sql for c in NEW_COLS), "new columns read before migration 037"
            uid, kw, st, src, mp = params[:5]
            extra = params[5:] if self.migrated else ()
            for r in self.rows:
                same = (r["user_id"] == uid and (r["keyword"] or "").lower() == (kw or "").lower()
                        and r["state_filter"] == st and r["source_filter"] == src and r["max_price"] == mp)
                if same and self.migrated:
                    same = (r["category"], r["min_price"], r["max_bids"], r["ending_within"]) == tuple(extra)
                if same:
                    return dict(r)
            return None
        if "INSERT INTO platform_alerts" in sql:
            cols = [c.strip() for c in sql.split("(", 1)[1].split(")", 1)[0].split(",")]
            row = dict(zip(cols, params[:len(cols)]), id=self.next_id, last_fired_at=None,
                       created_at=datetime(2026, 11, 15, tzinfo=timezone.utc))
            self.next_id += 1
            self.rows.append(row)
            return dict(row)
        if "UPDATE platform_alerts" in sql:
            for r in self.rows:
                if r["id"] == params[-2] and r["user_id"] == params[-1]:
                    if params[0] is not None:
                        r["active"] = params[0]
                    return dict(r)
            return None
        if "FROM platform_alerts" in sql:
            return next((dict(r) for r in self.rows if r["id"] == params[0] and r["user_id"] == params[1]), None)
        raise AssertionError(sql)

    def fetch_all(self, sql, params=None):
        return [dict(r) for r in self.rows]

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        return 2


def setup(monkeypatch, migrated=True):
    f = FakeDB(migrated)
    for n in ("fetch_all", "fetch_one", "execute"):
        monkeypatch.setattr(db, n, getattr(f, n))
    monkeypatch.setattr(platform_finds, "_columns_ready", False)
    # Star-cap tests here are about the Standard (pro) ceiling; the free cap has its own file.
    monkeypatch.setattr(platform_finds, "saved_plan", lambda uid: ("pro", platform_finds._max_saved_lots()))
    app = FastAPI()
    app.include_router(platform_routes.router)
    app.dependency_overrides[platform_auth.get_current_user] = lambda: {
        "user_id": ME, "email": "a@b.com", "onboarded_at": "2026-10-01"}
    return f, TestClient(app)


# 1 ── a search the old table cannot hold is a 422 with its own code, never the cap's 409
def test_no_keyword_before_037_is_422_keyword_required(monkeypatch):
    f, c = setup(monkeypatch, migrated=False)
    r = c.post("/platform/api/finds", json={"state": "ga"})
    assert r.status_code == 422 and r.json()["detail"] == {"error": "keyword_required"}
    assert f.rows == []


def test_clients_read_the_error_code_not_the_status_alone():
    merge = (WEB / "static" / "site" / "platform" / "merge.js").read_text()
    finds = (WEB / "static" / "site" / "platform" / "finds.js").read_text()
    for src in (merge, finds):
        assert "alert_limit" in src and "keyword_required" in src
    assert "rejected" in merge and "capped" in merge          # the page is told what did not move
    assert "could not be saved" in finds
    assert "err.detail = detail" in (WEB / "static" / "ui" / "state.js").read_text()


# 2 ── before 037 a dropped filter must not leave a broad alert switched on
def test_alert_is_held_when_a_filter_cannot_be_stored_yet(monkeypatch):
    f, c = setup(monkeypatch, migrated=False)
    r = c.post("/platform/api/finds", json={"q": "forklift", "max_bids": 0, "ending_within": 24, "alert": True})
    assert r.status_code == 200, r.text
    got = r.json()
    assert got["alert"] is False and got["alert_held"] is True and got["degraded"] is True
    assert f.rows[0]["active"] is False
    r = c.post("/platform/api/finds", json={"q": "pallet jack", "state": "ga", "alert": True})
    assert r.json()["alert"] is True and "alert_held" not in r.json()      # nothing dropped: alert stays on


# 4 ── an alert needs a keyword, category, state or site
@pytest.mark.parametrize("body", [{"min_price": 0}, {"max_bids": 1000}, {"ending_within": 720}, {"max_price": 50}])
def test_alert_on_needs_a_real_filter(monkeypatch, body):
    f, c = setup(monkeypatch)
    r = c.post("/platform/api/finds", json={**body, "alert": True})
    assert r.status_code == 422 and r.json()["detail"] == {"error": "alert_needs_filter"}
    assert f.rows == []
    assert c.post("/platform/api/finds", json=body).status_code == 200       # the search itself may be kept


def test_patch_cannot_switch_an_alert_on_without_a_real_filter(monkeypatch):
    f, c = setup(monkeypatch)
    fid = c.post("/platform/api/finds", json={"max_price": 50}).json()["id"]
    r = c.patch(f"/platform/api/finds/{fid}", json={"alert": True})
    assert r.status_code == 422 and r.json()["detail"] == {"error": "alert_needs_filter"}
    assert f.rows[0]["active"] is False
    ok = c.post("/platform/api/finds", json={"state": "ga"}).json()["id"]
    assert c.patch(f"/platform/api/finds/{ok}", json={"alert": True}).json()["alert"] is True


# 6 ── the same search twice is one row
@pytest.mark.parametrize("migrated", [True, False])
def test_an_identical_search_returns_the_existing_row(monkeypatch, migrated):
    f, c = setup(monkeypatch, migrated=migrated)
    a = c.post("/platform/api/finds", json={"q": "Forklift", "state": "ga", "max_price": 500})
    b = c.post("/platform/api/finds", json={"q": "forklift", "state": "GA", "max_price": 500.0})
    assert a.status_code == b.status_code == 200
    assert b.json()["id"] == a.json()["id"] and b.json()["duplicate"] is True and len(f.rows) == 1
    assert c.post("/platform/api/finds", json={"q": "forklift", "state": "FL"}).json()["id"] != a.json()["id"]


# 9 ── control characters and the saved-lot cap
@pytest.mark.parametrize("body", [{"q": "fork\x00lift"}, {"q": "forklift", "name": "a\x07b"}, {"q": "a\nb"}])
def test_control_characters_are_refused_in_a_search(monkeypatch, body):
    f, c = setup(monkeypatch)
    assert c.post("/platform/api/finds", json=body).status_code == 422 and f.rows == []


def test_control_characters_are_refused_in_a_rename_and_an_import(monkeypatch):
    f, c = setup(monkeypatch)
    fid = c.post("/platform/api/finds", json={"q": "chairs"}).json()["id"]
    assert c.patch(f"/platform/api/finds/{fid}", json={"name": "x\x00y"}).status_code == 422
    for lot in ({"source": "gs\x00a", "lot_key": "A1"}, {"source": "gsa", "lot_key": "A\x001"}):
        assert c.post("/platform/api/saved/import", json={"lots": [lot]}).status_code == 422
    assert not [s for s, _p in f.calls if "platform_saved_lots" in s]


def test_star_import_stops_at_the_saved_lot_cap(monkeypatch):
    monkeypatch.setenv("PLATFORM_MAX_SAVED_LOTS", "300")
    f, c = setup(monkeypatch)
    lots = [{"source": "gsa", "lot_key": "A1"}, {"source": "govdeals", "lot_key": "1/2/3"}, {"source": "gsa", "lot_key": "A3"}]
    r = c.post("/platform/api/saved/import", json={"lots": lots})
    assert r.status_code == 200 and r.json() == {"ok": True, "received": 3, "imported": 2}
    sql, params = next((s, p) for s, p in f.calls if "platform_saved_lots" in s and s.startswith("INSERT"))
    assert "LIMIT greatest(0, %s - (SELECT count(*) FROM platform_saved_lots WHERE user_id = %s))" in sql
    assert "ON CONFLICT (user_id, source, lot_key) DO NOTHING" in sql
    assert sql.count("%s") == len(params)
    assert params == (ME, ["gsa", "govdeals", "gsa"], ["A1", "1/2/3", "A3"], 300, ME)


# 7 ── the browser store drops what the server would refuse
def test_browser_store_mirrors_the_server_rules():
    js = (WEB / "static" / "site" / "platform" / "finds_store.js").read_text()
    assert "v.length < 2" in js                      # a 1-letter keyword is not a search
    line = js.split("const ZERO_IS_NOTHING", 1)[1].split("\n", 1)[0]
    assert "'min_price'" in line and "'max_price'" in line and "'ending_within'" in line
    assert "'max_bids'" not in line                  # 0 bids = "no bids", a real filter
