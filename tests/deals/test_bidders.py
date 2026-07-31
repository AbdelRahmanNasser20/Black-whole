from datetime import datetime, timezone

import pytest

from deals.bidders import (BidState, bidbox_to_state, is_bid_change,
                           parse_favorite_key, track_bidders)

NOW = datetime(2026, 7, 29, 3, 0, tzinfo=timezone.utc)
KEY = (17, 28505, 4)

# Trimmed from a live GET /bids/bidbox/GD/17/28505/4 (Fort Sill, 250 chairs).
BIDBOX = {
    "assetId": 17, "accountId": 28505, "auctionId": 4,
    "bidCount": 7, "currentBid": 785.0, "assetBidIncrement": 25.0,
    "highBidder": 3939619, "highBidderUsername": "ja*****",
    "visitors": 18, "hits": 53, "watcherCount": 1,
    "assetAuctionEndDateUTC": "2026-08-03T19:45:48Z", "assetStatusCd": "STA",
}


def _state(**over) -> BidState:
    base = dict(asset_id=17, account_id=28505, auction_id=4, observed_at=NOW,
                bid_count=7, current_bid=785.0, currency_code="USD",
                high_bidder=3939619, high_bidder_username="ja*****",
                bid_increment=25.0, visitors=18, hits=53, watcher_count=1,
                end_utc=None, status="STA")
    return BidState(**{**base, **over})


class TestParseFavoriteKey:
    def test_govdeals_key_is_asset_then_account(self):
        # Same order as the lot URL /en/asset/17/28505; swapping them silently
        # queries a different lot, so this is the test that matters most here.
        assert parse_favorite_key("17/28505") == (17, 28505)

    @pytest.mark.parametrize("raw", ["ps:4019110", "bs:e8b847b0-0ea4", "", "17", "a/b", "17/28505/4"])
    def test_non_govdeals_or_malformed_returns_none(self, raw):
        assert parse_favorite_key(raw) is None


class TestBidboxToState:
    def test_maps_the_bidder_identity_fields(self):
        s = bidbox_to_state(BIDBOX, KEY, NOW)
        assert (s.high_bidder, s.high_bidder_username) == (3939619, "ja*****")
        assert (s.bid_count, s.current_bid) == (7, 785.0)
        assert (s.visitors, s.hits, s.watcher_count) == (18, 53, 1)
        assert s.end_utc == datetime(2026, 8, 3, 19, 45, 48, tzinfo=timezone.utc)

    def test_keeps_the_mask_verbatim(self):
        # The asterisks encode handle length — stripping them loses the only
        # length signal GovDeals gives us.
        assert bidbox_to_state(BIDBOX, KEY, NOW).high_bidder_username == "ja*****"

    def test_zero_high_bidder_becomes_null(self):
        s = bidbox_to_state({**BIDBOX, "highBidder": 0, "bidCount": 0}, KEY, NOW)
        assert s.high_bidder is None

    @pytest.mark.parametrize("bad", [None, "n/a"])
    def test_missing_or_garbled_price_fails_loud(self, bad):
        # Mirrors mapping._price: a silent 0.0 reads as "nobody has bid" and
        # would talk us into a lot that is actually contested.
        with pytest.raises(ValueError):
            bidbox_to_state({**BIDBOX, "currentBid": bad}, KEY, NOW)

    def test_optional_fields_degrade_to_none(self):
        s = bidbox_to_state({"currentBid": 10.0}, KEY, NOW)
        assert (s.visitors, s.hits, s.watcher_count, s.end_utc) == (None, None, None, None)
        assert s.bid_count == 0 and s.currency_code == "USD"


class TestIsBidChange:
    def test_first_observation_always_records(self):
        assert is_bid_change(None, _state()) is True

    def test_lead_change_records_even_at_the_same_price(self):
        # A proxy-bid war can hand the lead over without moving the displayed
        # price; that's precisely the event we're collecting.
        assert is_bid_change(_state(high_bidder=1052989), _state()) is True

    @pytest.mark.parametrize("field,value", [("bid_count", 8), ("current_bid", 810.0)])
    def test_bid_activity_records(self, field, value):
        assert is_bid_change(_state(), _state(**{field: value})) is True

    def test_traffic_drift_alone_does_not_record(self):
        # visitors/hits tick on every poll; writing rows for them would bury
        # the lead changes under noise.
        assert is_bid_change(_state(), _state(visitors=99, hits=400, watcher_count=6)) is False


class _FakeAdapter:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def fetch_bid_state(self, asset_id, account_id, auction_id):
        self.calls.append((asset_id, account_id, auction_id))
        r = self.responses[(asset_id, account_id, auction_id)]
        if isinstance(r, Exception):
            raise r
        return r


class TestTrackBidders:
    def test_one_dead_lot_does_not_abort_the_sweep(self, monkeypatch):
        written = []
        monkeypatch.setattr("deals.store.append_bid_observation",
                            lambda s: (written.append(s), True)[1], raising=False)
        adapter = _FakeAdapter({
            (1, 2, 3): RuntimeError("204 empty body"),
            KEY: BIDBOX,
        })
        rep = track_bidders(adapter, [(1, 2, 3), KEY], now=NOW, verbose=False)
        assert rep["errors"] == 1 and rep["recorded"] == 1
        assert [s.high_bidder for s in written] == [3939619]

    def test_unchanged_state_is_counted_not_written(self, monkeypatch):
        monkeypatch.setattr("deals.store.append_bid_observation", lambda s: False, raising=False)
        rep = track_bidders(_FakeAdapter({KEY: BIDBOX}), [KEY], now=NOW, verbose=False)
        assert rep == {"polled": 1, "recorded": 0, "unchanged": 1, "errors": 0}
