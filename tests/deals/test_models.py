from datetime import datetime, timezone
from deals.models import Lot, Snapshot, Outcome, Lane, lot_key

def test_lot_key_combines_all_three_ids():
    assert lot_key(984, 6466, 2) == "984/6466/2"

def test_snapshot_is_tz_aware():
    s = Snapshot(asset_id=1, account_id=2, auction_id=3,
                 observed_at=datetime(2026,7,3,tzinfo=timezone.utc),
                 bid_count=0, current_bid=10.0,
                 end_utc=datetime(2026,7,3,13,tzinfo=timezone.utc), status="STA")
    assert s.observed_at.tzinfo is not None

def test_outcome_and_lane_values():
    assert Outcome.NO_BID.value == "no_bid"
    assert Lane.HOT.value == "hot"
