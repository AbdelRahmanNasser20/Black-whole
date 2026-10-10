"""TXAuction adapter — fixture-only. requests.get is monkeypatched to raise, so
any accidental fetch fails the test; the adapter's own transport is fed a fake
session that serves the saved pages."""
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

import deals.adapters.txauction as tx
from deals.adapters.txauction import (TXAuctionAdapter, card_to_lot, derive_title,
                                      extract_apollo_state, lot_to_snapshot, parse_auction_index,
                                      parse_catalog_page, parse_lot_page, parse_price,
                                      parse_search_page)
from deals.models import lot_key, synth_ids
from tests.deals.adapter_contract import check_lots

FIX = Path("tests/deals/fixtures/txauction")
SEARCH = (FIX / "search_page.html").read_text()
SEARCH_SEATING = (FIX / "search_page_seating.html").read_text()
AUCTIONS = (FIX / "auctions_index.html").read_text()
CATALOG = (FIX / "catalog_page.html").read_text()
LOT = (FIX / "lot_page.html").read_text()


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    def boom(*a, **kw):
        raise AssertionError("fixture test touched the network")
    monkeypatch.setattr(tx.requests, "get", boom)


class _Resp:
    def __init__(self, text, status=200):
        self.text, self.status_code = text, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise tx.requests.HTTPError(str(self.status_code))


class _Session:
    """Serves fixtures by URL; records every URL asked for."""
    def __init__(self, pages: dict[str, str], status: dict[str, int] | None = None):
        self.pages, self.status, self.urls = pages, status or {}, []

    def get(self, url, headers=None, timeout=None):
        self.urls.append(url)
        assert headers and "BLACKWHOLE" in headers["User-Agent"]
        assert "/api/" not in url and "/asset/" not in url, "robots.txt disallows /api/ and /asset/"
        for prefix, body in self.pages.items():
            if url.startswith(prefix):
                return _Resp(body, self.status.get(prefix, 200))
        return _Resp(SEARCH.replace('"total":1', '"total":0').replace('"lots":[{"__ref":"AuctionLot.60733"}]', '"lots":[]'))


# ── pure parsing ─────────────────────────────────────────────────────────────

def test_extract_apollo_state_and_absent_page_fails_loud():
    state = extract_apollo_state(LOT)
    assert "ROOT_QUERY" in state and "AuctionLot.57702" in state
    with pytest.raises(ValueError):
        extract_apollo_state("<html><body>Access denied</body></html>")
    with pytest.raises(ValueError):
        extract_apollo_state("<script>window.__APOLLO_STATE__ = {not json}; </script>")


def test_search_page_total_and_cards():
    total, cards = parse_search_page(SEARCH)
    assert total == 1 and [c["auction_lot_id"] for c in cards] == ["60733"]
    assert cards[0]["auction"]["title"] == "U.S. Marshals Service Art Auction"
    total, cards = parse_search_page(SEARCH_SEATING)
    assert total == 2 and len(cards) == 2


def test_auction_index_gives_two_letter_locations():
    idx = parse_auction_index(AUCTIONS)
    assert len(idx) == 20
    a = idx["31534"]
    assert (a["city"], a["state"], a["zip"], a["status"]) == ("Pflugerville", "TX", "78660", 200)
    assert all(v["state"] in ("TX", "") for v in idx.values())


def test_lot_page_gallery_location_and_text():
    lot = parse_lot_page(LOT)
    assert lot["auction_lot_id"] == "57702" and lot["auction_id"] == "31431"
    assert lot["_location"] == {"city": "Austin", "state": "TX", "zip": "78741"}
    assert len(lot["_gallery"]) == 2 and all("/large/" in u for u in lot["_gallery"])
    assert lot["winning_bidder"]["user_display"] == "f****r"


def test_derive_title_first_sentence_never_empty():
    assert derive_title("(500) MTS Seating Omega Stacker event chairs. Dimensions: Front to back: 22.5”", "11") \
        == "(500) MTS Seating Omega Stacker event chairs"
    assert derive_title("<p>ARTWORK: oil on canvas, no date. Signed.</p>", "4") == "ARTWORK: oil on canvas, no date"
    long = "word " * 80
    t = derive_title(long, "1")
    assert len(t) <= 140 and t.endswith("…")
    assert derive_title("", "11") == "Lot 11"
    assert derive_title(None, None) == "TXAuction lot"


def test_price_is_winning_bid_with_bids_else_starting_bid():
    lot = parse_lot_page(LOT)
    assert parse_price(lot) == 5645.0                       # 682 bids → winning bid
    assert parse_price({"auction_lot_id": "1", "bid_count": 0, "winning_bid_amount": None,
                        "starting_bid": 170}) == 170.0


@pytest.mark.parametrize("mutilate", [
    lambda c: c.update(starting_bid=None),                  # 0 bids, no opening price
    lambda c: c.update(starting_bid="abc"),
    lambda c: c.update(bid_count=3, winning_bid_amount=None),  # bids but no current price
])
def test_price_missing_or_garbled_fails_loud(mutilate):
    _, cards = parse_search_page(SEARCH)
    card = dict(cards[0])
    mutilate(card)
    with pytest.raises(ValueError):
        card_to_lot(card)


def test_missing_end_time_fails_loud():
    _, cards = parse_search_page(SEARCH)
    card = dict(cards[0], end_time=None)
    with pytest.raises(ValueError):
        card_to_lot(card)


def test_catalog_chair_lots_map_to_lots():
    lots = [card_to_lot(c) for c in parse_catalog_page(CATALOG)]
    check_lots(lots, site="txauction")
    assert len(lots) == 11
    by = {l.native_id: l for l in lots}
    big = by["31431/57702"]
    assert big.title == "(500) MTS Seating Omega Stacker event chairs"
    assert big.current_bid == 5645.0 and big.bid_count == 682 and big.opening_bid == 10.0
    assert big.status == "CLO" and big.is_sold is True
    assert (big.city, big.state, big.zip) == ("Austin", "TX", "78741")   # from the catalog's auction
    assert big.end_utc == datetime(2026, 8, 14, 16, 58, tzinfo=timezone.utc)
    assert (big.asset_id, big.account_id, big.auction_id) == synth_ids("txauction", "31431/57702", ordinal=10)
    assert big.account_id == -10
    assert by["31431/57700"].current_bid == 350.0           # sibling "(50) …" lot
    assert big.hero_image_url.startswith("https://d3j17a2r8lnfte.cloudfront.net/")
    assert len(json.dumps(big.raw).encode()) <= tx.RAW_MAX_BYTES


def test_search_card_live_lot_uses_auction_location():
    total, cards = parse_search_page(SEARCH)
    lot = card_to_lot(cards[0], parse_auction_index(AUCTIONS))
    check_lots([lot], site="txauction")
    assert lot.status == "STA" and lot.bid_count == 0 and lot.current_bid == 170.0
    assert (lot.city, lot.state) == ("Pflugerville", "TX")
    assert lot.seller == "Eastern District of Pennsylvania"
    assert lot.canonical_category == "other"


def test_status_tokens():
    assert tx._status({"auction_lot_status": 100, "is_past_end_time": False}) == "STA"
    assert tx._status({"auction_lot_status": 100, "is_past_end_time": True}) == "CLO"
    assert tx._status({"auction_lot_status": 200}) == "CLO"
    assert tx._status({"auction_lot_status": 200, "is_no_sale": True}) == "RNM"


def test_lot_page_snapshot_reads_final():
    s = lot_to_snapshot(parse_lot_page(LOT))
    assert (s.bid_count, s.current_bid, s.status) == (682, 5645.0, "CLO")
    assert (s.asset_id, s.account_id, s.auction_id) == synth_ids("txauction", "31431/57702", ordinal=10)


def test_lot_url():
    assert tx.lot_url("31431/57702") == "https://www.txauction.com/auctions/31431/lot/57702"
    with pytest.raises(ValueError):
        tx.lot_url("57702")
    assert tx.search_url("banquet chairs", 2) == "https://www.txauction.com/search?search=banquet+chairs&page=2"


# ── adapter (fake session) ───────────────────────────────────────────────────

def _adapter(pages, status=None):
    s = _Session(pages, status)
    return TXAuctionAdapter(terms=["chair"], delay_s=0, session=s), s


def test_discover_reads_locations_once_and_dedupes():
    a, s = _adapter({f"{tx.BASE_URL}/auctions": AUCTIONS, f"{tx.BASE_URL}/search?search=chair": SEARCH})
    a.terms = ["chair", "chair"]                          # same lot twice → yielded once
    lots = list(a.discover())
    check_lots(lots, site="txauction")
    assert [l.native_id for l in lots] == ["31534/60733"]
    assert sum(u.startswith(f"{tx.BASE_URL}/auctions") for u in s.urls) == 1
    assert lots[0].state == "TX"


def test_discover_stops_on_403():
    a, _ = _adapter({f"{tx.BASE_URL}/auctions": AUCTIONS}, {f"{tx.BASE_URL}/auctions": 403})
    with pytest.raises(RuntimeError, match="403"):
        list(a.discover())


def test_refetch_uses_remembered_native_ids(make_lot):
    a, s = _adapter({f"{tx.BASE_URL}/auctions/31431/lot/57702": LOT})
    ids = synth_ids("txauction", "31431/57702", ordinal=10)
    stored = make_lot(asset_id=ids[0], account_id=ids[1], auction_id=ids[2],
                      site="txauction", native_id="31431/57702")
    assert a.refetch([ids]) == {}                         # unknown id → nothing, no request
    a.remember([stored])
    got = a.refetch([ids])
    snap = got[lot_key(*ids)]
    assert snap.current_bid == 5645.0 and snap.status == "CLO"
    assert s.urls == [f"{tx.BASE_URL}/auctions/31431/lot/57702"]
    assert a.fetch_gallery(ids[0], ids[1])[0].startswith("https://d3j17a2r8lnfte.cloudfront.net/")
