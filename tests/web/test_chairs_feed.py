"""automation/web/chairs_feed.py + GET /api/deals/chairs + `deals.cli chairs`.

DB-free (readers monkeypatched). What must hold:
  · partition: a live lot is in exactly one of public /deals or the chairs
    feed — same constants, same test, both halves (GovDeals + snapshots);
  · operator picks: hidden from /deals always, shown (flagged) in chairs;
  · min_qty filters on the read-time quantity; unknown counts only at 0;
  · ranking: $/chair ascending, NULL bid last, ties by soonest end;
  · the endpoint is admin-walled, plain `def`, readcache-memoised.
"""
import importlib
import inspect
import io
import json
from argparse import Namespace
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from automation.web import auth as auth_svc
from automation.web import chairs_feed as cf
from automation.web import deals_sources as ds
from automation.web import public_deals as pd
from automation.web import readcache

app_mod = importlib.import_module("automation.web.app")
app = app_mod.app

NOW = datetime.now(timezone.utc)
NO_PICKS = pd.OperatorPicks()

# (lot id, title, category, bid, hours to close) — municibid rows, so the url builds from a numeric id
SNAP = [
    ("101", "Lot of 120 Banquet Chairs", None, 240.0, 5),          # seating, qty 120, $2/ea
    ("102", "150 Student Chairs", None, 600.0, 3),                 # seating via chair noun count, $4/ea
    ("103", "Stacking chairs", None, 10.0, 2),                     # seating, no count
    ("104", "Lot of (60) Church Pews", None, None, 1),             # seating, qty 60, no bid
    ("105", "Lot of (30) Dell Laptops", None, 300.0, 4),           # not seating
    ("106", "Folding tables", "Furniture", 40.0, 6),               # not seating
    ("107", "Lot of 80 Mixed", "Seating", 160.0, 7),               # seating by CATEGORY TEXT ("seating" word)
]


def _snap_row(lot_id, title, category, bid, hours, **kw):
    fields = ds.extract("municibid", {"title": title, "city": "Phoenix", "state": "AZ"})
    fields["category"] = category
    return {"source": "municibid", "source_lot_id": lot_id, "current_bid": bid, "bid_count": 0 if bid is None else 2,
            "end_date": NOW + timedelta(hours=hours), "first_seen": NOW - timedelta(days=1), **fields, **kw}


# GovDeals rows as deal_lots would hold them; the fake reader below applies the
# Python twin of `seating_where` / `exclusion_where`.
GD = [
    {"asset_id": 1, "account_id": 10, "auction_id": 100, "title": "Banquet chairs", "description": "Lot of 200 chairs.",
     "canonical_category": "seating_furniture", "city": "Mesa", "state": "AZ", "bid_count": 3, "current_bid": 400.0,
     "end_utc": NOW + timedelta(hours=2), "operator_pick": False},
    {"asset_id": 2, "account_id": 10, "auction_id": 100, "title": "Lot of (100) Stack Chairs", "description": None,
     "canonical_category": "office_furniture", "city": "Tempe", "state": "AZ", "bid_count": 1, "current_bid": 200.0,
     "end_utc": NOW + timedelta(hours=9), "operator_pick": True},
    {"asset_id": 3, "account_id": 10, "auction_id": 100, "title": "Lot of (20) Desks", "description": None,
     "canonical_category": "office_furniture", "city": "Tempe", "state": "AZ", "bid_count": 1, "current_bid": 20.0,
     "end_utc": NOW + timedelta(hours=9), "operator_pick": False},
]


def _gd_public():
    return [r for r in GD if not pd.is_excluded(r) and not r["operator_pick"]]


def _gd_seating(*, state=None, ending_within=None):
    """Mirrors the SQL: seating_where + the state / ending filters."""
    edge = NOW + timedelta(hours=ending_within) if ending_within is not None else None
    return [dict(r) for r in GD if pd.is_seating(r)
            and (not state or r["state"] == state) and (edge is None or r["end_utc"] <= edge)]


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    monkeypatch.setattr(ds.geo, "resolve_place", lambda c, s, z: (33.45, -112.07, "city"))
    monkeypatch.setattr(ds, "_read", lambda status: ([_snap_row(*s) for s in SNAP] if status == "open" else [], NO_PICKS))
    monkeypatch.setattr(cf, "_govdeals_rows", _gd_seating)
    ds.clear_cache()
    readcache.invalidate_all()
    yield
    ds.clear_cache()
    readcache.invalidate_all()


# ───────────────────────── partition ─────────────────────────

def test_every_live_lot_is_in_exactly_one_view():
    public = {r["id"] for r in ds.lots("active")} | {f"govdeals:{r['asset_id']}/{r['account_id']}/{r['auction_id']}"
                                                     for r in _gd_public()}
    chairs = {r["id"] for r in cf.fetch(min_qty=0)["rows"]}
    all_ids = {f"municibid:{s[0]}" for s in SNAP} | {f"govdeals:{r['asset_id']}/{r['account_id']}/{r['auction_id']}"
                                                     for r in GD}
    assert not public & chairs
    # the only lot in neither is impossible here: GD #2 is an operator pick AND seating → chairs
    assert public | chairs == all_ids
    assert chairs == {"municibid:101", "municibid:102", "municibid:103", "municibid:104", "municibid:107",
                      "govdeals:1/10/100", "govdeals:2/10/100"}


def test_seating_sql_is_the_inverse_of_the_public_exclusion():
    ex_sql, ex_args = pd.exclusion_where()
    seat_sql, seat_args = pd.seating_where()
    assert seat_args == ex_args[:2]                                       # same category list, same \y regex
    assert "canonical_category = ANY(%s)" in seat_sql and "<> ALL(%s)" in ex_sql
    assert "~* %s" in seat_sql and "!~* %s" in ex_sql
    assert "\\b" not in seat_args[1]                                      # Postgres reads \b as backspace


def test_operator_pick_is_flagged_in_chairs_and_hidden_from_public(monkeypatch):
    picks = pd.OperatorPicks(ids=["101", "105"])
    monkeypatch.setattr(ds, "_read", lambda status: ([_snap_row(*s) for s in SNAP] if status == "open" else [], picks))
    ds.clear_cache()
    public = {r["id"] for r in ds.lots("active")}
    assert "municibid:101" not in public and "municibid:105" not in public    # picks never public
    rows = {r["id"]: r for r in cf.fetch(min_qty=0)["rows"]}
    assert rows["municibid:101"]["operator_pick"] is True
    assert "municibid:105" not in rows                                          # a non-seating pick is not a chair lot
    assert rows["govdeals:2/10/100"]["operator_pick"] is True


# ───────────────────────── quantity + min_qty ─────────────────────────

def test_best_quantity_prefers_named_patterns_then_noun_count():
    assert cf.best_quantity("Lot of (100) Stack Chairs") == (100, "title")
    assert cf.best_quantity("150 Student Chairs") == (150, "title_noun")
    assert cf.best_quantity("Banquet chairs", "Lot of 200 chairs.") == (200, "description")
    assert cf.best_quantity("Banquet chairs", "About 75 padded chairs here") == (75, "description_noun")
    assert cf.best_quantity("Stacking chairs") == (None, "unknown")          # never a default 1
    assert cf.best_quantity("UMF Medical 8678 Power Phlebotomy Chair") == (None, "unknown")   # model no., singular


def test_min_qty_filters_and_zero_shows_unknown_counts():
    by = lambda n: {r["id"]: r["quantity"] for r in cf.fetch(min_qty=n)["rows"]}  # noqa: E731
    assert by(50) == {"municibid:101": 120, "municibid:102": 150, "municibid:104": 60, "municibid:107": 80,
                      "govdeals:1/10/100": 200, "govdeals:2/10/100": 100}
    assert set(by(100)) == {"municibid:101", "municibid:102", "govdeals:1/10/100", "govdeals:2/10/100"}
    assert by(0)["municibid:103"] is None                                    # unknown count only at 0
    assert "municibid:103" not in by(1)
    assert cf.fetch(min_qty="junk")["min_qty"] == 50


# ───────────────────────── ranking ─────────────────────────

def test_rank_unit_bid_ascending_nulls_last_ties_by_soonest_end():
    rows = cf.fetch(min_qty=50)["rows"]
    assert [r["id"] for r in rows] == [
        "govdeals:1/10/100",   # $400 / 200 = $2.00, ends in 2 h
        "municibid:101",       # $240 / 120 = $2.00, ends in 5 h   (tie → sooner first)
        "municibid:107",       # $160 / 80  = $2.00, ends in 7 h
        "govdeals:2/10/100",   # $200 / 100 = $2.00, ends in 9 h
        "municibid:102",       # $600 / 150 = $4.00
        "municibid:104",       # no bid → last
    ]
    units = [r["unit_bid"] for r in rows]
    present = [u for u in units if u is not None]
    assert present == sorted(present) and units[-1] is None and rows[-1]["id"] == "municibid:104"
    ties = [r for r in rows if r["unit_bid"] == 2.0]
    assert [r["end_utc"] for r in ties] == sorted(r["end_utc"] for r in ties)
    gd = next(r for r in rows if r["source"] == "govdeals")
    assert gd["landed_cost"] is not None and gd["unit_landed"] is not None and gd["viewer_url"].startswith("/deals/")
    snap = next(r for r in rows if r["source"] == "municibid")
    assert snap["landed_cost"] is None and snap["url"] == "https://municibid.com/Listing/Details/101"
    assert set(rows[0]) == set(cf.ROW_KEYS)


def test_site_state_and_ending_filters_and_partial_failure(monkeypatch):
    assert {r["source"] for r in cf.fetch(site="govdeals")["rows"]} == {"govdeals"}
    assert {r["source"] for r in cf.fetch(site="municibid")["rows"]} == {"municibid"}
    assert cf.fetch(state="tx")["rows"] == []
    assert {r["state"] for r in cf.fetch(state="az")["rows"]} == {"AZ"}
    soon = {r["id"] for r in cf.fetch(min_qty=50, ending_within=4)["rows"]}
    assert "municibid:101" not in soon and "municibid:102" in soon

    def boom(**_):
        raise RuntimeError("db down")
    monkeypatch.setattr(cf, "_govdeals_rows", boom)
    out = cf.fetch()
    assert out["errors"] and all(r["source"] != "govdeals" for r in out["rows"])
    with pytest.raises(RuntimeError):
        cf.fetch(site="govdeals")


# ───────────────────────── endpoint ─────────────────────────

def test_endpoint_is_sync_cached_and_admin_walled(monkeypatch):
    route = next(r for r in app.routes if getattr(r, "path", None) == "/api/deals/chairs")
    assert not inspect.iscoroutinefunction(route.endpoint)
    assert hasattr(route.endpoint, "__wrapped__")                             # @readcache.cached()
    monkeypatch.setenv("ADMIN_PASSWORD", "hunter2-but-much-longer")
    monkeypatch.setenv("SESSION_SECRET", "unit-test-secret")
    auth_svc.reset_caches()
    try:
        assert TestClient(app).get("/api/deals/chairs").status_code == 401
    finally:
        auth_svc.reset_caches()


def test_endpoint_returns_ranked_rows_and_validates(monkeypatch):
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    auth_svc.reset_caches()
    c = TestClient(app)
    body = c.get("/api/deals/chairs?min_qty=100").json()
    assert body["min_qty"] == 100 and body["total"] == 4 and body["site_names"]["govdeals"] == "GovDeals"
    assert c.get("/api/deals/chairs?site=nope").status_code == 400
    assert c.get("/api/deals/chairs?sort=nope").status_code == 400
    # the public surface never serves it
    assert c.get("/deals/api/chairs").status_code == 404
    auth_svc.reset_caches()


# ───────────────────────── CLI ─────────────────────────

def _args(**kw):
    base = dict(min_qty=50, site=None, state=None, ending=None, sort="unit_bid", limit=None, json=False)
    return Namespace(**{**base, **kw})


def test_cli_table_and_json():
    from deals import cli
    buf = io.StringIO()
    assert cli.run_chairs(_args(limit=2), out=buf) == 0
    text = buf.getvalue()
    assert text.startswith("6 live seating lot(s) with qty >= 50")
    assert "  1.     $2.00/ea" in text and "Banquet chairs" in text and "  3." not in text
    buf = io.StringIO()
    cli.run_chairs(_args(json=True, site="municibid", min_qty=100), out=buf)
    data = json.loads(buf.getvalue())
    assert [r["id"] for r in data["rows"]] == ["municibid:101", "municibid:102"]
    with pytest.raises(SystemExit):
        cli.run_chairs(_args(site="nope"), out=io.StringIO())


def test_cli_parser_dispatches_chairs_before_the_adapter(monkeypatch):
    from deals import cli
    seen = {}
    monkeypatch.setattr(cli, "run_chairs", lambda a: seen.update(vars(a)) or 0)
    monkeypatch.setattr(cli.sites, "get_adapter", lambda s: pytest.fail("adapter built for chairs"))
    monkeypatch.setattr("sys.argv", ["deals.cli", "chairs", "--min-qty", "80", "--site", "gsa", "--json"])
    cli.main()
    assert seen["min_qty"] == 80 and seen["site"] == "gsa" and seen["json"] is True
