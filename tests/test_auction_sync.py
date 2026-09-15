"""Auction expiry / relist sync — `automation/auction_sync.py`.

The pure half needs nothing mocked. The I/O half mocks the GovDeals adapter,
the ledger and the watch store, because the whole point of the module is what
it decides to *write*, and the assertion worth making is "which ledger call,
with which arguments".
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from automation import auction_sync as asy
from automation import auction_watch_store as watch_store

NOW = datetime(2026, 9, 15, 18, 0, tzinfo=timezone.utc)
ASSET, ACCOUNT, AUCTION = 420, 9312, 5

ROW = {
    "lot_id": "gd-420-9312",
    "title": "199 Banquet Chairs — Tan Crown-Back Stacking",
    "city": "Las Vegas",
    "state": "NV",
    "status": "active_bid",
    "govdeals_url": "https://www.govdeals.com/en/asset/420/9312",
}

# Trimmed from a live GET /bids/bidbox/GD/420/9312/5 (2026-09-15).
BIDBOX = {
    "assetId": 420, "accountId": 9312, "auctionId": 5,
    "assetStatusCd": "STA", "bidCount": 1, "currentBid": 800.0,
    "assetAuctionEndDate": "2026-09-17T17:00:05",          # naive US/Eastern — a trap
    "assetAuctionEndDateUTC": "2026-09-17T21:00:05Z",      # the only one we read
    "city": "Las Vegas", "state": "NV",
}
DETAIL = {"assetId": 420, "accountId": 9312, "auctionId": 5, "assetStatusCd": "STA"}


def _state(**over) -> asy.AuctionState:
    base = dict(asset_id=ASSET, account_id=ACCOUNT, auction_id=AUCTION, status="STA",
                end_utc=NOW + timedelta(days=2), city="Las Vegas", state="NV")
    return asy.AuctionState(**{**base, **over})


# ───────────────────────────── pure: URL parsing ─────────────────────────────

class TestLotRef:
    @pytest.mark.parametrize("url", [
        "https://www.govdeals.com/en/asset/420/9312",
        "https://www.govdeals.com/asset/420/9312",
        "https://www.govdeals.com/en/asset/420/9312?utm_source=x",
    ])
    def test_asset_comes_first(self, url):
        # The ordering IS the test: /en/asset/{assetId}/{accountId}. Swapped ids
        # don't error, they return an empty 204 body.
        assert asy.lot_ref({"govdeals_url": url}) == (420, 9312)

    @pytest.mark.parametrize("row", [
        {}, {"govdeals_url": None}, {"govdeals_url": ""},
        {"govdeals_url": "https://www.publicsurplus.com/sms/auction/view?auc=123"},
        {"govdeals_url": "https://www.govdeals.com/en/search?q=chairs"},
    ])
    def test_unparseable_is_none_not_an_exception(self, row):
        assert asy.lot_ref(row) is None


class TestDetailAuctionId:
    def test_reads_the_current_auction(self):
        assert asy.detail_auction_id(DETAIL) == 5

    @pytest.mark.parametrize("detail", [None, {}, {"auctionId": 0},
                                        {"auctionId": None}, {"auctionId": "nope"}])
    def test_missing_or_zero_is_none(self, detail):
        assert asy.detail_auction_id(detail) is None


# ───────────────────────────── pure: live / expired ──────────────────────────

class TestBidboxToAuctionState:
    def test_maps_status_and_utc_close(self):
        s = asy.bidbox_to_auction_state(BIDBOX, ASSET, ACCOUNT, AUCTION)
        assert (s.asset_id, s.account_id, s.auction_id) == (420, 9312, 5)
        assert s.status == "STA"
        assert s.end_utc == datetime(2026, 9, 17, 21, 0, 5, tzinfo=timezone.utc)

    def test_ignores_the_naive_eastern_close(self):
        # assetAuctionEndDate has no zone and is US/Eastern; reading it as UTC
        # would put the close 4 hours early. No close time beats a wrong one.
        raw = {k: v for k, v in BIDBOX.items() if k != "assetAuctionEndDateUTC"}
        assert asy.bidbox_to_auction_state(raw, ASSET, ACCOUNT, AUCTION).end_utc is None

    @pytest.mark.parametrize("raw", [None, {}])
    def test_empty_payload_is_none(self, raw):
        assert asy.bidbox_to_auction_state(raw, ASSET, ACCOUNT, AUCTION) is None


class TestIsLive:
    def test_sta_before_the_clock_is_live(self):
        assert asy.is_live(_state(), NOW) is True

    def test_status_off_sta_is_closed_even_with_time_left(self):
        assert asy.is_live(_state(status="SOA"), NOW) is False

    def test_past_the_clock_inside_grace_is_still_live(self):
        # GovDeals extends a close on a late bid; the grace absorbs it.
        assert asy.is_live(_state(end_utc=NOW - timedelta(minutes=5)), NOW) is True

    def test_well_past_the_clock_is_closed(self):
        assert asy.is_live(_state(end_utc=NOW - timedelta(hours=3)), NOW) is False

    @pytest.mark.parametrize("state", [None, _state(status=None), _state(status="")])
    def test_unreadable_is_unknown_not_closed(self, state):
        # The whole safety property: "the endpoint went quiet" must never read
        # as "the auction ended".
        assert asy.is_live(state, NOW) is None


class TestDecide:
    def test_live_lot_still_live_does_nothing(self):
        assert asy.decide(watch_state=None, live=True) == asy.ACTION_NOOP
        assert asy.decide(watch_state=watch_store.STATE_LIVE, live=True) == asy.ACTION_NOOP

    def test_live_lot_gone_closed_expires(self):
        assert asy.decide(watch_state=None, live=False) == asy.ACTION_EXPIRE
        assert asy.decide(watch_state=watch_store.STATE_LIVE, live=False) == asy.ACTION_EXPIRE

    def test_expired_lot_still_closed_does_nothing(self):
        assert asy.decide(watch_state=watch_store.STATE_EXPIRED, live=False) == asy.ACTION_NOOP

    def test_expired_lot_back_live_relists(self):
        assert asy.decide(watch_state=watch_store.STATE_EXPIRED, live=True) == asy.ACTION_RELIST

    @pytest.mark.parametrize("watch_state", [None, "live", "expired"])
    def test_unknown_never_acts(self, watch_state):
        assert asy.decide(watch_state=watch_state, live=None) == asy.ACTION_UNKNOWN


class TestIsNewAuction:
    def test_a_different_auction_id_is_a_relist(self):
        assert asy.is_new_auction(5, 6) is True

    def test_the_same_auction_id_is_not(self):
        # We called the close early and the extension ran — still restore, but
        # it isn't a second listing.
        assert asy.is_new_auction(5, 5) is False

    @pytest.mark.parametrize("known,current", [(None, 6), (5, None), (None, None)])
    def test_unknown_ids_read_as_new(self, known, current):
        assert asy.is_new_auction(known, current) is True


# ───────────────────────────── pure: the message ─────────────────────────────

class TestRelistMessage:
    def test_has_title_location_close_and_site_link(self):
        msg = asy.relist_message(ROW, _state(), new_auction=True,
                                 site_base="https://black-whole.com")
        assert msg.startswith("RELISTED: 199 Banquet Chairs")
        assert "Las Vegas, NV" in msg
        assert "https://black-whole.com/listings/gd-420-9312" in msg
        assert "closes " in msg

    def test_same_auction_reads_back_live(self):
        msg = asy.relist_message(ROW, _state(), new_auction=False)
        assert msg.startswith("BACK LIVE: ")

    def test_no_markdown_characters_are_introduced(self):
        # Plain text only — one stray underscore breaks Telegram's parser.
        msg = asy.relist_message({**ROW, "title": "Chairs"}, _state(), new_auction=True)
        assert "*" not in msg and "`" not in msg

    def test_missing_close_time_still_sends(self):
        msg = asy.relist_message(ROW, _state(end_utc=None), new_auction=True)
        assert "an unknown time" in msg

    def test_close_renders_in_the_operators_zone(self):
        out = asy.format_close(datetime(2026, 9, 17, 21, 0, 5, tzinfo=timezone.utc),
                               "America/Phoenix")
        assert "Sep 17" in out and "2:00 PM" in out


# ───────────────────────────── I/O: sync_once ────────────────────────────────

class FakeAdapter:
    """Two calls, both keyed asset-then-account (see fetch_state)."""

    def __init__(self, detail=None, bidbox=None, detail_exc=None):
        self.detail, self.bidbox, self.detail_exc = detail, bidbox, detail_exc
        self.calls: list[tuple] = []

    def fetch_detail(self, asset_id, account_id):
        self.calls.append(("detail", asset_id, account_id))
        if self.detail_exc:
            raise self.detail_exc
        return self.detail

    def fetch_bid_state(self, asset_id, account_id, auction_id):
        self.calls.append(("bidbox", asset_id, account_id, auction_id))
        return self.bidbox


@pytest.fixture
def wired(monkeypatch):
    """Everything `sync_once` can write to, replaced by a recorder."""
    from automation import inventory, lot_channels

    seen: dict = {"removed": [], "restored": [], "set_fields": [], "notified": [],
                  "expired": [], "relisted": [], "checked": [], "errors": []}

    monkeypatch.setattr(asy, "sync_candidates", lambda lot_ids=None: [dict(ROW)])
    monkeypatch.setattr(watch_store, "schema_ready", lambda: True)
    monkeypatch.setattr(watch_store, "get", lambda lot_id: seen.get("watch_row"))
    monkeypatch.setattr(watch_store, "mark_expired",
                        lambda lot_id, **kw: seen["expired"].append((lot_id, kw)))
    monkeypatch.setattr(watch_store, "mark_relisted",
                        lambda lot_id, **kw: seen["relisted"].append((lot_id, kw)))
    monkeypatch.setattr(watch_store, "mark_checked",
                        lambda lot_id, **kw: seen["checked"].append((lot_id, kw)))
    monkeypatch.setattr(watch_store, "mark_error",
                        lambda lot_id, **kw: seen["errors"].append((lot_id, kw)))
    monkeypatch.setattr(lot_channels, "remove_lot",
                        lambda lot_id, **kw: seen["removed"].append((lot_id, kw)))
    monkeypatch.setattr(lot_channels, "restore_lot",
                        lambda lot_id, **kw: seen["restored"].append((lot_id, kw)))
    monkeypatch.setattr(inventory, "set_fields",
                        lambda lot_id, **kw: seen["set_fields"].append((lot_id, kw)) or dict(ROW))
    monkeypatch.setattr(asy, "_notify", lambda text, log: seen["notified"].append(text))
    return seen


def _run(adapter, **kw):
    return asy.sync_once(adapter=adapter, now=NOW, log=lambda m: None, **kw)


class TestSyncOnceExpiry:
    def test_a_closed_auction_is_taken_off_site_and_feed(self, wired):
        rep = _run(FakeAdapter(DETAIL, {**BIDBOX, "assetStatusCd": "SOA"}))
        assert rep["expired"] == 1 and rep["unchanged"] == 0
        lot_id, kw = wired["removed"][0]
        assert lot_id == "gd-420-9312"
        # 'fb' is never in the channel set: Marketplace needs a browser and an
        # operator, not a background loop.
        assert kw["channels"] == ("site", "business")

    def test_expiry_records_when_and_why(self, wired):
        _run(FakeAdapter(DETAIL, {**BIDBOX, "assetStatusCd": "SOA"}))
        _, kw = wired["expired"][0]
        assert kw["reason"] == "assetStatusCd=SOA"
        assert kw["now"] == NOW
        assert kw["prior_status"] == "active_bid"     # so a relist restores it
        assert (kw["asset_id"], kw["account_id"]) == (ASSET, ACCOUNT)

    def test_a_live_auction_changes_nothing(self, wired):
        rep = _run(FakeAdapter(DETAIL, BIDBOX))
        assert rep["unchanged"] == 1 and rep["expired"] == 0
        assert wired["removed"] == [] and wired["restored"] == []

    def test_dry_run_writes_nothing(self, wired):
        rep = _run(FakeAdapter(DETAIL, {**BIDBOX, "assetStatusCd": "SOA"}), dry_run=True)
        assert rep["expired"] == 1 and rep["dry_run"] is True
        assert wired["removed"] == [] and wired["expired"] == [] and wired["notified"] == []


class TestSyncOnceRelist:
    @pytest.fixture
    def expired_watch(self, wired):
        wired["watch_row"] = {"state": "expired", "auction_id": 5, "prior_status": "active_bid"}
        return wired

    def test_new_auction_restores_the_lot_and_pings_telegram(self, expired_watch):
        rep = _run(FakeAdapter({**DETAIL, "auctionId": 6},
                               {**BIDBOX, "auctionId": 6, "assetStatusCd": "STA"}))
        assert rep["relisted"] == 1
        lot_id, kw = expired_watch["restored"][0]
        assert (lot_id, kw["status"]) == ("gd-420-9312", "active_bid")
        assert expired_watch["notified"][0].startswith("RELISTED: ")
        assert "black-whole.com/listings/gd-420-9312" in expired_watch["notified"][0]

    def test_the_new_auction_url_is_stamped_through_the_ledger(self, expired_watch):
        _run(FakeAdapter({**DETAIL, "auctionId": 6}, {**BIDBOX, "auctionId": 6}))
        lot_id, kw = expired_watch["set_fields"][0]
        assert lot_id == "gd-420-9312"
        # Asset first — the URL we write back must survive the next parse.
        assert kw["govdeals_url"] == "https://www.govdeals.com/en/asset/420/9312"

    def test_the_same_auction_back_live_restores_but_reads_back_live(self, expired_watch):
        rep = _run(FakeAdapter(DETAIL, BIDBOX))       # still auction 5
        assert rep["relisted"] == 1
        assert expired_watch["restored"], "a premature expiry must still self-heal"
        assert expired_watch["notified"][0].startswith("BACK LIVE: ")

    def test_relist_remembers_the_status_the_lot_had(self, expired_watch):
        expired_watch["watch_row"] = {"state": "expired", "auction_id": 5,
                                      "prior_status": "listed"}
        _run(FakeAdapter({**DETAIL, "auctionId": 6}, {**BIDBOX, "auctionId": 6}))
        assert expired_watch["restored"][0][1]["status"] == "listed"

    def test_a_still_closed_expired_lot_is_left_alone(self, expired_watch):
        rep = _run(FakeAdapter(DETAIL, {**BIDBOX, "assetStatusCd": "SOA"}))
        assert rep["unchanged"] == 1 and rep["relisted"] == 0
        assert expired_watch["restored"] == [] and expired_watch["notified"] == []


class TestSyncOnceNeverActsOnSilence:
    def test_a_204_from_maestro_leaves_the_lot_alone(self, wired):
        # Verified live on 53677/357: a purged asset answers 204 with no body.
        rep = _run(FakeAdapter(detail={}, bidbox={}))
        assert rep["unresolved"] == 1 and rep["expired"] == 0
        assert wired["removed"] == []
        assert wired["errors"], "the operator still needs to see it couldn't be read"

    def test_a_bidbox_with_no_status_leaves_the_lot_alone(self, wired):
        raw = {k: v for k, v in BIDBOX.items() if k != "assetStatusCd"}
        rep = _run(FakeAdapter(DETAIL, raw))
        assert rep["unresolved"] == 1 and rep["expired"] == 0
        assert wired["removed"] == []

    def test_an_adapter_exception_is_isolated_not_fatal(self, wired):
        rep = _run(FakeAdapter(detail_exc=RuntimeError("maestro 500")))
        assert rep["errors"] == 1 and rep["expired"] == 0
        assert wired["removed"] == []

    def test_a_row_with_a_junk_url_is_skipped_without_a_network_call(self, monkeypatch, wired):
        monkeypatch.setattr(asy, "sync_candidates",
                            lambda lot_ids=None: [{**ROW, "govdeals_url": "nope"}])
        adapter = FakeAdapter(DETAIL, BIDBOX)
        rep = _run(adapter)
        assert rep["unresolved"] == 1 and adapter.calls == []

    def test_without_the_migration_a_real_run_refuses(self, wired, monkeypatch):
        monkeypatch.setattr(watch_store, "schema_ready", lambda: False)
        rep = _run(FakeAdapter(DETAIL, {**BIDBOX, "assetStatusCd": "SOA"}))
        assert "011_inventory_auction_watch.sql" in rep["error"]
        assert wired["removed"] == [], "no ledger write before the table exists"

    def test_without_the_migration_a_dry_run_still_reports(self, wired, monkeypatch):
        monkeypatch.setattr(watch_store, "schema_ready", lambda: False)
        rep = _run(FakeAdapter(DETAIL, {**BIDBOX, "assetStatusCd": "SOA"}), dry_run=True)
        assert rep["expired"] == 1 and "error" not in rep


# ──────────────────── the storefront actually drops the lot ───────────────────

class TestExpiredLotLeavesEveryPublicSurface:
    """The chain the operator cares about: expire → the lot is off the site.

    `fake_sold_out` on its own does NOT do that — `inventory.list_public` and
    `list_catalog_feed` gate on `status`, and `fake_sold_out` is the flag the
    CRM honors by never offering the lot. `lot_channels.remove_lot` sets both,
    which is why the sync goes through it instead of writing the boolean.
    """

    def test_an_active_bid_lot_expires_to_a_status_no_public_surface_carries(self):
        from automation import inventory, lot_channels

        status = lot_channels.sold_status_for({"status": "active_bid"})
        assert status == "lost_sold_out"          # never owned, shown as SOLD
        assert status not in inventory.PUBLIC_STATUSES
        assert status not in inventory.CATALOG_FEED_STATUSES
        assert status in inventory.SOLD_STATUSES  # ALREADY MOVED strip

    def test_active_bid_is_on_every_public_surface_until_then(self):
        from automation import inventory

        assert "active_bid" in inventory.PUBLIC_STATUSES
        assert "active_bid" in inventory.CATALOG_FEED_STATUSES

    def test_the_sold_showcase_still_needs_a_headcount_and_a_photo(self):
        from automation import inventory

        # An expired lot keeps its photos and quantity_original precisely so it
        # can carry a card here — don't loosen this without a plan for the
        # half-imported folder stubs it filters out.
        where = inventory._SOLD_SHOWCASE_WHERE
        assert "quantity_original" in where and "hero_image_url" in where

    def test_the_ledger_can_restamp_govdeals_url(self):
        # The relist path writes the new auction's URL through set_fields, not
        # by raw UPDATE — so the column has to be on the whitelist.
        import inspect

        from automation import inventory

        assert '"govdeals_url"' in inspect.getsource(inventory.set_fields)
