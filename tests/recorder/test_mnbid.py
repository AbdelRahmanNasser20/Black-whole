"""Offline tests for the Minnesota MNBid adapter, against API responses
captured live 2026-10-09 (see recorder/sources/mnbid.py). No network —
polite_post is monkeypatched. The captured pages held no furniture (vehicles
only), so furniture cases rename one captured record.
"""
import copy
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
import requests

from recorder.models import Observation
from recorder.sources import mnbid
from recorder.sources.base import SourceFetchFailed

FIXTURES = Path(__file__).parent / "fixtures" / "mnbid"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


class _Resp:
    def __init__(self, body, status_code=200, url=mnbid.SEARCH_URL):
        self._body = body
        self.status_code = status_code
        self.url = url

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


def _with_furniture(body: dict, n: int = 2) -> dict:
    body = copy.deepcopy(body)
    recs = body["data"]["responseData"]["records"]
    for i, r in enumerate(recs[:n]):
        r["title"] = f"Lot of {10 + i} stacking chairs"
    body["data"]["responseData"]["totalRecords"] = len(recs)
    return body


def _router(*, open_body=None, sold_body=None, single_body=None, search_status=200, calls=None):
    def fake(url, *, json=None, headers=None, **kw):
        if calls is not None:
            calls.append((url, json, headers))
        if url == mnbid.SEARCH_URL:
            if search_status != 200:
                return _Resp({}, search_status, url)
            ms = json["market_status"][0]
            return _Resp(sold_body if ms == "sold" else open_body, 200, url)
        if url == mnbid.BIDHISTORY_URL:
            return _Resp(_load("bidhistory_16477.json"), 200, url)
        if url == mnbid.SINGLE_URL:
            return _Resp(single_body, 200, url)
        raise AssertionError(url)
    return fake


# --- discover() --------------------------------------------------------

def test_discover_returns_furniture_with_bid_counts(monkeypatch):
    calls = []
    body = _with_furniture(_load("search_open_page1.json"))
    monkeypatch.setattr(mnbid, "polite_post", _router(open_body=body, calls=calls))
    obs = mnbid.MNBidSource().discover()
    assert len(obs) == 2
    for o in obs:
        assert isinstance(o, Observation)
        assert o.source == "mnbid"
        assert o.source_lot_id.isdigit()
        assert o.status == "active"
        assert o.bid_count == 3
        assert isinstance(o.current_bid, Decimal)
        assert o.end_date is not None and o.end_date.tzinfo is not None
        for key in ("title", "city", "state", "category", "image_url", "url"):
            assert key in o.raw
        assert o.raw["state"] == "MN"
        assert o.raw["url"] == f"https://mnbid.mn.gov/productView/{o.source_lot_id}"
        json.dumps(o.raw)
    # every call carries the frontend Origin (else "Front Domain Mismatch")
    assert all(c[2]["Origin"] == mnbid.FRONTEND_URL for c in calls)
    search = [c for c in calls if c[0] == mnbid.SEARCH_URL]
    assert search[0][1]["market_status"] == ["open"]


def test_discover_dates_are_utc():
    rec = _load("search_open_page1.json")["data"]["responseData"]["records"][0]
    assert mnbid._parse_iso(rec["date_closed"]) == datetime(2026, 10, 19, 15, 25, tzinfo=timezone.utc)


def test_discover_skips_vehicles_clean_zero(monkeypatch):
    monkeypatch.setattr(mnbid, "polite_post", _router(open_body=_load("search_open_page1.json")))
    assert mnbid.MNBidSource().discover() == []


def test_discover_raises_on_domain_mismatch(monkeypatch):
    monkeypatch.setattr(mnbid, "polite_post", _router(open_body=_load("error_domain_mismatch.json")))
    with pytest.raises(SourceFetchFailed):
        mnbid.MNBidSource().discover()


def test_discover_raises_on_http_error(monkeypatch):
    monkeypatch.setattr(mnbid, "polite_post", _router(search_status=503))
    with pytest.raises(SourceFetchFailed):
        mnbid.MNBidSource().discover()


def test_discover_raises_on_transport_error(monkeypatch):
    def boom(*a, **k):
        raise requests.exceptions.ConnectionError("down")
    monkeypatch.setattr(mnbid, "polite_post", boom)
    with pytest.raises(SourceFetchFailed):
        mnbid.MNBidSource().discover()


def test_search_payload_shape():
    p = mnbid.search_payload("sold", 2, orderby="p.date_closed, desc")
    assert p["market_status"] == ["sold"]
    assert p["page"] == 2 and p["filters"] == {}


# --- poll() ------------------------------------------------------------

def test_poll_reads_single_product(monkeypatch):
    monkeypatch.setattr(mnbid, "polite_post", _router(single_body=_load("single_16477.json")))
    obs = mnbid.MNBidSource().poll([{"source_lot_id": "16477", "end_date": None}])
    assert len(obs) == 1
    assert obs[0].source_lot_id == "16477"
    assert obs[0].current_bid == Decimal("295")
    assert obs[0].bid_count == 3


def test_poll_empty_records_past_end_is_gone(monkeypatch):
    body = copy.deepcopy(_load("single_16477.json"))
    body["data"]["responseData"]["records"] = []
    monkeypatch.setattr(mnbid, "polite_post", _router(single_body=body))
    past = datetime.now(timezone.utc) - timedelta(days=1)
    obs = mnbid.MNBidSource().poll([{"source_lot_id": "16477", "end_date": past}])
    assert [o.status for o in obs] == ["gone"]


def test_poll_api_error_is_no_observation(monkeypatch):
    monkeypatch.setattr(mnbid, "polite_post", _router(single_body=_load("error_domain_mismatch.json")))
    past = datetime.now(timezone.utc) - timedelta(days=1)
    assert mnbid.MNBidSource().poll([{"source_lot_id": "16477", "end_date": past}]) == []


def test_poll_non_numeric_id_skipped(monkeypatch):
    monkeypatch.setattr(mnbid, "polite_post", _router())
    assert mnbid.MNBidSource().poll([{"source_lot_id": "abc", "end_date": None}]) == []


# --- sold_sweep() ------------------------------------------------------

def test_sold_sweep_furniture_closed(monkeypatch):
    body = _with_furniture(_load("search_sold_page1.json"), n=1)
    monkeypatch.setattr(mnbid, "polite_post", _router(sold_body=body))
    obs = mnbid.MNBidSource().sold_sweep()
    assert len(obs) == 1
    assert obs[0].status == "closed"


def test_sold_sweep_failure_is_empty(monkeypatch):
    monkeypatch.setattr(mnbid, "polite_post", _router(search_status=500))
    assert mnbid.MNBidSource().sold_sweep() == []
