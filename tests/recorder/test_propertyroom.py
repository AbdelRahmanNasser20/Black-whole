"""Offline tests for the PropertyRoom adapter, against pages captured live
2026-10-16 via the Pi SOCKS proxy (see recorder/sources/propertyroom.py).
No network — polite_get is monkeypatched. The captured search pages held no
furniture (the fuzzy 'chairs' search matched coin banks), so furniture cases
rename one captured card title, mirroring the mnbid tests.
"""
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import quote

import pytest
import requests

from recorder.models import Observation
from recorder.sources import propertyroom
from recorder.sources.base import FURNITURE_TERMS, SourceFetchFailed

FIXTURES = Path(__file__).parent / "fixtures" / "propertyroom"

FUZZY_TITLE = "Vintage Tammany Bank Boss Tweed Cast Iron Mechanical Coin Bank 1873 Style"
FURNITURE_TITLE = "Lot of 40 Stacking Chairs Banquet Hall"


def _load(name: str) -> str:
    return (FIXTURES / name).read_text()


def _with_furniture(html: str) -> str:
    """Rename one captured card's title to a FURNITURE_TERMS match (the
    .listing-title div is the only text the filter reads)."""
    needle = f'<div class="listing-title">{FUZZY_TITLE}</div>'
    assert needle in html
    return html.replace(needle, f'<div class="listing-title">{FURNITURE_TITLE}</div>')


class _Resp:
    def __init__(self, text, status_code=200, url="https://www.propertyroom.com/"):
        self.text = text
        self.status_code = status_code
        self.url = url


def _router(*, search_html=None, item_html=None, search_status=200, item_status=200, calls=None):
    def fake(url, *, headers=None, params=None, timeout=None, session=None):
        if calls is not None:
            calls.append((url, params))
        if url.startswith(propertyroom.BASE_URL + propertyroom.SEARCH_PATH):
            if search_status != 200:
                return _Resp("", search_status, url)
            return _Resp(search_html if search_html is not None else _load("search_office_furniture_empty.html"), 200, url)
        if url.startswith(propertyroom.BASE_URL + propertyroom.ITEM_PATH):
            if item_status != 200:
                return _Resp("", item_status, url)
            return _Resp(item_html, 200, url)
        raise AssertionError(url)
    return fake


# --- parse_search --------------------------------------------------------

def test_parse_search_cards_and_totals():
    cards, totals = propertyroom.parse_search(_load("search_chairs_page1.html"))
    assert totals == {"results": 3, "page": 1, "cards": 3}
    assert [c["lot_id"] for c in cards] == ["18992859", "18992860", "18989665"]
    first = cards[0]
    assert first["title"] == FUZZY_TITLE
    assert first["format"] == "English"
    assert first["current_bid"] == Decimal("300.00")
    assert first["bid_count"] == 0
    # data-end is already UTC ("...Z" with a 7-digit fraction)
    assert first["end_date"] == datetime(2026, 10, 19, 13, 15, tzinfo=timezone.utc)
    assert first["url"].startswith("https://www.propertyroom.com/l/")
    assert first["image_url"].startswith("https://content.propertyroom.com/")
    assert first["photos"] and all(p.startswith("https://") for p in first["photos"])


def test_parse_search_zero_results_page_is_clean():
    cards, totals = propertyroom.parse_search(_load("search_office_furniture_empty.html"))
    assert cards == []
    assert totals == {"results": 0, "page": 1, "cards": 0}


def test_parse_end_utc():
    dt = propertyroom._parse_end("2026-10-19T13:15:00.0000000Z")
    assert dt == datetime(2026, 10, 19, 13, 15, tzinfo=timezone.utc)
    assert propertyroom._parse_end("garbage") is None
    assert propertyroom._parse_end(None) is None


# --- discover() ----------------------------------------------------------

def test_discover_returns_furniture_titled_cards(monkeypatch):
    calls = []
    html = _with_furniture(_load("search_chairs_page1.html"))
    monkeypatch.setattr(propertyroom, "polite_get", _router(search_html=html, calls=calls))
    obs = propertyroom.PropertyRoomSource().discover()
    # every term served the same page; the matched card is deduped to ONE observation
    assert len(obs) == 1
    o = obs[0]
    assert isinstance(o, Observation)
    assert o.source == "propertyroom"
    assert o.source_lot_id == "18992859"
    assert o.status == "active"
    assert o.current_bid == Decimal("300.00")
    assert o.bid_count == 0
    assert o.end_date == datetime(2026, 10, 19, 13, 15, tzinfo=timezone.utc)
    for key in ("title", "city", "state", "category", "image_url", "url", "currency"):
        assert key in o.raw
    assert o.raw["title"] == FURNITURE_TITLE
    assert o.raw["currency"] == "USD"
    assert o.raw["url"].startswith("https://www.propertyroom.com/l/")
    json.dumps(o.raw)  # raw must be JSON-serialisable (Decimals/datetimes stringified)
    # one page per term (totals said 3 of 3 results served)
    assert len(calls) == len(FURNITURE_TERMS)
    assert calls[0][0] == f"{propertyroom.BASE_URL}/s/{quote(FURNITURE_TERMS[0])}"


def test_discover_fuzzy_mismatches_are_clean_zero(monkeypatch):
    # the real captured page: 3 cards, none furniture-titled — [] without raising
    monkeypatch.setattr(propertyroom, "polite_get",
                        _router(search_html=_load("search_chairs_page1.html")))
    assert propertyroom.PropertyRoomSource().discover() == []


def test_discover_zero_results_is_clean_zero(monkeypatch):
    monkeypatch.setattr(propertyroom, "polite_get",
                        _router(search_html=_load("search_office_furniture_empty.html")))
    assert propertyroom.PropertyRoomSource().discover() == []


def test_discover_raises_when_every_term_fails(monkeypatch):
    # the geo-block page: HTTP 403 "Not available in your country"
    monkeypatch.setattr(propertyroom, "polite_get", _router(search_status=403))
    with pytest.raises(SourceFetchFailed):
        propertyroom.PropertyRoomSource().discover()


def test_discover_raises_on_transport_error(monkeypatch):
    calls = []

    def boom(url, **kw):
        calls.append(url)
        raise requests.exceptions.ConnectionError("proxy down")
    monkeypatch.setattr(propertyroom, "polite_get", boom)
    with pytest.raises(SourceFetchFailed):
        propertyroom.PropertyRoomSource().discover()
    # host down stops the sweep after the FIRST term — not one timeout per term
    assert len(calls) == 1


# --- poll() --------------------------------------------------------------

def test_poll_active_lot(monkeypatch):
    monkeypatch.setattr(propertyroom, "polite_get",
                        _router(item_html=_load("lot_active_18986916.html")))
    obs = propertyroom.PropertyRoomSource().poll([{"source_lot_id": "18986916", "end_date": None}])
    assert len(obs) == 1
    o = obs[0]
    assert o.source_lot_id == "18986916"
    assert o.status == "active"
    assert o.current_bid == Decimal("179.00")
    assert o.bid_count == 20
    assert o.end_date == datetime(2026, 10, 19, 23, 30, tzinfo=timezone.utc)
    assert o.raw["category"] == "Rings"
    json.dumps(o.raw)


def test_poll_closed_lot_keeps_final_price_and_bids(monkeypatch):
    monkeypatch.setattr(propertyroom, "polite_get",
                        _router(item_html=_load("lot_closed_18901000.html")))
    obs = propertyroom.PropertyRoomSource().poll([{"source_lot_id": "18901000", "end_date": None}])
    assert len(obs) == 1
    o = obs[0]
    assert o.status == "closed"
    assert o.current_bid == Decimal("130.00")
    assert o.bid_count == 5
    assert o.end_date == datetime(2026, 8, 20, 1, 30, 58, tzinfo=timezone.utc)


def test_poll_closed_no_bid_lot(monkeypatch):
    monkeypatch.setattr(propertyroom, "polite_get",
                        _router(item_html=_load("lot_closed_nobid_18900500.html")))
    obs = propertyroom.PropertyRoomSource().poll([{"source_lot_id": "18900500", "end_date": None}])
    assert [o.status for o in obs] == ["closed"]
    assert obs[0].bid_count == 0
    assert obs[0].current_bid == Decimal("1.00")  # start price; sold_comps' bid_count gate drops it


def test_poll_404_past_end_is_gone(monkeypatch):
    monkeypatch.setattr(propertyroom, "polite_get", _router(item_status=404))
    past = datetime.now(timezone.utc) - timedelta(days=1)
    obs = propertyroom.PropertyRoomSource().poll([{"source_lot_id": "18901000", "end_date": past}])
    assert [o.status for o in obs] == ["gone"]
    assert obs[0].raw["recorder_probe"]["http_status"] == 404


def test_poll_404_before_end_is_nothing(monkeypatch):
    monkeypatch.setattr(propertyroom, "polite_get", _router(item_status=404))
    future = datetime.now(timezone.utc) + timedelta(days=1)
    assert propertyroom.PropertyRoomSource().poll(
        [{"source_lot_id": "18901000", "end_date": future}]) == []


def test_poll_403_is_a_failure_not_gone(monkeypatch):
    # geo-block / UA wall: no observation, retried next run
    monkeypatch.setattr(propertyroom, "polite_get", _router(item_status=403))
    past = datetime.now(timezone.utc) - timedelta(days=1)
    assert propertyroom.PropertyRoomSource().poll(
        [{"source_lot_id": "18901000", "end_date": past}]) == []


def test_poll_unrecognised_page_is_a_failure(monkeypatch):
    monkeypatch.setattr(propertyroom, "polite_get",
                        _router(item_html="<html><body>maintenance</body></html>"))
    past = datetime.now(timezone.utc) - timedelta(days=1)
    assert propertyroom.PropertyRoomSource().poll(
        [{"source_lot_id": "18901000", "end_date": past}]) == []


# --- sold_sweep() --------------------------------------------------------

def test_sold_sweep_is_empty_no_network(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("sold_sweep must not touch the network")
    monkeypatch.setattr(propertyroom, "polite_get", boom)
    assert propertyroom.PropertyRoomSource().sold_sweep() == []
