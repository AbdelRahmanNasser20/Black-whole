"""/sources — public, server-rendered, no photos, no private fields, 503 when the facets query fails."""
import importlib

import pytest
from fastapi.testclient import TestClient

from automation.web import auth as auth_svc
from automation.web import public_deals as pd

app_mod = importlib.import_module("automation.web.app")
app = app_mod.app

FACETS = {"categories": [{"value": "vehicles", "count": 3}],
          "states": [{"value": "TX", "count": 120}, {"value": "AZ", "count": 80}],
          "stats": {"tracked": 210932, "active": 9791, "closed": 200000, "no_bid": 40000, "states": 44, "since": None},
          "cached_at": 0}


@pytest.fixture(autouse=True)
def _no_auth(monkeypatch):
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    auth_svc.reset_caches()
    yield
    auth_svc.reset_caches()


def test_sources_page_is_public_and_server_rendered(monkeypatch):
    monkeypatch.setattr(pd, "fetch_facets", lambda: FACETS)
    html = TestClient(app).get("/sources").text
    assert "Where the lots come from" in html and "GovDeals" in html
    assert "9,791" in html and "TX" in html and "12.5" in html
    assert "<img" not in html and "storage_note" not in html
    assert 'data-state="loading"' not in html  # nothing to fetch client-side


def test_sources_503_when_facets_fail(monkeypatch):
    monkeypatch.setattr(pd, "fetch_facets", lambda: (_ for _ in ()).throw(RuntimeError("db")))
    assert TestClient(app).get("/sources").status_code == 503
