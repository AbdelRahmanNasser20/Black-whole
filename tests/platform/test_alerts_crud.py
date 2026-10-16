"""Alerts + saved-lots CRUD under /platform/api, behind require_onboarded."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from automation import db
from automation.web import platform_auth, platform_routes

ME = "11111111-1111-1111-1111-111111111111"
OTHER = "22222222-2222-2222-2222-222222222222"


class FakeDB:
    """Tiny in-memory stand-in keyed off the SQL text the routes send."""

    def __init__(self):
        self.alerts, self.saved, self.next_id = [], set(), 1

    def fetch_all(self, sql, params=None):
        if "FROM platform_alerts" in sql:
            return [a for a in self.alerts if a["user_id"] == params[0]]
        if "FROM platform_saved_lots" in sql:
            return [{"source": s, "lot_key": k} for (u, s, k) in sorted(self.saved) if u == params[0]]
        raise AssertionError(sql)

    def fetch_one(self, sql, params=None):
        assert "INSERT INTO platform_alerts" in sql and "%s" in sql
        uid, kw, st, src, mp, _uid2, cap = params
        if len([a for a in self.alerts if a["user_id"] == uid]) >= cap:
            return None
        row = {"id": self.next_id, "user_id": uid, "keyword": kw, "state_filter": st,
               "source_filter": src, "max_price": mp, "active": True}
        self.next_id += 1
        self.alerts.append(row)
        return row

    def execute(self, sql, params=None):
        if "DELETE FROM platform_alerts" in sql:
            before = len(self.alerts)
            self.alerts = [a for a in self.alerts if not (a["id"] == params[0] and a["user_id"] == params[1])]
            return before - len(self.alerts)
        if "INSERT INTO platform_saved_lots" in sql:
            assert "ON CONFLICT" in sql and "DO NOTHING" in sql
            self.saved.add(tuple(params))
            return 1
        if "DELETE FROM platform_saved_lots" in sql:
            key = tuple(params)
            n = int(key in self.saved)
            self.saved.discard(key)
            return n
        raise AssertionError(sql)


@pytest.fixture
def fake(monkeypatch):
    f = FakeDB()
    for n in ("fetch_all", "fetch_one", "execute"):
        monkeypatch.setattr(db, n, getattr(f, n))
    return f


def make_client(user_id=ME, onboarded="2026-10-01"):
    app = FastAPI()
    app.include_router(platform_routes.router)
    app.dependency_overrides[platform_auth.get_current_user] = lambda: {
        "user_id": user_id, "email": "a@b.com", "onboarded_at": onboarded}
    return TestClient(app)


@pytest.fixture
def client(fake):
    return make_client()


def test_create_list_delete_alert(client, fake):
    r = client.post("/platform/api/alerts", json={"keyword": "banquet chairs", "state_filter": "az", "max_price": 500})
    assert r.status_code == 200
    a = r.json()
    assert a["keyword"] == "banquet chairs" and a["state_filter"] == "AZ" and a["max_price"] == 500
    listed = client.get("/platform/api/alerts").json()
    assert [x["id"] for x in listed["alerts"]] == [a["id"]]
    assert client.delete(f"/platform/api/alerts/{a['id']}").status_code == 200
    assert client.get("/platform/api/alerts").json()["alerts"] == []


def test_alert_cap_409(client, fake, monkeypatch):
    monkeypatch.setenv("PLATFORM_MAX_ALERTS_PER_USER", "2")
    for i in range(2):
        assert client.post("/platform/api/alerts", json={"keyword": f"kw{i}"}).status_code == 200
    r = client.post("/platform/api/alerts", json={"keyword": "kw3"})
    assert r.status_code == 409
    assert r.json() == {"detail": {"error": "alert_limit", "max": 2}}
    assert len(fake.alerts) == 2


def test_alert_cap_default_10(client, fake, monkeypatch):
    monkeypatch.delenv("PLATFORM_MAX_ALERTS_PER_USER", raising=False)
    for i in range(10):
        assert client.post("/platform/api/alerts", json={"keyword": f"kw{i}"}).status_code == 200
    assert client.post("/platform/api/alerts", json={"keyword": "one more"}).status_code == 409


def test_other_users_alert_404(client, fake):
    fake.alerts.append({"id": 99, "user_id": OTHER, "keyword": "theirs", "state_filter": None,
                        "source_filter": None, "max_price": None, "active": True})
    mine = client.delete("/platform/api/alerts/99")
    ghost = client.delete("/platform/api/alerts/12345")
    assert mine.status_code == ghost.status_code == 404
    assert mine.json() == ghost.json()
    assert len(fake.alerts) == 1
    assert client.get("/platform/api/alerts").json()["alerts"] == []


@pytest.mark.parametrize("body", [
    {"keyword": "a"}, {"keyword": "x" * 81}, {"keyword": ""}, {"keyword": "   "}, {"keyword": " a "}, {},
    {"keyword": "chairs", "state_filter": "ARI"}, {"keyword": "chairs", "max_price": -5},
])
def test_keyword_length_422(client, fake, body):
    assert client.post("/platform/api/alerts", json=body).status_code == 422
    assert fake.alerts == []


def test_keyword_bounds_ok(client):
    assert client.post("/platform/api/alerts", json={"keyword": "ab"}).status_code == 200
    assert client.post("/platform/api/alerts", json={"keyword": "x" * 80}).status_code == 200


def test_saved_lot_roundtrip(client, fake):
    body = {"source": "govdeals", "lot_key": "123-45"}
    assert client.post("/platform/api/saved", json=body).status_code == 200
    assert client.post("/platform/api/saved", json=body).status_code == 200  # idempotent
    assert client.get("/platform/api/saved").json()["saved"] == [body]
    assert client.delete("/platform/api/saved/govdeals/123-45").status_code == 200
    assert client.get("/platform/api/saved").json()["saved"] == []


def test_not_onboarded_403(fake):
    c = make_client(onboarded=None)
    assert c.get("/platform/api/alerts").status_code == 403
    assert c.post("/platform/api/alerts", json={"keyword": "chairs"}).status_code == 403
    assert c.post("/platform/api/saved", json={"source": "s", "lot_key": "k"}).status_code == 403
    assert fake.alerts == [] and fake.saved == set()


def test_unauthed_401(monkeypatch, fake):
    monkeypatch.setenv("SUPABASE_JWT_SECRET", "s" * 40)
    app = FastAPI()
    app.include_router(platform_routes.router)
    assert TestClient(app).get("/platform/api/alerts").status_code == 401


def test_alerts_page_renders():
    from automation.web.app import app
    r = TestClient(app, follow_redirects=False).get("/platform/alerts")
    assert r.status_code == 302 and r.headers["location"] == "/platform/signup"


def test_alerts_page_renders_for_onboarded_user(monkeypatch):
    from automation.web.app import app
    monkeypatch.setattr(platform_auth, "maybe_user",
                        lambda req: {"user_id": ME, "email": "a@b.com", "onboarded_at": "2026-10-01"})
    r = TestClient(app).get("/platform/alerts")
    assert r.status_code == 200 and "/platform/api/alerts" in r.text and "a@b.com" in r.text


def test_alerts_page_not_onboarded_redirects(monkeypatch):
    from automation.web.app import app
    monkeypatch.setattr(platform_auth, "maybe_user",
                        lambda req: {"user_id": ME, "email": "a@b.com", "onboarded_at": None})
    r = TestClient(app, follow_redirects=False).get("/platform/alerts")
    assert r.status_code == 302 and r.headers["location"] == "/platform/onboarding"


def test_source_filter_lowercased(client, fake):
    r = client.post("/platform/api/alerts", json={"keyword": "chairs", "source_filter": " GovDeals "})
    assert r.json()["source_filter"] == "govdeals"


def test_unsave_slashed_lot_key(client, fake):
    fake.saved.add((ME, "govdeals", "a/b"))
    assert client.delete("/platform/api/saved/govdeals/a%2Fb").status_code == 200
    assert fake.saved == set()
