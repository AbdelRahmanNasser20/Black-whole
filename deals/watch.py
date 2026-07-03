import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from deals.models import lot_key, Outcome
from deals.store import (due_for_poll, append_snapshot, record_outcome,
                         set_poll_schedule, latest_snapshot)
from deals.watcher_logic import schedule_lane, next_poll_delay, detect_outcome

@dataclass
class PollReport:
    polled: int = 0; snapshotted: int = 0; finalized: int = 0

def poll_once(adapter, now: datetime) -> PollReport:
    rep = PollReport()
    due = due_for_poll(now)
    if not due:
        return rep
    keys = [(l.asset_id, l.account_id, l.auction_id) for l in due]
    present = adapter.refetch(keys)
    for lot in due:
        key = (lot.asset_id, lot.account_id, lot.auction_id)
        rep.polled += 1
        snap = present.get(lot_key(*key))
        if snap is None:
            # dropped from search => closed. Finalize from last snapshot (or the lot itself).
            last = latest_snapshot(key)
            fb = last.current_bid if last else lot.current_bid
            fbc = last.bid_count if last else lot.bid_count
            outcome, complete = detect_outcome(last or _as_snapshot(lot, now), dropped=True)
            record_outcome(key, outcome.value, fb, fbc, now, complete)
            rep.finalized += 1
            continue
        append_snapshot(snap)
        rep.snapshotted += 1
        lane = schedule_lane(snap.end_utc, now)          # re-read end_utc absorbs extensions
        delay = next_poll_delay(snap.end_utc, now, lane)
        set_poll_schedule(key, now + timedelta(seconds=delay), lane.value)
    return rep

def _as_snapshot(lot, now):
    from deals.models import Snapshot
    return Snapshot(lot.asset_id, lot.account_id, lot.auction_id, now,
                    lot.bid_count, lot.current_bid, lot.end_utc, lot.status)

def run_watcher(adapter, sleep_seconds: float = 5.0) -> None:      # pragma: no cover
    while True:
        poll_once(adapter, datetime.now().astimezone())
        time.sleep(sleep_seconds)
