from datetime import datetime, timedelta, timezone

from recorder.schedule import is_due, poll_interval

NOW = datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc)


# --- poll_interval: every cadence boundary ---------------------------------

def test_poll_interval_no_end_date_is_far():
    assert poll_interval(NOW, None) == timedelta(hours=6)


def test_poll_interval_far_out():
    assert poll_interval(NOW, NOW + timedelta(days=3)) == timedelta(hours=6)


def test_poll_interval_just_outside_24h_is_far():
    assert poll_interval(NOW, NOW + timedelta(hours=24, minutes=1)) == timedelta(hours=6)


def test_poll_interval_at_24h_edge_is_near():
    assert poll_interval(NOW, NOW + timedelta(hours=24)) == timedelta(hours=1)


def test_poll_interval_just_inside_24h_is_near():
    assert poll_interval(NOW, NOW + timedelta(hours=23, minutes=59)) == timedelta(hours=1)


def test_poll_interval_just_outside_1h_is_near():
    assert poll_interval(NOW, NOW + timedelta(hours=1, minutes=1)) == timedelta(hours=1)


def test_poll_interval_at_1h_edge_is_hot():
    assert poll_interval(NOW, NOW + timedelta(hours=1)) == timedelta(minutes=5)


def test_poll_interval_just_before_end_is_hot():
    assert poll_interval(NOW, NOW + timedelta(seconds=1)) == timedelta(minutes=5)


def test_poll_interval_exactly_at_end_is_confirming():
    assert poll_interval(NOW, NOW) == timedelta(0)


def test_poll_interval_just_after_end_is_confirming():
    assert poll_interval(NOW, NOW - timedelta(seconds=1)) == timedelta(0)


# --- is_due ------------------------------------------------------------

def test_is_due_false_when_recently_polled_far_out():
    last = NOW - timedelta(hours=5)
    assert is_due(NOW, last, NOW + timedelta(days=3)) is False


def test_is_due_true_once_far_interval_elapsed():
    last = NOW - timedelta(hours=7)
    assert is_due(NOW, last, NOW + timedelta(days=3)) is True


def test_is_due_false_inside_24h_window_before_interval_elapsed():
    end = NOW + timedelta(hours=10)
    last = NOW - timedelta(minutes=30)
    assert is_due(NOW, last, end) is False


def test_is_due_true_inside_final_hour_after_hot_interval_elapsed():
    end = NOW + timedelta(minutes=30)
    last = NOW - timedelta(minutes=6)
    assert is_due(NOW, last, end) is True


def test_is_due_true_for_confirming_poll_just_after_end():
    end = NOW - timedelta(minutes=5)
    last = NOW - timedelta(minutes=10)   # last observation was BEFORE the close
    assert is_due(NOW, last, end) is True


def test_is_due_false_when_last_observed_at_equals_now_after_post_end_observation():
    # anti-snipe: the confirming poll just ran and found the auction extended
    # with a fresh, later end_date — the lot must not be immediately due again.
    new_end = NOW + timedelta(days=2)
    last = NOW
    assert is_due(NOW, last, new_end) is False


def test_is_due_true_repeatedly_once_last_observed_at_is_past_end():
    # once last_observed_at >= end_date, the "confirming poll" special-case no
    # longer applies, but the base interval (0 once end has passed) still fires
    # every cycle for as long as the lot's latest snapshot claims 'active'.
    end = NOW - timedelta(minutes=5)
    last = NOW - timedelta(seconds=1)
    assert is_due(NOW, last, end) is True
