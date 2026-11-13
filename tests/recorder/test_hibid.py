"""Offline tests for the HiBid adapter, against GraphQL responses captured
live 2026-10-16 (see recorder/sources/hibid.py). No network — polite_post is
monkeypatched. Fixture facts the assertions lean on:

- `search_open_page1.json`: 10 OPEN lots ("folding chairs", TIME_LEFT ASC),
  7 of which pass the `FURNITURE_TERMS` re-check.
- `search_open_stackable.json`: 10 lots incl. one `POSTED` with a NEGATIVE
  `timeLeftSeconds` and one OPEN with `timeLeftSeconds: 0`.
- `search_archive_page1.json`: 10 CLOSED archive rows — sold ones carry
  `priceRealized` > 0, no-sale ones 0; archive `bidCount` is always 0.
- `lot_active.json` / `lot_closed.json` / `lot_not_found.json`: GetLotDetails
  bodies (`accessability: EVENTITEM_NOT_FOUND` + `lot: null` = purged).
- `error_invalid_value.json`: a GraphQL `errors` body.
"""
import copy
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
import requests

from recorder.models import Observation
from recorder.sources import hibid
from recorder.sources.base import SourceFetchFailed

FIXTURES = Path(__file__).parent / "fixtures" / "hibid"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


class _Resp:
    def __init__(self, body, status_code=200, url=hibid.GRAPHQL_URL):
        self._body = body
        self.status_code = status_code
        self.url = url

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


def _router(*, open_body=None, archive_body=None, lot_bodies=None, http_status=200, calls=None):
    """Dispatch on operationName + isArchive; `lot_bodies` maps lotId → body."""
    def fake(url, *, json=None, headers=None, **kw):
        if calls is not None:
            calls.append((url, json, headers))
        assert url == hibid.GRAPHQL_URL
        if http_status != 200:
            return _Resp({}, http_status, url)
        op = json.get("operationName")
        if op == "LotSearch":
            body = archive_body if json["variables"]["isArchive"] else open_body
            return _Resp(body, 200, url)
        if op == "GetLotDetails":
            return _Resp(lot_bodies[str(json["variables"]["lotId"])], 200, url)
        raise AssertionError(op)
    return fake


def _scrub_furniture(body: dict) -> dict:
    """Every row rewritten so nothing matches FURNITURE_TERMS — the 'fetched
    fine, matched 0 lots' case."""
    body = copy.deepcopy(body)
    for r in body["data"]["lotSearch"]["pagedResults"]["results"]:
        r["lead"] = "2014 Ford F-150 pickup truck"
        r["description"] = "Runs and drives."
        r["category"] = [{"categoryName": "Vehicles", "fullCategory": "Vehicles - Trucks"}]
    return body


# --- discover() --------------------------------------------------------

def test_discover_returns_furniture_lots(monkeypatch):
    calls = []
    monkeypatch.setattr(hibid, "polite_post",
                        _router(open_body=_load("search_open_page1.json"), calls=calls))
    obs = hibid.HiBidSource().discover()
    assert len(obs) == 7          # 10 rows, 7 pass the FURNITURE_TERMS re-check
    for o in obs:
        assert isinstance(o, Observation)
        assert o.source == "hibid"
        assert o.source_lot_id.isdigit()
        assert o.status == "active"
        assert isinstance(o.bid_count, int)
        assert isinstance(o.current_bid, Decimal)
        for key in ("title", "city", "state", "category", "image_url", "url", "currency"):
            assert key in o.raw
        assert o.raw["url"] == f"https://hibid.com/lot/{o.source_lot_id}"
        assert o.raw["currency"] == "USD"
        assert "hibid" in o.raw
        json.dumps(o.raw)
    # one search per FURNITURE_TERMS term, dedup'd by lot id; every call is a
    # live (non-archive) OPEN search sorted soonest-close-first
    searches = [c[1]["variables"] for c in calls if c[1]["operationName"] == "LotSearch"]
    assert len(searches) == len(hibid.FURNITURE_TERMS)
    assert all(v["isArchive"] is False and v["status"] == "OPEN" and
               v["sortOrder"] == "TIME_LEFT" and v["sortDirection"] == "ASC" and
               v["countryName"] == "United States" for v in searches)


def test_discover_end_dates_are_utc_minute_rounded(monkeypatch):
    monkeypatch.setattr(hibid, "polite_post", _router(open_body=_load("search_open_page1.json")))
    before = datetime.now(timezone.utc)
    obs = hibid.HiBidSource().discover()
    after = datetime.now(timezone.utc)
    for o in obs:
        assert o.end_date is not None
        assert o.end_date.tzinfo is not None and o.end_date.utcoffset().total_seconds() == 0
        assert o.end_date.second == 0 and o.end_date.microsecond == 0
        seconds = float(o.raw["hibid"]["lotState"]["timeLeftSeconds"])
        assert (before + timedelta(seconds=seconds - 61)) <= o.end_date \
            <= (after + timedelta(seconds=seconds + 61))


def test_discover_posted_and_zero_clock_lots_are_active_without_end_date(monkeypatch):
    monkeypatch.setattr(hibid, "polite_post",
                        _router(open_body=_load("search_open_stackable.json")))
    obs = {o.source_lot_id: o for o in hibid.HiBidSource().discover()}
    assert len(obs) == 10
    posted = obs["324066163"]       # POSTED, timeLeftSeconds negative
    assert posted.status == "active" and posted.end_date is None
    zero = obs["326615155"]         # OPEN, timeLeftSeconds == 0
    assert zero.status == "active" and zero.end_date is None


def test_discover_clean_zero_matches_returns_empty(monkeypatch):
    monkeypatch.setattr(hibid, "polite_post",
                        _router(open_body=_scrub_furniture(_load("search_open_page1.json"))))
    assert hibid.HiBidSource().discover() == []


def test_discover_raises_on_http_error(monkeypatch):
    monkeypatch.setattr(hibid, "polite_post", _router(http_status=403))
    with pytest.raises(SourceFetchFailed):
        hibid.HiBidSource().discover()


def test_discover_raises_on_graphql_errors_body(monkeypatch):
    monkeypatch.setattr(hibid, "polite_post",
                        _router(open_body=_load("error_invalid_value.json")))
    with pytest.raises(SourceFetchFailed):
        hibid.HiBidSource().discover()


def test_discover_raises_on_transport_error(monkeypatch):
    def boom(*a, **k):
        raise requests.exceptions.ConnectionError("down")
    monkeypatch.setattr(hibid, "polite_post", boom)
    with pytest.raises(SourceFetchFailed):
        hibid.HiBidSource().discover()


def test_max_pages_per_term_env(monkeypatch):
    monkeypatch.delenv("RECORDER_HIBID_MAX_PAGES_PER_TERM", raising=False)
    assert hibid.max_pages_per_term() == 1
    monkeypatch.setenv("RECORDER_HIBID_MAX_PAGES_PER_TERM", "3")
    assert hibid.max_pages_per_term() == 3
    monkeypatch.setenv("RECORDER_HIBID_MAX_PAGES_PER_TERM", "junk")
    assert hibid.max_pages_per_term() == 1


# --- poll() ------------------------------------------------------------

def test_poll_reads_lot_details(monkeypatch):
    monkeypatch.setattr(hibid, "polite_post",
                        _router(lot_bodies={"325447181": _load("lot_active.json")}))
    obs = hibid.HiBidSource().poll([{"source_lot_id": "325447181", "end_date": None}])
    assert len(obs) == 1
    o = obs[0]
    assert o.source_lot_id == "325447181"
    assert o.status == "active"
    assert o.end_date is not None and o.end_date.tzinfo is not None
    assert o.raw["city"] == "Avilla" and o.raw["state"] == "IN"


def test_poll_closed_lot_records_price_realized(monkeypatch):
    monkeypatch.setattr(hibid, "polite_post",
                        _router(lot_bodies={"326531422": _load("lot_closed.json")}))
    obs = hibid.HiBidSource().poll([{"source_lot_id": "326531422", "end_date": None}])
    assert len(obs) == 1
    assert obs[0].status == "closed"
    assert obs[0].current_bid == Decimal("28.0")
    assert obs[0].end_date is None      # naive auctioneer-local clocks are never trusted


def test_poll_not_found_past_end_is_gone(monkeypatch):
    monkeypatch.setattr(hibid, "polite_post",
                        _router(lot_bodies={"999999999": _load("lot_not_found.json")}))
    past = datetime.now(timezone.utc) - timedelta(days=1)
    obs = hibid.HiBidSource().poll([{"source_lot_id": "999999999", "end_date": past}])
    assert [o.status for o in obs] == ["gone"]
    assert obs[0].raw["recorder_probe"]["result"] == "not_found"


def test_poll_not_found_before_end_is_no_observation(monkeypatch):
    monkeypatch.setattr(hibid, "polite_post",
                        _router(lot_bodies={"999999999": _load("lot_not_found.json")}))
    future = datetime.now(timezone.utc) + timedelta(days=1)
    assert hibid.HiBidSource().poll([{"source_lot_id": "999999999", "end_date": future}]) == []


def test_poll_graphql_error_is_no_observation(monkeypatch):
    monkeypatch.setattr(hibid, "polite_post",
                        _router(lot_bodies={"326531422": _load("error_invalid_value.json")}))
    past = datetime.now(timezone.utc) - timedelta(days=1)
    assert hibid.HiBidSource().poll([{"source_lot_id": "326531422", "end_date": past}]) == []


def test_poll_budget_aborts_batch(monkeypatch):
    attempts = []

    def boom(*a, **k):
        attempts.append(1)
        raise requests.exceptions.ConnectionError("down")
    monkeypatch.setattr(hibid, "polite_post", boom)
    monkeypatch.setenv("RECORDER_POLL_MAX_CONSECUTIVE_FAILURES", "3")
    lots = [{"source_lot_id": str(n), "end_date": None} for n in range(10)]
    assert hibid.HiBidSource().poll(lots) == []
    assert len(attempts) == 3


# --- sold_sweep() ------------------------------------------------------

def test_sold_sweep_closed_lots_with_finals(monkeypatch):
    calls = []
    monkeypatch.setattr(hibid, "polite_post",
                        _router(archive_body=_load("search_archive_page1.json"), calls=calls))
    obs = {o.source_lot_id: o for o in hibid.HiBidSource().sold_sweep()}
    assert len(obs) == 10
    assert all(o.status == "closed" for o in obs.values())
    assert all(o.end_date is None for o in obs.values())
    sold = obs["326531422"]
    assert sold.current_bid == Decimal("28.0")      # priceRealized, the real final
    no_sale = obs["325864243"]
    assert no_sale.current_bid == Decimal("0.0")    # highBid fallback when nothing realized
    searches = [c[1]["variables"] for c in calls]
    assert all(v["isArchive"] is True and v["sortDirection"] == "DESC" for v in searches)


def test_sold_sweep_failure_is_empty(monkeypatch):
    monkeypatch.setattr(hibid, "polite_post", _router(http_status=500))
    assert hibid.HiBidSource().sold_sweep() == []


# --- parsing helpers ---------------------------------------------------

def test_is_furniture_reads_title_description_and_category():
    assert hibid._is_furniture({"lead": "Lot of 60 folding chairs", "description": "",
                                "category": []})
    assert hibid._is_furniture({"lead": "Misc pallet", "description": "Includes banquet tables",
                                "category": []})
    assert hibid._is_furniture({"lead": "Bar stools", "description": "",
                                "category": [{"fullCategory": "Furniture - Chairs / Stools"}]})
    assert not hibid._is_furniture({"lead": "2014 Ford F-150", "description": "Runs",
                                    "category": [{"fullCategory": "Vehicles - Trucks"}]})


def test_end_date_rounds_to_minute_and_handles_bad_clock():
    now = datetime(2026, 10, 16, 15, 0, 20, tzinfo=timezone.utc)
    lot = {"lotState": {"isClosed": False, "status": "OPEN", "timeLeftSeconds": 95.4}}
    assert hibid._end_date(lot, now=now) == datetime(2026, 10, 16, 15, 2, tzinfo=timezone.utc)
    lot["lotState"]["timeLeftSeconds"] = 0
    assert hibid._end_date(lot, now=now) is None
    lot["lotState"]["timeLeftSeconds"] = "not-a-number"
    assert hibid._end_date(lot, now=now) is None
    lot["lotState"]["timeLeftSeconds"] = 9999
    lot["lotState"]["isClosed"] = True
    assert hibid._end_date(lot, now=now) is None
