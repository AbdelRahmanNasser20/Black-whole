"""Public /deals/sold ("Sold prices"): confirmed closes only, USD only, the same
chair-buyer isolation as /deals, and no private field in a response. DB-free."""
import importlib

import pytest
from fastapi.testclient import TestClient

from automation.web import auth as auth_svc
from automation.web import public_deals as pd
from automation.web import public_sold as ps

app_mod = importlib.import_module("automation.web.app")
app = app_mod.app

DATA = {
    "summary": {"sold": 120, "reserve_not_met": 80, "no_bid": 10, "sold_share": 60.0,
                "median": 410.0, "p25": 120.0, "p75": 1500.0,
                "since": "2026-09-06", "until": "2026-11-13"},
    "categories": [{"value": "vehicles", "sold": 40, "not_sold": 20, "sold_share": 66.7,
                    "median": 3200.0, "p25": 1500.0, "p75": 6100.0}],
    "rows": [{"source": "govdeals", "lot_key": "305/10340/1", "title": "Lot of (30) Laptops",
              "canonical_category": "computers_electronics", "city": "Houston", "state": "TX",
              "closed_at": "2026-11-01T18:00:00+00:00", "final_price": 300.0, "bid_count": 7,
              "viewer_url": "/deals/305/10340/1"}],
    "total": 1, "page": 1, "per_page": 50, "pages": 1,
}


@pytest.fixture(autouse=True)
def _auth_on(monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "hunter2-but-much-longer")
    monkeypatch.setenv("SESSION_SECRET", "unit-test-secret")
    auth_svc.reset_caches()
    yield
    auth_svc.reset_caches()


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(ps, "fetch_sold", lambda **kw: dict(DATA, echo=kw))
    return TestClient(app)


# ── policy: what the SQL may read ────────────────────────────────────────────

def test_base_where_keeps_chair_buyer_isolation():
    where, args = ps.base_where()
    assert "canonical_category <> ALL(%s)" in where
    assert "COALESCE(title, '') !~* %s" in where
    for tbl in ("tracked_lots", "auction_favorites", "deal_list_items"):
        assert f"FROM {tbl}" in where
    assert args[:2] == [sorted(pd.EXCLUDED_CATEGORIES), pd.EXCLUDED_TITLE_SQL_RE]


def test_base_where_is_usd_only():
    # final_price is never aggregated across currencies (listing CLAUDE.md, migration 019).
    where, args = ps.base_where()
    assert "currency = %s" in where and "USD" in args


def test_filters_are_parameterized_and_title_only():
    where, args = ps.build_where(q="laptop", category="vehicles", state="tx", days=30)
    assert "title ILIKE %s" in where and "%laptop%" in args
    assert "laptop" not in where and "vehicles" not in where
    assert "vehicles" in args and "TX" in args and 30 in args
    assert "description" not in where and "seller" not in where


def test_unknown_days_means_all_time():
    where, args = ps.build_where(days=7)
    assert "closed_at >=" not in where


def test_a_sale_is_a_confirmed_sold_outcome_only():
    # reserve_not_met / no_bid / unknown closes carry a top bid but are not sales.
    assert ps.SOLD == "outcome = 'sold' AND final_price IS NOT NULL"


def test_public_cols_never_leak_private_fields():
    for col in ("seller", "status_code", "photo_count", "raw", "description"):
        assert col not in ps.PUBLIC_COLS


def test_viewer_url_only_for_a_govdeals_three_part_key():
    assert ps.viewer_url("govdeals", "305/10340/1") == "/deals/305/10340/1"
    assert ps.viewer_url("govdeals", "305/10340") is None
    assert ps.viewer_url("allsurplus", "305/10340/1") is None
    assert ps.viewer_url("govdeals", "a/b/c") is None


def test_share_is_none_without_a_denominator():
    assert ps.share(0, 0) is None
    assert ps.share(60, 40) == 60.0


# ── routes ───────────────────────────────────────────────────────────────────

def test_api_is_open_without_a_session_and_passes_filters(client):
    r = client.get("/deals/api/sold?q=laptop&category=vehicles&state=TX&days=90&page=2")
    assert r.status_code == 200
    body = r.json()
    assert body["echo"] == {"q": "laptop", "category": "vehicles", "state": "TX",
                            "days": 90, "page": 2}
    assert body["summary"]["sold"] == 120


def test_api_reports_a_failed_read_as_503(monkeypatch):
    def boom(**kw):
        raise RuntimeError("db down")
    monkeypatch.setattr(ps, "fetch_sold", boom)
    assert TestClient(app).get("/deals/api/sold").status_code == 503


def test_page_renders_numbers_and_the_honesty_labels(client):
    r = client.get("/deals/sold")
    assert r.status_code == 200
    html = r.text
    assert "<title>Sold prices" in html
    assert "Lot of (30) Laptops" in html and 'href="/deals/305/10340/1"' in html
    assert "$410" in html                      # median
    assert "60%" in html                       # share of bid-on closes that sold
    assert "Confirmed close" in html
    assert "reserve" in html.lower()
    # hard rules shared with /deals: no photos, no private fields, noindex until the operator decides
    assert "<img" not in html and "storage_note" not in html
    assert 'name="robots" content="noindex' in html


def test_page_shows_a_plain_message_when_the_read_fails(monkeypatch):
    def boom(**kw):
        raise RuntimeError("db down")
    monkeypatch.setattr(ps, "fetch_sold", boom)
    r = TestClient(app).get("/deals/sold")
    assert r.status_code == 503
    assert "Sold prices are not available" in r.text


def test_deals_feed_links_to_sold_prices():
    assert 'href="/deals/sold"' in TestClient(app).get("/deals").text
