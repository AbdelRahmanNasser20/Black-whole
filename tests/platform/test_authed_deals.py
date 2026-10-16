"""Signed-in /deals experience: flip extras + save-ability for onboarded users only."""
from datetime import datetime, timezone

import importlib

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from automation import db
from automation.web import deals_sources, platform_auth, platform_routes, public_deals
from deals import verdict_store

webapp = importlib.import_module("automation.web.app")

ME = "11111111-1111-1111-1111-111111111111"
END = datetime(2026, 12, 1, tzinfo=timezone.utc)


def gd_row(asset=1, account=2, auction=3):
    return {"asset_id": asset, "account_id": account, "auction_id": auction, "title": "Banquet chairs",
            "current_bid": 100.0, "bid_count": 2, "end_utc": END, "state": "AZ"}


class FakeConn:
    def __init__(self, log):
        self.log = log

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=()):
        self.log.append(sql)
        outer = self

        class Cur:
            def fetchall(_):
                return [gd_row()] if "SELECT asset_id" in sql else []

            def fetchone(_):
                return {"c": 1}
        return Cur()


@pytest.fixture
def env(monkeypatch):
    sql_log = []
    monkeypatch.setattr(db, "connect", lambda *a, **k: FakeConn(sql_log))
    monkeypatch.setattr(deals_sources, "lots", lambda *a, **k: [])
    monkeypatch.setattr(verdict_store, "_has_flip_columns", lambda: True)

    def fetch_all(sql, params=None):
        if "FROM deal_verdicts" in sql:
            return [{"asset_id": 1, "account_id": 2, "auction_id": 3, "flip_score": 71.5,
                     "flip": {"max_bid": {"25": 150.0, "50": 88.0, "100": 40.0}}}]
        if "FROM platform_saved_lots" in sql:
            return [{"source": "govdeals", "lot_key": "1/2/3"}]
        raise AssertionError(sql)
    monkeypatch.setattr(db, "fetch_all", fetch_all)
    public_deals.clear_cache()
    return sql_log


@pytest.fixture
def client(monkeypatch):
    """client(user) -> TestClient with platform_auth.maybe_user patched (and restored)."""
    def make(user):
        monkeypatch.setattr(platform_auth, "maybe_user", lambda request: user)
        return TestClient(webapp.app)
    return make


AUTHED = {"user_id": ME, "email": "a@b.com", "onboarded_at": "2026-10-01"}


def test_anon_json_has_no_max_bid(env, client):
    body = client(None).get("/deals/api/lots").json()
    row = body["rows"][0]
    assert "max_bid" not in row and "flip_score" not in row and "saved" not in row


def test_authed_json_has_max_bid(env, client):
    r = client(AUTHED).get("/deals/api/lots")
    row = r.json()["rows"][0]
    assert row["max_bid"] == 88.0 and row["flip_score"] == 71.5
    assert row["source"] == "govdeals" and row["lot_key"] == "1/2/3" and row["saved"] is True
    assert "no-store" in r.headers["cache-control"] and "private" in r.headers["cache-control"]
    # an anonymous request right after must not see the per-user data
    assert "max_bid" not in client(None).get("/deals/api/lots").json()["rows"][0]


def test_unonboarded_gets_public_view(env, client):
    u = dict(AUTHED, onboarded_at=None)
    assert "max_bid" not in client(u).get("/deals/api/lots").json()["rows"][0]


def test_tenant_sees_no_excluded_lots(env, client):
    client(None).get("/deals/api/lots")
    anon_sql = [s for s in env if "FROM deal_lots" in s]
    env.clear()
    client(AUTHED).get("/deals/api/lots")
    authed_sql = [s for s in env if "FROM deal_lots" in s]
    assert authed_sql and authed_sql == anon_sql
    for frag in ("tracked_lots", "auction_favorites", "deal_list_items"):
        assert all(frag in s for s in authed_sql)


def test_deals_page_cta_anon_only(env, client):
    assert "/platform/signup" in client(None).get("/deals").text
    page = client(AUTHED).get("/deals")
    assert "Sign up free for alerts" not in page.text
    assert "no-store" in page.headers["cache-control"]


def test_saved_page_lists_lots(monkeypatch):
    seen = []

    def fetch_all(sql, params=None):
        seen.append(sql)
        assert "FROM platform_saved_lots" in sql and "LEFT JOIN deal_lots" in sql
        for frag in ("tracked_lots", "auction_favorites", "deal_list_items"):
            assert frag in sql, frag
        return [
            {"source": "govdeals", "lot_key": "1/2/3", "title": "Banquet chairs", "current_bid": 100.0,
             "end_utc": END, "bid_count": 2, "outcome_complete": False, "asset_id": 1, "account_id": 2},
            {"source": "govdeals", "lot_key": "9/9/9", "title": None, "current_bid": None,
             "end_utc": None, "bid_count": None, "outcome_complete": None, "asset_id": None, "account_id": None},
        ]
    monkeypatch.setattr(db, "fetch_all", fetch_all)
    app = FastAPI()
    app.include_router(platform_routes.router)
    monkeypatch.setattr(platform_auth, "maybe_user", lambda request: AUTHED)
    r = TestClient(app).get("/platform/saved")
    assert r.status_code == 200
    assert "Banquet chairs" in r.text and "$100" in r.text
    assert "9/9/9" in r.text and "no longer" in r.text.lower()
    assert len(seen) == 1
    # anon -> signup redirect
    monkeypatch.setattr(platform_auth, "maybe_user", lambda request: None)
    r = TestClient(app).get("/platform/saved", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/platform/signup"


def test_extras_failure_degrades_to_public_payload(env, client, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(public_deals, "add_platform_extras", boom)
    r = client(AUTHED).get("/deals/api/lots")
    assert r.status_code == 200 and "max_bid" not in r.json()["rows"][0]


def test_viewer_lookup_failure_serves_anonymous(env, monkeypatch):
    def boom(request):
        raise RuntimeError("db down")
    monkeypatch.setattr(platform_auth, "maybe_user", boom)
    assert TestClient(webapp.app).get("/deals/api/lots").status_code == 200
    assert TestClient(webapp.app).get("/deals").status_code == 200


def test_snapshot_source_rows_get_no_save_button():
    row = {"source": "publicsurplus", "source_lot_id": "77", "asset_id": None, "account_id": None, "auction_id": None}
    assert public_deals.lot_ref(row) is None
