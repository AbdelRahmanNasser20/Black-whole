"""GovDeals bidbox finals — offline, against real bidbox payloads captured
2026-09-28 (`fixtures/govdeals/bidbox_examples.json`). No network, no DB:
`GovDealsAdapter.fetch_bid_state` / `.refetch` / `.fetch_detail` are
monkeypatched at the class level; store calls are monkeypatched in cli."""
import copy
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
import requests

from recorder import cli, store
from recorder.sources import govdeals

FIXTURES = Path(__file__).parent / "fixtures" / "govdeals"
PAYLOADS = json.loads((FIXTURES / "bidbox_examples.json").read_text())["payloads"]


def payload(name: str) -> dict:
    return copy.deepcopy(PAYLOADS[name]["bidbox"])


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture(autouse=True)
def _no_throttle(monkeypatch):
    monkeypatch.setattr(govdeals, "BIDBOX_MIN_INTERVAL_SECONDS", 0.0)


def _no_refetch(self, keys):
    raise AssertionError("past-end lots must not trigger the 60-page refetch")


def _no_detail(self, a, c):
    raise AssertionError("fetch_detail is only the 204 fallback")


def _serve(monkeypatch, raw_by_key: dict):
    calls = []

    def fake(self, asset_id, account_id, auction_id):
        k = f"{asset_id}/{account_id}/{auction_id}"
        calls.append(k)
        v = raw_by_key[k]
        if isinstance(v, Exception):
            raise v
        return v

    monkeypatch.setattr(govdeals.GovDealsAdapter, "fetch_bid_state", fake)
    monkeypatch.setattr(govdeals.GovDealsAdapter, "refetch", _no_refetch)
    monkeypatch.setattr(govdeals.GovDealsAdapter, "fetch_detail", _no_detail)
    return calls


def _poll(lot_id: str, end_date: datetime):
    return govdeals.GovDealsSource().poll([{"source_lot_id": lot_id, "end_date": end_date}])


# --- close_outcome: same names/precedence as deals.tracking.close_outcome (PR #105)

@pytest.mark.parametrize("code,bids,expected", [
    ("SOA", 5, "sold"), ("SOL", 1, "sold"),
    ("RNM", 9, "reserve_not_met"), ("CNB", 0, "reserve_not_met"),
    ("CAN", 3, "cancelled"), ("SOA", 0, "no_bid"), ("CLO", 4, "unknown"),
    ("HFR", 2, "unknown"), ("STA", 5, "unknown"), (None, None, "no_bid"),
])
def test_close_outcome(code, bids, expected):
    assert govdeals.close_outcome(code, bids) == expected


# --- poll(): the seven cases ---------------------------------------------------

def test_sta_extended_records_new_end_and_stays_active(monkeypatch):
    now = datetime.now(timezone.utc)
    raw = payload("sta_live_11_3156_1")
    raw["assetAuctionEndDateUTC"] = _iso(now + timedelta(minutes=3))   # soft close moved it
    _serve(monkeypatch, {"11/3156/1": raw})
    obs = _poll("11/3156/1", now - timedelta(minutes=1))
    assert len(obs) == 1
    o = obs[0]
    assert o.status == "active"
    assert o.end_date > now
    assert o.current_bid == Decimal("1275.0") and o.bid_count == 24
    assert o.raw["recorder_capture"]["method"] == "bidbox_live"
    assert o.raw["bidbox"] == raw                     # payload untouched


def test_sta_inside_grace_emits_nothing(monkeypatch):
    now = datetime.now(timezone.utc)
    raw = payload("sta_live_11_3156_1")
    raw["assetAuctionEndDateUTC"] = _iso(now - timedelta(minutes=5))
    calls = _serve(monkeypatch, {"11/3156/1": raw})
    assert _poll("11/3156/1", now - timedelta(minutes=5)) == []
    assert calls == ["11/3156/1"]


def test_sta_past_grace_is_terminal_unknown(monkeypatch):
    raw = payload("sta_stale_264_30240_8")            # STA 11 days after its clock, live
    _serve(monkeypatch, {"264/30240/8": raw})
    (o,) = _poll("264/30240/8", datetime(2026, 9, 17, 19, 19, tzinfo=timezone.utc))
    assert o.status == "closed"
    assert o.current_bid == Decimal("500.0")
    assert o.raw["recorder_capture"]["outcome"] == "unknown"
    assert o.raw["recorder_capture"]["status_code"] == "STA"


def test_soa_final_uses_bidbox_price_and_extended_end(monkeypatch):
    raw = payload("soa_extended_23_16539_1")          # clock 18:15 -> closed 18:18
    _serve(monkeypatch, {"23/16539/1": raw})
    (o,) = _poll("23/16539/1", datetime(2026, 9, 28, 18, 15, tzinfo=timezone.utc))
    assert o.status == "closed"
    assert o.current_bid == Decimal("525.0") and o.bid_count == 2
    assert o.end_date == datetime(2026, 9, 28, 18, 18, tzinfo=timezone.utc)
    cap = o.raw["recorder_capture"]
    assert cap == {**cap, "method": "bidbox_final", "status_code": "SOA", "outcome": "sold"}
    assert o.raw["bidbox"] == raw


def test_rnm_final_is_reserve_not_met(monkeypatch):
    _serve(monkeypatch, {"8/32408/4": payload("rnm_8_32408_4")})
    (o,) = _poll("8/32408/4", datetime(2026, 9, 28, 18, 21, tzinfo=timezone.utc))
    assert o.status == "closed"
    assert o.current_bid == Decimal("10000.0") and o.bid_count == 1
    assert o.raw["recorder_capture"]["outcome"] == "reserve_not_met"


def test_zero_bids_is_no_bid(monkeypatch):
    raw = payload("soa_extended_23_16539_1")
    raw.update(assetStatusCd="CLO", bidCount=0)
    _serve(monkeypatch, {"23/16539/1": raw})
    (o,) = _poll("23/16539/1", datetime(2026, 9, 28, 18, 15, tzinfo=timezone.utc))
    assert o.status == "closed" and o.bid_count == 0
    assert o.raw["recorder_capture"]["outcome"] == "no_bid"


def test_cnb_zero_bids_follows_pr105_as_reserve_not_met(monkeypatch):
    _serve(monkeypatch, {"9/32408/4": payload("cnb_9_32408_4")})
    (o,) = _poll("9/32408/4", datetime(2026, 9, 28, 18, 8, tzinfo=timezone.utc))
    assert o.raw["recorder_capture"]["outcome"] == "reserve_not_met"


def test_204_purged_falls_back_to_absence_gone(monkeypatch, capsys):
    _serve(monkeypatch, {"999/888/1": {}})
    monkeypatch.setattr(govdeals.GovDealsAdapter, "fetch_detail",
                        lambda self, a, c: (_ for _ in ()).throw(
                            requests.exceptions.JSONDecodeError("Expecting value", "", 0)))
    (o,) = _poll("999/888/1", datetime.now(timezone.utc) - timedelta(hours=2))
    assert o.status == "gone"                          # -> sold_comps last_snapshot
    assert "recorder_probe" in o.raw
    assert "bidbox 204 (purged)" in capsys.readouterr().out


def test_bidbox_error_emits_nothing(monkeypatch, capsys):
    _serve(monkeypatch, {"23/16539/1": requests.exceptions.ConnectionError("boom")})
    assert _poll("23/16539/1", datetime.now(timezone.utc) - timedelta(hours=1)) == []
    assert "RECORDER ERROR" in capsys.readouterr().out


def test_bidbox_missing_current_bid_is_an_error_not_a_zero(monkeypatch):
    raw = payload("soa_extended_23_16539_1")
    del raw["currentBid"]
    _serve(monkeypatch, {"23/16539/1": raw})
    assert _poll("23/16539/1", datetime.now(timezone.utc) - timedelta(hours=1)) == []


def test_mixed_batch_only_refetches_upcoming_lots(monkeypatch):
    now = datetime.now(timezone.utc)
    _serve(monkeypatch, {"8/32408/4": payload("rnm_8_32408_4")})
    seen = []
    monkeypatch.setattr(govdeals.GovDealsAdapter, "refetch",
                        lambda self, keys: seen.extend(keys) or {})
    obs = govdeals.GovDealsSource().poll([
        {"source_lot_id": "8/32408/4", "end_date": now - timedelta(hours=1)},
        {"source_lot_id": "11/3156/1", "end_date": now + timedelta(hours=5)},
    ])
    assert seen == [(11, 3156, 1)]
    assert [o.status for o in obs] == ["closed"]


def test_bidbox_cap_reads_just_closed_first(monkeypatch, capsys):
    now = datetime.now(timezone.utc)
    monkeypatch.setenv("RECORDER_GOVDEALS_BIDBOX_MAX_PER_RUN", "2")
    raw = payload("rnm_8_32408_4")
    calls = _serve(monkeypatch, {k: raw for k in ("1/1/1", "2/2/2", "3/3/3")})
    govdeals.GovDealsSource().poll([
        {"source_lot_id": "1/1/1", "end_date": now - timedelta(days=3)},
        {"source_lot_id": "2/2/2", "end_date": now - timedelta(hours=1)},
        {"source_lot_id": "3/3/3", "end_date": now - timedelta(hours=5)},
    ])
    assert calls == ["2/2/2", "3/3/3"]          # newest close first, oldest waits
    assert "wait for the next run" in capsys.readouterr().out


def test_far_lots_stay_out_of_the_refetch(monkeypatch):
    now = datetime.now(timezone.utc)
    seen = []
    monkeypatch.setattr(govdeals.GovDealsAdapter, "refetch",
                        lambda self, keys: seen.extend(keys) or {})
    govdeals.GovDealsSource().poll([
        {"source_lot_id": "11/3156/1", "end_date": now + timedelta(hours=5)},
        {"source_lot_id": "12/3156/1", "end_date": now + timedelta(days=4)},
        {"source_lot_id": "13/3156/1", "end_date": None},
    ])
    assert seen == [(11, 3156, 1), (13, 3156, 1)]


def test_bidbox_throttle_spaces_requests(monkeypatch):
    monkeypatch.setattr(govdeals, "BIDBOX_MIN_INTERVAL_SECONDS", 1.0)
    sleeps = []
    monkeypatch.setattr(govdeals.time, "sleep", lambda s: sleeps.append(s))
    govdeals._bidbox_last_at[:] = []
    govdeals._bidbox_throttle()
    govdeals._bidbox_throttle()
    assert len(sleeps) == 1 and 0.9 < sleeps[0] <= 1.0


# --- 7-day SOA re-check ------------------------------------------------------------

PRIOR = {"source_lot_id": "5282/3780/2", "status_code": "SOA", "current_bid": Decimal("1725.00"),
         "bid_count": 54, "end_date": datetime(2026, 9, 3, 1, 21, 51, tzinfo=timezone.utc)}


def test_recheck_status_change_is_recorded_as_changed(monkeypatch):
    raw = payload("soa_old_5282_3780_2")
    raw["assetStatusCd"] = "CAN"                        # buyer defaulted, seller cancelled
    _serve(monkeypatch, {"5282/3780/2": raw})
    (o,) = govdeals.GovDealsSource().recheck_finals([PRIOR])
    cap = o.raw["recorder_capture"]
    assert o.status == "closed"
    assert cap["method"] == "bidbox_recheck" and cap["changed"] is True
    assert cap["outcome"] == "cancelled" and cap["prior_status_code"] == "SOA"


def test_recheck_unchanged_still_writes_the_marker(monkeypatch):
    _serve(monkeypatch, {"5282/3780/2": payload("soa_old_5282_3780_2")})
    (o,) = govdeals.GovDealsSource().recheck_finals([PRIOR])
    assert o.raw["recorder_capture"]["changed"] is False
    assert o.current_bid == Decimal("1725.0")


def test_recheck_purged_carries_prior_numbers(monkeypatch):
    _serve(monkeypatch, {"5282/3780/2": {}})
    (o,) = govdeals.GovDealsSource().recheck_finals([PRIOR])
    cap = o.raw["recorder_capture"]
    assert cap["result"] == "purged" and cap["changed"] is None
    assert o.current_bid == PRIOR["current_bid"] and o.bid_count == 54
    assert cap["outcome"] == "sold"


def test_recheck_error_writes_nothing(monkeypatch):
    _serve(monkeypatch, {"5282/3780/2": requests.exceptions.Timeout("slow")})
    assert govdeals.GovDealsSource().recheck_finals([PRIOR]) == []


def test_cmd_recheck_inserts_without_change_gating(monkeypatch, capsys):
    _serve(monkeypatch, {"5282/3780/2": payload("soa_old_5282_3780_2")})
    monkeypatch.setattr(store, "soa_recheck_due",
                        lambda codes, secs, limit, source="govdeals": [PRIOR] if source == "govdeals" else [])
    monkeypatch.setattr(store, "filter_changed",
                        lambda obs: (_ for _ in ()).throw(AssertionError("must not gate")))
    inserted = []
    monkeypatch.setattr(store, "insert_observations", lambda obs: inserted.extend(obs) or len(obs))
    assert cli.cmd_recheck_finals({"govdeals": govdeals.GovDealsSource()}) == 0
    assert len(inserted) == 1
    assert "recheck source=govdeals due=1 inserted=1 changed=0" in capsys.readouterr().out


# --- finals-backfill ----------------------------------------------------------------

def _backfill_rows():
    return [
        {"source_lot_id": "5282/3780/2", "status": "gone",
         "end_date": datetime(2026, 9, 3, 1, 0, 51, tzinfo=timezone.utc),
         "last_price": Decimal("460.00"), "last_bid_count": 20},
        {"source_lot_id": "999/888/1", "status": "gone",
         "end_date": datetime(2026, 9, 25, tzinfo=timezone.utc),
         "last_price": Decimal("50.00"), "last_bid_count": 2},
    ]


def test_finals_backfill_dry_run_writes_nothing(monkeypatch, capsys):
    _serve(monkeypatch, {"5282/3780/2": payload("soa_old_5282_3780_2"), "999/888/1": {}})
    monkeypatch.setattr(store, "finals_backfill_candidates", lambda s, d, l: _backfill_rows())
    monkeypatch.setattr(store, "insert_observations",
                        lambda obs: (_ for _ in ()).throw(AssertionError("dry-run wrote")))
    assert cli.cmd_finals_backfill("govdeals", 8, 100, apply=False,
                                   adapter=govdeals.GovDealsAdapter()) == 0
    out = capsys.readouterr().out
    assert "checked=2 with_final=1 purged(204)=1" in out
    assert "$460.00" in out and "$1,725.00" in out and "higher=1" in out
    assert "would insert=1" in out


def test_finals_backfill_apply_inserts_finals(monkeypatch):
    _serve(monkeypatch, {"5282/3780/2": payload("soa_old_5282_3780_2"), "999/888/1": {}})
    monkeypatch.setattr(store, "finals_backfill_candidates", lambda s, d, l: _backfill_rows())
    monkeypatch.setattr(store, "filter_changed", lambda obs: list(obs))
    inserted = []
    monkeypatch.setattr(store, "insert_observations", lambda obs: inserted.extend(obs) or len(obs))
    cli.cmd_finals_backfill("govdeals", 8, 100, apply=True, adapter=govdeals.GovDealsAdapter())
    assert [(o.source_lot_id, o.status, o.current_bid) for o in inserted] == [
        ("5282/3780/2", "closed", Decimal("1725.0"))]


def test_finals_backfill_rejects_other_sources():
    assert cli.cmd_finals_backfill("gsa", 8, 10, apply=False) == 2


def test_finals_backfill_parser_defaults_to_dry_run():
    args = cli.build_parser().parse_args(["finals-backfill", "--source", "govdeals"])
    assert args.apply is False and args.since_days == 8


# --- store backoff ------------------------------------------------------------------

def test_read_with_backoff_retries_pool_full(monkeypatch):
    import psycopg
    monkeypatch.setattr(store.time, "sleep", lambda s: None)
    attempts = []

    def flaky():
        attempts.append(1)
        if len(attempts) < 3:
            raise psycopg.OperationalError("FATAL: (EMAXCONNSESSION) max clients reached")
        return ["ok"]

    assert store._read_with_backoff(flaky) == ["ok"]
    assert len(attempts) == 3


def test_read_with_backoff_raises_other_errors(monkeypatch):
    import psycopg

    def broken():
        raise psycopg.OperationalError("password authentication failed")

    with pytest.raises(psycopg.OperationalError):
        store._read_with_backoff(broken)
