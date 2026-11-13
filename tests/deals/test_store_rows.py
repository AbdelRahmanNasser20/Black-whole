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


def test_row_to_lot_round_trips_a_foreign_row(make_lot):
    from deals.models import synth_ids
    from deals.store import row_to_lot
    ids = synth_ids("txauction", "31431/57702", ordinal=10)
    lot = make_lot(asset_id=ids[0], account_id=ids[1], auction_id=ids[2], site="txauction",
                   native_id="31431/57702", raw={"auction_lot_id": "57702"})
    row = dict(zip(LOT_COLUMNS, lot_row(lot)))
    row["raw"] = lot.raw                      # psycopg hands JSONB back as a dict
    back = row_to_lot(row)
    assert back == lot


def test_due_for_poll_filters_by_site(monkeypatch):
    from deals import store
    cap = []
    monkeypatch.setattr(store.db, "fetch_all", lambda sql, params=(): cap.append((sql, params)) or [])
    now = datetime(2026, 7, 3, 12, tzinfo=timezone.utc)
    store.due_for_poll(now)
    store.due_for_poll(now, site="txauction")
    (gd_sql, gd_p), (tx_sql, tx_p) = cap
    assert "site = %s" in gd_sql and gd_p[0] == "govdeals" and "raw" not in gd_sql.split(" FROM ")[0]
    assert tx_p[0] == "txauction" and "native_id" in tx_sql   # stored columns, not maestro raw


def test_due_for_poll_never_loads_raw_and_is_bounded(monkeypatch):
    """deals-watch OOM (512 MiB): the govdeals pass loaded the ~15 KB `raw`
    blob of every due row (135k+) with no LIMIT. Select the poll columns only,
    soonest-ending first, capped by DEALS_WATCH_MAX_PER_RUN."""
    from deals import store
    cap = []
    monkeypatch.setattr(store.db, "fetch_all", lambda sql, params=(): cap.append((sql, params)) or [])
    monkeypatch.setenv("DEALS_WATCH_MAX_PER_RUN", "123")
    now = datetime(2026, 7, 3, 12, tzinfo=timezone.utc)
    store.due_for_poll(now)
    store.due_for_poll(now, site="txauction")
    for sql, params in cap:
        select = sql.split(" FROM ")[0]
        assert "raw" not in select and "description" not in select
        assert "raw IS NOT NULL" in sql and "end_utc IS NOT NULL" in sql
        assert "ORDER BY end_utc" in sql and "LIMIT %s" in sql and params[-1] == 123


def test_due_for_poll_default_limit(monkeypatch):
    from deals import store
    cap = []
    monkeypatch.setattr(store.db, "fetch_all", lambda sql, params=(): cap.append(params) or [])
    monkeypatch.delenv("DEALS_WATCH_MAX_PER_RUN", raising=False)
    store.due_for_poll(datetime(2026, 7, 3, 12, tzinfo=timezone.utc))
    assert cap[0][-1] == 5000


def test_due_for_poll_builds_govdeals_lot_from_columns(monkeypatch):
    from deals import store
    end = datetime(2026, 7, 3, 13, tzinfo=timezone.utc)
    row = {"asset_id": 984, "account_id": 6466, "auction_id": 2, "end_utc": end,
           "bid_count": 0, "current_bid": 10, "status": "STA", "site": "govdeals",
           "native_id": "984/6466/2"}
    monkeypatch.setattr(store.db, "fetch_all", lambda sql, params=(): [row])
    (lot,) = store.due_for_poll(datetime(2026, 7, 3, 12, tzinfo=timezone.utc))
    assert (lot.asset_id, lot.account_id, lot.auction_id) == (984, 6466, 2)
    assert lot.end_utc == end and lot.bid_count == 0 and lot.current_bid == 10.0
    assert lot.status == "STA" and lot.site == "govdeals"
