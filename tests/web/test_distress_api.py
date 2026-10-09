"""DB-free tests for /distress/api/*: public routes open, contacts never public,
operator route gated, bad params 400, the old /distress page redirects to /platform/bankruptcies."""
import importlib

import pytest
from fastapi.testclient import TestClient

from automation.web import auth as auth_svc
from automation.web import public_distress as pdx
from automation.web import readcache

app_mod = importlib.import_module("automation.web.app")
app = app_mod.app

ROW = {"id": 1, "source": "courtlistener", "docket_id": 73301234, "case_name": "Silver Spur Resort LP",
       "court_id": "txeb", "docket_number": "26-60568", "date_filed": "2026-10-03", "chapter": "7",
       "industry_tag": "resort", "state": "TX", "sale_noticed_at": None}


@pytest.fixture(autouse=True)
def _auth_on(monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "hunter2-but-much-longer")
    monkeypatch.setenv("SESSION_SECRET", "unit-test-secret")
    auth_svc.reset_caches()
    readcache.invalidate_all()
    yield
    auth_svc.reset_caches()
    readcache.invalidate_all()


@pytest.fixture
def calls(monkeypatch):
    seen = []

    def fake_page(**kw):
        seen.append(kw)
        row = dict(ROW)
        if kw.get("admin"):
            row.update(trustee="Jane Doe", attorneys=[{"name": "A. Lawyer"}], parties=["Silver Spur Resort LP"])
        return {"rows": [row], "total": 1, "page": 1, "per_page": 25, "pages": 1, "near": None, "unlocated": 0}

    monkeypatch.setattr(pdx, "fetch_page", fake_page)
    monkeypatch.setattr(pdx, "fetch_facets", lambda: {"industries": [{"value": "resort", "count": 1}],
                                                      "states": [], "chapters": [], "stats": {"cases": 1},
                                                      "last_run": {}, "cached_at": 0})
    return seen


@pytest.fixture
def client(calls):
    return TestClient(app, base_url="https://testserver")


def test_public_routes_need_no_session(client):
    assert client.get("/distress/api/facets").json()["stats"]["cases"] == 1
    r = client.get("/distress/api/cases?tab=sales&chapter=7&industry=resort&state=tx")
    assert r.status_code == 200 and r.json()["rows"][0]["case_name"] == "Silver Spur Resort LP"


def test_public_never_returns_contacts(client, calls):
    body = client.get("/distress/api/cases").json()
    for k in ("trustee", "attorneys", "parties"):
        assert k not in body["rows"][0]
    assert calls[-1]["admin"] is False


def test_admin_route_is_gated(client):
    assert client.get("/api/distress/cases").status_code == 401


def test_admin_route_returns_contacts_with_session(client, calls, monkeypatch):
    monkeypatch.setattr(auth_svc, "is_authenticated", lambda request: True, raising=False)
    monkeypatch.delenv("ADMIN_PASSWORD")
    auth_svc.reset_caches()
    r = client.get("/api/distress/cases?chapter=7")
    assert r.status_code == 200
    assert r.json()["rows"][0]["trustee"] == "Jane Doe" and calls[-1]["admin"] is True


@pytest.mark.parametrize("qs", ["tab=bogus", "chapter=13", "source=pacer", "industry=airline", "radius_mi=0"])
def test_bad_params_400(client, qs):
    assert client.get(f"/distress/api/cases?{qs}").status_code == 400


def test_old_distress_page_redirects_to_platform_bankruptcies(client, calls):
    r = client.get("/distress", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/platform/bankruptcies"
    assert calls == []                                   # the redirect reads nothing


def test_reader_failure_is_503_the_pages_fallback_signal(client, monkeypatch):
    def boom(**kw):
        raise RuntimeError('relation "distress_cases" does not exist')
    monkeypatch.setattr(pdx, "fetch_page", boom)
    monkeypatch.setattr(pdx, "fetch_facets", lambda: boom())
    assert client.get("/distress/api/cases").status_code == 503
    assert client.get("/distress/api/facets").status_code == 503


def test_standalone_page_is_gone():
    from pathlib import Path
    web = Path(__file__).resolve().parents[2] / "automation" / "web"
    assert not (web / "templates" / "distress_public.html").exists()
    assert not (web / "static" / "distress").exists()
    for tpl in ("_base.html", "deals_public.html"):
        assert 'href="/distress"' not in (web / "templates" / tpl).read_text()


def test_public_cols_exclude_contacts():
    for k in ("trustee", "attorneys", "parties", "zip_code"):
        assert k not in pdx.PUBLIC_COLS
        assert k in pdx.ADMIN_COLS


def test_build_where_binds_everything():
    where, args = pdx.build_where(q="inn", industry="hotel", state="ga", chapter="7", tab="sales")
    assert "%s" in where and "inn" not in where and "GA" in args and "sale_noticed_at IS NOT NULL" in where


def test_order_clause_allowlist():
    assert pdx.order_clause("drop table", None) == "ORDER BY date_filed DESC NULLS LAST, id DESC"
    assert pdx.order_clause("sale", "asc", "sales").startswith("ORDER BY sale_noticed_at ASC")
