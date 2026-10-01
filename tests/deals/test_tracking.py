from datetime import datetime, timedelta, timezone

import pytest

from deals.bidders import BidState
from deals.tracking import (CLOSE_GRACE, fill_missing_costs, COLD_INTERVAL, HOT_INTERVAL, WARM_INTERVAL,
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
        self.rows, self.states, self.errors, self.costs = rows, [], [], []
        self.missing, self.statuses, self.reread_limits = [], [], []

    def due(self, now):
        return list(self.rows)

    def record_state(self, state, *, next_poll_at, closed_at):
        self.states.append((state, next_poll_at, closed_at))

    def mark_error(self, asset_id, account_id, error, next_poll_at):
        self.errors.append((asset_id, account_id, error))

    def record_costs(self, asset_id, account_id, costs):
        self.costs.append((asset_id, account_id, costs))

    def needs_reread(self, limit):
        self.reread_limits.append(limit)
        return self.missing[:limit]

    def set_status(self, asset_id, account_id, status):
        self.statuses.append((asset_id, account_id, status))


@pytest.fixture
def wired(monkeypatch):
    """Route sync_tracked's I/O into fakes; returns (fake_store, observations, outcomes)."""
    import deals.tracking_store as ts
    observations, outcomes = [], []

    def _wire(rows):
        fake = _FakeStore(rows)
        for name in ("due", "record_state", "mark_error", "record_costs", "needs_reread",
                     "set_status"):
            monkeypatch.setattr(ts, name, getattr(fake, name))
        import deals.store as st
        monkeypatch.setattr(st, "append_bid_observation",
                            lambda s: (observations.append(s), True)[1])
        monkeypatch.setattr(st, "record_outcome",
                            lambda key, o, fb, fbc, ca, c: outcomes.append((key, o, fb, fbc, c)))
        monkeypatch.setattr(st, "live_auction_id", lambda a, acc: None)
        import deals.tracking as tr
        monkeypatch.setattr(tr, "_costs_tried", set())
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


class TestCosts:
    def test_every_poll_saves_the_invoice_fields(self, wired):
        fake, _, _ = wired([{"asset_id": 96, "account_id": 27562, "auction_id": 3}])
        box = {**BIDBOX, "premiumPercent": 12.5, "adminFeeAmount": 0.0, "totalTaxAmount": 0.0,
               "grandTotalAmount": 0.0, "state": "VA"}
        sync_tracked(_FakeAdapter({KEY: box}), now=NOW, verbose=False)
        assert fake.costs == [(96, 27562, {"premium_pct": 12.5, "admin_fee": 0.0, "tax_total": 0.0,
                                           "grand_total": 0.0, "lot_state": "VA"})]

    def test_closed_lots_without_costs_are_filled_once(self, wired):
        fake, _, _ = wired([])
        fake.missing = [{"asset_id": 420, "account_id": 9312, "auction_id": 5}]
        box = {**BIDBOX, "premiumPercent": 12.5, "grandTotalAmount": 900.0, "state": "NV"}
        assert fill_missing_costs(_FakeAdapter({(420, 9312, 5): box}), limit=5) == 1
        assert fake.costs[0][2]["grand_total"] == 900.0

    def test_fill_skips_a_dead_lot(self, wired):
        fake, _, _ = wired([])
        fake.missing = [{"asset_id": 1, "account_id": 2, "auction_id": 3}]
        assert fill_missing_costs(_FakeAdapter({(1, 2, 3): RuntimeError("204")}), limit=5) == 0


class TestRereadCorrectsOutcome:
    """STA at close = provisional 'sold'. The re-read that fills costs also
    reads the bidbox status, and a later RNM/CNB/CAN corrects the outcome."""
    CLOSED = {"asset_id": 3357, "account_id": 527, "auction_id": 4, "status": "STA",
              "final_bid": 1850.0, "final_bid_count": 20, "closed_at": NOW}

    def test_later_rnm_corrects_status_and_deal_lots_outcome(self, wired):
        fake, _, outcomes = wired([])
        fake.missing = [dict(self.CLOSED)]
        box = {**BIDBOX, "assetStatusCd": "RNM", "bidCount": 20, "currentBid": 1850.0}
        assert fill_missing_costs(_FakeAdapter({(3357, 527, 4): box}), limit=5) == 1
        assert fake.statuses == [(3357, 527, "RNM")]
        assert outcomes == [((3357, 527, 4), "reserve_not_met", 1850.0, 20, True)]

    def test_later_sold_status_updates_status_only(self, wired):
        fake, _, outcomes = wired([])
        fake.missing = [dict(self.CLOSED)]
        box = {**BIDBOX, "assetStatusCd": "SOA"}
        fill_missing_costs(_FakeAdapter({(3357, 527, 4): box}), limit=5)
        assert fake.statuses == [(3357, 527, "SOA")] and outcomes == []

    def test_confirmed_non_sale_is_never_flipped_back(self, wired):
        fake, _, outcomes = wired([])
        fake.missing = [{**self.CLOSED, "status": "RNM"}]
        box = {**BIDBOX, "assetStatusCd": "SOA"}
        fill_missing_costs(_FakeAdapter({(3357, 527, 4): box}), limit=5)
        assert fake.statuses == []
        assert [o[1] for o in outcomes] in ([], ["reserve_not_met"])

    def test_scan_size_is_capped(self, wired, monkeypatch):
        import deals.tracking as tr
        fake, _, _ = wired([])
        monkeypatch.setattr(tr, "_costs_tried", {(i, i, i) for i in range(5000)})
        fill_missing_costs(_FakeAdapter({}), limit=5)
        assert fake.reread_limits and fake.reread_limits[0] <= tr.REREAD_SCAN_MAX


class TestBackfillOutcomes:
    """scripts/backfill_tracking_outcomes.py: fix lots already stored as 'sold'."""
    ROWS = [
        # closed on the clock reading STA, bidbox now says RNM → both fixed
        {"asset_id": 3357, "account_id": 527, "auction_id": 4, "status": "STA", "final_bid": 1850.0,
         "final_bid_count": 20, "closed_at": NOW, "deal_outcome": "sold", "has_deal_row": True},
        # status already RNM, deal_lots still 'sold' (pre-bf3dec9), bidbox purged → outcome only
        {"asset_id": 1, "account_id": 2, "auction_id": 3, "status": "RNM", "final_bid": 900.0,
         "final_bid_count": 9, "closed_at": NOW, "deal_outcome": "sold", "has_deal_row": True},
        # a real sale → untouched
        {"asset_id": 5282, "account_id": 3780, "auction_id": 2, "status": "SOA", "final_bid": 1725.0,
         "final_bid_count": 54, "closed_at": NOW, "deal_outcome": "sold", "has_deal_row": True},
        # cancelled, no deal_lots row → status only
        {"asset_id": 7, "account_id": 8, "auction_id": 9, "status": "STA", "final_bid": 50.0,
         "final_bid_count": 2, "closed_at": NOW, "deal_outcome": None, "has_deal_row": False},
    ]
    BOXES = {(3357, 527, 4): {**BIDBOX, "assetStatusCd": "RNM"},
             (1, 2, 3): RuntimeError("204"),
             (5282, 3780, 2): {**BIDBOX, "assetStatusCd": "SOA"},
             (7, 8, 9): {**BIDBOX, "assetStatusCd": "CAN"}}

    def test_dry_run_reports_and_writes_nothing(self, wired):
        from deals.tracking import backfill_outcomes
        fake, _, outcomes = wired([])
        changes = backfill_outcomes(_FakeAdapter(self.BOXES), [dict(r) for r in self.ROWS], apply=False)
        assert [c["key"] for c in changes] == [(3357, 527, 4), (1, 2, 3), (7, 8, 9)]
        assert changes[0]["status"] == ("STA", "RNM") and changes[0]["outcome"] == ("sold", "reserve_not_met")
        assert changes[1]["status"] == ("RNM", None) and changes[1]["outcome"] == ("sold", "reserve_not_met")
        assert changes[2]["status"] == ("STA", "CAN") and changes[2]["outcome"] == (None, None)
        assert fake.statuses == [] and outcomes == []

    def test_apply_writes_and_is_idempotent(self, wired):
        from deals.tracking import backfill_outcomes
        fake, _, outcomes = wired([])
        backfill_outcomes(_FakeAdapter(self.BOXES), [dict(r) for r in self.ROWS], apply=True)
        assert fake.statuses == [(3357, 527, "RNM"), (7, 8, "CAN")]
        assert outcomes == [((3357, 527, 4), "reserve_not_met", 1850.0, 20, True),
                            ((1, 2, 3), "reserve_not_met", 900.0, 9, True)]
        # second run over the corrected rows finds nothing
        fixed = [{**r} for r in self.ROWS]
        fixed[0].update(status="RNM", deal_outcome="reserve_not_met")
        fixed[1].update(deal_outcome="reserve_not_met")
        fixed[3].update(status="CAN")
        assert backfill_outcomes(_FakeAdapter(self.BOXES), fixed, apply=True) == []
