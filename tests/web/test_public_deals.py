"""Chair-buyer isolation + paging contract for the public /deals surface.
Pure SQL-building tests; fetch_* are covered via the endpoint tests in
tests/web/test_public_deals_api.py with the DB monkeypatched."""
import re

import pytest

from automation.web import public_deals as pd


def test_exclusion_where_names_every_operator_table():
    where, args = pd.exclusion_where()
    assert "canonical_category <> ALL(%s)" in where
    assert "COALESCE(title, '') !~* %s" in where
    for tbl in ("tracked_lots", "auction_favorites", "deal_list_items"):
        assert f"FROM {tbl}" in where
    assert args[0] == sorted(pd.EXCLUDED_CATEGORIES)
    assert args[1] == pd.EXCLUDED_TITLE_SQL_RE


def test_sql_regex_uses_postgres_word_boundary():
    # Postgres reads `\b` as backspace, so the Python-dialect regex would match
    # nothing in SQL and every chair lot would leak (live smoke, 2026-09-04).
    assert "\\b" not in pd.EXCLUDED_TITLE_SQL_RE and "\\y" in pd.EXCLUDED_TITLE_SQL_RE
    assert pd.EXCLUDED_TITLE_SQL_RE.replace("\\y", "\\b") == pd.EXCLUDED_TITLE_RE


def test_title_regex_blocks_seating_only():
    rx = re.compile(pd.EXCLUDED_TITLE_RE, re.I)
    assert rx.search("Lot of (199) Banquet Chairs")
    assert rx.search("Church pews - 40 sections")
    assert rx.search("Bar Stools, set of 12")
    assert not rx.search("Lot of (30) Lenovo Thinkpads T460")
    assert not rx.search("Wheelchair accessible van")  # 'chair' must be a whole word


def test_is_excluded_row():
    assert pd.is_excluded({"canonical_category": "seating_furniture", "title": "Desk"})
    assert pd.is_excluded({"canonical_category": "other", "title": "Stacking chairs x 200"})
    assert not pd.is_excluded({"canonical_category": "other", "title": "Vulcan fryer"})
    assert not pd.is_excluded({"canonical_category": None, "title": "Water plant pumps"})


def test_build_public_where_is_title_only_search():
    where, args = pd.build_public_where(q="laptop", status="active")
    assert "title ILIKE %s" in where and "description" not in where
    assert args.count("%laptop%") == 1


def test_public_order_rejects_private_sorts():
    assert pd.public_order("margin", None) == "ORDER BY end_utc ASC NULLS LAST"
    assert pd.public_order("bid", "asc") == "ORDER BY current_bid ASC NULLS LAST"
    assert pd.public_order("newest", None) == "ORDER BY first_seen_at DESC NULLS LAST"


def test_public_cols_never_leak_private_fields():
    for col in ("hero_image_url", "archived_hero_url", "gallery_urls", "description",
                "seller", "high_bidder", "raw"):
        assert col not in pd.PUBLIC_COLS


@pytest.mark.parametrize("page,per_page,expect", [(0, 25, (1, 25)), (5, 33, (5, 25)), (9999, 100, (400, 100))])
def test_page_clamping(page, per_page, expect):
    assert pd.clamp_page(page, per_page) == expect


# ───────────────────────── union of deal_lots + the other recorder sources ─────────────────────────
from datetime import datetime, timedelta, timezone  # noqa: E402

from automation.web import deals_sources as ds  # noqa: E402

NOW = datetime.now(timezone.utc)


def _gd(asset_id, *, bid, bids, hours, state="TX"):
    return {"asset_id": asset_id, "account_id": 10, "auction_id": 1, "title": f"Lot {asset_id}",
            "canonical_category": "computers_electronics", "native_category_name": "Computers",
            "city": "Houston", "state": state, "bid_count": bids, "current_bid": bid, "currency_code": "USD",
            "end_utc": NOW + timedelta(hours=hours), "outcome": None, "final_bid": None,
            "final_bid_count": None, "outcome_complete": False, "first_seen_at": NOW - timedelta(days=1),
            "lat": 29.76, "lng": -95.37}


def _snap(source, lot, *, bid, bids, hours, state="OK", lat=None, lng=None):
    return {"id": f"{source}:{lot}", "source": source, "source_name": ds.SITES[source], "source_lot_id": lot,
            "asset_id": None, "account_id": None, "auction_id": None, "title": f"Lot of (4) {lot}",
            "canonical_category": None, "native_category_name": None, "city": "Woodward", "state": state,
            "lat": lat, "lng": lng, "bid_count": bids, "current_bid": bid, "currency_code": "USD",
            "end_utc": NOW + timedelta(hours=hours), "outcome": None, "final_bid": None,
            "final_bid_count": None, "outcome_complete": False, "first_seen_at": NOW - timedelta(hours=2),
            "url": f"https://example.test/{lot}", "viewer_url": None}


class _Conn:
    """deal_lots rows for the page query, a count for the count query; records every SQL."""

    def __init__(self, rows):
        self.rows, self.seen = rows, []

    def execute(self, sql, params=()):
        self.seen.append((sql, params))
        if "FILTER" in sql:
            return type("Cur", (), {"fetchone": lambda _s: {"tracked": 100, "active": 50, "closed": 50, "no_bid": 5, "states": 3, "since": None}})()
        if "GROUP BY" in sql:
            col = "canonical_category" if "SELECT canonical_category AS value" in sql else "state"
            return type("Cur", (), {"fetchall": lambda _s: [{"value": "computers_electronics" if col == "canonical_category" else "TX", "count": len(self.rows)}]})()
        if "count(*)" in sql:
            return type("Cur", (), {"fetchone": lambda _s: {"c": len(self.rows)}})()
        limit = params[-1]
        return type("Cur", (), {"fetchall": lambda _s: [dict(r) for r in self.rows[:limit]]})()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


GD = [_gd(1, bid=10, bids=0, hours=1), _gd(2, bid=500, bids=3, hours=3), _gd(3, bid=None, bids=0, hours=9)]
SNAP = [_snap("gsa", "g1", bid=100, bids=1, hours=2, lat=36.4, lng=-99.4),
        _snap("purple_wave", "p1", bid=None, bids=None, hours=5)]


@pytest.fixture
def union(monkeypatch):
    conn = _Conn(GD)
    monkeypatch.setattr(pd.db, "connect", lambda *a, **k: conn)
    monkeypatch.setattr(pd.db, "fetch_all", lambda sql, params=(): [dict(r) for r in GD if r["lat"] is not None])
    monkeypatch.setattr(ds, "lots", lambda status="active", now=None: [dict(r) for r in SNAP])
    pd.clear_cache()
    yield conn
    pd.clear_cache()


def test_page_merges_both_halves_in_sort_order(union):
    body = pd.fetch_page(sort="ends")
    assert body["total"] == 5 and body["pages"] == 1
    assert [(r["source"], r.get("asset_id") or r.get("source_lot_id")) for r in body["rows"]] == [
        ("govdeals", 1), ("gsa", "g1"), ("govdeals", 2), ("purple_wave", "p1"), ("govdeals", 3)]
    gd, gsa = body["rows"][0], body["rows"][1]
    assert gd["url"] == gd["govdeals_url"] and gd["viewer_url"] == "/deals/1/10/1" and gd["landed_cost"] == 11.25
    assert gsa["viewer_url"] is None and gsa["landed_cost"] is None            # no invented buyer premium
    assert (gsa["quantity"], gsa["quantity_source"], gsa["unit_bid"]) == (4, "title", 25.0)   # lot_quantity ran on the snapshot title
    # bid sorts: NULLS LAST on both halves, in both directions
    assert [r.get("current_bid") for r in pd.fetch_page(sort="bid", dir="desc")["rows"]] == [500, 100, 10, None, None]
    assert [r.get("current_bid") for r in pd.fetch_page(sort="bid", dir="asc")["rows"]] == [10, 100, 500, None, None]
    assert [r["source"] for r in pd.fetch_page(sort="newest")["rows"]][:2] == ["gsa", "purple_wave"]


def test_paging_pulls_top_n_from_sql_and_slices_the_union(union, monkeypatch):
    p1 = pd.fetch_page(sort="ends", page=1, per_page=25)
    sql, params = [x for x in union.seen if "LIMIT %s" in x[0]][-1]
    assert params[-1] == 25 and "OFFSET" not in sql and len(p1["rows"]) == 5
    union.seen.clear()
    monkeypatch.setattr(pd, "PER_PAGE_CHOICES", (2, 25, 50, 100))
    p2 = pd.fetch_page(sort="ends", page=2, per_page=2)
    assert [x for x in union.seen if "LIMIT %s" in x[0]][-1][1][-1] == 4       # N = page × per_page
    assert [r.get("asset_id") or r["source_lot_id"] for r in p2["rows"]] == [2, "p1"]
    assert p2["total"] == 5 and p2["pages"] == 3 and len(pd.fetch_page(page=3, per_page=2)["rows"]) == 1


def test_site_filter_selects_one_half(union):
    only_gd = pd.fetch_page(site="govdeals")
    assert only_gd["total"] == 3 and {r["source"] for r in only_gd["rows"]} == {"govdeals"}
    union.seen.clear()
    only_gsa = pd.fetch_page(site="gsa")
    assert union.seen == []                                                    # deal_lots is not even queried
    assert only_gsa["total"] == 1 and only_gsa["rows"][0]["source"] == "gsa"


def test_pins_union_keeps_only_mapped_points(union):
    pins = pd.fetch_pins()
    assert [(p["source"], p.get("asset_id") or p.get("source_lot_id")) for p in pins["points"]] == [
        ("govdeals", 1), ("gsa", "g1"), ("govdeals", 2), ("govdeals", 3)]
    gd, gsa = pins["points"][0], pins["points"][1]
    assert gd["viewer_url"] == "/deals/1/10/1" and gd["url"] == gd["govdeals_url"]
    assert gsa["viewer_url"] is None and gsa["url"] == "https://example.test/g1" and gsa["source_name"] == "GSA Auctions"
    assert "raw" not in gsa and pins["capped"] is False
    assert {p["source"] for p in pd.fetch_pins(site="gsa")["points"]} == {"gsa"}


def test_facets_union_adds_sites_states_and_live_counts(union):
    f = pd.fetch_facets()
    assert [(s["value"], s["count"]) for s in f["sites"]] == [("govdeals", 3), ("gsa", 1), ("purple_wave", 1)]
    assert f["sites"][1]["name"] == "GSA Auctions"
    assert {s["value"]: s["count"] for s in f["states"]} == {"TX": 3, "OK": 2}
    assert f["stats"]["active"] == 52 and f["stats"]["states"] == 2
    assert [c["value"] for c in f["categories"]] == ["computers_electronics"]


def test_facets_survive_a_failed_snapshot_read(union, monkeypatch):
    def boom(status="active", now=None):
        raise RuntimeError("backing off")

    monkeypatch.setattr(ds, "lots", boom)
    f = pd.fetch_facets()
    assert f["sites"] == [{"value": "govdeals", "name": "GovDeals", "count": 3}] and f["stats"]["active"] == 50
