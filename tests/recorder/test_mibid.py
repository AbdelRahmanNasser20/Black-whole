"""Offline tests for the MiBid (Michigan) adapter, against fixtures captured
live 2026-07-31 (see recorder/sources/mibid.py's module docstring for the
verified endpoints/markup). No network calls — polite_get is monkeypatched.
"""
import json
from decimal import Decimal
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from recorder.models import Observation
from recorder.sources import mibid

FIXTURES = Path(__file__).parent / "fixtures" / "mibid"


def _load_text(name: str) -> str:
    return (FIXTURES / name).read_text()


def _load_json(name: str):
    return json.loads((FIXTURES / name).read_text())


class _FakeResponse:
    def __init__(self, text=None, json_data=None, status_code=200, url="https://mibid.michigan.gov/fake"):
        self._text = text
        self._json = json_data
        self.status_code = status_code
        self.url = url

    @property
    def text(self):
        return self._text

    def json(self):
        if self._json is not None:
            return self._json
        return json.loads(self._text)


@pytest.fixture
def discover_html():
    return _load_text("discover_sample.html")


@pytest.fixture
def detail_active_html():
    return _load_text("detail_active_8006.html")


@pytest.fixture
def detail_closed_html():
    return _load_text("detail_closed_5342.html")


@pytest.fixture
def detail_locked_html():
    return _load_text("detail_locked_soft404.html")


@pytest.fixture
def basic_info_active():
    return _load_json("basic_info_active_8006.json")


@pytest.fixture
def basic_info_closed():
    return _load_json("basic_info_closed_5342.json")


@pytest.fixture
def basic_info_by_guid():
    return _load_json("basic_info_by_guid.json")


def _sample_items(discover_html):
    idx = discover_html.find(mibid.RAW_AUCTIONS_MARKER)
    start = idx + len(mibid.RAW_AUCTIONS_MARKER)
    data, _end = json.JSONDecoder().raw_decode(discover_html, start)
    return data


# --- discover() --------------------------------------------------------

def test_discover_finds_zero_active_furniture_live_at_recon_but_parses_cleanly(monkeypatch, discover_html, capsys):
    # Verified live 2026-07-31: ALL 36 real FURNITURE_TERMS matches on MiBid
    # were status=4 (closed) — zero currently-active furniture lots. The
    # fixture reflects that honestly (0 active furniture items in the
    # sample); this proves discover()'s fetch+filter pipeline runs cleanly
    # and fires the required WARNING rather than silently returning [].
    monkeypatch.setattr(mibid, "polite_get", lambda *a, **k: _FakeResponse(text=discover_html))
    obs = mibid.MiBidSource().discover()
    assert obs == []
    out = capsys.readouterr().out
    assert "WARNING" in out
    assert "0 active furniture lots" in out


def test_discover_active_furniture_path_via_synthetic_status_override(monkeypatch, discover_html, basic_info_closed):
    # No live active-furniture example exists at recon time (see above) — this
    # exercises the active-parsing path with a real closed-furniture item
    # dict, status-overridden to an active code, via a hand-built page. This
    # is a pure-function/path test, not a claim that this payload was
    # observed live in this shape.
    items = _sample_items(discover_html)
    real_item = next(it for it in items if it["id"] == 5342).copy()
    real_item["status"] = 1  # active
    real_item["isCanceled"] = False
    page = (
        "<script>let AuctionListViewModel = function () { this.rawAuctions = "
        "ko.observableArray(" + json.dumps([real_item]) + "); };</script>"
    )

    def fake_get(url, *, headers=None, params=None, timeout=30):
        if url == mibid.HOME_URL:
            return _FakeResponse(text=page)
        assert url == mibid.BASIC_INFO_URL
        assert params["guid"] == real_item["guid"]
        return _FakeResponse(json_data=basic_info_closed)

    monkeypatch.setattr(mibid, "polite_get", fake_get)
    obs = mibid.MiBidSource().discover()
    assert len(obs) == 1
    o = obs[0]
    assert o.source == "mibid"
    assert o.source_lot_id == real_item["guid"]
    assert o.status == "active"
    assert o.raw == real_item
    assert o.bid_count == basic_info_closed["numberOfBids"]
    assert o.current_bid == Decimal(str(basic_info_closed["currentBid"]))
    assert o.end_date.tzinfo is not None


def test_discover_excludes_closed_scheduled_and_canceled(monkeypatch, discover_html):
    monkeypatch.setattr(mibid, "polite_get", lambda *a, **k: _FakeResponse(text=discover_html))
    obs = mibid.MiBidSource().discover()
    items = _sample_items(discover_html)
    excluded_guids = {it["guid"] for it in items if it["status"] not in mibid.ACTIVE_STATUS_CODES or it.get("isCanceled")}
    obs_guids = {o.source_lot_id for o in obs}
    assert not (excluded_guids & obs_guids)


# --- sold_sweep() --------------------------------------------------------

def test_sold_sweep_parses_all_closed_furniture_matches(monkeypatch, discover_html, basic_info_by_guid):
    monkeypatch.setattr(mibid, "polite_get", lambda *a, **k: _FakeResponse(text=discover_html))
    _stub_basic_info(monkeypatch, basic_info_by_guid)
    obs = mibid.MiBidSource().sold_sweep()
    assert len(obs) == 8  # all 8 real closed-furniture items in the curated fixture
    assert all(o.status == "closed" for o in obs)


def test_sold_sweep_enriches_bid_count_from_get_basic_info(monkeypatch, discover_html, basic_info_by_guid):
    monkeypatch.setattr(mibid, "polite_get", lambda *a, **k: _FakeResponse(text=discover_html))
    _stub_basic_info(monkeypatch, basic_info_by_guid)
    obs = mibid.MiBidSource().sold_sweep()
    by_guid = {o.source_lot_id: o for o in obs}
    # id 5342 "Eight (8) Green Haworth Chairs on Pallet #3" — bulk embed's
    # numberOfBids is always 0 (see module docstring); the real value (33)
    # only comes from GetBasicInfo.
    target = basic_info_by_guid["902bbd12-8a45-f111-9243-005056936aaa"]
    assert by_guid["902bbd12-8a45-f111-9243-005056936aaa"].bid_count == target["numberOfBids"] == 33
    assert by_guid["902bbd12-8a45-f111-9243-005056936aaa"].raw["numberOfBids"] == 0  # raw stays untouched


def test_sold_sweep_raw_roundtrips_unmodified(monkeypatch, discover_html, basic_info_by_guid):
    monkeypatch.setattr(mibid, "polite_get", lambda *a, **k: _FakeResponse(text=discover_html))
    _stub_basic_info(monkeypatch, basic_info_by_guid)
    obs = mibid.MiBidSource().sold_sweep()
    items = _sample_items(discover_html)
    by_guid = {it["guid"]: it for it in items}
    for o in obs:
        assert o.raw == by_guid[o.source_lot_id]


def test_sold_sweep_returns_empty_on_fetch_failure(monkeypatch):
    monkeypatch.setattr(mibid, "polite_get", lambda *a, **k: _FakeResponse(status_code=403))
    assert mibid.MiBidSource().sold_sweep() == []


def _stub_basic_info(monkeypatch, basic_info_by_guid):
    orig_polite_get = mibid.polite_get

    def fake_get(url, *, headers=None, params=None, timeout=30):
        if url == mibid.BASIC_INFO_URL:
            guid = params["guid"]
            return _FakeResponse(json_data=basic_info_by_guid[guid])
        return orig_polite_get(url, headers=headers, params=params, timeout=timeout)

    monkeypatch.setattr(mibid, "polite_get", fake_get)


# --- poll() --------------------------------------------------------

def test_poll_active_lot_derives_status_from_end_date_not_hardcoded(monkeypatch, detail_active_html, basic_info_active):
    def fake_get(url, *, headers=None, params=None, timeout=30):
        if url == mibid.BASIC_INFO_URL:
            return _FakeResponse(json_data=basic_info_active)
        return _FakeResponse(text=detail_active_html, url="https://mibid.michigan.gov/AuctionBid/Index/4ac9b7c8-8887-f111-925a-005056936aaa")

    monkeypatch.setattr(mibid, "polite_get", fake_get)
    obs = mibid.MiBidSource().poll([{"source_lot_id": "4ac9b7c8-8887-f111-925a-005056936aaa"}])
    assert len(obs) == 1
    o = obs[0]
    assert o.status == "active"  # end_date (8/3/2026) is in the future relative to "now"
    assert o.end_date == datetime(2026, 8, 3, 14, 0, 0, tzinfo=timezone.utc)  # 10:00 AM EDT (UTC-4)
    assert o.current_bid == Decimal("520.00")
    assert o.bid_count == 3


def test_poll_closed_lot_derives_status_from_past_end_date(monkeypatch, detail_closed_html, basic_info_closed):
    def fake_get(url, *, headers=None, params=None, timeout=30):
        if url == mibid.BASIC_INFO_URL:
            return _FakeResponse(json_data=basic_info_closed)
        return _FakeResponse(text=detail_closed_html)

    monkeypatch.setattr(mibid, "polite_get", fake_get)
    obs = mibid.MiBidSource().poll([{"source_lot_id": "902bbd12-8a45-f111-9243-005056936aaa"}])
    assert len(obs) == 1
    o = obs[0]
    assert o.status == "closed"
    assert o.end_date == datetime(2025, 8, 18, 14, 10, 0, tzinfo=timezone.utc)  # 10:10 AM EDT (UTC-4)
    assert o.current_bid == Decimal("82.00")
    assert o.bid_count == 33


def test_poll_soft_locked_page_after_end_date_emits_gone(monkeypatch, detail_locked_html):
    monkeypatch.setattr(mibid, "polite_get", lambda *a, **k: _FakeResponse(text=detail_locked_html))
    past_end = datetime.now(timezone.utc) - timedelta(hours=1)
    obs = mibid.MiBidSource().poll([{"source_lot_id": "00000000-0000-0000-0000-000000000000", "end_date": past_end}])
    assert len(obs) == 1
    assert obs[0].status == "gone"
    assert obs[0].raw["recorder_probe"]["result"] == "not_found"
    assert obs[0].raw["recorder_probe"]["http_status"] == 200


def test_poll_real_404_after_end_date_emits_gone(monkeypatch):
    # verified live 2026-07-31: a genuinely random nonexistent guid returns
    # a real HTTP 404 with an empty body.
    monkeypatch.setattr(mibid, "polite_get", lambda *a, **k: _FakeResponse(text="", status_code=404))
    past_end = datetime.now(timezone.utc) - timedelta(hours=1)
    obs = mibid.MiBidSource().poll([{"source_lot_id": "25fad18a-04c1-4c62-8180-865cd53b39b1", "end_date": past_end}])
    assert len(obs) == 1
    assert obs[0].status == "gone"
    assert obs[0].raw["recorder_probe"]["http_status"] == 404


def test_poll_not_found_before_end_date_emits_nothing(monkeypatch, detail_locked_html):
    monkeypatch.setattr(mibid, "polite_get", lambda *a, **k: _FakeResponse(text=detail_locked_html))
    future_end = datetime.now(timezone.utc) + timedelta(hours=1)
    obs = mibid.MiBidSource().poll([{"source_lot_id": "00000000-0000-0000-0000-000000000000", "end_date": future_end}])
    assert obs == []


def test_poll_not_found_with_unknown_end_date_emits_nothing(monkeypatch, detail_locked_html):
    monkeypatch.setattr(mibid, "polite_get", lambda *a, **k: _FakeResponse(text=detail_locked_html))
    obs = mibid.MiBidSource().poll([{"source_lot_id": "00000000-0000-0000-0000-000000000000", "end_date": None}])
    assert obs == []


def test_poll_get_basic_info_failure_still_emits_observation_with_none_bid_fields(monkeypatch, detail_active_html):
    def fake_get(url, *, headers=None, params=None, timeout=30):
        if url == mibid.BASIC_INFO_URL:
            return _FakeResponse(status_code=500)
        return _FakeResponse(text=detail_active_html)

    monkeypatch.setattr(mibid, "polite_get", fake_get)
    obs = mibid.MiBidSource().poll([{"source_lot_id": "4ac9b7c8-8887-f111-925a-005056936aaa"}])
    assert len(obs) == 1
    assert obs[0].current_bid is None
    assert obs[0].bid_count is None
    assert obs[0].end_date is not None  # detail-page data still made it through


def test_poll_fetch_failure_for_detail_page_skips_lot_without_raising(monkeypatch, capsys):
    def raise_connection_error(*a, **k):
        raise mibid.requests.exceptions.ConnectionError("boom")

    monkeypatch.setattr(mibid, "polite_get", raise_connection_error)
    obs = mibid.MiBidSource().poll([{"source_lot_id": "x", "end_date": datetime.now(timezone.utc) - timedelta(hours=1)}])
    assert obs == []
    out = capsys.readouterr().out
    assert "RECORDER ERROR" in out


def test_poll_fetch_failure_for_one_lot_does_not_block_others(monkeypatch, detail_active_html, basic_info_active, capsys):
    def fake_get(url, *, headers=None, params=None, timeout=30):
        if "bad-guid" in url:
            return _FakeResponse(status_code=403)
        if url == mibid.BASIC_INFO_URL:
            return _FakeResponse(json_data=basic_info_active)
        return _FakeResponse(text=detail_active_html)

    monkeypatch.setattr(mibid, "polite_get", fake_get)
    obs = mibid.MiBidSource().poll([
        {"source_lot_id": "bad-guid", "end_date": datetime.now(timezone.utc) - timedelta(hours=1)},
        {"source_lot_id": "4ac9b7c8-8887-f111-925a-005056936aaa"},
    ])
    assert len(obs) == 1
    assert obs[0].source_lot_id == "4ac9b7c8-8887-f111-925a-005056936aaa"
    out = capsys.readouterr().out
    assert "RECORDER ERROR" in out
    assert "bad-guid" in out


def test_poll_empty_lots_makes_no_request(monkeypatch):
    def fail_get(*a, **k):
        raise AssertionError("poll([]) must not hit the network")

    monkeypatch.setattr(mibid, "polite_get", fail_get)
    assert mibid.MiBidSource().poll([]) == []


def test_poll_unrecognized_page_shape_is_a_fetch_failure(monkeypatch, capsys):
    monkeypatch.setattr(mibid, "polite_get", lambda *a, **k: _FakeResponse(text="<title>Something Else - MiBid</title>"))
    obs = mibid.MiBidSource().poll([{"source_lot_id": "x", "end_date": datetime.now(timezone.utc) - timedelta(hours=1)}])
    assert obs == []
    out = capsys.readouterr().out
    assert "RECORDER ERROR" in out


# --- HTTP / fetch-failure handling (discover/sold_sweep share _fetch_raw_auctions) ---

def test_discover_returns_empty_on_403_without_raising(monkeypatch):
    monkeypatch.setattr(mibid, "polite_get", lambda *a, **k: _FakeResponse(status_code=403))
    assert mibid.MiBidSource().discover() == []


def test_discover_returns_empty_on_missing_marker(monkeypatch):
    monkeypatch.setattr(mibid, "polite_get", lambda *a, **k: _FakeResponse(text="<html>no marker here</html>"))
    assert mibid.MiBidSource().discover() == []


def test_discover_returns_empty_and_prints_loud_error_on_connection_exception(monkeypatch, capsys):
    def raise_connection_error(*a, **k):
        raise mibid.requests.exceptions.ConnectionError("boom")

    monkeypatch.setattr(mibid, "polite_get", raise_connection_error)
    assert mibid.MiBidSource().discover() == []
    out = capsys.readouterr().out
    assert "RECORDER ERROR" in out


# --- parsing helpers --------------------------------------------------------

def test_status_from_code_maps_active_and_closed():
    assert mibid._status_from_code(1) == "active"
    assert mibid._status_from_code(2) == "active"
    assert mibid._status_from_code(3) == "active"
    assert mibid._status_from_code(4) == "closed"
    assert mibid._status_from_code(5) is None
    assert mibid._status_from_code(6) is None


def test_parse_dt_converts_et_to_utc_edt():
    dt = mibid._parse_dt("2026-08-03T10:00:00")
    assert dt == datetime(2026, 8, 3, 14, 0, 0, tzinfo=timezone.utc)


def test_parse_dt_handles_est_season_too():
    dt = mibid._parse_dt("2026-01-15T09:00:00")
    assert dt == datetime(2026, 1, 15, 14, 0, 0, tzinfo=timezone.utc)


def test_parse_dt_handles_missing():
    assert mibid._parse_dt(None) is None
    assert mibid._parse_dt("") is None


def test_parse_detail_dt_matches_parse_dt_for_same_instant():
    # the bulk-embed endTime and the detail-page dayjs literal encode the
    # same wall-clock value for the same auction — verified live for #8006.
    a = mibid._parse_dt("2026-08-03T10:00:00")
    b = mibid._parse_detail_dt("8/3/2026 10:00:00 AM")
    assert a == b


def test_parse_money_and_int_helpers():
    assert mibid._parse_money("25.00") == Decimal("25.00")
    assert mibid._parse_money(25.0) == Decimal("25.0")
    assert mibid._parse_money(None) is None
    assert mibid._int_or_none("3") == 3
    assert mibid._int_or_none(None) is None


def test_is_furniture_matches_shared_terms():
    assert mibid._is_furniture("Eight (8) Green Haworth Chairs on Pallet #3")
    assert mibid._is_furniture("6 Piece Steelcase Conference Table with 10 Haworth Chairs")
    assert not mibid._is_furniture("2018 Chevrolet Express 3DR RWD Van")
