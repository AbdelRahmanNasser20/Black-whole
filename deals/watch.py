import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from deals.models import lot_key, Outcome
from deals.store import (due_for_poll, append_snapshot, record_outcome,
                         set_poll_schedule, latest_snapshot, update_live_state)
from deals.watcher_logic import schedule_lane, next_poll_delay, detect_outcome, HOT_WINDOW

@dataclass
class PollReport:
    polled: int = 0; snapshotted: int = 0; finalized: int = 0; requeued: int = 0

def poll_once(adapter, now: datetime, extra_where: tuple[str, list] | None = None) -> PollReport:
    rep = PollReport()
    # keep the bare call when no profile is given (existing callers + test fakes)
    due = due_for_poll(now, extra_where) if extra_where else due_for_poll(now)
    if not due:
        return rep
    keys = [(l.asset_id, l.account_id, l.auction_id) for l in due]
    present = adapter.refetch(keys)
    for lot in due:
        key = (lot.asset_id, lot.account_id, lot.auction_id)
        rep.polled += 1
        snap = present.get(lot_key(*key))
        if snap is None:
            # Absent from the close-sorted firehose. Only CLOSED if the lot is
            # actually at/near its close; a COLD lot deep in the firehose can be
            # missed by the page cap and is NOT dropped — reschedule it instead.
            if now >= lot.end_utc - HOT_WINDOW:
                last = latest_snapshot(key)
                fb = last.current_bid if last else lot.current_bid
                fbc = last.bid_count if last else lot.bid_count
                outcome, complete = detect_outcome(last or _as_snapshot(lot, now), dropped=True)
                record_outcome(key, outcome.value, fb, fbc, now, complete)
                rep.finalized += 1
            else:
                lane = schedule_lane(lot.end_utc, now)
                delay = next_poll_delay(lot.end_utc, now, lane)
                set_poll_schedule(key, now + timedelta(seconds=delay), lane.value)
                rep.requeued += 1
            continue
        append_snapshot(snap)
        rep.snapshotted += 1
        lane = schedule_lane(snap.end_utc, now)          # re-read end_utc absorbs extensions
        delay = next_poll_delay(snap.end_utc, now, lane)
        update_live_state(key, snap, now + timedelta(seconds=delay), lane.value)
    return rep

def _as_snapshot(lot, now):
    from deals.models import Snapshot
    return Snapshot(lot.asset_id, lot.account_id, lot.auction_id, now,
                    lot.bid_count, lot.current_bid, lot.end_utc, lot.status)

def run_watcher(adapter, sleep_seconds: float = 5.0) -> None:      # pragma: no cover
    while True:
        poll_once(adapter, datetime.now().astimezone())
        time.sleep(sleep_seconds)
