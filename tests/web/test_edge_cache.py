"""Edge cache headers on the anonymous public feed (automation/web/edge_cache.py)."""
import pytest
from fastapi.testclient import TestClient

from automation.web import auth as auth_svc
from automation.web import edge_cache, platform_api, platform_auth
from automation.web.app import app


@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    monkeypatch.setattr(platform_api, "sites", lambda rows: {"ok": True, "sites": []})
    return TestClient(app, base_url="https://testserver")


def test_public_json_carries_public_cache_control(client):
    r = client.get("/platform/api/sites")
    assert r.status_code == 200
    assert r.headers["cache-control"] == edge_cache.HEADER
    assert "s-maxage=300" in r.headers["cache-control"]
    assert "stale-while-revalidate" in r.headers["cache-control"]


def test_platform_session_cookie_disables_it(client):
    r = client.get("/platform/api/sites", cookies={platform_auth.COOKIE_NAME: "x"})
    assert r.status_code == 200
    assert r.headers.get("cache-control") != edge_cache.HEADER


def test_admin_session_cookie_disables_it(client):
    r = client.get("/platform/api/sites", cookies={auth_svc.SESSION_COOKIE: "x"})
    assert r.headers.get("cache-control") != edge_cache.HEADER


def test_kill_switch(client, monkeypatch):
    monkeypatch.setenv("EDGE_CACHE_ENABLED", "0")
    r = client.get("/platform/api/sites")
    assert "cache-control" not in r.headers or r.headers["cache-control"] != edge_cache.HEADER


def test_only_listed_paths():
    assert edge_cache.cacheable("/deals/api/lots")
    assert not edge_cache.cacheable("/platform/api/alerts")    # per-user, never
    assert not edge_cache.cacheable("/deals/lot/abc")          # signed-in view differs
    assert not edge_cache.cacheable("/api/inventory")          # admin
