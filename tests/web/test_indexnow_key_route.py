"""GET /<key>.txt — the IndexNow key file (dark when INDEXNOW_KEY is unset)."""
import pytest
from fastapi.testclient import TestClient

from automation.web import auth as auth_svc
from automation.web.app import app

KEY = "0123456789abcdef0123456789abcdef"


@pytest.fixture(autouse=True)
def _clean_auth(monkeypatch):
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("SITE_BASE_URL", raising=False)
    auth_svc.reset_caches()
    yield
    auth_svc.reset_caches()


def _client():
    return TestClient(app, base_url="https://testserver")


def test_unset_key_404s(monkeypatch):
    monkeypatch.delenv("INDEXNOW_KEY", raising=False)
    r = _client().get(f"/{KEY}.txt")
    assert r.status_code == 404
    assert r.headers.get("cache-control") == "no-store"


def test_key_file_served(monkeypatch):
    monkeypatch.setenv("INDEXNOW_KEY", KEY)
    r = _client().get(f"/{KEY}.txt")
    assert r.status_code == 200
    assert r.text == KEY
    assert r.headers["content-type"].startswith("text/plain")


def test_wrong_name_404s(monkeypatch):
    monkeypatch.setenv("INDEXNOW_KEY", KEY)
    assert _client().get("/notthekey1234.txt").status_code == 404


def test_robots_txt_not_shadowed(monkeypatch):
    monkeypatch.setenv("INDEXNOW_KEY", KEY)
    r = _client().get("/robots.txt")
    assert r.status_code == 200
    assert "User-agent: *" in r.text and "Sitemap:" in r.text
