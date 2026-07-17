# tests/deals/test_backfill.py
from datetime import datetime, timedelta, timezone
from deals.backfill import backfill_plan

NOW = datetime(2026, 7, 17, 12, 0, tzinfo=timezone.utc)

def _row(**kw):
    base = dict(asset_id=1, account_id=2, auction_id=3,
                end_utc=NOW - timedelta(days=3), bid_count=0, current_bid=5.0,
                snap_bid_count=None, snap_current_bid=None, snap_observed_at=None)
    base.update(kw)
    return base

def test_no_bid_from_live_state_when_no_snapshot():
    plan = backfill_plan([_row()], NOW)
    assert plan[0]["outcome"] == "no_bid"
    assert plan[0]["final_bid"] == 5.0
    assert plan[0]["complete"] is False          # honest: single observation

def test_snapshot_preferred_over_live_state():
    plan = backfill_plan([_row(snap_bid_count=4, snap_current_bid=120.0,
                                snap_observed_at=NOW - timedelta(days=3, hours=1))], NOW)
    assert plan[0]["outcome"] == "sold"
    assert plan[0]["final_bid"] == 120.0
    assert plan[0]["final_bid_count"] == 4

def test_one_bid_is_low_bid_not_no_bid():
    plan = backfill_plan([_row(bid_count=1)], NOW)
    assert plan[0]["outcome"] == "low_bid"

def test_recent_end_gets_grace_period():
    plan = backfill_plan([_row(end_utc=NOW - timedelta(minutes=30))], NOW)
    assert plan == []                            # watcher may still catch it
