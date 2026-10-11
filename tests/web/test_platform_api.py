"""GET /platform/api/auctions + /platform/api/sites — the public, read-only
JSON behind the /platform landing page (automation/web/platform_api.py).

DB-free. What must hold:
  · response shape is the contract the page codes against;
  · the public_deals policy applies to recorder rows too: no seating lots, and
    nothing in tracked_lots / auction_favorites / deal_list_items;
  · allow-list: no seller contact (GSA coEmail/coPhone …), no photo, no raw;
  · a failed read answers `ok: false` with no items and no error text;
  · a site with no adapter is `planned` with `lots: null` — never shown as scraped.
"""
import inspect
import json
import re
import sys
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from automation.web import auth as auth_svc
from automation.web import platform_api as api
from automation.web import public_deals as pd
from automation.web.app import app

app_module = sys.modules["automation.web.app"]
NOW = datetime.now(timezone.utc)
NO_PICKS = pd.OperatorPicks()
TOP_KEYS = {"ok", "as_of", "total", "page", "per_page", "items", "facets"}


def _row(source="govdeals", lot="100/200/1", title="Lot of (30) Laptops", **kw):
    base = {"source": source, "source_lot_id": lot, "current_bid": 50, "bid_count": 2,
            "end_date": NOW + timedelta(hours=5), "first_seen": NOW - timedelta(days=1),
            "title": title, "category": "Computers", "category_code": "29",
            "city": "Houston", "state": "TX", "country": "USA", "currency": "USD",
            "ref_a": None, "ref_b": None}
    return {**base, **kw}


ROWS = [
    _row(),
    _row(lot="101/200/1", title="Dump Truck", category="Trucks", current_bid=9000, bid_count=0,
         end_date=NOW + timedelta(days=3), first_seen=NOW - timedelta(hours=1), city="Mesa", state="AZ"),
    _row("gsa", "1-1-QSC-I-26-329-023", "2009 CHEVROLET SILVERADO", category=None, category_code=None,
         city="DAWSON", state="GA", country=None, currency=None, current_bid=4300, bid_count=8,
         end_date=NOW + timedelta(days=10), ref_a="https://www.gsaauctions.gov/auctions/preview/378156",
         # what a careless `SELECT raw` would have carried along:
         coEmail="officer@gsa.gov", coPhone="555-867-5309", contractOfficer="Some Person",
         imageURL="https://www.ppms.gov/x.jpg", propertyAddr1="1011 Forrester Drive",
         raw={"coEmail": "officer@gsa.gov"}),
    _row("purple_wave", "936901", "(4) Picnic Table Sets", category="Furniture", category_code=None,
         city="Jennings", state="OK", country=None, currency=None, current_bid=None, bid_count=None,
         end_date=NOW + timedelta(days=20),
         ref_a="261021", ref_b="FK5255", item_contact="Jo 555-111-2222", image_url="https://cdn/x"),
]


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("PLATFORM_PUBLIC_SOURCES", raising=False)
    monkeypatch.setattr(api.geo, "resolve_place", lambda c, s, z: (
        (29.76, -95.37, "city") if c == "Houston" else (33.0, -112.0, "state")))
    auth_svc.reset_caches()
    api.clear_cache()
    yield
    api.clear_cache()
    auth_svc.reset_caches()


@pytest.fixture
def reads(monkeypatch):
    """Fake the one DB read; `calls` counts how often it ran."""
    calls = []

    def fake(status):
        calls.append(status)
        return ([dict(r) for r in ROWS] if status == "open" else []), NO_PICKS

    monkeypatch.setattr(api, "_read", fake)
    return calls


def _get(path="/platform/api/auctions", **params):
    return TestClient(app, base_url="https://testserver").get(path, params=params)


# ───────────────────────── shape ─────────────────────────

def test_auctions_shape_matches_the_contract(reads):
    r = _get()
    assert r.status_code == 200
    body = r.json()
    assert set(body) == TOP_KEYS and body["ok"] is True
    assert (body["total"], body["page"], body["per_page"]) == (4, 1, 50)
    datetime.fromisoformat(body["as_of"])
    for item in body["items"]:
        assert tuple(item) == api.ITEM_KEYS
    assert set(body["facets"]) == {"sites", "categories", "states"}
    for group in body["facets"].values():
        assert all(set(f) == {"key", "name", "count"} for f in group)
    assert {"key": "govdeals", "name": "GovDeals", "count": 2} in body["facets"]["sites"]

    first = body["items"][0]                       # default sort: ending soonest
    assert first == {
        "id": "govdeals:100/200/1", "site": "govdeals", "site_name": "GovDeals",
        "title": "Lot of (30) Laptops", "category": "Computers", "city": "Houston", "state": "TX",
        "lat": 29.76, "lng": -95.37, "current_bid": 50.0, "bid_count": 2,
        "ends_at": first["ends_at"], "status": "open", "final_price": None, "closed_at": None,
        "url": "https://www.govdeals.com/en/asset/100/200",
    }
    by = {i["site"]: i for i in body["items"]}
    assert by["gsa"]["url"] == "https://www.gsaauctions.gov/auctions/preview/378156"
    assert by["gsa"]["city"] == "Dawson"
    assert by["purple_wave"]["url"] == "https://www.purplewave.com/auction/261021/item/FK5255"
    assert by["purple_wave"]["current_bid"] is None and by["purple_wave"]["bid_count"] is None


def test_pins_are_city_level_or_null(reads):
    items = _get().json()["items"]
    assert [(i["lat"], i["lng"]) for i in items if i["city"] == "Houston"] == [(29.76, -95.37)]
    # a state-centroid fallback is not a city: no pin rather than a wrong one
    assert all(i["lat"] is None and i["lng"] is None for i in items if i["city"] != "Houston")


def test_filters_sort_and_paging(reads):
    def ids(**p):
        return [i["id"].split(":", 1)[1] for i in _get(**p).json()["items"]]

    assert ids(site="gsa") == ["1-1-QSC-I-26-329-023"]
    assert ids(state="az") == ["101/200/1"]
    assert ids(category="computers") == ["100/200/1"]
    assert ids(q="dump  TRUCK") == ["101/200/1"]
    assert ids(no_bids=1) == ["101/200/1"]                    # bid_count == 0 only; null is not "no bids"
    assert ids(max_bid=100) == ["100/200/1"]                  # a null bid never passes a price cap
    assert ids(ending="24h") == ["100/200/1"]
    assert ids(ending="7d") == ["100/200/1", "101/200/1"]
    assert ids(sort="bid_low")[:3] == ["100/200/1", "1-1-QSC-I-26-329-023", "101/200/1"]
    assert ids(sort="bids")[0] == "1-1-QSC-I-26-329-023"
    assert ids(sort="newest")[0] == "101/200/1"
    page2 = _get(per_page=3, page=2).json()
    assert (page2["total"], page2["page"], page2["per_page"], len(page2["items"])) == (4, 2, 3, 1)
    assert _get(per_page=5000).json()["per_page"] == api.MAX_PER_PAGE
    assert _get(sort="nonsense", status="nonsense").json()["ok"] is True


def test_closed_is_never_substituted_for_open(monkeypatch):
    closed = _row(lot="7/8/1", title="Sold Forklift", end_date=NOW - timedelta(days=2), bid_count=3, current_bid=1200)
    monkeypatch.setattr(api, "_read", lambda status: (([closed] if status == "closed" else []), NO_PICKS))
    opened = _get().json()
    assert opened["ok"] is True and opened["total"] == 0 and opened["items"] == []
    item = _get(status="closed").json()["items"][0]
    assert item["status"] == "closed" and item["final_price"] == 1200.0 and item["closed_at"] == item["ends_at"]
    # an open lot whose end passed while the memo was warm is dropped, not shown as open
    stale = _row(lot="9/9/1", end_date=NOW - timedelta(minutes=1))
    api.clear_cache()
    monkeypatch.setattr(api, "_read", lambda status: ([stale], NO_PICKS))
    assert _get().json()["total"] == 0


def test_closed_lot_with_no_bids_has_no_final_price():
    rec = api._normalise(_row(bid_count=0, current_bid=25, end_date=NOW - timedelta(days=1)), "closed", NO_PICKS)
    assert rec["final_price"] is None and rec["current_bid"] == 25.0


# ───────────────────────── exclusion policy ─────────────────────────

@pytest.mark.parametrize("row", [
    _row(title="Lot of 40 Banquet Chairs"),                            # seating word in the title
    _row(title="Office surplus", category="Chairs"),                   # seating word in the native category
    _row(title="Assorted items", category="Furniture", category_code="47B"),   # maestro seating category code
    _row(country="GBR", currency="GBP"),                               # not USD: no currency field to say so
    _row(country="CAN", currency="USD"),
    _row(title="   "),                                                 # no title
    _row(city=None, state="ZA-WC"),                                    # no usable place
    _row("municibid", "55", "Pickup truck"),                           # no normaliser for this source
])
def test_policy_drops_the_lot(row):
    assert api._normalise(row, "open", NO_PICKS) is None


def test_operator_picks_are_dropped_for_every_source():
    picks = pd.OperatorPicks(
        pairs={(100, 200)}, ids={"936901", "abc-uuid"},
        urls={pd._norm_url("https://www.gsaauctions.gov/auctions/preview/378156/")},
        titles={pd._norm_title("Dump  truck!")})
    kept = api._build([dict(r) for r in ROWS] + [_row(lot="555/666/1", title="Generator")], "open", picks)
    assert [k["id"] for k in kept] == ["govdeals:555/666/1"]
    # same asset/account under another maestro site, or another auction number, is still the pick
    assert api._normalise(_row("allsurplus", "100/200/7"), "open", picks) is None
    assert pd.is_operator_pick(picks, source_lot_id="bs:abc-uuid")
    assert pd.is_operator_pick(picks, source_lot_id="x", url="https://www.govdeals.com/en/asset/100/200")
    assert not pd.is_operator_pick(picks, source_lot_id="555/666/1", title="Generator", url=None)


class _Conn:
    def __init__(self, tables, fail=None):
        self.tables, self.fail, self.seen = tables, fail, []

    def execute(self, sql, params=()):
        self.seen.append((sql, params))
        for name, rows in self.tables.items():
            if f"FROM {name}" in sql:
                if name == self.fail:
                    raise RuntimeError("relation unavailable")
                return type("Cur", (), {"fetchall": lambda _self, rows=rows: rows})()
        raise AssertionError(sql)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


PICK_TABLES = {
    "tracked_lots": [{"asset_id": 100, "account_id": 200, "title": "Tracked Thing",
                      "url": "https://www.govdeals.com/en/asset/100/200"}],
    "auction_favorites": [
        {"asset_id": "300/400", "link": "https://www.govdeals.com/en/asset/300/400", "title": "Fav One"},
        {"asset_id": "bs:abc-uuid", "link": "https://www.bidspotter.com/en-us/lot-abc-uuid", "title": "Fav Two"}],
    "deal_list_items": [{"asset_id": 500, "account_id": 600}],
}


def test_load_operator_picks_reads_all_three_tables_bounded():
    conn = _Conn(PICK_TABLES)
    picks = pd.load_operator_picks(conn)
    assert picks.pairs == {(100, 200), (300, 400), (500, 600)}
    assert {"300/400", "bs:abc-uuid", "abc-uuid"} <= picks.ids
    assert "bidspotter.com/en-us/lot-abc-uuid" in picks.urls
    assert picks.titles == {"tracked thing", "fav one", "fav two"}
    assert len(conn.seen) == 3
    for sql, params in conn.seen:                       # parameterised + bounded, read-only
        assert sql.lstrip().upper().startswith("SELECT") and "LIMIT %s" in sql and params == (pd.PICKS_ROW_CAP,)


def test_unreadable_pick_table_publishes_nothing(monkeypatch):
    """If the picks cannot be loaded the lots are never served unfiltered."""
    ran = []

    class Conn(_Conn):
        def execute(self, sql, params=()):
            if "listing_snapshots" in sql:
                ran.append(sql)
                return type("Cur", (), {"fetchall": lambda _s: [dict(r) for r in ROWS]})()
            return super().execute(sql, params)

    monkeypatch.setattr(api.db, "connect", lambda *a, **k: Conn(PICK_TABLES, fail="auction_favorites"))
    body = _get().json()
    assert body["ok"] is False and body["items"] == [] and body["total"] == 0
    assert ran == []                                     # picks are read first; the lots query never ran


def test_read_applies_picks_end_to_end(monkeypatch):
    class Conn(_Conn):
        def execute(self, sql, params=()):
            if "listing_snapshots" in sql:
                assert "%s" in sql and params[0] == list(api._NORMALISED) and api.ROW_CAP in params
                return type("Cur", (), {"fetchall": lambda _s: [dict(r) for r in ROWS]})()
            return super().execute(sql, params)

    monkeypatch.setattr(api.db, "connect", lambda *a, **k: Conn(PICK_TABLES))
    ids = [i["id"] for i in _get().json()["items"]]
    assert "govdeals:100/200/1" not in ids and "govdeals:101/200/1" in ids and len(ids) == 3


# ───────────────────────── allow-list ─────────────────────────

_CONTACT_KEY = re.compile(r"email|phone|contact|officer|addr|image|photo|raw|seller", re.I)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.\w+")
_PHONE = re.compile(r"\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}")


def test_no_contact_field_or_value_can_leave(reads, monkeypatch):
    dirty = _row(lot="77/88/1", title="Skid steer - call Bob 555-123-4567 or bob@seller.com",
                 city="houston tx bob@seller.com", category="Loaders (602) 555-0000")
    monkeypatch.setattr(api, "_read", lambda status: ([dict(r) for r in ROWS] + [dirty], NO_PICKS))
    body = _get().json()
    assert body["total"] == 5
    for item in body["items"]:
        assert tuple(item) == api.ITEM_KEYS
    assert not [k for k in api.ITEM_KEYS if _CONTACT_KEY.search(k)]
    blob = json.dumps(body)
    assert not _EMAIL.search(blob) and not _PHONE.search(blob)
    for leak in ("coEmail", "coPhone", "officer@gsa.gov", "867-5309", "Some Person", "ppms.gov",
                 "Forrester", "item_contact", "cdn/x"):
        assert leak not in blob
    assert [i["title"] for i in body["items"] if i["id"].endswith("77/88/1")] == ["Skid steer - call Bob or"]


def test_sql_is_an_allow_list_parameterised_and_bounded():
    sql = api._SQL
    for banned in ("coEmail", "coPhone", "contractOfficer", "item_contact", "imageURL", "image_url",
                   "propertyAddr", "locationAddress", "latitude", "longitude", "lotInfo"):
        assert banned not in sql
    assert not re.search(r"(SELECT|,)\s*(\w\.)?raw\s*(,|FROM|AS)", sql, re.I)     # raw is never returned whole
    assert "observed_at > now() - make_interval" in sql and "LIMIT %s" in sql and "LIMIT 1" in sql
    assert "{pick}" in sql and all("'" not in v.replace("'active'", "").replace("'closed'", "") for v in api._PICK.values())
    assert not re.search(r"\b(INSERT|UPDATE|DELETE|ALTER|CREATE|DROP)\b", sql, re.I)


# ───────────────────────── failure + cache ─────────────────────────

def test_failed_read_answers_ok_false_without_error_text(monkeypatch):
    calls = []

    def boom(status):
        calls.append(status)
        raise RuntimeError("password authentication failed for user postgres")

    monkeypatch.setattr(api, "_read", boom)
    r = _get()
    body = r.json()
    assert r.status_code == 200 and set(body) == TOP_KEYS
    assert body["ok"] is False and body["items"] == [] and body["total"] == 0
    assert body["facets"] == {"sites": [], "categories": [], "states": []}
    assert "password" not in r.text and "postgres" not in r.text
    _get(); _get()
    assert calls == ["open"]                      # an outage is not re-queried per page view
    # …and the failure is not memoised as an answer: once the back-off clears, it reads again
    api._fail_until.clear()
    monkeypatch.setattr(api, "_read", lambda status: ([dict(ROWS[0])], NO_PICKS))
    assert _get().json()["total"] == 1


def test_page_views_share_one_read(reads):
    for params in ({}, {"state": "TX"}, {"sort": "bids", "page": 2}, {"q": "truck"}):
        assert _get(**params).json()["ok"] is True
    assert reads == ["open"]
    _get(status="closed")
    assert reads == ["open", "closed"]


def test_routes_are_public_plain_def(reads, monkeypatch):
    for fn in (app_module.public_platform_auctions, app_module.public_platform_sites):
        assert not inspect.iscoroutinefunction(fn)
    for path in ("/platform/api/auctions", "/platform/api/sites"):
        assert not path.startswith(auth_svc.PROTECTED_PREFIXES)
    monkeypatch.setenv("ADMIN_PASSWORD", "pw")    # auth on: still answers without a login
    auth_svc.reset_caches()
    assert _get().status_code == 200


def test_public_sources_env_cannot_add_an_unnormalised_source(monkeypatch):
    monkeypatch.setenv("PLATFORM_PUBLIC_SOURCES", "gsa, municibid ,nope")
    assert api.public_sources() == ["gsa"]


# ───────────────────────── sites ─────────────────────────

def _source_rows():
    return [
        {"source": "govdeals", "lots": 5349, "last_seen": NOW - timedelta(minutes=9)},
        {"source": "purple_wave", "lots": 1119, "last_seen": NOW - timedelta(hours=23)},
        {"source": "municibid", "lots": 582, "last_seen": NOW - timedelta(days=30)},
    ]


def test_sites_states_are_honest(monkeypatch):
    monkeypatch.setattr(app_module, "_platform_source_rows", _source_rows)
    r = _get("/platform/api/sites")
    assert r.status_code == 200
    sites = r.json()
    assert isinstance(sites, list)
    for s in sites:
        assert set(s) == {"key", "name", "kind", "status", "lots", "last_seen"}
        assert s["kind"] in ("government", "commercial") and s["status"] in ("live", "paused", "planned")
    by = {s["key"]: s for s in sites}
    assert by["govdeals"]["status"] == "live" and by["govdeals"]["lots"] == 5349
    assert by["purple_wave"]["status"] == "live"                               # inside 24 h
    assert by["municibid"]["status"] == "paused" and by["municibid"]["lots"] == 582
    assert by["mibid"] == {"key": "mibid", "name": "MiBid", "kind": "government",
                           "status": "paused", "lots": 0, "last_seen": None}   # adapter, never observed
    planned = [s for s in sites if s["status"] == "planned"]
    assert {s["key"] for s in planned} == set(api.PLANNED_SITES)
    assert all(s["lots"] is None and s["last_seen"] is None for s in planned)


def test_a_site_without_an_adapter_is_never_shown_as_scraped(monkeypatch):
    """Even if rows exist for it (or someone moves it into SITES), no adapter = planned, lots null."""
    rows = _source_rows() + [{"source": "hibid", "lots": 9999, "last_seen": NOW}]
    assert not api.has_adapter("hibid") and api.has_adapter("govdeals")
    by = {s["key"]: s for s in api.sites(lambda: rows)}
    assert by["hibid"] == {"key": "hibid", "name": "HiBid", "kind": "commercial",
                           "status": "planned", "lots": None, "last_seen": None}
    monkeypatch.setitem(api.SITES, "hibid", ("HiBid", "commercial"))
    assert {s["key"]: s for s in api.sites(lambda: rows)}["hibid"]["status"] == "planned"
    for key in api.SITES:
        if key != "hibid":
            assert api.has_adapter(key), key
    assert not set(api.PLANNED_SITES) & set(app_module._PLATFORM_SOURCE_NAMES)
    assert {k: v[0] for k, v in api.SITES.items() if k != "hibid"} == app_module._PLATFORM_SOURCE_NAMES


def test_sites_failure_is_a_503_not_a_guess(monkeypatch):
    def boom():
        raise RuntimeError("connection refused: secret-host")

    monkeypatch.setattr(app_module, "_platform_source_rows", boom)
    r = _get("/platform/api/sites")
    assert r.status_code == 503 and r.json() == {"ok": False, "error": "unavailable"}
    assert "secret-host" not in r.text
