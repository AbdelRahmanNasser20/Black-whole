"""Deals sitemap: built off the request path, chunked, policy-filtered. DB-free."""
import importlib
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from automation.web import deal_sitemap as sm
from automation.web import deals_sources as ds

app_mod = importlib.import_module("automation.web.app")
BASE = "https://x.test"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    sm.clear_cache()
    monkeypatch.setattr(sm, "_spawn", lambda: None)          # no thread in tests
    yield
    sm.clear_cache()


def _load(urls):
    sm._state.update(urls=list(urls), at=sm.time.monotonic(), busy=False)


def test_a_request_never_reads_the_database(monkeypatch):
    spawned = []
    monkeypatch.setattr(sm, "_spawn", lambda: spawned.append(1))
    monkeypatch.setattr(sm.db, "fetch_all", lambda *a, **k: (_ for _ in ()).throw(AssertionError("DB on the request path")))
    monkeypatch.setattr(ds, "lots", lambda *a, **k: (_ for _ in ()).throw(AssertionError("memo read on the request path")))
    assert sm.urls() == [] and spawned == [1]                # cold: empty answer, one rebuild started
    assert sm.urls() == [] and spawned == [1]                # still busy: no second rebuild
    assert "<loc>https://x.test/sitemap.xml</loc>" in sm.index_xml(BASE)
    assert "<loc>https://x.test/deals</loc>" in sm.child_xml(BASE, 1)
    client = TestClient(app_mod.app)
    assert client.get("/sitemap-index.xml").status_code == 200
    assert client.get("/sitemaps/deals-1.xml").headers["content-type"].startswith("application/xml")
    assert client.get("/sitemaps/deals-2.xml").status_code == 404


def test_children_are_chunked_and_the_index_lists_each(monkeypatch):
    monkeypatch.setattr(sm, "CHUNK", 2)
    _load([(f"/deals/lot/l{i}-tx-govdeals-{i}.1.1", "2026-11-14" if i % 2 else None) for i in range(5)])
    assert sm.children() == 3
    index = sm.index_xml(BASE)
    assert index.count("<sitemap>") == 4 and "/sitemaps/deals-3.xml" in index and "/sitemaps/deals-4.xml" not in index
    first, last = sm.child_xml(BASE, 1), sm.child_xml(BASE, 3)
    assert first.count("<url>") == 4                          # /deals + /deals/sold + 2 lots
    assert "<loc>https://x.test/deals/sold</loc>" in first and "<lastmod>2026-11-14</lastmod>" in first
    assert last.count("<url>") == 1 and "/deals/sold" not in last
    assert sm.child_xml(BASE, 0) is None and sm.child_xml(BASE, 4) is None


def test_collect_reads_three_public_sets_through_the_policy(monkeypatch):
    seen = []

    def fetch_all(sql, params=()):
        seen.append((sql, params))
        if "FROM deal_lots" in sql:
            return [{"site": "govdeals", "native_id": "1/2/3", "title": "Desk", "state": "AZ",
                     "updated_at": datetime(2026, 11, 14, tzinfo=timezone.utc)},
                    {"site": "ebay", "native_id": "9", "title": "no page", "state": "AZ", "updated_at": None}]
        return [{"source": "allsurplus", "lot_key": "7/8/9", "title": "Lathe", "state": None,
                 "closed_at": datetime(2026, 11, 1, tzinfo=timezone.utc)},
                {"source": "govdeals", "lot_key": "1/2/3", "title": "Desk", "state": "AZ", "closed_at": None}]
    monkeypatch.setattr(sm.db, "fetch_all", fetch_all)
    monkeypatch.setattr(ds, "lots", lambda status="active", **k: [
        {"viewer_url": "/deals/lot/mower-pa-municibid-5", "first_seen_at": None}, {"viewer_url": None}])
    assert sm.collect() == [("/deals/lot/desk-az-govdeals-1.2.3", "2026-11-14"),
                            ("/deals/lot/mower-pa-municibid-5", None),
                            ("/deals/lot/lathe-us-allsurplus-7.8.9", "2026-11-01")]
    live, sold = seen
    assert "outcome_complete IS NOT TRUE AND end_utc > now()" in live[0]
    for sql in (live[0], sold[0]):
        for tbl in ("tracked_lots", "auction_favorites", "deal_list_items"):
            assert f"FROM {tbl}" in sql                       # an operator pick is never listed
    assert "outcome = 'sold' AND final_price IS NOT NULL" in sold[0] and "currency = %s" in sold[0]
    assert sold[1][-2:] == (sm.SOLD_DAYS, 50_000)


def test_one_failed_read_keeps_the_rest_and_a_total_failure_keeps_the_old_list(monkeypatch):
    monkeypatch.setattr(sm.db, "fetch_all", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db down")))
    monkeypatch.setattr(ds, "lots", lambda status="active", **k: [{"viewer_url": "/deals/lot/a-pa-municibid-5", "first_seen_at": None}])
    assert sm.collect() == [("/deals/lot/a-pa-municibid-5", None)]
    monkeypatch.setattr(ds, "lots", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("memo down")))
    with pytest.raises(RuntimeError):
        sm.collect()
    _load([("/deals/lot/kept-az-govdeals-1.2.3", None)])
    sm._state["busy"] = True
    sm._refresh()
    assert sm._state["urls"] == [("/deals/lot/kept-az-govdeals-1.2.3", None)] and sm._state["busy"] is False


def test_url_cap_comes_from_env(monkeypatch):
    monkeypatch.setenv("DEAL_SITEMAP_MAX_URLS", "2")
    monkeypatch.setattr(sm.db, "fetch_all", lambda *a, **k: [])
    monkeypatch.setattr(ds, "lots", lambda status="active", **k: [
        {"viewer_url": f"/deals/lot/a-pa-municibid-{i}", "first_seen_at": None} for i in range(5)])
    assert len(sm.collect()) == 2
