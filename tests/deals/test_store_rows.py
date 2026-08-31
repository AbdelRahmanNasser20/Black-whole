from datetime import datetime, timezone
from deals.models import Lot, Snapshot
from deals.store import lot_row, snapshot_row, LOT_COLUMNS, SNAPSHOT_COLUMNS

def _lot():
    return Lot(asset_id=984, account_id=6466, auction_id=2, title="t", description="d",
        native_category_id="372", native_category_name="Furniture and Furnishings",
        canonical_category="seating_furniture", end_utc=datetime(2026,7,3,13,tzinfo=timezone.utc),
        bid_count=0, opening_bid=10.0, current_bid=10.0, currency_code="USD", high_bidder=0,
        has_reserve=False, reserve_not_met=False, reserve_price=None, is_free=False,
        seller="City", city="Warren", state="ME", zip="04864", lat=44.1, lng=-69.2,
        hero_image_url="http://x/y.jpg", status="STA", is_sold=False, raw={"a": 1})

def test_lot_row_matches_column_count():
    row = lot_row(_lot())
    assert len(row) == len(LOT_COLUMNS)

def test_lot_row_price_position_is_current_bid():
    row = lot_row(_lot())
    assert row[LOT_COLUMNS.index("current_bid")] == 10.0
    assert row[LOT_COLUMNS.index("opening_bid")] == 10.0

def test_snapshot_row_matches_columns():
    s = Snapshot(984,6466,2, datetime(2026,7,3,12,tzinfo=timezone.utc), 0, 10.0,
                 datetime(2026,7,3,13,tzinfo=timezone.utc), "STA")
    assert len(snapshot_row(s)) == len(SNAPSHOT_COLUMNS)
    assert snapshot_row(s)[SNAPSHOT_COLUMNS.index("site")] == "govdeals"

def test_lot_row_ends_with_site_and_native_id():
    row = lot_row(_lot())
    assert row[-2:] == ("govdeals", "984/6466/2")

def test_lot_row_foreign_site_round_trips(make_lot):
    from deals.models import synth_ids
    ids = synth_ids("marknet", "47644/12", ordinal=4)
    row = lot_row(make_lot(*[], asset_id=ids[0], account_id=ids[1], auction_id=ids[2],
                           site="marknet", native_id="47644/12"))
    assert row[LOT_COLUMNS.index("site")] == "marknet"
    assert row[LOT_COLUMNS.index("native_id")] == "47644/12"
    assert row[LOT_COLUMNS.index("account_id")] == -4
