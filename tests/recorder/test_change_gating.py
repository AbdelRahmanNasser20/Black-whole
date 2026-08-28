"""Change-gating for the recorder — recorder/store.py::is_changed.

`discover()` re-reports every active lot on every sweep and has no memory, so
without gating the table fills with rows that say nothing new: 31,383 of
47,115 rows on 2026-08-28 were strictly interior to a run of identical state.

`is_changed` is pure, so these tests need no database.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from recorder.models import Observation
from recorder.store import is_changed

END = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


def _obs(**kw):
    base = dict(source="govdeals", source_lot_id="1/2/3", status="active",
                raw={}, current_bid=Decimal("100.00"), bid_count=2, end_date=END)
    base.update(kw)
    return Observation(**base)


def _prev(**kw):
    base = dict(status="active", current_bid=Decimal("100.00"), bid_count=2,
                end_date=END)
    base.update(kw)
    return base


def test_first_sighting_is_always_kept():
    assert is_changed(_obs(), None) is True


def test_identical_state_is_dropped():
    assert is_changed(_obs(), _prev()) is False


def test_bid_change_is_kept():
    assert is_changed(_obs(current_bid=Decimal("125.00")), _prev()) is True


def test_bid_count_change_is_kept():
    assert is_changed(_obs(bid_count=3), _prev()) is True


def test_status_change_is_kept():
    assert is_changed(_obs(status="closed"), _prev()) is True


def test_anti_snipe_extension_is_kept():
    """end_date moving is the whole point of watching a closing lot."""
    assert is_changed(_obs(end_date=END + timedelta(minutes=5)), _prev()) is True


def test_none_to_value_counts_as_change():
    assert is_changed(_obs(current_bid=Decimal("100.00")),
                      _prev(current_bid=None)) is True
    assert is_changed(_obs(current_bid=None), _prev()) is True


def test_post_close_evidence_is_kept_even_when_identical():
    """coverage() decides a close was CAUGHT by finding a snapshot past
    end_date. Gating that away would make the recorder misreport a caught
    close as a missed one — the one thing it exists to get right."""
    o = _obs(observed_at=END + timedelta(minutes=1))
    assert is_changed(o, _prev()) is True


def test_pre_close_identical_observation_is_still_dropped():
    o = _obs(observed_at=END - timedelta(hours=3))
    assert is_changed(o, _prev()) is False


def test_no_observed_at_cannot_trigger_the_post_close_rule():
    """observed_at=None means the DB stamps now(); there is nothing to compare.
    Safe, because a real close also flips `status`, which rule 2 catches."""
    assert is_changed(_obs(observed_at=None), _prev()) is False


def test_missing_end_date_everywhere_does_not_crash():
    o = _obs(end_date=None, observed_at=END)
    assert is_changed(o, _prev(end_date=None)) is False
