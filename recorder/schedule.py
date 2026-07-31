"""Adaptive polling cadence — pure functions, no I/O, no DB.

Catching the close is the whole game: poll far-out lots rarely (6h), tighten
to 1h inside the final 24h, 5 min inside the final hour, and fire one
"confirming" poll (interval 0 -> due immediately) the instant `end_date`
passes so we capture the closing state before the lot vanishes.

Anti-snipe: `end_date` is re-read from the source on every poll. If a poll
finds the lot still active with a *later* end_date, the lot simply keeps
polling on the new schedule — nothing here treats "past end_date" as
special once a fresh observation has been recorded for it.
"""
from __future__ import annotations

from datetime import datetime, timedelta

FAR_INTERVAL = timedelta(hours=6)      # end_date unknown, or > 24h away
NEAR_INTERVAL = timedelta(hours=1)     # <= 24h to end
HOT_INTERVAL = timedelta(minutes=5)    # <= 1h to end
CONFIRM_INTERVAL = timedelta(0)        # end_date has passed -> due now


def poll_interval(now: datetime, end_date: datetime | None) -> timedelta:
    """How often a tracked lot should be polled, given the current time."""
    if end_date is None:
        return FAR_INTERVAL
    remaining = end_date - now
    if remaining <= timedelta(0):
        return CONFIRM_INTERVAL
    if remaining <= timedelta(hours=1):
        return HOT_INTERVAL
    if remaining <= timedelta(hours=24):
        return NEAR_INTERVAL
    return FAR_INTERVAL


def is_due(now: datetime, last_observed_at: datetime, end_date: datetime | None) -> bool:
    """Whether a tracked lot is due for another poll right now."""
    if end_date is not None and end_date <= now and last_observed_at < end_date:
        # the confirming poll: we haven't observed anything since the close yet.
        return True
    return (now - last_observed_at) >= poll_interval(now, end_date)
