"""One parser for auction end dates, shared by the Auctions tab and favorites.

Bug (2026-10-05): the Orlando lots 28859/28860-2863 close at 11:02 AM Eastern
(15:02 UTC). GovDeals' maestro API sends that as the NAIVE string
"2026-10-05T11:02:00". The favorites path already read naive times as Eastern;
``top_chairs._is_active`` still read them as UTC, so the Auctions tab called
both lots ended at 11:02 UTC and hid them for the last 4 hours of the auction
while they sat starred in Favorites.

Run standalone (no pytest needed):
    python auction_extractors/tests/test_end_dates.py
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from end_dates import parse_end_date
from top_chairs import _is_active


def _utc(*a) -> datetime:
    return datetime(*a, tzinfo=timezone.utc)


ORLANDO = {"end_date": "2026-10-05T11:02:00", "last_seen_at": "2026-10-05T08:23:07+00:00"}


def test_naive_govdeals_time_is_eastern() -> None:
    assert parse_end_date("2026-10-05T11:02:00") == _utc(2026, 10, 5, 15, 2)
    assert parse_end_date("2026-01-15T19:50:00") == _utc(2026, 1, 16, 0, 50)  # EST


def test_explicit_zones_are_kept() -> None:
    assert parse_end_date("2026-06-24T00:30:00Z") == _utc(2026, 6, 24, 0, 30)
    assert parse_end_date("April 20, 2026 01:00 PM EDT") == _utc(2026, 4, 20, 17, 0)


def test_empty_and_garbage_are_none() -> None:
    for raw in (None, "", "   ", "soon"):
        assert parse_end_date(raw) is None, raw


def test_orlando_lot_is_active_until_its_real_close() -> None:
    # 13:30 UTC = 9:30 AM ET — 90 minutes left. The UTC reading said "ended".
    assert _is_active(ORLANDO, _utc(2026, 10, 5, 13, 30), max_stale_days=2)
    assert not _is_active(ORLANDO, _utc(2026, 10, 5, 15, 3), max_stale_days=2)


def test_favorites_and_auctions_share_the_parser() -> None:
    """Two copies of this rule drifted once. Keep it one function."""
    import top_chairs
    assert top_chairs.parse_end_date is parse_end_date or \
        top_chairs.parse_end_date.__module__.endswith("end_dates")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ok  {name}")
    print("all end-date tests passed")
