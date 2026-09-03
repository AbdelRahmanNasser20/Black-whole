"""Public Surplus adapter — fixture-only. No network, no browser: both
transports are monkeypatched to raise, so any accidental fetch fails the test."""
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

import deals.adapters._browser as _browser
import deals.adapters.publicsurplus as ps
from deals.adapters.publicsurplus import (PublicSurplusAdapter, card_to_lot,
                                          parse_detail_page, parse_search_cards)
from tests.deals.adapter_contract import check_lots

FIXTURES = Path("tests/deals/fixtures/publicsurplus")
SEARCH_HTML = (FIXTURES / "search_page.html").read_text()
DETAIL_HTML = (FIXTURES / "detail_page.html").read_text()


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    def boom(*a, **kw):
        raise AssertionError("fixture test touched the network")
    monkeypatch.setattr(ps.requests, "get", boom)
    monkeypatch.setattr(_browser, "fetch_rendered", boom)


def _lots():
    return [card_to_lot(c) for c in parse_search_cards(SEARCH_HTML)]


def test_contract_on_fixture():
    lots = _lots()
    assert len(lots) >= 10
    check_lots(lots, site="publicsurplus")


def test_price_stripped_fails_loud():
    cards = parse_search_cards(SEARCH_HTML)
    auc = cards[0]["auc_id"]
    mutilated = re.sub(r'<b id="val_%ssearchGrid">\s*[^<]*</b>' % auc, "", SEARCH_HTML, count=1)
    bad = [c for c in parse_search_cards(mutilated) if c["auc_id"] == auc][0]
    assert bad["price"] == ""
    with pytest.raises(ValueError, match="price missing"):
        card_to_lot(bad)


def test_garbled_price_fails_loud():
    card = dict(parse_search_cards(SEARCH_HTML)[0], price="$abc")
    with pytest.raises(ValueError, match="garbled price"):
        card_to_lot(card)


def test_missing_end_fails_loud():
    card = dict(parse_search_cards(SEARCH_HTML)[0], end_epoch_ms=None)
    with pytest.raises(ValueError, match="no end epoch"):
        card_to_lot(card)


def test_synth_ids_use_ordinal_2():
    for lot in _lots():
        assert lot.account_id == -2 and lot.auction_id == 0 and lot.asset_id > 0


def test_native_id_unique_and_numeric():
    ids = [l.native_id for l in _lots()]
    assert len(ids) == len(set(ids)) and all(i.isdigit() for i in ids)


def test_end_utc_matches_fixture_epoch():
    lot = {l.native_id: l for l in _lots()}["4079872"]
    assert lot.end_utc.tzinfo is not None
    assert lot.end_utc == datetime.fromtimestamp(1789678800000 / 1000, tz=timezone.utc)
    assert lot.end_utc == datetime(2026, 9, 17, 21, 0, tzinfo=timezone.utc)


def test_card_fields_and_title_prefix():
    lot = {l.native_id: l for l in _lots()}["4079872"]
    assert lot.title == "40 Black Banquet Chairs - Lot C"
    assert lot.current_bid == 325.0 and lot.state == "MI"
    assert "/thumb-l/" in lot.hero_image_url
    assert lot.raw["title"].startswith("#4079872")
    assert lot.canonical_category == "other"    # deals' classify pass takes over


def test_detail_page_parses_bid_state():
    d = parse_detail_page(DETAIL_HTML)
    assert d["auc_id"] == "4079872" and d["price"] == "$325.00"
    assert d["bid_count"] == 0 and d["end_epoch_ms"] == 1789678800000
    assert (d["seller"], d["city"], d["state"], d["zip"]) == ("City of Taylor", "Taylor", "MI", "48180")
    assert "Qty: 40" in d["description"]
    assert d["image_url"].startswith("https://") and "/thumb-l/" in d["image_url"]


def test_detail_bid_count_number():
    html = DETAIL_HTML.replace('<span class="text-danger">No Bids</span>', "<span>7</span>")
    assert parse_detail_page(html)["bid_count"] == 7


def test_discover_dedups_across_terms(monkeypatch):
    a = PublicSurplusAdapter(terms=["banquet chairs", "chairs"], delay_s=0)
    calls = []
    monkeypatch.setattr(a, "_search_html", lambda term, page: (calls.append((term, page)), SEARCH_HTML)[1])
    lots = list(a.discover(max_pages=3))
    assert len(lots) == 13                      # 13 cards × 2 terms → 13 unique
    assert calls == [("banquet chairs", 0), ("chairs", 0)]   # <25 cards → next term
    assert a._native_by_key                      # key → auc map primed for refetch


def test_refetch_uses_detail_page(monkeypatch):
    a = PublicSurplusAdapter(terms=["banquet chairs"], delay_s=0)
    monkeypatch.setattr(a, "_search_html", lambda term, page: SEARCH_HTML)
    monkeypatch.setattr(a, "_detail_html", lambda auc: DETAIL_HTML)
    lot = {l.native_id: l for l in a.discover()}["4079872"]
    key = (lot.asset_id, lot.account_id, lot.auction_id)
    snaps = a.refetch([key])
    s = snaps["%d/%d/%d" % key]
    assert s.current_bid == 325.0 and s.bid_count == 0 and s.end_utc == lot.end_utc


def test_gated_search_without_browser_fails_loud(monkeypatch):
    a = PublicSurplusAdapter(terms=["chairs"], delay_s=0)
    monkeypatch.setattr(a, "_get", lambda url: "<html>noAuctionsFound</html>")
    monkeypatch.setattr(_browser, "available", lambda: False)
    with pytest.raises(RuntimeError, match="no browser"):
        list(a.discover())


def test_browser_helper_flags_challenge():
    assert _browser._CHALLENGE_RE.search("<h1>Access Denied</h1>")
    assert isinstance(_browser.available(), bool)
