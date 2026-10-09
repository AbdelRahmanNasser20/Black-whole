"""Offline tests for the Maxanet adapter shared by Wisconsin Surplus and
USGovBid, against partials captured live 2026-10-09 on
bid.wisconsinsurplus.com (see recorder/sources/maxanet.py). No network —
polite_get is monkeypatched.
"""
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
import requests

from recorder.models import Observation
from recorder.sources import base, maxanet
from recorder.sources.base import FURNITURE_TERMS, SourceFetchFailed
from recorder.sources.usgovbid import USGovBidSource
from recorder.sources.wisconsin_surplus import WisconsinSurplusSource

FIXTURES = Path(__file__).parent / "fixtures" / "maxanet"


def _html(name: str) -> str:
    return (FIXTURES / name).read_text()


class _Resp:
    def __init__(self, text="", status_code=200, url="https://bid.example/Public", history=None):
        self.text = text
        self.status_code = status_code
        self.url = url
        self.history = history or []


def _router(*, search=None, past=None, detail=None, landing_status=200, search_status=200,
            calls=None):
    """search/past: html for Current/Past searches (same page every term)."""
    def fake(url, *, params=None, headers=None, session=None, **kw):
        if calls is not None:
            calls.append((url, dict(params or {}), session))
        if url.endswith("/Public"):
            return _Resp(_html("landing_redirect.html"), landing_status, url)
        if "GetGlobalSearchResults" in url:
            if search_status != 200:
                return _Resp("", search_status, url)
            filt = (params or {}).get("filter")
            body = past if filt == "Past" else search
            return _Resp(body if body is not None else _html("search_current_empty.html"), 200, url)
        if "AuctionItemDetail" in url:
            if detail is None:
                return _Resp(_html("landing_redirect.html"), 200,
                             "https://bid.example/Public/Error/NotFound")
            return _Resp(detail, 200, url)
        raise AssertionError(url)
    return fake


# --- parsers -----------------------------------------------------------

def test_parse_search_current_cards():
    items, pages, total = maxanet.parse_search(_html("search_current_chairs.html"))
    assert len(items) == 12 and pages == 1 and total == 44
    first = items[0]
    assert first["item_id"] == "15590736"
    assert first["title"] == "Lot of 60 Metal Folding Chairs with Cart"
    assert first["price"] == Decimal("165.00")
    assert first["price_label"] == "Current"
    assert first["auction_title"].endswith("Camp Douglas, WI")
    assert first["auction_token"] and first["item_token"]


def test_parse_search_past_cards_are_final_and_sold():
    items, _, _ = maxanet.parse_search(_html("search_past_chairs.html"))
    assert items
    assert all(i["price_label"] == "Final" for i in items)
    assert items[0]["sold_label"] is True


def test_parse_search_empty():
    assert maxanet.parse_search(_html("search_current_empty.html"))[0] == []


def test_parse_detail():
    d = maxanet.parse_detail(_html("item_detail_15590736.html"))
    assert d["bid_count"] == 9
    assert d["price"] == Decimal("165.00")
    assert d["end_local"] == "2026-10-12 10:16:00 AM"
    assert maxanet.parse_detail(_html("landing_redirect.html")) is None


def test_dates_are_central_to_utc():
    # 10:16 CDT == 15:16 UTC
    want = datetime(2026, 10, 12, 15, 16, tzinfo=timezone.utc)
    assert maxanet.parse_auc_date("10/12/2026 10:16:00") == want
    assert maxanet.parse_enddate("2026-10-12 10:16:00 AM") == want


def test_city_state_from_auction_title():
    assert maxanet._city_state("#26-1410 - Wisconsin Dept. of Military Affairs - Camp Douglas, WI") \
        == ("Camp Douglas", "WI")
    assert maxanet._city_state("Monmouth County - Freehold, New Jersey") == ("Freehold", "NJ")
    assert maxanet._city_state("no location here") == (None, None)


def test_lot_id_roundtrip():
    lid = maxanet.lot_id_for("A+b/c==", "X/y+z==")
    assert maxanet.split_lot_id(lid) == ("A+b/c==", "X/y+z==")
    assert maxanet.split_lot_id("nope") is None


# --- discover() --------------------------------------------------------

def test_discover_returns_furniture_items(monkeypatch):
    calls = []
    monkeypatch.setattr(maxanet, "polite_get", _router(search=_html("search_current_chairs.html"), calls=calls))
    obs = WisconsinSurplusSource().discover()
    assert len(obs) == 11                      # 12 cards, 11 match FURNITURE_TERMS
    for o in obs:
        assert isinstance(o, Observation)
        assert o.source == "wisconsin_surplus"
        assert maxanet.split_lot_id(o.source_lot_id)
        assert o.status == "active" or o.end_date <= datetime.now(timezone.utc)
        assert o.end_date is not None and o.end_date.tzinfo is not None
        for key in ("title", "city", "state", "category", "image_url", "url"):
            assert key in o.raw
        assert o.raw["url"].startswith("https://bid.wisconsinsurplus.com/Public/Auction/AuctionItemDetail")
        json.dumps(o.raw)
    # one session, warmed first, reused for every search
    assert calls[0][0].endswith("/Public")
    assert len({id(c[2]) for c in calls}) == 1
    searches = [c for c in calls if "GetGlobalSearchResults" in c[0]]
    assert {c[1]["search"] for c in searches} == set(FURNITURE_TERMS)
    assert all(c[1]["filter"] == "Current" for c in searches)


def test_discover_clean_zero_match_returns_empty(monkeypatch):
    monkeypatch.setattr(maxanet, "polite_get", _router(search=_html("search_current_empty.html")))
    assert WisconsinSurplusSource().discover() == []


def test_discover_raises_when_landing_fails(monkeypatch):
    monkeypatch.setattr(maxanet, "polite_get", _router(landing_status=503))
    with pytest.raises(SourceFetchFailed):
        WisconsinSurplusSource().discover()


def test_discover_raises_when_every_search_fails(monkeypatch):
    monkeypatch.setattr(maxanet, "polite_get", _router(search_status=500))
    with pytest.raises(SourceFetchFailed):
        WisconsinSurplusSource().discover()


def test_discover_raises_when_session_lost(monkeypatch):
    """A search partial that 302s to the NotFound page (no session) is a
    failure, not a clean zero."""
    def fake(url, **kw):
        return _Resp(_html("landing_redirect.html"), 200, url)
    monkeypatch.setattr(maxanet, "polite_get", fake)
    with pytest.raises(SourceFetchFailed):
        WisconsinSurplusSource().discover()


def test_discover_raises_on_transport_error(monkeypatch):
    def boom(*a, **k):
        raise requests.exceptions.ConnectionError("down")
    monkeypatch.setattr(maxanet, "polite_get", boom)
    with pytest.raises(SourceFetchFailed):
        USGovBidSource().discover()


# --- poll() ------------------------------------------------------------

def _lot_id():
    items, _, _ = maxanet.parse_search(_html("search_current_chairs.html"))
    return maxanet.lot_id_for(items[0]["auction_token"], items[0]["item_token"])


def test_poll_reads_detail(monkeypatch):
    monkeypatch.setattr(maxanet, "polite_get", _router(detail=_html("item_detail_15590736.html")))
    obs = WisconsinSurplusSource().poll([{"source_lot_id": _lot_id(), "end_date": None}])
    assert len(obs) == 1
    o = obs[0]
    assert o.bid_count == 9
    assert o.current_bid == Decimal("165.00")
    assert o.end_date == datetime(2026, 10, 12, 15, 16, tzinfo=timezone.utc)
    assert o.raw["title"] == "Lot of 60 Metal Folding Chairs with Cart"


def test_poll_not_found_past_end_is_gone(monkeypatch):
    calls = []
    monkeypatch.setattr(maxanet, "polite_get", _router(detail=None, calls=calls))
    past = datetime.now(timezone.utc) - timedelta(days=1)
    obs = WisconsinSurplusSource().poll([{"source_lot_id": _lot_id(), "end_date": past}])
    assert [o.status for o in obs] == ["gone"]
    # re-warmed the session once before believing NotFound
    assert sum(1 for c in calls if c[0].endswith("/Public")) == 2


def test_poll_not_found_before_end_is_skipped(monkeypatch):
    monkeypatch.setattr(maxanet, "polite_get", _router(detail=None))
    future = datetime.now(timezone.utc) + timedelta(days=1)
    assert WisconsinSurplusSource().poll([{"source_lot_id": _lot_id(), "end_date": future}]) == []


def test_poll_malformed_lot_id_is_skipped(monkeypatch):
    monkeypatch.setattr(maxanet, "polite_get", _router())
    assert WisconsinSurplusSource().poll([{"source_lot_id": "12345", "end_date": None}]) == []


# --- sold_sweep() ------------------------------------------------------

def test_sold_sweep_returns_closed_finals(monkeypatch):
    monkeypatch.setattr(maxanet, "polite_get", _router(past=_html("search_past_chairs.html")))
    obs = WisconsinSurplusSource().sold_sweep()
    assert obs
    assert all(o.status == "closed" for o in obs)
    assert all(o.raw["maxanet"]["price_label"] == "Final" for o in obs)


def test_sold_sweep_all_failed_is_empty(monkeypatch):
    monkeypatch.setattr(maxanet, "polite_get", _router(search_status=500))
    assert WisconsinSurplusSource().sold_sweep() == []


# --- tenants -----------------------------------------------------------

def test_tenants_bind_hosts():
    assert WisconsinSurplusSource().HOST == "https://bid.wisconsinsurplus.com"
    assert USGovBidSource().HOST == "https://bid.usgovbid.com"
    assert USGovBidSource.SOURCE == "usgovbid"


def test_usgovbid_crawl_delay_registered():
    assert base.host_interval("bid.usgovbid.com") >= 30.0
    assert base.host_interval("bid.wisconsinsurplus.com") == base.MIN_HOST_INTERVAL_SECONDS


def test_throttle_honours_host_interval(monkeypatch):
    slept = []
    clock = iter([100.0, 100.0, 105.0, 105.0])
    monkeypatch.setattr(base.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(base.time, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr(base, "_last_request_at", {})
    base._throttle("bid.usgovbid.com")
    base._throttle("bid.usgovbid.com")
    assert slept == [pytest.approx(25.0)]
