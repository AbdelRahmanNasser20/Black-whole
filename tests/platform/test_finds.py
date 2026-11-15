"""Finds = saved searches on platform_alerts: CRUD, lots, seen, degrade before migration 037, star import."""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from automation import db
from automation.web import platform_auth, platform_finds, platform_routes, public_deals

ME = "11111111-1111-1111-1111-111111111111"
OTHER = "22222222-2222-2222-2222-222222222222"
NEW_COLS = ("name", "category", "min_price", "max_bids", "ending_within")


class FakeDB:
    def __init__(self, migrated=True):
        self.rows, self.next_id, self.migrated, self.sql, self.saved = [], 1, migrated, [], []

    def fetch_one(self, sql, params=None):
        self.sql.append(sql)
        if "information_schema" in sql:
            return {"?column?": 1} if self.migrated else None
        if "INSERT INTO platform_alerts" in sql:
            cols = [c.strip() for c in sql.split("(", 1)[1].split(")", 1)[0].split(",")]
            if not self.migrated:
                assert not set(cols) & set(NEW_COLS), "new columns used before migration 037"
            uid, cap = params[-2], params[-1]
            if len([r for r in self.rows if r["user_id"] == uid]) >= cap:
                return None
            row = dict(zip(cols, params[:len(cols)]), id=self.next_id, last_fired_at=None,
                       created_at=datetime(2026, 11, 15, tzinfo=timezone.utc))
            if self.migrated:
                row.setdefault("last_viewed_at", None)
            self.next_id += 1
            self.rows.append(row)
            return dict(row)
        if "UPDATE platform_alerts" in sql:
            for r in self.rows:
                if r["id"] == params[-2] and r["user_id"] == params[-1]:
                    if "last_viewed_at" in sql:
                        assert self.migrated
                        r["last_viewed_at"] = datetime.now(timezone.utc)
                    else:
                        if params[0] is not None:
                            r["active"] = params[0]
                        if "name" in sql and params[1] is not None:
                            r["name"] = params[1]
                    return dict(r)
            return None
        if "FROM platform_alerts" in sql:
            for r in self.rows:
                if r["id"] == params[0] and r["user_id"] == params[1]:
                    return dict(r)
            return None
        raise AssertionError(sql)

    def fetch_all(self, sql, params=None):
        self.sql.append(sql)
        assert "FROM platform_alerts" in sql
        return [dict(r) for r in self.rows if r["user_id"] == params[0]]

    def execute(self, sql, params=None):
        self.sql.append(sql)
        if "DELETE FROM platform_alerts" in sql:
            n = len(self.rows)
            self.rows = [r for r in self.rows if not (r["id"] == params[0] and r["user_id"] == params[1])]
            return n - len(self.rows)
        if "INSERT INTO platform_saved_lots" in sql:
            assert "ON CONFLICT" in sql and "DO NOTHING" in sql and "unnest" in sql
            self.saved.append(params)
            return len(params[1])
        raise AssertionError(sql)


def setup(monkeypatch, migrated=True, user_id=ME, onboarded="2026-10-01"):
    f = FakeDB(migrated)
    for n in ("fetch_all", "fetch_one", "execute"):
        monkeypatch.setattr(db, n, getattr(f, n))
    monkeypatch.setattr(platform_finds, "_columns_ready", False)
    app = FastAPI()
    app.include_router(platform_routes.router)
    app.dependency_overrides[platform_auth.get_current_user] = lambda: {
        "user_id": user_id, "email": "a@b.com", "onboarded_at": onboarded}
    return f, TestClient(app)


FULL = {"name": "GA forklifts", "q": "forklift", "category": "vehicles", "state": "ga", "site": "GovDeals",
        "max_bids": 3, "ending_within": 48, "min_price": 100, "max_price": 5000, "alert": True}


def test_create_stores_every_filter_and_lists_it_back(monkeypatch):
    f, c = setup(monkeypatch)
    r = c.post("/platform/api/finds", json=FULL)
    assert r.status_code == 200, r.text
    got = r.json()
    assert got == {**got, "name": "GA forklifts", "q": "forklift", "category": "vehicles", "state": "GA",
                   "site": "govdeals", "max_bids": 3, "ending_within": 48, "min_price": 100, "max_price": 5000,
                   "alert": True}
    assert r.headers["cache-control"] == "private, no-store"
    listed = c.get("/platform/api/finds")
    assert [x["id"] for x in listed.json()["finds"]] == [got["id"]]
    assert listed.headers["cache-control"] == "private, no-store"
    assert f.rows[0]["keyword"] == "forklift" and f.rows[0]["active"] is True


def test_a_search_needs_a_filter_and_alert_is_off_unless_asked(monkeypatch):
    f, c = setup(monkeypatch)
    assert c.post("/platform/api/finds", json={"name": "everything"}).status_code == 422
    assert c.post("/platform/api/finds", json={"q": "x"}).status_code == 422          # 1 char keyword
    assert c.post("/platform/api/finds", json={"state": "Georgia"}).status_code == 422
    r = c.post("/platform/api/finds", json={"state": "ga"})                             # no keyword is fine after 037
    assert r.status_code == 200 and r.json()["alert"] is False and r.json()["q"] is None
    assert f.rows[0]["active"] is False


def test_cap_is_shared_with_alerts(monkeypatch):
    monkeypatch.setenv("PLATFORM_MAX_ALERTS_PER_USER", "1")
    f, c = setup(monkeypatch)
    assert c.post("/platform/api/finds", json={"q": "chairs"}).status_code == 200
    r = c.post("/platform/api/finds", json={"q": "desks"})
    assert r.status_code == 409 and r.json()["detail"]["error"] == "alert_limit"


def test_patch_and_delete_touch_only_my_rows(monkeypatch):
    f, c = setup(monkeypatch)
    fid = c.post("/platform/api/finds", json={"q": "chairs"}).json()["id"]
    assert c.patch(f"/platform/api/finds/{fid}", json={"alert": True}).json()["alert"] is True
    assert c.patch(f"/platform/api/finds/{fid}", json={"name": "Chairs"}).json()["name"] == "Chairs"
    f.rows[0]["user_id"] = OTHER
    assert c.patch(f"/platform/api/finds/{fid}", json={"alert": False}).status_code == 404
    assert c.delete(f"/platform/api/finds/{fid}").status_code == 404
    assert c.get(f"/platform/api/finds/{fid}/lots").status_code == 404
    f.rows[0]["user_id"] = ME
    assert c.delete(f"/platform/api/finds/{fid}").json() == {"ok": True}
    assert f.rows == []


def test_lots_reads_the_public_feed_newest_first_with_the_saved_filters(monkeypatch):
    f, c = setup(monkeypatch)
    fid = c.post("/platform/api/finds", json=FULL).json()["id"]
    seen = {}

    def fetch_page(**kw):
        seen.update(kw)
        return {"rows": [{"title": "Forklift", "first_seen_at": datetime.now(timezone.utc) - timedelta(hours=2)}],
                "total": 1, "page": 1, "per_page": 50, "pages": 1}

    monkeypatch.setattr(public_deals, "fetch_page", fetch_page)
    r = c.get(f"/platform/api/finds/{fid}/lots")
    assert r.status_code == 200 and r.headers["cache-control"] == "private, no-store"
    assert seen == {"q": "forklift", "category": "vehicles", "state": "GA", "site": "govdeals", "max_bids": 3,
                    "ending_within": 48, "min_price": 100, "max_price": 5000, "status": "active",
                    "sort": "newest", "page": 1, "per_page": 50}
    body = r.json()
    assert body["total"] == 1 and body["rows"][0]["title"] == "Forklift" and body["last_viewed_at"] is None


def test_seen_stamps_the_visit(monkeypatch):
    f, c = setup(monkeypatch)
    fid = c.post("/platform/api/finds", json={"q": "chairs"}).json()["id"]
    assert c.post(f"/platform/api/finds/{fid}/seen").json() == {"ok": True}
    assert f.rows[0]["last_viewed_at"] is not None


def test_before_migration_037_only_the_old_columns_are_used(monkeypatch):
    f, c = setup(monkeypatch, migrated=False)
    r = c.post("/platform/api/finds", json=FULL)
    assert r.status_code == 200, r.text
    got = r.json()
    assert got["q"] == "forklift" and got["state"] == "GA" and got["max_price"] == 5000
    assert got["category"] is None and got["name"] is None and got["degraded"] is True
    assert c.post("/platform/api/finds", json={"state": "ga"}).status_code == 409        # keyword is NOT NULL there
    assert c.post(f"/platform/api/finds/{got['id']}/seen").json() == {"ok": True}       # a no-op, never a 500
    assert c.patch(f"/platform/api/finds/{got['id']}", json={"alert": False}).json()["alert"] is False
    assert c.get("/platform/api/finds").json()["finds"][0]["q"] == "forklift"


def test_column_check_caches_only_a_yes(monkeypatch):
    f, c = setup(monkeypatch, migrated=False)
    assert platform_finds.has_finds_columns() is False
    f.migrated = True
    assert platform_finds.has_finds_columns() is True
    f.migrated = False
    assert platform_finds.has_finds_columns() is True


def test_per_user_endpoints_need_an_onboarded_account(monkeypatch):
    f, c = setup(monkeypatch, onboarded=None)
    assert c.get("/platform/api/finds").status_code == 403
    assert c.post("/platform/api/finds", json={"q": "chairs"}).status_code == 403
    assert c.post("/platform/api/saved/import", json={"lots": []}).status_code == 403


def test_star_import_is_one_bounded_insert(monkeypatch):
    f, c = setup(monkeypatch)
    lots = [{"source": "gsa", "lot_key": "A1"}, {"source": "govdeals", "lot_key": "1/2/3"}]
    r = c.post("/platform/api/saved/import", json={"lots": lots})
    assert r.status_code == 200 and r.json() == {"ok": True, "received": 2}
    uid, sources, keys = f.saved[0]
    assert uid == ME and sources == ["gsa", "govdeals"] and keys == ["A1", "1/2/3"]
    assert c.post("/platform/api/saved/import", json={"lots": [{"source": "gsa", "lot_key": "x"}] * 201}).status_code == 422
    assert c.post("/platform/api/saved/import", json={"lots": [{"source": "", "lot_key": "x"}]}).status_code == 422
    assert c.post("/platform/api/saved/import", json={"lots": []}).json() == {"ok": True, "received": 0}
    assert len(f.saved) == 1                                                             # nothing to write, no query


def test_no_readcache_on_per_user_finds():
    import inspect
    assert "readcache" not in inspect.getsource(platform_finds)


def test_lot_ref_covers_snapshot_rows_so_any_source_can_be_saved():
    assert public_deals.lot_ref({"asset_id": 1, "account_id": 2, "auction_id": 3, "source": "allsurplus"}) == ("allsurplus", "1/2/3")
    assert public_deals.lot_ref({"asset_id": None, "source": "gsa", "source_lot_id": "A1"}) == ("gsa", "A1")
    assert public_deals.lot_ref({"asset_id": None, "source": "gsa"}) is None
