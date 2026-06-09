"""Mapper guard for the GovDeals JSON-API scrape path.

`_asset_to_card` turns one raw `assetSearchResults` item (from the maestro
JSON API) into the card dict the rest of the pipeline consumes — the same
shape `_scrape_one_page` produces from the DOM, but with description / image /
pickup-zip pre-filled so no per-lot Playwright page load is needed.

Pure / no network. Run standalone:  python tests/test_asset_to_card.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from govdeals_chairs_extraction import _asset_to_card

# A real assetSearchResults item, trimmed to the fields the mapper reads.
SAMPLE = {
    "accountId": 195,
    "assetId": 1405,
    "assetShortDescription": "Blue Conference/Banquet Chairs (370)",
    "assetLongDescription": "Lot of 370 stackable banquet chairs, good condition.",
    "currentBid": 12.0,
    "assetBidPrice": 10.0,
    "currencyCode": "USD",
    "locationCity": "Jefferson",
    "locationState": "GA",
    "locationZip": "30549",
    "assetAuctionEndDate": "2026-06-15T19:50:00",
    "lotNumber": "1405-195",
    "photo": "195_1405_abc123.jpg?cb=260603154103",
    "timeRemaining": "3d 4h",
}


def _check(name: str, got, expected):
    ok = got == expected
    print(f"  [{'ok' if ok else 'FAIL'}] {name}: {got!r}")
    return ok


def main() -> int:
    card = _asset_to_card(SAMPLE)
    checks = [
        _check("link", card["link"], "https://www.govdeals.com/en/asset/195/1405"),
        _check("location", card["location"], "Jefferson, GA"),
        _check("price", card["price"], "USD 12.0"),          # currentBid wins over assetBidPrice
        _check("description", card["description"],
               "Lot of 370 stackable banquet chairs, good condition."),
        _check("image_url", card["image_url"],
               "https://webassets.lqdt1.com/assets/photos/195/195_1405_abc123.jpg?cb=260603154103"),
        _check("pickup_zip", card["pickup_zip"], "30549"),
        _check("lot_number", card["lot_number"], "1405-195"),
        _check("end_date", card["end_date"], "2026-06-15T19:50:00"),
        _check("time_left", card["time_left"], "3d 4h"),
        _check("quantity_source", card["quantity_source"], "regex_title"),
        # Every key the browser path / cache expects must be present.
        _check(
            "has_all_keys",
            set(card) >= {
                "title", "link", "quantity", "quantity_source", "quantity_confidence",
                "location", "price", "lot_number", "end_date", "time_left",
                "description", "image_url", "pickup_zip", "contact_email", "contact_phone",
            },
            True,
        ),
    ]

    # Graceful handling of a sparse item (missing photo / ids / bid).
    sparse = _asset_to_card({"assetShortDescription": "Chairs"})
    checks.append(_check("sparse_link_empty", sparse["link"], ""))
    checks.append(_check("sparse_image_empty", sparse["image_url"], ""))
    checks.append(_check("sparse_price_empty", sparse["price"], ""))

    passed = all(checks)
    print(f"\n{'ALL PASSED' if passed else 'FAILURES PRESENT'} "
          f"({sum(checks)}/{len(checks)})")
    return 0 if passed else 1


def test_asset_to_card():
    """pytest entry point."""
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
