"""Parser guard for the BidSpotter plain-HTTP scrape path.

Fixture is a real search page saved 2026-07-21, trimmed to 3 representative
cards (see the fixture's header comment). Pure / no network.
Run standalone:  python tests/test_bidspotter_parse.py
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bidspotter_automation import (
    _apply_bid_info,
    _format_time_left,
    _parse_bid_info,
    _parse_for_item,
    _parse_search_cards,
    _parse_total_pages,
)
import bidspotter_automation as bs_mod

_FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")

with open(os.path.join(_FIXTURES, "bidspotter_search_chairs.html")) as f:
    SEARCH_HTML = f.read()

with open(os.path.join(_FIXTURES, "bidspotter_bid_info.json")) as f:
    BID_INFO = json.load(f)

# Contract with listings_db / top_chairs — same keys the PS parser guarantees.
CARD_KEYS = {
    "title", "link", "quantity", "quantity_source", "quantity_confidence",
    "location", "price", "lot_number", "end_date", "time_left", "image_url",
}

GHOST = "aaf53c9e-a9d1-4de6-a794-b477015c7b1f"


def test_card_contract_and_structured_quantity():
    cards = _parse_search_cards(SEARCH_HTML)
    assert len(cards) == 3
    by_guid = {c["lot_guid"]: c for c in cards}

    ghost = by_guid[GHOST]
    assert CARD_KEYS - set(ghost) == set()
    assert ghost["title"] == "Ghost chairs"
    assert ghost["link"] == (
        "https://www.bidspotter.com/en-us/auction-catalogues/stone/"
        "catalogue-id-stone-10117/lot-" + GHOST)
    assert ghost["quantity"] == 43
    assert ghost["quantity_source"] == "structured"
    assert ghost["quantity_confidence"] == "high"
    assert ghost["location"] == "Belmont, North Carolina"
    assert ghost["lot_number"] == "stone-10117#93"
    assert ghost["end_date"] == "2026-07-22T15:32:00Z"   # forItem "Lot End Time UTC"
    assert ghost["image_url"].startswith("https://cdn.globalauctionplatform.com/")
    assert "?" not in ghost["image_url"]                  # resize query stripped
    assert ghost["auction_type"] == "timed"

    tuscan = by_guid["ea890a07-d99f-4a24-94e9-b477015c7b1f"]
    assert tuscan["quantity"] == 127
    assert tuscan["quantity_source"] == "structured"


def test_missing_structured_quantity_seeds_regex_title():
    cards = _parse_search_cards(SEARCH_HTML)
    eps = next(c for c in cards if c["lot_guid"].startswith("00a1c5e1"))
    # No li.quantity on this card (forItem says "1" — the ambiguous default),
    # so it must take the untrusted regex-title seed and flow to the LLM pass.
    assert eps["quantity_source"] == "regex_title"
    assert eps["location"] == "Grand Rapids, Michigan"
    assert eps["description"]        # snippet present for the LLM
    assert eps["end_date"] == "2026-08-25T16:40:00Z"


def test_for_item_json():
    fi = _parse_for_item(SEARCH_HTML)
    assert set(fi) == {
        "00a1c5e1-2b6b-4029-8bb3-b45200f057c5",
        GHOST,
        "ea890a07-d99f-4a24-94e9-b477015c7b1f",
    }
    assert fi[GHOST]["Lot Quantity"] == "43"
    assert fi[GHOST]["Lot End Time UTC"].endswith("Z")
    assert fi[GHOST]["Auction House Name"]


def test_pagination_and_garbage():
    assert _parse_total_pages(SEARCH_HTML) == 13
    assert _parse_total_pages("<html></html>") == 1
    assert _parse_search_cards("<html></html>") == []
    assert _parse_search_cards("") == []
    assert _parse_for_item("<html></html>") == {}


def test_bid_info_parse_and_price():
    info = _parse_bid_info(BID_INFO)          # live-captured, Model-wrapped
    assert GHOST in info
    m = info[GHOST]
    assert m["LeadingBid"] == 7.0 and m["TotalBids"] == 3

    cards = _parse_search_cards(SEARCH_HTML)
    _apply_bid_info(cards, info)
    ghost = next(c for c in cards if c["lot_guid"] == GHOST)
    assert ghost["price"] == "USD 7.00"       # LeadingBid, GovDeals-style format
    assert ghost["bid_count"] == 3
    assert ghost["time_left"]                 # derived from SecondsRemaining


def test_bid_info_zero_bids_uses_start_price_and_unwrapped_rows():
    # Synthetic zero-bid row + a bare (non-Model-wrapped) row — the site's own
    # parse handler accepts both shapes, so must we.
    data = [
        {"Model": {"LotId": "00000000-0000-4000-8000-00000000000a",
                   "TotalBids": 0, "LeadingBid": 0.0, "StartPrice": 25.0,
                   "Currency": "USD", "SecondsRemaining": 90}},
        {"LotId": "00000000-0000-4000-8000-00000000000b",
         "TotalBids": 2, "LeadingBid": 40.0, "StartPrice": 10.0,
         "Currency": "CAD", "SecondsRemaining": 4000},
    ]
    info = _parse_bid_info(data)
    card_a = {"lot_guid": "00000000-0000-4000-8000-00000000000a",
              "currency": "USD", "price": "", "time_left": ""}
    card_b = {"lot_guid": "00000000-0000-4000-8000-00000000000b",
              "currency": "CAD", "price": "", "time_left": ""}
    card_c = {"lot_guid": "ffffffff-ffff-4fff-8fff-ffffffffffff",
              "currency": "USD", "price": "", "time_left": ""}   # not in response
    _apply_bid_info([card_a, card_b, card_c], info)
    assert card_a["price"] == "USD 25.00"     # zero bids → opening price
    assert card_b["price"] == "CAD 40.00"
    assert card_c["price"] == ""              # missing lot → untouched


def test_format_time_left():
    assert _format_time_left(2998788) == "34d 16h"
    assert _format_time_left(4000) == "1h 6m"
    assert _format_time_left(0) == ""
    assert _format_time_left(None) == ""
    assert _format_time_left("junk") == ""


class _FakeResp:
    def __init__(self, status_code, headers=None):
        self.status_code = status_code
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")


class _FakeSession:
    """Yields queued responses; records how many requests were made."""
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    def request(self, method, url, **kwargs):
        self.calls += 1
        return self._responses.pop(0)


def test_waf_202_retries_then_succeeds(monkeypatch=None):
    old = bs_mod.WAF_BACKOFF_SEC
    bs_mod.WAF_BACKOFF_SEC = 0            # no sleeping in tests
    try:
        ok = _FakeResp(200)
        challenged = _FakeResp(202, {"x-amzn-waf-action": "challenge"})
        sess = _FakeSession([challenged, challenged, ok])
        resp = bs_mod._fetch(sess, "get", "https://x/", retries=3)
        assert resp is ok
        assert sess.calls == 3
    finally:
        bs_mod.WAF_BACKOFF_SEC = old


def test_waf_202_exhausted_raises():
    old = bs_mod.WAF_BACKOFF_SEC
    bs_mod.WAF_BACKOFF_SEC = 0
    try:
        challenged = _FakeResp(202, {"x-amzn-waf-action": "challenge"})
        sess = _FakeSession([challenged] * 3)
        try:
            bs_mod._fetch(sess, "get", "https://x/", retries=2)
            raise AssertionError("expected RuntimeError")
        except RuntimeError as e:
            assert "WAF" in str(e)
        assert sess.calls == 3                    # retries+1 attempts
    finally:
        bs_mod.WAF_BACKOFF_SEC = old


def test_plain_202_without_waf_header_is_not_retried():
    # A 202 without x-amzn-waf-action is NOT a challenge — pass it through.
    sess = _FakeSession([_FakeResp(202)])
    resp = bs_mod._fetch(sess, "get", "https://x/", retries=5)
    assert resp.status_code == 202
    assert sess.calls == 1


def test_search_url_and_dedup():
    assert bs_mod._search_url("chairs", 2) == (
        "https://www.bidspotter.com/en-us/search-results?searchTerm=chairs&page=2")
    a = {"link": "https://x/1", "title": "a"}
    b = {"link": "https://x/1", "title": "dupe of a"}
    c = {"link": "https://x/2", "title": "c"}
    assert bs_mod._dedup([a, b, c]) == [a, c]


_ALL_TESTS = (
    test_card_contract_and_structured_quantity,
    test_missing_structured_quantity_seeds_regex_title,
    test_for_item_json,
    test_pagination_and_garbage,
    test_bid_info_parse_and_price,
    test_bid_info_zero_bids_uses_start_price_and_unwrapped_rows,
    test_format_time_left,
    test_waf_202_retries_then_succeeds,
    test_waf_202_exhausted_raises,
    test_plain_202_without_waf_header_is_not_retried,
    test_search_url_and_dedup,
)


def main() -> int:
    failed = 0
    for t in _ALL_TESTS:
        try:
            t()
            print(f"  [ok ] {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  [FAIL] {t.__name__}: {e}")
    if failed:
        print(f"{failed}/{len(_ALL_TESTS)} test(s) failed")
        return 1
    print("ok — bidspotter parse")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
