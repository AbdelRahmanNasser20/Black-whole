from datetime import datetime, timedelta
from deals.models import Snapshot, Lane, Outcome

HOT_WINDOW = timedelta(minutes=30)
WARM_WINDOW = timedelta(hours=24)
HOT_POLL_SECONDS = 5.0

def schedule_lane(end_utc: datetime, now: datetime) -> Lane:
    remaining = end_utc - now
    if remaining <= HOT_WINDOW:          # includes past-clock (extension may be live)
        return Lane.HOT
    if remaining <= WARM_WINDOW:
        return Lane.WARM
    return Lane.COLD

def next_poll_delay(end_utc: datetime, now: datetime, lane: Lane) -> float:
    if lane == Lane.COLD:
        return 6 * 3600.0
    if lane == Lane.WARM:
        return 3600.0
    # HOT: poll every few seconds, but never sleep past the (possibly moving) close
    secs_to_close = (end_utc - now).total_seconds()
    if secs_to_close <= 0:
        return HOT_POLL_SECONDS
    return min(HOT_POLL_SECONDS, secs_to_close)

def is_snapshot_change(prev: Snapshot | None, new: Snapshot) -> bool:
    if prev is None:
        return True
    return (prev.bid_count != new.bid_count or prev.current_bid != new.current_bid
            or prev.end_utc != new.end_utc or prev.status != new.status)

def detect_outcome(last: Snapshot, dropped: bool, low_bid_threshold: int = 1) -> tuple[Outcome, bool]:
    if not dropped:
        return (Outcome.UNKNOWN, False)          # still live; not final
    if last.bid_count == 0:
        return (Outcome.NO_BID, True)
    if last.bid_count <= low_bid_threshold:
        return (Outcome.LOW_BID, True)
    return (Outcome.SOLD, True)
