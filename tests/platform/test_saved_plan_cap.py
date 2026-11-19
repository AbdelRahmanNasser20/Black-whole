"""Free plan = 3 saved lots; Standard ('pro') = PLATFORM_MAX_SAVED_LOTS. The cap is enforced server-side."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from automation import db
from automation.web import platform_auth, platform_routes

ME = "11111111-1111-1111-1111-111111111111"


class FakeDB:
    def __init__(self, plan):
        self.plan, self.saved = plan, []

    def fetch_one(self, sql, params=None):
        if "FROM platform_users" in sql:
            return {"plan": self.plan}
        if "FROM platform_saved_lots" in sql and "lot_key = %s" in sql:
            return {"x": 1} if (params[1], params[2]) in self.saved else None
        raise AssertionError(sql)

    def fetch_all(self, sql, params=None):
        return [{"source": s, "lot_key": k} for (s, k) in self.saved]

    def execute(self, sql, params=None):
        assert "INSERT INTO platform_saved_lots" in sql and "ON CONFLICT" in sql
        uid, source, key, _uid2, cap = params
        if (source, key) in self.saved or len(self.saved) >= cap:
            return 0
        self.saved.append((source, key))
        return 1


def client(monkeypatch, plan):
    f = FakeDB(plan)
    for n in ("fetch_all", "fetch_one", "execute"):
        monkeypatch.setattr(db, n, getattr(f, n))
    app = FastAPI()
    app.include_router(platform_routes.router)
    app.dependency_overrides[platform_auth.get_current_user] = lambda: {
        "user_id": ME, "email": "a@b.co", "onboarded_at": "2026-01-01"}
    return TestClient(app), f


@pytest.mark.parametrize("plan,allowed", [("beta", 3), ("pro", 10)])
def test_saved_cap_by_plan(monkeypatch, plan, allowed):
    c, f = client(monkeypatch, plan)
    codes = [c.post("/platform/api/saved", json={"source": "govdeals", "lot_key": f"k{i}"}).status_code
             for i in range(10)]
    assert codes[:allowed] == [200] * allowed
    assert all(code == 402 for code in codes[allowed:])
    assert len(f.saved) == allowed


def test_resave_when_full_is_not_refused(monkeypatch):
    c, _ = client(monkeypatch, "beta")
    for i in range(3):
        c.post("/platform/api/saved", json={"source": "govdeals", "lot_key": f"k{i}"})
    assert c.post("/platform/api/saved", json={"source": "govdeals", "lot_key": "k0"}).status_code == 200


def test_over_cap_answer_names_the_limit(monkeypatch):
    c, _ = client(monkeypatch, "beta")
    for i in range(3):
        c.post("/platform/api/saved", json={"source": "govdeals", "lot_key": f"k{i}"})
    r = c.post("/platform/api/saved", json={"source": "govdeals", "lot_key": "k9"})
    assert r.json()["detail"] == {"error": "star_limit", "max": 3}


def test_list_saved_reports_plan(monkeypatch):
    c, _ = client(monkeypatch, "pro")
    body = c.get("/platform/api/saved").json()
    assert body["plan"] == "pro" and body["limit"] >= 3
