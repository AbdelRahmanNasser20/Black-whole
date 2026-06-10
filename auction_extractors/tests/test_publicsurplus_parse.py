"""Parser guard for the Public Surplus plain-HTTP scrape path.

Public Surplus has no JSON API (legacy JSP site), but the search and detail
pages are fully server-rendered and served to plain ``requests`` with no bot
block. ``_parse_search_cards`` turns one search-results page of HTML into the
card dicts the pipeline consumes — the same shape ``_scrape_one_page``
produces via Playwright, but with ``image_url`` and ``end_date`` pre-filled
(both are embedded in the search HTML; the browser path never captured them).
``_parse_detail_page`` extracts description + image from one auction page.

Fixtures are real pages saved from publicsurplus.com on 2026-06-10.

Pure / no network. Run standalone:  python tests/test_publicsurplus_parse.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from public_surplus_automation import _parse_search_cards, _parse_detail_page

_FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")

with open(os.path.join(_FIXTURES, "publicsurplus_search_chairs.html")) as f:
    SEARCH_HTML = f.read()
with open(os.path.join(_FIXTURES, "publicsurplus_detail_4020144.html")) as f:
    DETAIL_HTML = f.read()

# Keys every card must carry — the contract with listings_db / top_chairs.
CARD_KEYS = {
    "title", "link", "quantity", "quantity_source", "quantity_confidence",
    "location", "price", "lot_number", "end_date", "time_left", "image_url",
}


def _check(name: str, actual, expected) -> bool:
    ok = actual == expected
    mark = "ok " if ok else "FAIL"
    print(f"  [{mark}] {name}: {actual!r}" + ("" if ok else f" != {expected!r}"))
    return ok


def main() -> int:
    checks = []
    cards = _parse_search_cards(SEARCH_HTML)

    checks.append(_check("card_count", len(cards), 25))

    first = cards[0]
    checks.append(_check("keys", CARD_KEYS - set(first), set()))
    checks.append(_check("title", first["title"], "#4020144 - Lot of 70 ea. Chairs"))
    checks.append(_check(
        "link", first["link"],
        "https://www.publicsurplus.com/sms/auction/view?auc=4020144"))
    checks.append(_check("lot_number", first["lot_number"], "AUC#4020144"))
    checks.append(_check("price", first["price"], "$70.00"))
    checks.append(_check("location", first["location"], "CA"))
    checks.append(_check("time_left", first["time_left"], "1 hour 37 mins"))
    checks.append(_check("quantity", first["quantity"], 70))
    checks.append(_check("quantity_source", first["quantity_source"], "regex_title"))
    checks.append(_check("quantity_confidence", first["quantity_confidence"], "medium"))
    # Pre-filled from the card markup — browser path never had these.
    checks.append(_check(
        "image_url", first["image_url"],
        "https://d37qv0n5b4mbzm.cloudfront.net/sms/docviewer/cdnmainaucdoc/thumb-b/4020144/70098099"))
    checks.append(_check("end_date", first["end_date"], "2026-06-10T18:00:00Z"))

    # Every card on the page parses with a usable title + link.
    checks.append(_check(
        "all_titles_nonempty", all(c["title"] for c in cards), True))
    checks.append(_check(
        "all_links_absolute",
        all(c["link"].startswith("https://www.publicsurplus.com/") for c in cards),
        True))

    # Garbage in → empty list out, no crash (lets the caller fall back).
    checks.append(_check("garbage_html", _parse_search_cards("<html></html>"), []))
    checks.append(_check("empty_html", _parse_search_cards(""), []))

    desc, img = _parse_detail_page(DETAIL_HTML)
    checks.append(_check("detail_desc_has_lot", "Lot of 70 ea. Chairs" in desc, True))
    checks.append(_check("detail_desc_capped", len(desc) <= 4000, True))
    checks.append(_check(
        "detail_image", img,
        "https://d37qv0n5b4mbzm.cloudfront.net/sms/docviewer/cdnaucdoc/thumb-b/4020144/70098099?x=2245"))
    d2, i2 = _parse_detail_page("<html></html>")
    checks.append(_check("detail_garbage", (d2, i2), ("", "")))

    passed = all(checks)
    print(f"\n{'ALL PASSED' if passed else 'FAILURES PRESENT'} "
          f"({sum(checks)}/{len(checks)})")
    return 0 if passed else 1


def test_publicsurplus_parse():
    """pytest entry point."""
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
