"""Tracking list — follow chosen GovDeals lots through their close.

**Why this exists.** `track-bidders` samples *categories* of lots (every
contested chair lot, soonest-closing first) on a fixed cron. That is breadth.
This module is depth: a short list of lots the operator explicitly cares
about — starred favorites, the banquet-chair lots we're pricing against, a
rival's lot we want to watch lose — polled hard enough near the close that we
end up with the thing GovDeals never publishes: what it actually sold for, how
many bids it took, and who (`th*****`) walked away with it.

Membership lives in `tracked_lots`, keyed by (asset_id, account_id) rather
than by auction because an unsold lot relists under the same asset with a new
auction id and we want to keep following it. The history itself is written to
`deal_bid_observations`, the same change-gated table `bidders.py` fills, so a
tracked lot's timeline and a rival's profile come from one place.

Cadence is per-lot and adaptive (`poll_interval`): 30 min while the close is
days away, 5 min inside the last day, 60 s inside the last half hour. The
FastAPI process runs `sync_tracked` on its scheduler tick, so this needs no
cron; `deals.cli track sync` is the same pass for the terminal.

Pure functions live at the top and are unit-tested without a DB or network
(`tests/deals/test_tracking.py`); everything below `sync_tracked` is I/O.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from deals.bidders import BidState, bidbox_to_state, parse_favorite_key

# GovDeals bidbox `assetStatusCd`: STA = still accepting bids. Anything else
# (SOA sold-awaiting-payment, SOL sold, CLO closed, CAN cancelled…) means the
# auction is no longer live. Match on "not STA" rather than enumerating the
# closed codes so an unfamiliar code errs toward "closed" — the cost of that
# is one wasted final poll; the cost of the opposite is polling forever.
LIVE_STATUS = "STA"

# A lot past its clock can still be live: GovDeals extends the close on a late
# bid. Wait this long past end_utc before calling it closed on time alone.
CLOSE_GRACE = timedelta(minutes=15)

COLD_INTERVAL = 30 * 60      # > 24h out
WARM_INTERVAL = 5 * 60       # <= 24h out
HOT_INTERVAL = 60            # <= 30 min out (incl. past-clock, extension may be live)
UNKNOWN_END_INTERVAL = 5 * 60

FAVORITES_LABEL = "favorites"

_REF_RE = re.compile(r"(?:^|/asset/)(\d+)/(\d+)(?:[/?#]|$)")


def parse_lot_ref(text: str) -> tuple[int, int] | None:
    """Turn whatever the operator pasted into (asset_id, account_id).

    Accepts the lot URL (`https://www.govdeals.com/en/asset/96/27562`), the
    bare `96/27562` the favorites table uses, or `asset/96/27562`. Same
    asset-then-account order as the URL — swapped ids silently query a
    different lot, which is why this only accepts the one ordering.
    """
    if not text:
        return None
    s = text.strip()
    m = _REF_RE.search(s)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def is_closed(state: BidState, now: datetime) -> bool:
    """True once the auction is over: status flipped off STA, or the clock
    plus grace has passed (the grace absorbs anti-snipe extensions)."""
    if state.status and state.status != LIVE_STATUS:
        return True
    if state.end_utc is not None and now >= state.end_utc + CLOSE_GRACE:
        return True
    return False


def poll_interval(end_utc: datetime | None, now: datetime) -> int:
    """Seconds until the next poll, tightening as the close approaches.

    Lead changes only exist while they're live (GovDeals keeps no history), and
    nearly all of them happen in the final minutes — so that's where the
    requests go.
    """
    if end_utc is None:
        return UNKNOWN_END_INTERVAL
    remaining = (end_utc - now).total_seconds()
    if remaining <= 30 * 60:
        return HOT_INTERVAL
    if remaining <= 24 * 3600:
        return WARM_INTERVAL
    return COLD_INTERVAL


def bidder_summary(observations: list[dict]) -> list[dict]:
    """Collapse a lot's observation rows into one entry per bidder id.

    Rows are the `deal_bid_observations` dicts in observed_at order. A bidder
    who led at three different prices is one rival with three sightings, not
    three rivals.
    """
    by_id: dict[int, dict] = {}
    for o in observations:
        b = o.get("high_bidder")
        if b is None:
            continue
        e = by_id.get(b)
        bid = float(o["current_bid"]) if o.get("current_bid") is not None else None
        if e is None:
            e = by_id[b] = {
                "bidder_id": b,
                "handle": o.get("high_bidder_username"),
                "times_led": 0,
                "first_led_at": o.get("observed_at"),
                "last_led_at": o.get("observed_at"),
                "max_bid": bid,
            }
        e["times_led"] += 1
        e["last_led_at"] = o.get("observed_at")
        if not e["handle"] and o.get("high_bidder_username"):
            e["handle"] = o["high_bidder_username"]
        if bid is not None and (e["max_bid"] is None or bid > e["max_bid"]):
            e["max_bid"] = bid
    return sorted(by_id.values(), key=lambda e: (e["max_bid"] or 0), reverse=True)


# ── I/O below this line ──────────────────────────────────────────────────────

def _resolve_auction(adapter, asset_id: int, account_id: int) -> int | None:
    from deals import store
    auction_id = store.live_auction_id(asset_id, account_id)
    if auction_id is not None:
        return auction_id
    detail = adapter.fetch_detail(asset_id, account_id) or {}
    try:
        return int(detail.get("auctionId") or 0) or None
    except (TypeError, ValueError):
        return None


def add_tracked(adapter, ref: str, *, label: str = "default", note: str | None = None,
                source: str = "manual", title: str | None = None) -> dict:
    """Put a lot on the list. Resolves the live auction id immediately so the
    first poll can happen on the next tick, and pulls a title from the detail
    endpoint when the caller has none (the URL alone is unreadable in a table).
    Raises ValueError on an unparseable ref."""
    from deals import tracking_store
    pair = parse_lot_ref(ref)
    if not pair:
        raise ValueError(f"not a GovDeals lot reference: {ref!r}")
    asset_id, account_id = pair
    auction_id = None
    try:
        auction_id = _resolve_auction(adapter, asset_id, account_id)
        if not title:
            detail = adapter.fetch_detail(asset_id, account_id) or {}
            title = detail.get("assetShortDesc") or None
    except Exception as e:  # noqa: BLE001 — a dead endpoint must not block adding
        print(f"[tracking] resolve failed for {asset_id}/{account_id}: {type(e).__name__}: {e}")
    return tracking_store.upsert(
        asset_id, account_id, auction_id=auction_id, label=label, note=note,
        source=source, title=title,
        url=f"https://www.govdeals.com/en/asset/{asset_id}/{account_id}")


def adopt_favorites(adapter=None, *, verbose: bool = False) -> int:
    """Every GovDeals favorite joins the tracking list under `favorites`.

    Unstarring does NOT remove the row — the history is the valuable part and
    the operator can delete it from the Tracking tab if they really mean it.
    Returns the number of newly adopted lots."""
    from deals import store, tracking_store
    known = tracking_store.known_keys()
    added = 0
    for fav in store.favorite_rows():
        pair = parse_favorite_key(fav["asset_id"])
        if not pair or pair in known:
            continue
        auction_id = None
        if adapter is not None:
            try:
                auction_id = _resolve_auction(adapter, *pair)
            except Exception as e:  # noqa: BLE001
                print(f"[tracking] resolve failed for favorite {fav['asset_id']}: {e}")
        tracking_store.upsert(pair[0], pair[1], auction_id=auction_id,
                              label=FAVORITES_LABEL, source="favorite",
                              title=fav.get("title"), url=fav.get("link"))
        added += 1
        if verbose:
            print(f"[tracking] adopted favorite {fav['asset_id']}: {fav.get('title')}")
    return added


def sync_tracked(adapter, *, now: datetime | None = None, verbose: bool = True) -> dict:
    """One pass: poll every open tracked lot that is due, record changes,
    stamp finals on the ones that closed, and reschedule the rest.

    Per-lot error isolation, same as `track_bidders`: a 204 from one relisted
    lot must not stop the one closing in four minutes from being sampled.
    """
    from deals import store, tracking_store

    now = now or datetime.now(timezone.utc)
    report = {"due": 0, "polled": 0, "recorded": 0, "closed": 0, "errors": 0}
    for row in tracking_store.due(now):
        report["due"] += 1
        asset_id, account_id = row["asset_id"], row["account_id"]
        try:
            auction_id = row.get("auction_id") or _resolve_auction(adapter, asset_id, account_id)
            if auction_id is None:
                tracking_store.mark_error(asset_id, account_id, "no live auction",
                                          now + timedelta(seconds=COLD_INTERVAL))
                report["errors"] += 1
                continue
            key = (asset_id, account_id, auction_id)
            raw = adapter.fetch_bid_state(*key)
            report["polled"] += 1
            if not raw:
                tracking_store.mark_error(asset_id, account_id, "empty bidbox",
                                          now + timedelta(seconds=WARM_INTERVAL))
                report["errors"] += 1
                continue
            state = bidbox_to_state(raw, key, now)
            if store.append_bid_observation(state):
                report["recorded"] += 1
                if verbose:
                    who = state.high_bidder_username or "—"
                    print(f"  [tracking] {asset_id}/{account_id}/{auction_id}  "
                          f"{state.bid_count} bids  ${state.current_bid:,.2f}  "
                          f"high={who} ({state.high_bidder})")
            closed = is_closed(state, now)
            next_at = None if closed else now + timedelta(seconds=poll_interval(state.end_utc, now))
            tracking_store.record_state(state, next_poll_at=next_at, closed_at=(now if closed else None))
            if closed:
                report["closed"] += 1
                # Fill deal_lots' outcome too when that row exists — this is
                # the exact final price the watcher can only infer.
                outcome = ("no_bid" if state.bid_count == 0
                           else "low_bid" if state.bid_count <= 1 else "sold")
                store.record_outcome(key, outcome, state.current_bid, state.bid_count, now, True)
                if verbose:
                    print(f"  [tracking] CLOSED {asset_id}/{account_id}: ${state.current_bid:,.2f} "
                          f"after {state.bid_count} bids → {state.high_bidder_username or '—'}")
        except Exception as e:  # noqa: BLE001 — one bad lot must not end the pass
            report["errors"] += 1
            tracking_store.mark_error(asset_id, account_id, f"{type(e).__name__}: {e}",
                                      now + timedelta(seconds=WARM_INTERVAL))
            print(f"[tracking] {asset_id}/{account_id}: {type(e).__name__}: {e}")
    return report
