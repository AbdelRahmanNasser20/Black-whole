"""extract_asset_id must key BidSpotter lot URLs as ``bs:<lotGuid>`` —
otherwise every BidSpotter row is silently skipped by store_listings.
Run standalone:  python tests/test_bidspotter_cache_key.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from listings_db import extract_asset_id

BS_URL = ("https://www.bidspotter.com/en-us/auction-catalogues/stone/"
          "catalogue-id-stone-10117/lot-aaf53c9e-a9d1-4de6-a794-b477015c7b1f")


def test_bidspotter_url_maps_to_bs_prefixed_guid():
    assert extract_asset_id(BS_URL) == "bs:aaf53c9e-a9d1-4de6-a794-b477015c7b1f"


def test_existing_patterns_unchanged():
    assert extract_asset_id("https://www.govdeals.com/en/asset/305/10340") == "305/10340"
    assert extract_asset_id(
        "https://www.publicsurplus.com/sms/auction/view?auc=4020144") == "ps:4020144"


def test_garbage_still_uncacheable():
    assert extract_asset_id("") == ""
    assert extract_asset_id("https://example.com/lot-123") == ""
    # GUID shape on a non-bidspotter host must NOT match.
    assert extract_asset_id(
        "https://evil.example/lot-aaf53c9e-a9d1-4de6-a794-b477015c7b1f") == ""


if __name__ == "__main__":
    test_bidspotter_url_maps_to_bs_prefixed_guid()
    test_existing_patterns_unchanged()
    test_garbage_still_uncacheable()
    print("ok — bidspotter cache key")
