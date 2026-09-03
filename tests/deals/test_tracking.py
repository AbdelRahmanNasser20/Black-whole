from datetime import datetime, timedelta, timezone

import pytest

from deals.bidders import BidState
from deals.tracking import (CLOSE_GRACE, COLD_INTERVAL, HOT_INTERVAL, WARM_INTERVAL,
                            bidder_summary, is_closed, parse_lot_ref, poll_interval,
                            sync_tracked)

NOW = datetime(2026, 9, 3, 18, 0, tzinfo=timezone.utc)
KEY = (96, 27562, 3)

# Trimmed from a live GET /bids/bidbox/GD/96/27562/3 (Fort Myer, 195 banquet chairs).
BIDBOX = {
    "bidCount": 15, "currentBid": 310.0, "assetBidIncrement": 10.0,
    "highBidder": 1052989, "highBidderUsername": "sa*****",
    "visitors": 53, "hits": 184, "watcherCount": 29,
    "assetAuctionEndDateUTC": "2026-09-14T19:30:00Z", "assetStatusCd": "STA",
}


def _state(**over) -> BidState:
    base = dict(asset_id=96, account_id=27562, auction_id=3, observed_at=NOW,
                bid_count=15, current_bid=310.0, currency_code="USD",
                high_bidder=1052989, high_bidder_username="sa*****",
                bid_increment=10.0, visitors=53, hits=184, watcher_count=29,
                end_utc=NOW + timedelta(days=11), status="STA")
    return BidState(**{**base, **over})


class TestParseLotRef:
    @pytest.mark.parametrize("ref", [
        "https://www.govdeals.com/en/asset/96/27562",
        "https://www.govdeals.com/en/asset/96/27562?utm=x",
        "www.govdeals.com/asset/96/27562/",
        "96/27562",
        "  96/27562 \n",
    ])
    def test_url_and_bare_forms(self, ref):
        # Same asset-then-account order as the URL: the ordering is the whole test.
        assert parse_lot_ref(ref) == (96, 27562)

    @pytest.mark.parametrize("ref", ["", None, "ps:4019110", "chairs", "96", "a/b"])
    def test_garbage_is_none(self, ref):
        assert parse_lot_ref(ref) is None


class TestIsClosed:
    def test_live_lot_before_close_is_open(self):
        assert is_closed(_state(), NOW) is False

    def test_status_off_sta_is_closed_even_before_clock(self):
        # 5282/3780 came back SOA with the clock still readable — status wins.
        assert is_closed(_state(status="SOA"), NOW) is True

    def test_past_clock_inside_grace_is_still_open(self):
        # Anti-snipe extension may be in flight.
        s = _state(end_utc=NOW - CLOSE_GRACE + timedelta(minutes=1))
        assert is_closed(s, NOW) is False

    def test_past_clock_beyond_grace_is_closed(self):
        s = _state(end_utc=NOW - CLOSE_GRACE - timedelta(seconds=1))
        assert is_closed(s, NOW) is True

    def test_unknown_end_and_live_status_stays_open(self):
        assert is_closed(_state(end_utc=None), NOW) is False


class TestPollInterval:
    def test_tightens_toward_the_close(self):
        assert poll_interval(NOW + timedelta(days=3), NOW) == COLD_INTERVAL
        assert poll_interval(NOW + timedelta(hours=5), NOW) == WARM_INTERVAL
        assert poll_interval(NOW + timedelta(minutes=10), NOW) == HOT_INTERVAL

    def test_past_clock_is_hot(self):
        # Extension may be live: keep sampling every minute until status flips.
        assert poll_interval(NOW - timedelta(minutes=3), NOW) == HOT_INTERVAL

    def test_unknown_end_polls_warm(self):
        assert poll_interval(None, NOW) == WARM_INTERVAL


class TestBidderSummary:
    def test_collapses_by_bidder_id_and_keeps_max_bid(self):
        obs = [
            {"high_bidder": 1, "high_bidder_username": "sa*****", "current_bid": 100, "observed_at": 1},
            {"high_bidder": 2, "high_bidder_username": "th*****", "current_bid": 120, "observed_at": 2},
            {"high_bidder": 1, "high_bidder_username": "sa*****", "current_bid": 150, "observed_at": 3},
            {"high_bidder": None, "high_bidder_username": None, "current_bid": 10, "observed_at": 0},
        ]
        out = bidder_summary(obs)
        assert [e["bidder_id"] for e in out] == [1, 2]          # sorted by max_bid desc
        assert out[0] == {"bidder_id": 1, "handle": "sa*****", "times_led": 2,
                          "first_led_at": 1, "last_led_at": 3, "max_bid": 150.0}


class _FakeAdapter:
    def __init__(self, responses, detail=None):
        self.responses, self.detail = responses, detail or {}
        self.calls = []

    def fetch_bid_state(self, asset_id, account_id, auction_id):
        self.calls.append((asset_id, account_id, auction_id))
        r = self.responses[(asset_id, account_id, auction_id)]
        if isinstance(r, Exception):
            raise r
        return r

    def fetch_detail(self, asset_id, account_id):
        return self.detail.get((asset_id, account_id), {})


class _FakeStore:
    """Captures what sync_tracked writes, in place of deals.tracking_store."""
    def __init__(self, rows):
        self.rows, self.states, self.errors = rows, [], []

    def due(self, now):
        return list(self.rows)

    def record_state(self, state, *, next_poll_at, closed_at):
        self.states.append((state, next_poll_at, closed_at))

    def mark_error(self, asset_id, account_id, error, next_poll_at):
        self.errors.append((asset_id, account_id, error))


@pytest.fixture
def wired(monkeypatch):
    """Route sync_tracked's I/O into fakes; returns (fake_store, observations, outcomes)."""
    import deals.tracking_store as ts
    observations, outcomes = [], []

    def _wire(rows):
        fake = _FakeStore(rows)
        for name in ("due", "record_state", "mark_error"):
            monkeypatch.setattr(ts, name, getattr(fake, name))
        import deals.store as st
        monkeypatch.setattr(st, "append_bid_observation",
                            lambda s: (observations.append(s), True)[1])
        monkeypatch.setattr(st, "record_outcome",
                            lambda key, o, fb, fbc, ca, c: outcomes.append((key, o, fb, fbc, c)))
        monkeypatch.setattr(st, "live_auction_id", lambda a, acc: None)
        return fake, observations, outcomes
    return _wire


class TestSyncTracked:
    def test_live_lot_is_recorded_and_rescheduled(self, wired):
        fake, obs, outcomes = wired([{"asset_id": 96, "account_id": 27562, "auction_id": 3}])
        rep = sync_tracked(_FakeAdapter({KEY: BIDBOX}), now=NOW, verbose=False)
        assert rep == {"due": 1, "polled": 1, "recorded": 1, "closed": 0, "errors": 0}
        assert obs[0].high_bidder_username == "sa*****"
        state, next_at, closed_at = fake.states[0]
        assert closed_at is None and next_at == NOW + timedelta(seconds=COLD_INTERVAL)
        assert outcomes == []

    def test_closed_lot_stamps_finals_and_deal_lots_outcome(self, wired):
        fake, obs, outcomes = wired([{"asset_id": 5282, "account_id": 3780, "auction_id": 2}])
        sold = {**BIDBOX, "bidCount": 54, "currentBid": 1725.0, "highBidder": 3800371,
                "highBidderUsername": "th*****", "assetStatusCd": "SOA",
                "assetAuctionEndDateUTC": "2026-09-03T01:21:51Z"}
        rep = sync_tracked(_FakeAdapter({(5282, 3780, 2): sold}), now=NOW, verbose=False)
        assert rep["closed"] == 1
        state, next_at, closed_at = fake.states[0]
        assert next_at is None and closed_at == NOW           # stops polling
        assert outcomes == [((5282, 3780, 2), "sold", 1725.0, 54, True)]

    def test_auction_id_is_resolved_from_detail_when_missing(self, wired):
        fake, obs, _ = wired([{"asset_id": 96, "account_id": 27562, "auction_id": None}])
        adapter = _FakeAdapter({KEY: BIDBOX}, detail={(96, 27562): {"auctionId": 3}})
        rep = sync_tracked(adapter, now=NOW, verbose=False)
        assert rep["polled"] == 1 and adapter.calls == [KEY]

    def test_one_dead_lot_does_not_abort_the_pass(self, wired):
        fake, obs, _ = wired([
            {"asset_id": 1, "account_id": 2, "auction_id": 3},
            {"asset_id": 96, "account_id": 27562, "auction_id": 3},
        ])
        adapter = _FakeAdapter({(1, 2, 3): RuntimeError("204 empty body"), KEY: BIDBOX})
        rep = sync_tracked(adapter, now=NOW, verbose=False)
        assert rep["errors"] == 1 and rep["recorded"] == 1
        assert fake.errors[0][:2] == (1, 2) and "RuntimeError" in fake.errors[0][2]
