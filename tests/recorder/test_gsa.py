"""Offline tests for the GSA Auctions adapter, against a fixture captured live
2026-07-31 (see recorder/sources/gsa.py's module docstring for the verified
endpoint + params). No network calls — polite_get is monkeypatched.
"""
import json
from decimal import Decimal
from datetime import datetime, timezone
from pathlib import Path

import pytest

from recorder.models import Observation
from recorder.sources import gsa

FIXTURES = Path(__file__).parent / "fixtures" / "gsa"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


class _FakeResponse:
    def __init__(self, payload, status_code=200, url=gsa.AUCTIONS_URL):
        self._payload = payload
        self.status_code = status_code
        self.url = url

    def json(self):
        return self._payload


@pytest.fixture
def sample_payload():
    return _load("discover_sample.json")


@pytest.fixture
def furniture_items(sample_payload):
    return [
        it for it in sample_payload["Results"]
        if str(it.get("auctionStatus", "")).lower() == "active" and gsa._is_furniture(it)
    ]


# --- discover() --------------------------------------------------------

def test_discover_parses_n_geq_5_observations(monkeypatch, sample_payload):
    monkeypatch.setattr(gsa, "polite_get", lambda *a, **k: _FakeResponse(sample_payload))
    obs = gsa.GSASource().discover()
    assert len(obs) >= 5
    assert all(isinstance(o, Observation) for o in obs)


def test_discover_observations_have_required_shape(monkeypatch, sample_payload):
    monkeypatch.setattr(gsa, "polite_get", lambda *a, **k: _FakeResponse(sample_payload))
    obs = gsa.GSASource().discover()
    for o in obs:
        assert o.source == "gsa"
        assert o.source_lot_id
        assert o.status == "active"
        assert o.current_bid is None or isinstance(o.current_bid, Decimal)
        assert o.end_date is not None
        assert o.end_date.tzinfo is not None


def test_discover_raw_roundtrips_source_item_unmodified(monkeypatch, sample_payload, furniture_items):
    monkeypatch.setattr(gsa, "polite_get", lambda *a, **k: _FakeResponse(sample_payload))
    obs = gsa.GSASource().discover()
    raws = {o.source_lot_id: o.raw for o in obs}
    for item in furniture_items:
        lot_id = f"{item['saleNo']}-{item['lotNo']}"
        assert raws[lot_id] == item


def test_discover_excludes_non_furniture_items(monkeypatch, sample_payload):
    monkeypatch.setattr(gsa, "polite_get", lambda *a, **k: _FakeResponse(sample_payload))
    obs = gsa.GSASource().discover()
    non_furniture_names = {
        it["itemName"] for it in sample_payload["Results"] if not gsa._is_furniture(it)
    }
    obs_item_names = {o.raw.get("itemName") for o in obs}
    assert not (non_furniture_names & obs_item_names)


def test_discover_excludes_preview_status(monkeypatch, sample_payload):
    # the fixture intentionally includes one lowercase "preview" edge case
    preview_items = [it for it in sample_payload["Results"] if str(it.get("auctionStatus", "")).lower() != "active"]
    assert preview_items, "fixture must contain a non-active item to exercise this filter"
    monkeypatch.setattr(gsa, "polite_get", lambda *a, **k: _FakeResponse(sample_payload))
    obs = gsa.GSASource().discover()
    obs_ids = {o.source_lot_id for o in obs}
    for it in preview_items:
        assert f"{it['saleNo']}-{it['lotNo']}" not in obs_ids


def test_discover_uses_gsa_api_key_or_demo_fallback(monkeypatch, sample_payload):
    captured = {}

    def fake_get(url, *, headers=None, params=None, timeout=30):
        captured["url"] = url
        captured["params"] = params
        return _FakeResponse(sample_payload)

    monkeypatch.delenv(gsa.GSA_API_KEY_ENV, raising=False)
    monkeypatch.setattr(gsa, "polite_get", fake_get)
    gsa.GSASource().discover()
    assert captured["url"] == gsa.AUCTIONS_URL
    assert captured["params"]["api_key"] == "DEMO_KEY"
    assert captured["params"]["format"] == "JSON"


def test_discover_uses_env_api_key_when_set(monkeypatch, sample_payload):
    captured = {}
    monkeypatch.setenv(gsa.GSA_API_KEY_ENV, "real-key-123")
    monkeypatch.setattr(gsa, "polite_get", lambda url, **k: (captured.update(k), _FakeResponse(sample_payload))[1])
    gsa.GSASource().discover()
    assert captured["params"]["api_key"] == "real-key-123"


# --- sold_sweep() --------------------------------------------------------

def test_sold_sweep_returns_empty_list():
    assert gsa.GSASource().sold_sweep() == []


# --- poll() --------------------------------------------------------

def test_poll_returns_active_observation_for_still_present_lot(monkeypatch, sample_payload, furniture_items):
    monkeypatch.setattr(gsa, "polite_get", lambda *a, **k: _FakeResponse(sample_payload))
    target = furniture_items[0]
    lot_id = f"{target['saleNo']}-{target['lotNo']}"
    obs = gsa.GSASource().poll([{"source_lot_id": lot_id}])
    assert len(obs) == 1
    assert obs[0].source_lot_id == lot_id
    assert obs[0].status == "active"
    assert obs[0].raw == target


def test_poll_returns_gone_for_vanished_lot(monkeypatch, sample_payload):
    monkeypatch.setattr(gsa, "polite_get", lambda *a, **k: _FakeResponse(sample_payload))
    obs = gsa.GSASource().poll([{"source_lot_id": "9-9-NOPE-99-999-999"}])
    assert len(obs) == 1
    assert obs[0].status == "gone"
    assert obs[0].raw["recorder_probe"]["result"] == "not_found"
    assert obs[0].raw["recorder_probe"]["http_status"] == 200
    assert obs[0].raw["recorder_probe"]["url"] == gsa.AUCTIONS_URL


def test_poll_empty_lots_makes_no_request(monkeypatch):
    def fail_get(*a, **k):
        raise AssertionError("poll([]) must not hit the network")

    monkeypatch.setattr(gsa, "polite_get", fail_get)
    assert gsa.GSASource().poll([]) == []


# --- HTTP failure handling --------------------------------------------------------

def test_discover_returns_empty_on_403_without_raising(monkeypatch):
    monkeypatch.setattr(gsa, "polite_get", lambda *a, **k: _FakeResponse(None, status_code=403))
    assert gsa.GSASource().discover() == []


def test_discover_returns_empty_on_missing_results_key(monkeypatch):
    monkeypatch.setattr(gsa, "polite_get", lambda *a, **k: _FakeResponse({"unexpected": "shape"}))
    assert gsa.GSASource().discover() == []


# --- parsing helpers --------------------------------------------------------

def test_parse_money_handles_float_and_none():
    assert gsa._parse_money(9950.0) == Decimal("9950.0")
    assert gsa._parse_money(None) is None


def test_parse_end_date_is_end_of_day_utc():
    dt = gsa._parse_end_date("2026-08-05")
    assert dt == datetime(2026, 8, 5, 23, 59, 59, tzinfo=timezone.utc)


def test_parse_end_date_handles_missing():
    assert gsa._parse_end_date(None) is None
    assert gsa._parse_end_date("") is None


def test_lot_id_combines_sale_and_lot_number():
    item = {"saleNo": "2-1-QSC-I-26-318", "lotNo": "001"}
    assert gsa._lot_id(item) == "2-1-QSC-I-26-318-001"
