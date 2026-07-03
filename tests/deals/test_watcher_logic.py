from datetime import datetime, timezone, timedelta
from deals.models import Snapshot, Lane, Outcome
from deals.watcher_logic import schedule_lane, next_poll_delay, is_snapshot_change, detect_outcome

NOW = datetime(2026,7,3,12,0,tzinfo=timezone.utc)
def snap(bc=0, cur=10.0, end=NOW+timedelta(hours=5), status="STA"):
    return Snapshot(1,2,3, NOW, bc, cur, end, status)

def test_lane_by_time_to_close():
    assert schedule_lane(NOW+timedelta(days=2), NOW) == Lane.COLD
    assert schedule_lane(NOW+timedelta(hours=5), NOW) == Lane.WARM
    assert schedule_lane(NOW+timedelta(minutes=10), NOW) == Lane.HOT
    assert schedule_lane(NOW-timedelta(minutes=1), NOW) == Lane.HOT   # past clock but maybe extending -> still hot

def test_poll_delay_tightens_near_close():
    assert next_poll_delay(NOW+timedelta(days=2), NOW, Lane.COLD) == 6*3600
    assert next_poll_delay(NOW+timedelta(hours=5), NOW, Lane.WARM) == 3600
    # hot: poll every few seconds, but never sleep past the close
    assert next_poll_delay(NOW+timedelta(minutes=10), NOW, Lane.HOT) == 5.0

def test_snapshot_change_detects_bid_price_end_or_status():
    prev = snap()
    assert is_snapshot_change(None, prev) is True                       # first ever
    assert is_snapshot_change(prev, snap()) is False                    # identical -> skip write
    assert is_snapshot_change(prev, snap(bc=1)) is True                 # new bid
    assert is_snapshot_change(prev, snap(cur=12.0)) is True             # price moved
    assert is_snapshot_change(prev, snap(end=NOW+timedelta(hours=5,minutes=3))) is True  # extension
    assert is_snapshot_change(prev, snap(status="SOLD")) is True        # status changed

def test_outcome_no_bid_when_dropped_at_zero():
    o, complete = detect_outcome(snap(bc=0), dropped=True)
    assert o == Outcome.NO_BID and complete is True

def test_outcome_low_bid_and_sold():
    assert detect_outcome(snap(bc=1), dropped=True)[0] == Outcome.LOW_BID
    assert detect_outcome(snap(bc=8), dropped=True)[0] == Outcome.SOLD

def test_outcome_incomplete_if_not_yet_dropped():
    o, complete = detect_outcome(snap(bc=0), dropped=False)
    assert complete is False                                            # still live; not a final outcome

def test_hot_poll_never_overshoots_sub_second_close():
    d = next_poll_delay(NOW + timedelta(milliseconds=300), NOW, Lane.HOT)
    assert d <= 0.3 + 1e-9        # must not be floored up to 1.0 past the close
