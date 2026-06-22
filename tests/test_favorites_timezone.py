"""Regression tests for auction end-date timezone handling.

Bug: GovDeals displays every auction close in US Eastern site-wide (verified —
CA/CO/TX lots all stamp "EDT"), and its maestro API returns a *naive* ISO
string with no zone. The parser used to label naive timestamps UTC, shifting
every close 4–5h early and firing the countdown alerts ahead of time.
"""
from datetime import datetime, timezone

from automation import favorites


def _utc(y, mo, d, h, mi, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc)


def test_naive_govdeals_end_date_is_eastern_not_utc():
    # maestro API: naive 7:50 PM on 2026-06-15. June → EDT (-4), so 23:50 UTC.
    dt = favorites._parse_end_date("2026-06-15T19:50:00")
    assert dt == _utc(2026, 6, 15, 23, 50)


def test_naive_winter_end_date_uses_est_offset():
    # January → EST (-5), so 19:50 ET == 00:50 UTC next day.
    dt = favorites._parse_end_date("2026-01-15T19:50:00")
    assert dt == _utc(2026, 1, 16, 0, 50)


def test_explicit_eastern_abbreviation_resolves_to_eastern():
    # GovDeals browser/DOM path emits "... PM EDT"; dateutil must resolve it.
    dt = favorites._parse_end_date("April 20, 2026 07:00 PM EDT")
    assert dt == _utc(2026, 4, 20, 23, 0)


def test_public_surplus_explicit_utc_is_preserved():
    # Public Surplus path already emits an explicit-UTC "...Z" string.
    dt = favorites._parse_end_date("2026-06-15T19:50:00Z")
    assert dt == _utc(2026, 6, 15, 19, 50)


def test_idaho_lot_real_world_case():
    # The reported Boise, ID lot: naive 12:20:26 is ET (GovDeals is ET site-wide),
    # NOT UTC. June → EDT (-4) → 16:20:26 UTC, four hours later than the old bug.
    dt = favorites._parse_end_date("2026-06-22T12:20:26")
    assert dt == _utc(2026, 6, 22, 16, 20, 26)
