"""Offline tests for the Purple Wave adapter, against fixtures captured live
2026-07-31 (see recorder/sources/purple_wave.py's module docstring for the
verified endpoint + params). No network calls — polite_get is monkeypatched.
"""
import json
from decimal import Decimal
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from recorder.models import Observation
from recorder.sources import purple_wave as pw

FIXTURES = Path(__file__).parent / "fixtures" / "purple_wave"


def _load(name: str) -> list[dict]:
    return json.loads((FIXTURES / name).read_text())


class _FakeResponse:
    def __init__(self, payload, status_code=200, url="https://www.purplewave.com/v1/search/search"):
        self._payload = payload
        self.status_code = status_code
        self.url = url

    def json(self):
        return self._payload


@pytest.fixture
def discover_items():
    return _load("discover_furniture.json")


@pytest.fixture
def sold_items():
    return _load("sold_sweep.json")


# --- discover() --------------------------------------------------------

def test_discover_parses_n_geq_5_observations(monkeypatch, discover_items):
    monkeypatch.setattr(pw, "polite_get", lambda *a, **k: _FakeResponse(discover_items))
    source = pw.PurpleWaveSource()
    obs = source.discover()
    assert len(obs) >= 5
    assert all(isinstance(o, Observation) for o in obs)


def test_discover_observations_have_required_shape(monkeypatch, discover_items):
    monkeypatch.setattr(pw, "polite_get", lambda *a, **k: _FakeResponse(discover_items))
    obs = pw.PurpleWaveSource().discover()
    for o in obs:
        assert o.source == "purple_wave"
        assert o.source_lot_id  # non-null / non-empty
        assert o.status in ("active", "closed", "gone")
        assert o.current_bid is None or isinstance(o.current_bid, Decimal)
        assert o.end_date is not None
        assert o.end_date.tzinfo is not None  # tz-aware


def test_discover_raw_roundtrips_source_item_unmodified(monkeypatch, discover_items):
    monkeypatch.setattr(pw, "polite_get", lambda *a, **k: _FakeResponse(discover_items))
    obs = pw.PurpleWaveSource().discover()
    raws = {o.source_lot_id: o.raw for o in obs}
    for item in discover_items:
        assert raws[str(item["id"])] == item


def test_discover_status_active_when_not_closed(monkeypatch, discover_items):
    monkeypatch.setattr(pw, "polite_get", lambda *a, **k: _FakeResponse(discover_items))
    obs = pw.PurpleWaveSource().discover()
    # fixture is the live active-furniture sweep — none are closed
    assert all(o.status == "active" for o in obs)


def test_discover_request_uses_furniture_family_filter(monkeypatch, discover_items):
    captured = {}

    def fake_get(url, *, headers=None, params=None, timeout=30):
        captured["url"] = url
        captured["params"] = params
        return _FakeResponse(discover_items)

    monkeypatch.setattr(pw, "polite_get", fake_get)
    pw.PurpleWaveSource().discover()
    assert captured["url"] == pw.SEARCH_URL
    assert captured["params"]["filters"] == f"family_category_id:{pw.FURNITURE_FAMILY_CATEGORY_ID}"
    assert "dateType" not in captured["params"]


# --- sold_sweep() --------------------------------------------------------

def test_sold_sweep_parses_n_geq_5_closed_observations(monkeypatch, sold_items):
    monkeypatch.setattr(pw, "polite_get", lambda *a, **k: _FakeResponse(sold_items))
    obs = pw.PurpleWaveSource().sold_sweep()
    assert len(obs) >= 5
    assert all(o.status == "closed" for o in obs)


def test_sold_sweep_prices_are_decimal_and_priced(monkeypatch, sold_items):
    monkeypatch.setattr(pw, "polite_get", lambda *a, **k: _FakeResponse(sold_items))
    obs = pw.PurpleWaveSource().sold_sweep()
    for o in obs:
        assert isinstance(o.current_bid, Decimal)
        assert o.current_bid >= 0


def test_sold_sweep_request_adds_date_type_past(monkeypatch, sold_items):
    captured = {}

    def fake_get(url, *, headers=None, params=None, timeout=30):
        captured["params"] = params
        return _FakeResponse(sold_items)

    monkeypatch.setattr(pw, "polite_get", fake_get)
    pw.PurpleWaveSource().sold_sweep()
    assert captured["params"]["dateType"] == "past"
    assert captured["params"]["filters"] == f"family_category_id:{pw.FURNITURE_FAMILY_CATEGORY_ID}"


def test_sold_sweep_raw_roundtrips_unmodified(monkeypatch, sold_items):
    monkeypatch.setattr(pw, "polite_get", lambda *a, **k: _FakeResponse(sold_items))
    obs = pw.PurpleWaveSource().sold_sweep()
    raws = {o.source_lot_id: o.raw for o in obs}
    for item in sold_items:
        assert raws[str(item["id"])] == item


def test_sold_sweep_returns_empty_on_fetch_failure(monkeypatch):
    monkeypatch.setattr(pw, "polite_get", lambda *a, **k: _FakeResponse(None, status_code=403))
    assert pw.PurpleWaveSource().sold_sweep() == []


# --- poll() --------------------------------------------------------

def test_poll_returns_active_observation_for_still_present_lot(monkeypatch, discover_items):
    monkeypatch.setattr(pw, "polite_get", lambda *a, **k: _FakeResponse(discover_items))
    target = discover_items[0]
    lots = [{"source_lot_id": str(target["id"])}]
    obs = pw.PurpleWaveSource().poll(lots)
    assert len(obs) == 1
    assert obs[0].source_lot_id == str(target["id"])
    assert obs[0].status == "active"
    assert obs[0].raw == target


def test_poll_returns_gone_for_vanished_lot_after_its_end_date(monkeypatch, discover_items):
    # fix round 1: 'gone' requires (a) a healthy fetch and (b) the tracked
    # lot's own end_date to have already passed — not mere absence.
    monkeypatch.setattr(pw, "polite_get", lambda *a, **k: _FakeResponse(discover_items))
    past_end = datetime.now(timezone.utc) - timedelta(hours=1)
    lots = [{"source_lot_id": "not-a-real-id-999999", "end_date": past_end}]
    obs = pw.PurpleWaveSource().poll(lots)
    assert len(obs) == 1
    assert obs[0].status == "gone"
    assert obs[0].raw["recorder_probe"]["result"] == "not_found"
    assert obs[0].raw["recorder_probe"]["http_status"] == 200
    assert obs[0].raw["recorder_probe"]["url"] == pw.SEARCH_URL


def test_poll_absent_lot_before_end_date_emits_nothing(monkeypatch, discover_items):
    monkeypatch.setattr(pw, "polite_get", lambda *a, **k: _FakeResponse(discover_items))
    future_end = datetime.now(timezone.utc) + timedelta(hours=1)
    lots = [{"source_lot_id": "not-a-real-id-999999", "end_date": future_end}]
    obs = pw.PurpleWaveSource().poll(lots)
    assert obs == []


def test_poll_absent_lot_with_unknown_end_date_emits_nothing(monkeypatch, discover_items):
    monkeypatch.setattr(pw, "polite_get", lambda *a, **k: _FakeResponse(discover_items))
    lots = [{"source_lot_id": "not-a-real-id-999999", "end_date": None}]
    obs = pw.PurpleWaveSource().poll(lots)
    assert obs == []


def test_poll_fetch_failure_emits_no_observations_even_for_past_end_lots(monkeypatch, capsys):
    # a transient HTTP failure must never mass-mark tracked lots 'gone' —
    # append-only means that mistake would be permanent.
    monkeypatch.setattr(pw, "polite_get", lambda *a, **k: _FakeResponse(None, status_code=403))
    past_end = datetime.now(timezone.utc) - timedelta(hours=1)
    lots = [
        {"source_lot_id": "would-be-gone-1", "end_date": past_end},
        {"source_lot_id": "would-be-active-2", "end_date": None},
    ]
    obs = pw.PurpleWaveSource().poll(lots)
    assert obs == []
    err = capsys.readouterr().out
    assert "RECORDER ERROR" in err
    assert "purple_wave" in err


def test_poll_returns_empty_and_prints_loud_error_on_connection_exception(monkeypatch, capsys):
    def raise_connection_error(*a, **k):
        raise pw.requests.exceptions.ConnectionError("boom")

    monkeypatch.setattr(pw, "polite_get", raise_connection_error)
    past_end = datetime.now(timezone.utc) - timedelta(hours=1)
    obs = pw.PurpleWaveSource().poll([{"source_lot_id": "x", "end_date": past_end}])
    assert obs == []
    out = capsys.readouterr().out
    assert "RECORDER ERROR" in out


def test_poll_empty_lots_makes_no_request(monkeypatch):
    def fail_get(*a, **k):
        raise AssertionError("poll([]) must not hit the network")

    monkeypatch.setattr(pw, "polite_get", fail_get)
    assert pw.PurpleWaveSource().poll([]) == []


# --- HTTP failure handling --------------------------------------------------------

def test_discover_returns_empty_on_403_without_raising(monkeypatch):
    monkeypatch.setattr(pw, "polite_get", lambda *a, **k: _FakeResponse(None, status_code=403))
    assert pw.PurpleWaveSource().discover() == []


def test_discover_returns_empty_on_non_list_payload(monkeypatch):
    monkeypatch.setattr(pw, "polite_get", lambda *a, **k: _FakeResponse({"unexpected": "shape"}))
    assert pw.PurpleWaveSource().discover() == []


def test_discover_returns_empty_and_prints_loud_error_on_connection_exception(monkeypatch, capsys):
    def raise_connection_error(*a, **k):
        raise pw.requests.exceptions.ConnectionError("boom")

    monkeypatch.setattr(pw, "polite_get", raise_connection_error)
    assert pw.PurpleWaveSource().discover() == []
    out = capsys.readouterr().out
    assert "RECORDER ERROR" in out


def test_discover_warns_loudly_on_healthy_but_empty_result(monkeypatch, capsys):
    monkeypatch.setattr(pw, "polite_get", lambda *a, **k: _FakeResponse([]))
    assert pw.PurpleWaveSource().discover() == []
    out = capsys.readouterr().out
    assert "WARNING" in out
    assert "0 furniture observations" in out


# --- money parsing edge cases (mixed str/int in real payloads) -------------

def test_parse_money_handles_string_and_numeric_and_none():
    assert pw._parse_money("55.00") == Decimal("55.00")
    assert pw._parse_money(325) == Decimal("325")
    assert pw._parse_money(None) is None


def test_parse_end_date_prefers_auction_timestamp():
    item = {"auction_timestamp": "2026-08-04T16:00:00.000Z", "endtime": 1}
    dt = pw._parse_end_date(item)
    assert dt == datetime(2026, 8, 4, 16, 0, 0, tzinfo=timezone.utc)


def test_parse_end_date_falls_back_to_endtime_epoch():
    item = {"endtime": 1785855600}
    dt = pw._parse_end_date(item)
    assert dt.tzinfo is not None
