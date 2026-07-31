"""Offline tests for the GovDeals recorder adapter, against fixtures captured
live 2026-07-31 (see recorder/sources/govdeals.py's module docstring for the
verified endpoint + behavior). No network calls — `GovDealsAdapter.discover`/
`.refetch` are monkeypatched at the class level.

Fixtures are real `Lot.raw` maestro payload dicts + real `dataclasses.asdict
(Snapshot)` outputs. `Lot`/`Snapshot` objects are reconstructed from them via
`deals.mapping.asset_to_lot` (a pure function, imported not modified) and
`deals.models.Snapshot(**...)` so this module's own mapping functions get
exercised against real shapes without hitting the network.
"""
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
import requests

from deals.mapping import asset_to_lot
from deals.models import Snapshot, lot_key
from recorder.models import Observation
from recorder.sources import govdeals

FIXTURES = Path(__file__).parent / "fixtures" / "govdeals"


def _load(name: str):
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture
def lot_raws():
    return _load("lot_raw_examples.json")


@pytest.fixture
def lots(lot_raws):
    return [asset_to_lot(raw) for raw in lot_raws]


@pytest.fixture
def snapshot_dicts():
    return _load("snapshot_examples.json")


def _snapshot_from_fixture(d: dict) -> Snapshot:
    d = dict(d)
    d["observed_at"] = datetime.fromisoformat(d["observed_at"])
    d["end_utc"] = datetime.fromisoformat(d["end_utc"])
    return Snapshot(**d)


@pytest.fixture
def snapshots(snapshot_dicts):
    return [_snapshot_from_fixture(d) for d in snapshot_dicts]


# --- _lot_to_observation --------------------------------------------------------

def test_lot_to_observation_shape(lots):
    for lot in lots:
        obs = govdeals._lot_to_observation(lot)
        assert isinstance(obs, Observation)
        assert obs.source == "govdeals"
        assert obs.source_lot_id == f"{lot.asset_id}/{lot.account_id}/{lot.auction_id}"
        assert obs.status == "active"  # every fixture lot carries assetStatusCd "STA"
        assert isinstance(obs.current_bid, Decimal)
        assert obs.end_date is not None
        assert obs.end_date.tzinfo is not None


def test_lot_to_observation_raw_roundtrips_unmodified(lots, lot_raws):
    for lot, raw in zip(lots, lot_raws):
        obs = govdeals._lot_to_observation(lot)
        assert obs.raw == raw
        assert obs.raw == lot.raw


def test_lot_to_observation_current_bid_matches_source(lots):
    for lot in lots:
        obs = govdeals._lot_to_observation(lot)
        assert obs.current_bid == Decimal(str(lot.current_bid))


# --- _snapshot_to_observation --------------------------------------------------------

def test_snapshot_to_observation_shape(snapshots):
    for snap in snapshots:
        key = lot_key(snap.asset_id, snap.account_id, snap.auction_id)
        obs = govdeals._snapshot_to_observation(key, snap)
        assert obs.source == "govdeals"
        assert obs.source_lot_id == key
        assert obs.status == "active"  # fixture snapshots all carry status "STA"
        assert isinstance(obs.current_bid, Decimal)
        assert obs.end_date is not None
        assert obs.end_date.tzinfo is not None


def test_snapshot_to_observation_raw_is_asdict(snapshots):
    import dataclasses
    for snap in snapshots:
        key = lot_key(snap.asset_id, snap.account_id, snap.auction_id)
        obs = govdeals._snapshot_to_observation(key, snap)
        assert obs.raw == dataclasses.asdict(snap)


def test_snapshot_to_observation_normalizes_local_observed_at_end_date_to_utc(snapshots):
    # fixture snapshots' end_utc is already UTC; end_date on the Observation
    # must still come back tz-aware UTC regardless.
    for snap in snapshots:
        key = lot_key(snap.asset_id, snap.account_id, snap.auction_id)
        obs = govdeals._snapshot_to_observation(key, snap)
        assert obs.end_date.tzinfo == timezone.utc or obs.end_date.utcoffset() == timedelta(0)


# --- _status_of --------------------------------------------------------

def test_status_of_active_for_sta():
    assert govdeals._status_of("STA") == "active"


def test_status_of_defensive_closed_sold_mapping():
    assert govdeals._status_of("SOLD") == "closed"
    assert govdeals._status_of("Closed") == "closed"
    assert govdeals._status_of("sold_out") == "closed"


def test_status_of_none_defaults_active():
    assert govdeals._status_of(None) == "active"
    assert govdeals._status_of("") == "active"


# --- _ensure_utc --------------------------------------------------------

def test_ensure_utc_attaches_utc_to_naive():
    naive = datetime(2026, 8, 1, 12, 0, 0)
    out = govdeals._ensure_utc(naive)
    assert out.tzinfo is not None
    assert out == naive.replace(tzinfo=timezone.utc)


def test_ensure_utc_converts_aware_local_to_utc():
    local = datetime(2026, 8, 1, 6, 0, 0, tzinfo=timezone(timedelta(hours=-7)))  # MST-ish
    out = govdeals._ensure_utc(local)
    assert out == datetime(2026, 8, 1, 13, 0, 0, tzinfo=timezone.utc)


def test_ensure_utc_none_passthrough():
    assert govdeals._ensure_utc(None) is None


# --- _parse_lot_key --------------------------------------------------------

def test_parse_lot_key_valid():
    assert govdeals._parse_lot_key("41961/432/2") == (41961, 432, 2)


def test_parse_lot_key_malformed():
    assert govdeals._parse_lot_key("not-a-key") is None
    assert govdeals._parse_lot_key("1/2") is None
    assert govdeals._parse_lot_key("1/2/x") is None


# --- _parse_money --------------------------------------------------------

def test_parse_money_handles_float_str_none():
    assert govdeals._parse_money(10.0) == Decimal("10.0")
    assert govdeals._parse_money("5.50") == Decimal("5.50")
    assert govdeals._parse_money(None) is None


# --- _parse_detail_end_date --------------------------------------------------------

def test_parse_detail_end_date_converts_naive_eastern_to_utc():
    # verified live: fetch_detail()'s assetAuctionEndDate for asset 41961
    # ("2026-07-31T09:01:00", naive ET) matched the SAME lot's known-UTC
    # assetAuctionEndDateUtc from discover() ("2026-07-31T13:01:00+00:00").
    dt = govdeals._parse_detail_end_date("2026-07-31T09:01:00")
    assert dt == datetime(2026, 7, 31, 13, 1, 0, tzinfo=timezone.utc)


def test_parse_detail_end_date_handles_missing():
    assert govdeals._parse_detail_end_date(None) is None
    assert govdeals._parse_detail_end_date("") is None
    assert govdeals._parse_detail_end_date("not-a-date") is None


# --- _corroborate_absence --------------------------------------------------------

def test_corroborate_absence_active_verdict(monkeypatch):
    adapter = govdeals.GovDealsAdapter()
    monkeypatch.setattr(
        govdeals.GovDealsAdapter, "fetch_detail",
        lambda self, a, c: {"assetStatusCd": "STA", "assetAuctionEndDate": "2026-08-05T09:01:00"},
    )
    verdict, payload = govdeals._corroborate_absence(adapter, 999, 888)
    assert verdict == "active"
    assert payload["assetStatusCd"] == "STA"


def test_corroborate_absence_closed_verdict(monkeypatch):
    adapter = govdeals.GovDealsAdapter()
    monkeypatch.setattr(
        govdeals.GovDealsAdapter, "fetch_detail",
        lambda self, a, c: {"assetStatusCd": "closed"},
    )
    verdict, payload = govdeals._corroborate_absence(adapter, 999, 888)
    assert verdict == "closed"


def test_corroborate_absence_gone_on_json_decode_error(monkeypatch):
    adapter = govdeals.GovDealsAdapter()

    def raise_jde(self, a, c):
        raise govdeals.requests.exceptions.JSONDecodeError("Expecting value", "", 0)

    monkeypatch.setattr(govdeals.GovDealsAdapter, "fetch_detail", raise_jde)
    verdict, payload = govdeals._corroborate_absence(adapter, 999, 888)
    assert verdict == "gone"
    assert payload is None


def test_corroborate_absence_gone_on_falsy_detail(monkeypatch):
    adapter = govdeals.GovDealsAdapter()
    monkeypatch.setattr(govdeals.GovDealsAdapter, "fetch_detail", lambda self, a, c: {})
    verdict, payload = govdeals._corroborate_absence(adapter, 999, 888)
    assert verdict == "gone"
    assert payload == {}


def test_corroborate_absence_unknown_on_connection_error(monkeypatch, capsys):
    adapter = govdeals.GovDealsAdapter()

    def raise_conn(self, a, c):
        raise govdeals.requests.exceptions.ConnectionError("boom")

    monkeypatch.setattr(govdeals.GovDealsAdapter, "fetch_detail", raise_conn)
    verdict, payload = govdeals._corroborate_absence(adapter, 999, 888)
    assert verdict == "unknown"
    assert payload is None
    out = capsys.readouterr().out
    assert "RECORDER ERROR" in out


def test_corroborate_absence_unknown_on_http_error(monkeypatch, capsys):
    adapter = govdeals.GovDealsAdapter()

    def raise_http(self, a, c):
        resp = govdeals.requests.Response()
        resp.status_code = 403
        raise govdeals.requests.exceptions.HTTPError("blocked", response=resp)

    monkeypatch.setattr(govdeals.GovDealsAdapter, "fetch_detail", raise_http)
    verdict, payload = govdeals._corroborate_absence(adapter, 999, 888)
    assert verdict == "unknown"
    out = capsys.readouterr().out
    assert "RECORDER ERROR" in out
    assert "403" in out


# --- discover() --------------------------------------------------------

def test_discover_calls_category_cluster_and_remaining_furniture_terms(monkeypatch, lots):
    calls = []

    def fake_discover(self, *, category_ids="", search_text="", max_pages=60, end_before=None):
        calls.append({"category_ids": category_ids, "search_text": search_text, "max_pages": max_pages})
        return iter(lots if search_text == "chairs" and category_ids else [])

    monkeypatch.setattr(govdeals.GovDealsAdapter, "discover", fake_discover)
    obs = govdeals.GovDealsSource().discover()

    assert len(obs) == len(lots)
    # one call for the category+"chairs" cluster, one per remaining FURNITURE_TERMS entry
    cat_calls = [c for c in calls if c["category_ids"] == govdeals.FURNITURE_CATEGORY_IDS]
    assert len(cat_calls) == 1
    assert cat_calls[0]["search_text"] == "chairs"
    assert cat_calls[0]["max_pages"] == govdeals.CATEGORY_MAX_PAGES

    term_calls = [c for c in calls if c["category_ids"] == ""]
    term_texts = {c["search_text"] for c in term_calls}
    expected_terms = {t for t in govdeals.FURNITURE_TERMS if t != "chairs"}
    assert term_texts == expected_terms
    assert all(c["max_pages"] == govdeals.TERM_MAX_PAGES for c in term_calls)


def test_discover_dedupes_across_sweeps(monkeypatch, lots):
    def fake_discover(self, *, category_ids="", search_text="", max_pages=60, end_before=None):
        # every sweep returns the SAME lots — must still dedupe to len(lots)
        return iter(lots)

    monkeypatch.setattr(govdeals.GovDealsAdapter, "discover", fake_discover)
    obs = govdeals.GovDealsSource().discover()
    assert len(obs) == len(lots)
    ids = {o.source_lot_id for o in obs}
    assert len(ids) == len(lots)


def test_discover_partial_failure_keeps_lots_collected_before_the_error(monkeypatch, lots, capsys):
    def fake_discover(self, *, category_ids="", search_text="", max_pages=60, end_before=None):
        if category_ids:  # the category+"chairs" sweep succeeds
            return iter(lots)
        # every term sweep fails mid-generator, after yielding nothing
        def gen():
            raise requests.exceptions.ConnectionError("boom")
            yield  # pragma: no cover
        return gen()

    monkeypatch.setattr(govdeals.GovDealsAdapter, "discover", fake_discover)
    obs = govdeals.GovDealsSource().discover()
    assert len(obs) == len(lots)  # category sweep's results are kept
    out = capsys.readouterr().out
    assert "RECORDER ERROR" in out


def test_discover_all_sweeps_fail_returns_empty_and_prints_loud_error(monkeypatch, capsys):
    def fake_discover(self, *, category_ids="", search_text="", max_pages=60, end_before=None):
        def gen():
            raise requests.exceptions.ConnectionError("boom")
            yield  # pragma: no cover
        return gen()

    monkeypatch.setattr(govdeals.GovDealsAdapter, "discover", fake_discover)
    obs = govdeals.GovDealsSource().discover()
    assert obs == []
    out = capsys.readouterr().out
    assert "RECORDER ERROR" in out
    assert "govdeals" in out


def test_discover_healthy_but_empty_warns_loudly(monkeypatch, capsys):
    def fake_discover(self, *, category_ids="", search_text="", max_pages=60, end_before=None):
        return iter([])

    monkeypatch.setattr(govdeals.GovDealsAdapter, "discover", fake_discover)
    obs = govdeals.GovDealsSource().discover()
    assert obs == []
    out = capsys.readouterr().out
    assert "WARNING" in out
    assert "0 lots" in out


# --- poll() --------------------------------------------------------

def test_poll_found_snapshot_returns_observation(monkeypatch, snapshots):
    snap = snapshots[0]
    key = lot_key(snap.asset_id, snap.account_id, snap.auction_id)

    def fake_refetch(self, keys):
        assert keys == [(snap.asset_id, snap.account_id, snap.auction_id)]
        return {key: snap}

    monkeypatch.setattr(govdeals.GovDealsAdapter, "refetch", fake_refetch)
    obs = govdeals.GovDealsSource().poll([{"source_lot_id": key}])
    assert len(obs) == 1
    assert obs[0].source_lot_id == key
    assert obs[0].status == "active"


def _fail_fetch_detail(self, asset_id, account_id):
    raise AssertionError(f"fetch_detail must not be called for {asset_id}/{account_id} here")


# --- poll() corroboration (fix round 1, review finding #2) -----------------
#
# Absence from a healthy refetch, past the tracked lot's own end_date, is no
# longer enough on its own to emit 'gone' — poll() now corroborates with ONE
# extra GovDealsAdapter.fetch_detail(asset_id, account_id) call first. These
# tests cover its four verdict branches plus the cap/skip behavior.

def test_poll_corroboration_confirms_gone_via_204_empty_json_decode_error(monkeypatch):
    # verified live (see module docstring): a nonexistent asset/account pair
    # returns HTTP 204 empty body, which raise_for_status() does NOT reject —
    # the empty body surfaces as JSONDecodeError from r.json() instead. This
    # IS the endpoint's real "doesn't exist" signal, corroborating 'gone'.
    monkeypatch.setattr(govdeals.GovDealsAdapter, "refetch", lambda self, keys: {})

    def fake_fetch_detail(self, asset_id, account_id):
        raise govdeals.requests.exceptions.JSONDecodeError("Expecting value", "", 0)

    monkeypatch.setattr(govdeals.GovDealsAdapter, "fetch_detail", fake_fetch_detail)
    past_end = datetime.now(timezone.utc) - timedelta(hours=1)
    obs = govdeals.GovDealsSource().poll([{"source_lot_id": "999/888/1", "end_date": past_end}])
    assert len(obs) == 1
    assert obs[0].status == "gone"
    assert obs[0].raw["recorder_probe"]["result"] == "not_found"
    assert obs[0].raw["recorder_probe"]["http_status"] == 204
    assert "999/888" in obs[0].raw["recorder_probe"]["url"]


def test_poll_corroboration_confirms_still_active_with_later_end_date(monkeypatch):
    monkeypatch.setattr(govdeals.GovDealsAdapter, "refetch", lambda self, keys: {})
    detail_payload = {
        "assetId": 999, "accountId": 888, "auctionId": 1,
        "assetStatusCd": "STA",
        "assetAuctionEndDate": "2026-08-05T09:01:00",  # naive ET — see docstring
    }

    def fake_fetch_detail(self, asset_id, account_id):
        assert (asset_id, account_id) == (999, 888)
        return dict(detail_payload)

    monkeypatch.setattr(govdeals.GovDealsAdapter, "fetch_detail", fake_fetch_detail)
    past_end = datetime.now(timezone.utc) - timedelta(hours=1)
    obs = govdeals.GovDealsSource().poll([{"source_lot_id": "999/888/1", "end_date": past_end}])
    assert len(obs) == 1
    o = obs[0]
    assert o.status == "active"
    assert o.source_lot_id == "999/888/1"
    assert o.raw == detail_payload
    assert o.current_bid is None  # fetch_detail carries no pricing fields
    assert o.bid_count is None
    # naive ET 09:01 -> UTC 13:01 (EDT, -4h) — the anti-snipe-extension case
    assert o.end_date == datetime(2026, 8, 5, 13, 1, 0, tzinfo=timezone.utc)


def test_poll_corroboration_confirms_closed(monkeypatch):
    monkeypatch.setattr(govdeals.GovDealsAdapter, "refetch", lambda self, keys: {})
    detail_payload = {
        "assetId": 999, "accountId": 888, "auctionId": 1,
        "assetStatusCd": "SOLD",  # defensive — never observed live, see _status_of
        "assetAuctionEndDate": "2026-07-30T09:01:00",
    }
    monkeypatch.setattr(
        govdeals.GovDealsAdapter, "fetch_detail",
        lambda self, asset_id, account_id: dict(detail_payload),
    )
    past_end = datetime.now(timezone.utc) - timedelta(hours=1)
    obs = govdeals.GovDealsSource().poll([{"source_lot_id": "999/888/1", "end_date": past_end}])
    assert len(obs) == 1
    assert obs[0].status == "closed"
    assert obs[0].raw == detail_payload


def test_poll_corroboration_unknown_network_failure_emits_nothing(monkeypatch, capsys):
    monkeypatch.setattr(govdeals.GovDealsAdapter, "refetch", lambda self, keys: {})

    def fake_fetch_detail(self, asset_id, account_id):
        raise govdeals.requests.exceptions.ConnectionError("boom")

    monkeypatch.setattr(govdeals.GovDealsAdapter, "fetch_detail", fake_fetch_detail)
    past_end = datetime.now(timezone.utc) - timedelta(hours=1)
    obs = govdeals.GovDealsSource().poll([{"source_lot_id": "999/888/1", "end_date": past_end}])
    assert obs == []  # NOT 'gone' — corroboration itself failed, fetch-failure rule applies
    out = capsys.readouterr().out
    assert "RECORDER ERROR" in out


def test_poll_corroboration_real_http_error_emits_nothing(monkeypatch, capsys):
    monkeypatch.setattr(govdeals.GovDealsAdapter, "refetch", lambda self, keys: {})

    def fake_fetch_detail(self, asset_id, account_id):
        resp = govdeals.requests.Response()
        resp.status_code = 500
        raise govdeals.requests.exceptions.HTTPError("server error", response=resp)

    monkeypatch.setattr(govdeals.GovDealsAdapter, "fetch_detail", fake_fetch_detail)
    past_end = datetime.now(timezone.utc) - timedelta(hours=1)
    obs = govdeals.GovDealsSource().poll([{"source_lot_id": "999/888/1", "end_date": past_end}])
    assert obs == []
    out = capsys.readouterr().out
    assert "RECORDER ERROR" in out


def test_poll_corroboration_cap_falls_back_to_absence_alone_gone(monkeypatch, capsys):
    monkeypatch.setattr(govdeals.GovDealsAdapter, "refetch", lambda self, keys: {})
    calls = []

    def fake_fetch_detail(self, asset_id, account_id):
        calls.append((asset_id, account_id))
        raise govdeals.requests.exceptions.JSONDecodeError("Expecting value", "", 0)

    monkeypatch.setattr(govdeals.GovDealsAdapter, "fetch_detail", fake_fetch_detail)
    past_end = datetime.now(timezone.utc) - timedelta(hours=1)
    n = govdeals.CORROBORATION_CAP_PER_BATCH + 3
    lots = [{"source_lot_id": f"{i}/888/1", "end_date": past_end} for i in range(n)]
    obs = govdeals.GovDealsSource().poll(lots)
    assert len(obs) == n  # every lot still ends up 'gone' one way or another
    assert all(o.status == "gone" for o in obs)
    assert len(calls) == govdeals.CORROBORATION_CAP_PER_BATCH  # cap respected
    out = capsys.readouterr().out
    assert "WARNING" in out
    assert "corroboration cap" in out


def test_poll_absent_lot_before_end_date_emits_nothing(monkeypatch):
    monkeypatch.setattr(govdeals.GovDealsAdapter, "refetch", lambda self, keys: {})
    monkeypatch.setattr(govdeals.GovDealsAdapter, "fetch_detail", _fail_fetch_detail)
    future_end = datetime.now(timezone.utc) + timedelta(hours=1)
    obs = govdeals.GovDealsSource().poll([{"source_lot_id": "999/888/1", "end_date": future_end}])
    assert obs == []


def test_poll_absent_lot_with_unknown_end_date_emits_nothing(monkeypatch):
    monkeypatch.setattr(govdeals.GovDealsAdapter, "refetch", lambda self, keys: {})
    monkeypatch.setattr(govdeals.GovDealsAdapter, "fetch_detail", _fail_fetch_detail)
    obs = govdeals.GovDealsSource().poll([{"source_lot_id": "999/888/1", "end_date": None}])
    assert obs == []


def test_poll_refetch_failure_emits_no_observations(monkeypatch, capsys):
    def fake_refetch(self, keys):
        raise requests.exceptions.ConnectionError("boom")

    monkeypatch.setattr(govdeals.GovDealsAdapter, "refetch", fake_refetch)
    past_end = datetime.now(timezone.utc) - timedelta(hours=1)
    obs = govdeals.GovDealsSource().poll([{"source_lot_id": "999/888/1", "end_date": past_end}])
    assert obs == []
    out = capsys.readouterr().out
    assert "RECORDER ERROR" in out


def test_poll_empty_lots_makes_no_request(monkeypatch):
    def fail_refetch(self, keys):
        raise AssertionError("poll([]) must not hit the network")

    monkeypatch.setattr(govdeals.GovDealsAdapter, "refetch", fail_refetch)
    assert govdeals.GovDealsSource().poll([]) == []


def test_poll_skips_unparseable_source_lot_id(monkeypatch, capsys):
    monkeypatch.setattr(govdeals.GovDealsAdapter, "refetch", lambda self, keys: {})
    obs = govdeals.GovDealsSource().poll([{"source_lot_id": "not-a-valid-key"}])
    assert obs == []
    out = capsys.readouterr().out
    assert "RECORDER ERROR" in out
    assert "cannot parse" in out


def test_sold_sweep_returns_empty_list():
    assert govdeals.GovDealsSource().sold_sweep() == []
