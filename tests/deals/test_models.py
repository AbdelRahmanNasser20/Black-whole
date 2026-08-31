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

def test_lot_native_id_derived_for_govdeals(make_lot):
    lot = make_lot(asset_id=17, account_id=28505, auction_id=3)
    assert lot.site == "govdeals" and lot.native_id == "17/28505/3"

def test_lot_native_id_not_overwritten_when_given(make_lot):
    lot = make_lot(site="marknet", native_id="47644/12")
    assert lot.native_id == "47644/12"

def test_full_key(make_lot):
    from deals.models import full_key
    assert full_key(make_lot(asset_id=1, account_id=2, auction_id=3)) == "govdeals:1/2/3"

def test_synth_ids_deterministic_and_int_range():
    from deals.models import synth_ids
    a = synth_ids("marknet", "47644/12", ordinal=4)
    assert a == synth_ids("marknet", "47644/12", ordinal=4)
    assert 0 <= a[0] <= 0x7FFFFFFF and a[1] == -4 and a[2] == 0
