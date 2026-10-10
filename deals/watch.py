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

# A foreign site's refetch reads the lot page itself, which keeps serving a
# closed lot (TXAuction) — so "absent from the firehose" never happens there.
# These statuses on a fresh snapshot mean the auction is over: finalize now.
CLOSED_STATUSES = {"CLO": None, "RNM": Outcome.RESERVE_NOT_MET}

def poll_once(adapter, now: datetime, extra_where: tuple[str, list] | None = None,
              site: str = "govdeals") -> PollReport:
    rep = PollReport()
    # keep the bare call when no profile is given (existing callers + test fakes)
    if site != "govdeals":
        due = due_for_poll(now, extra_where, site=site)
    else:
        due = due_for_poll(now, extra_where) if extra_where else due_for_poll(now)
    if not due:
        return rep
    if hasattr(adapter, "remember"):
        adapter.remember(due)          # synthesized ids → the site's own lot ids
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
        if site == "govdeals":
            append_snapshot(snap)
        else:
            append_snapshot(snap, site)
        rep.snapshotted += 1
        if site != "govdeals" and snap.status in CLOSED_STATUSES:
            outcome, complete = detect_outcome(snap, dropped=True)
            outcome = CLOSED_STATUSES[snap.status] or outcome
            record_outcome(key, outcome.value, snap.current_bid, snap.bid_count,
                           snap.end_utc, complete)
            rep.finalized += 1
            continue
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
