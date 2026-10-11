"""automation/web/deals_sources.py — the non-GovDeals half of the public /deals
page, read from the recorder's `listing_snapshots`.

DB-free. What must hold:
  · the SQL is an allow-list (never `raw` whole, no contact / photo / address
    keys), parameterised, windowed and capped, with one CASE branch per source;
  · each source's recorded raw sample normalises to the deal_lots row shape
    with the right title / place / url — and never a seller field;
  · the public_deals policy applies: seating, non-USD, operator picks all drop;
  · GovDeals is never served from here;
  · pins are city-level or null, memoised; a failed read backs off.
"""
import re
from datetime import datetime, timedelta, timezone

import pytest

from automation.web import deals_sources as ds
from automation.web import public_deals as pd

NOW = datetime.now(timezone.utc)
NO_PICKS = pd.OperatorPicks()

# Recorded `raw` samples per source (tests/recorder/fixtures + a 2026-10-09 probe),
# trimmed to the keys that matter plus the ones a careless SELECT would leak.
RAW = {
    "allsurplus": {
        "assetId": 257, "accountId": 20948, "auctionId": 13, "assetShortDescription": "Anton Paar HPA-2    High Pressure Asher",
        "assetCategory": "57M", "categoryDescription": "Laboratory Equipment, Miscellaneous",
        "locationCity": "Białogard", "locationState": "PL-ZP", "country": "POL", "currencyCode": "EUR",
        "locationAddress1": "ul. Zwycięstwa 4", "latitude": 54.006924, "longitude": 15.995917, "photo": "20948_257.jpg",
    },
    "allsurplus_us": {
        "assetId": 3, "accountId": 32415, "auctionId": 5, "assetShortDescription": "Lot of (12) Dell OptiPlex 7090",
        "assetCategory": "29", "categoryDescription": "Computers", "locationCity": "houston", "locationState": "TX",
        "country": "USA", "currencyCode": "USD", "locationAddress1": "1 Main St", "photo": "x.jpg",
    },
    "gsa": {
        "saleNo": "3-1-QSC-I-27-001", "lotNo": "003", "aucEndDt": "2026-10-13",
        "itemName": "2016 Ford Super Duty F-250 SRW 4WD SuperCab",
        "propertyAddr1": "3300 Oklahoma Ave.", "propertyCity": "Woodward", "propertyState": "OK", "propertyZip": "73801",
        "locationCity": "Springfield                   ", "locationST": "IL", "locationOrg": "Illinois Federal Surplus Property",
        "biddersCount": 1, "highBidAmount": 15000.0, "itemDescURL": "https://www.gsaauctions.gov/auctions/preview/379244",
        "imageURL": "https://www.ppms.gov/gw/auction/ppms/api/v1/auction/image/31", "lotInfo": "<h4>Overview</h4>",
        "contractOfficer": "AlfonsoBrown", "coEmail": "alfonso.brown@gsa.gov", "coPhone": "8179780771",
    },
    "purple_wave": {
        "id": 934772, "auction": "261013", "item": "FK5002", "title": "Tuesday October 13 Government Auction",
        "first_line_description": "Furniture", "category": "Furniture", "city": "Muskogee", "state_abbreviation": "OK",
        "latitude": 35.739899, "longitude": -95.384903, "image_url": "https://d323w7klwy72q3.cloudfront.net/i/a/x.jpg",
        "item_contact": "Jo 555-111-2222", "address": "", "current_bid": "17.50", "bid_count": 3,
    },
    "public_surplus": {
        "auc_id": "4006273", "title": "#4006273 - Procedure table", "location": "CA",
        "link": "https://www.publicsurplus.com/sms/auction/view?auc=4006273", "price_raw": "$300.00",
        "end_epoch_ms_raw": "1803700800000", "page_url": "https://www.publicsurplus.com/sms/browse/search?keyWord=x",
    },
    "municibid": {
        "id": 83491678, "title": "Grandfather clock", "city": "Immaculata", "state": "PA", "price": 750.0,
        "end": "2026-08-31T19:22:35.803", "lat": 40.030536, "lng": -75.565, "seller": "Immaculata University",
        "img": "https://storagemunicibidpro.blob.core.windows.net/assets/media/e6d4b7c",
    },
    "mibid": {
        "id": 7826, "guid": "a659f600-b77b-f111-925a-005056936aaa", "title": "Ten Haworth Office Desks (B)",
        "location": "Lansing   ", "status": 4, "endTime": "2026-07-17T10:20:00", "currentBid": 50.0, "primaryImage": "",
    },
}
LOT_IDS = {"allsurplus": "257/20948/13", "allsurplus_us": "3/32415/5", "gsa": "3-1-QSC-I-27-001-003",
           "purple_wave": "934772", "public_surplus": "4006273", "municibid": "83491678",
           "mibid": "a659f600-b77b-f111-925a-005056936aaa"}


def _row(sample: str, **kw) -> dict:
    """What the SQL hands `normalise` for one recorded raw sample."""
    source = sample.split("_us")[0]
    fields = ds.extract(source, RAW[sample])
    assert fields is not None, sample
    base = {"source": source, "source_lot_id": LOT_IDS[sample], "current_bid": 50, "bid_count": 2,
            "end_date": NOW + timedelta(hours=5), "first_seen": NOW - timedelta(days=1), **fields}
    return {**base, **kw}


@pytest.fixture(autouse=True)
def _geo(monkeypatch):
    calls = []

    def fake(city, state, zip_code):
        calls.append((city, state))
        return (29.76, -95.37, "city") if (city, state) == ("Houston", "TX") else (33.0, -112.0, "state")

    monkeypatch.setattr(ds.geo, "resolve_place", fake)
    ds.clear_cache()
    yield calls
    ds.clear_cache()


# ───────────────────────── SQL shape ─────────────────────────

def test_sql_is_an_allow_list_parameterised_and_bounded():
    sql = ds._SQL
    for banned in ("coEmail", "coPhone", "contractOfficer", "item_contact", "imageURL", "image_url", "photo",
                   "propertyAddr", "locationAddress", "latitude", "longitude", "lotInfo", "seller", "img"):
        assert f"'{banned}" not in sql, banned
    assert not re.search(r"(SELECT|,)\s*(\w\.)?raw\s*(,|FROM|AS)", sql, re.I)     # raw is never returned whole
    assert "observed_at > now() - make_interval" in sql and "LIMIT %s" in sql and "LIMIT 1" in sql
    assert "s.raw ?| %s" in sql                                                   # title-key guard is a bound array
    assert "{pick}" in sql
    assert not re.search(r"\b(INSERT|UPDATE|DELETE|ALTER|CREATE|DROP)\b", sql, re.I)
    for src in ds.SOURCES:
        assert f"WHEN '{src}' THEN" in sql
    assert "WHEN 'govdeals'" not in sql
    # the title branch reads each source's own key, never a neighbour's
    assert "WHEN 'purple_wave' THEN s.raw->'first_line_description'" in sql
    assert "WHEN 'gsa' THEN s.raw->'propertyCity'" in sql


def test_read_binds_sources_window_cap_and_title_keys(monkeypatch):
    seen = []

    class Conn:
        def execute(self, sql, params=()):
            seen.append((sql, params))
            if "listing_snapshots" in sql:
                return type("Cur", (), {"fetchall": lambda _s: []})()
            return type("Cur", (), {"fetchall": lambda _s: []})()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(ds.db, "connect", lambda *a, **k: Conn())
    rows, picks = ds._read("open")
    sql, params = [x for x in seen if "listing_snapshots" in x[0]][0]
    assert params == (list(ds.SOURCES), ds.OPEN_WINDOW_DAYS, ds.ROW_CAP, ds.title_keys())
    assert "govdeals" not in params[0] and "'" not in ds._PICK["open"].replace("'active'", "")
    assert isinstance(picks, pd.OperatorPicks)
    seen.clear()
    ds._read("closed")
    sql, params = [x for x in seen if "listing_snapshots" in x[0]][0]
    assert params == (list(ds.SOURCES), ds.CLOSED_WINDOW_DAYS + ds.OPEN_WINDOW_DAYS, ds.CLOSED_WINDOW_DAYS,
                      ds.ROW_CAP, ds.title_keys())


def test_picks_are_read_before_the_lots_and_a_failure_publishes_nothing(monkeypatch):
    ran = []

    class Conn:
        def execute(self, sql, params=()):
            if "listing_snapshots" in sql:
                ran.append(sql)
                return type("Cur", (), {"fetchall": lambda _s: [_row("gsa")]})()
            raise RuntimeError("relation unavailable")

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(ds.db, "connect", lambda *a, **k: Conn())
    with pytest.raises(RuntimeError):
        ds.lots("active")
    assert ran == []


# ───────────────────────── normalise per source ─────────────────────────

def test_every_source_sample_normalises_to_the_row_shape():
    for sample in ("allsurplus_us", "gsa", "purple_wave", "public_surplus", "municibid", "mibid"):
        rec = ds.normalise(_row(sample), "active", NO_PICKS)
        assert rec is not None, sample
        assert tuple(rec) == ds.ROW_KEYS
        assert rec["source"] == sample.split("_us")[0] and rec["source_name"] == ds.SITES[rec["source"]]
        assert rec["asset_id"] is None and rec["viewer_url"] is None and rec["currency_code"] == "USD"
        assert rec["outcome_complete"] is False and rec["end_utc"].tzinfo is not None


def test_allsurplus_sample():
    assert ds.normalise(_row("allsurplus"), "active", NO_PICKS) is None     # EUR / POL: never shown as dollars
    rec = ds.normalise(_row("allsurplus_us"), "active", NO_PICKS)
    assert rec["title"] == "Lot of (12) Dell OptiPlex 7090" and rec["city"] == "houston"
    assert rec["canonical_category"] == "computers_electronics"           # maestro code → bucket
    assert rec["url"] == "https://www.allsurplus.com/en/asset/3/32415"


def test_gsa_sample_uses_the_property_location_not_the_sales_office():
    rec = ds.normalise(_row("gsa"), "active", NO_PICKS)
    assert (rec["city"], rec["state"]) == ("Woodward", "OK")
    assert rec["title"].startswith("2016 Ford Super Duty")
    assert rec["url"] == "https://www.gsaauctions.gov/auctions/preview/379244"
    assert rec["canonical_category"] is None and rec["native_category_name"] is None


def test_purple_wave_sample_uses_the_lot_line_not_the_auction_title():
    rec = ds.normalise(_row("purple_wave"), "active", NO_PICKS)
    assert rec["title"] == "Furniture" and rec["native_category_name"] == "Furniture"
    assert (rec["city"], rec["state"]) == ("Muskogee", "OK")
    assert rec["url"] == "https://www.purplewave.com/auction/261013/item/FK5002"


def test_public_surplus_municibid_mibid_samples():
    ps = ds.normalise(_row("public_surplus"), "active", NO_PICKS)
    assert ps["city"] is None and ps["state"] == "CA"
    assert ps["url"] == "https://www.publicsurplus.com/sms/auction/view?auc=4006273"
    mu = ds.normalise(_row("municibid"), "active", NO_PICKS)
    assert (mu["city"], mu["state"]) == ("Immaculata", "PA")
    assert mu["url"] == "https://municibid.com/Listing/Details/83491678"
    mi = ds.normalise(_row("mibid"), "active", NO_PICKS)
    assert (mi["city"], mi["state"]) == ("Lansing", "MI")                  # single-state site, city trimmed
    assert mi["url"] == "https://mibid.michigan.gov/AuctionBid/Index/a659f600-b77b-f111-925a-005056936aaa"


def test_bad_ids_never_become_urls():
    assert ds.normalise(_row("municibid", source_lot_id="8349;drop"), "active", NO_PICKS)["url"] is None
    assert ds.normalise(_row("mibid", source_lot_id="not-a-uuid"), "active", NO_PICKS)["url"] is None
    assert ds.normalise(_row("gsa", ref_a="javascript:alert(1)"), "active", NO_PICKS)["url"] is None
    assert ds.normalise(_row("purple_wave", ref_b="FK/../x"), "active", NO_PICKS)["url"] is None


def test_no_seller_field_or_value_can_leave():
    leak = re.compile(r"email|phone|contact|officer|addr|image|photo|raw|seller|img|lat$|lng$", re.I)
    assert not [k for k in ds.ROW_KEYS if leak.search(k) and k not in ("lat", "lng")]
    dirty = _row("gsa", title="Skid steer - call Bob 555-123-4567 or bob@seller.com", city="woodward ok bob@seller.com")
    rec = ds.normalise(dirty, "active", NO_PICKS)
    assert rec["title"] == "Skid steer - call Bob or" and rec["city"] == "woodward ok"
    blob = repr(rec)
    for s in ("alfonso", "8179780771", "ppms.gov", "Oklahoma Ave", "AlfonsoBrown"):
        assert s not in blob


def test_extract_returns_none_for_a_poll_row_and_unknown_source():
    assert ds.extract("municibid", {"detail_page": {"url": "https://municibid.com/Listing/Details/1"}}) is None
    assert ds.extract("govdeals", {"assetShortDescription": "x"}) is None
    assert ds.extract("nope", {"title": "x"}) is None


# ───────────────────────── policy ─────────────────────────

@pytest.mark.parametrize("row", [
    _row("gsa", title="Lot of 40 Banquet Chairs"),                       # seating word in the title
    _row("purple_wave", first_line_description="Office surplus", category="Chairs"),   # seating word in the category
    _row("allsurplus_us", category_code="47B"),                          # maestro seating category code
    _row("allsurplus_us", country="CAN"),
    _row("allsurplus_us", currency="GBP"),
    _row("gsa", title="   "),                                            # no title
    _row("gsa", city=None, state="ZA-WC"),                               # no usable place
    _row("public_surplus", state="California"),                          # not a 2-letter state, no city
    {**_row("gsa"), "source": "govdeals", "source_lot_id": "1/2/3"},      # GovDeals is served from deal_lots
    {**_row("gsa"), "source": "hibid"},                                  # no spec for this source
])
def test_policy_drops_the_lot(row):
    assert ds.normalise(row, "active", NO_PICKS) is None


def test_operator_picks_drop_for_every_source():
    picks = pd.OperatorPicks(
        pairs={(3, 32415)}, ids={"934772"},
        urls={pd._norm_url("https://www.gsaauctions.gov/auctions/preview/379244/")},
        titles={pd._norm_title("Grandfather  clock!")})
    rows = [_row(s) for s in ("allsurplus_us", "gsa", "purple_wave", "municibid", "mibid")]
    assert [r["source"] for r in ds.build(rows, "active", picks)] == ["mibid"]


def test_closed_rows_carry_outcome_and_final_bid():
    sold = ds.normalise(_row("gsa", bid_count=4, current_bid=1200), "closed", NO_PICKS)
    assert (sold["outcome"], sold["final_bid"], sold["final_bid_count"], sold["outcome_complete"]) == ("sold", 1200.0, 4, True)
    none = ds.normalise(_row("gsa", bid_count=0, current_bid=25), "closed", NO_PICKS)
    assert (none["outcome"], none["final_bid"], none["current_bid"]) == ("no_bid", None, 25.0)
    unknown = ds.normalise(_row("public_surplus", bid_count=None), "closed", NO_PICKS)
    assert unknown["outcome"] is None and unknown["final_bid"] is None


# ───────────────────────── pins + cache ─────────────────────────

def test_pins_are_city_level_or_null_and_memoised(_geo):
    a = ds.normalise(_row("allsurplus_us", city="Houston"), "active", NO_PICKS)
    b = ds.normalise(_row("allsurplus_us", city="Houston"), "active", NO_PICKS)
    g = ds.normalise(_row("gsa"), "active", NO_PICKS)
    assert (a["lat"], a["lng"]) == (29.76, -95.37) and (b["lat"], b["lng"]) == (29.76, -95.37)
    assert g["lat"] is None and g["lng"] is None            # state centroid is not a city: no pin
    assert _geo.count(("Houston", "TX")) == 1                # second lookup hit the memo


def test_lots_cache_filters_and_backoff(monkeypatch):
    calls = []
    stale = _row("gsa", source_lot_id="old", end_date=NOW - timedelta(minutes=1))
    rows = {"open": [_row("gsa"), _row("purple_wave"), _row("municibid"), stale],
            "closed": [_row("mibid", end_date=NOW - timedelta(days=2))]}
    monkeypatch.setattr(ds, "_read", lambda status: (calls.append(status), ([dict(r) for r in rows[status]], NO_PICKS))[1])
    live = ds.lots("active")
    assert [r["source"] for r in live] == ["gsa", "purple_wave", "municibid"]       # ended-while-warm is dropped
    assert len(ds.lots("all")) == 4 and ds.lots("closed")[0]["outcome_complete"] is True
    assert calls == ["open", "closed"]                                                 # one read per status
    f = lambda **kw: [r["source"] for r in ds.apply_filters(ds.lots("active"), **kw)]  # noqa: E731
    assert f(site="gsa") == ["gsa"] and f(state="ok") == ["gsa", "purple_wave"]
    assert f(q="FORD") == ["gsa"] and f(max_bids=1) == [] and f(min_price=60) == []
    assert f(bbox=(29.0, -96.0, 30.0, -95.0)) == []                                      # unmapped lots never match
    assert f(ending_within=1) == [] and f(ending_within=6) == ["gsa", "purple_wave", "municibid"]
    assert f(category="computers_electronics") == []
    # a failed read raises and is not re-queried per page view
    ds.clear_cache()
    boom = []

    def fail(status):
        boom.append(status)
        raise RuntimeError("password authentication failed")

    monkeypatch.setattr(ds, "_read", fail)
    for _ in range(3):
        with pytest.raises(RuntimeError):
            ds.lots("active")
    assert boom == ["open"]
