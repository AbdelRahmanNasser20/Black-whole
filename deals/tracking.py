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


# ── landed cost: what the lot really costs, and per chair ────────────────────
#
# The bid is not the cost. GovDeals adds a buyer premium (premiumPercent, 12.5 %
# on most sellers, 10 % on some) plus sales tax on bid AND premium. Once a lot
# sells, the bidbox carries GovDeals' own invoice total (grandTotalAmount) —
# that is the answer. Before then tax reads 0, so an open lot borrows the
# effective rate of a sold lot from the same seller, else the same state; with
# neither, the total is bid + premium and says tax is missing. Never a
# guessed rate.

def bidbox_costs(raw: dict) -> dict:
    """The bidbox's fee fields, as the tracked_lots cost columns."""
    from deals.bidders import _float
    return {"premium_pct": _float(raw.get("premiumPercent")),
            "admin_fee": _float(raw.get("adminFeeAmount")),
            "tax_total": _float(raw.get("totalTaxAmount")),
            "grand_total": _float(raw.get("grandTotalAmount")),
            "lot_state": (raw.get("state") or None)}


def _num(v) -> float | None:
    return float(v) if v is not None else None


def _tax_rate(row: dict) -> float | None:
    """Effective tax rate on a sold lot's invoice: tax / (total - tax)."""
    grand, tax = _num(row.get("grand_total")), _num(row.get("tax_total"))
    if not grand or grand <= 0 or tax is None or grand - tax <= 0:
        return None
    return tax / (grand - tax)


def landed_costs(rows: list[dict]) -> list[dict]:
    """Each row plus a `cost` dict: qty, bid, premium, fees, tax, total,
    per_chair, basis ('exact' | 'est' | 'est_no_tax'), tax_from."""
    from deals.fees import fee_model_from_env
    from deals.quantity import chair_quantity

    by_seller: dict[int, float] = {}
    by_state: dict[str, float] = {}
    for r in rows:
        rate = _tax_rate(r)
        if rate is None:
            continue
        by_seller.setdefault(r["account_id"], rate)
        if r.get("lot_state"):
            by_state.setdefault(r["lot_state"], rate)
    default_pct = fee_model_from_env().buyer_premium_pct * 100

    out = []
    for r in rows:
        if r.get("quantity"):
            qty, qty_source = int(r["quantity"]), "manual"
        else:
            qty, qty_source = chair_quantity(r.get("title"))
        bid = _num(r.get("final_bid") if r.get("closed_at") else r.get("current_bid"))
        cost = {"qty": qty, "qty_source": qty_source, "bid": bid, "premium": None,
                "fees": None, "tax": None, "total": None, "per_chair": None,
                "basis": None, "tax_from": None}
        if bid:
            pct = _num(r.get("premium_pct"))
            premium = round(bid * (default_pct if pct is None else pct) / 100, 2)
            fees = _num(r.get("admin_fee")) or 0.0
            grand = _num(r.get("grand_total"))
            if grand and grand > 0:
                tax = _num(r.get("tax_total")) or 0.0
                cost.update(premium=premium, fees=fees, tax=round(tax, 2),
                            total=round(grand, 2), basis="exact")
            else:
                rate, source = by_seller.get(r["account_id"]), "seller"
                if rate is None and r.get("lot_state") in by_state:
                    rate, source = by_state[r["lot_state"]], "state"
                pre_tax = bid + premium + fees
                if rate is None:
                    cost.update(premium=premium, fees=fees, total=round(pre_tax, 2),
                                basis="est_no_tax")
                else:
                    tax = round(pre_tax * rate, 2)
                    cost.update(premium=premium, fees=fees, tax=tax,
                                total=round(pre_tax + tax, 2), basis="est", tax_from=source)
            if qty:
                cost["per_chair"] = round(cost["total"] / qty, 2)
        out.append({**r, "cost": cost})
    return out


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
            tracking_store.record_costs(asset_id, account_id, bidbox_costs(raw))
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


# Keys tried this process: a purged lot answers 204 forever, and the web tick
# runs every 30 s — try each once per restart, not 2,880 times a day.
_costs_tried: set[tuple[int, int, int]] = set()


def fill_missing_costs(adapter, *, limit: int = 10, verbose: bool = False) -> int:
    """Read the invoice fields for closed lots that never got them. A closed
    lot's bidbox keeps serving its final totals for days, so this backfills the
    lots that closed before migration 013. Small batches: it rides the 30 s tick."""
    from deals import tracking_store

    filled = 0
    rows = tracking_store.missing_costs(limit + len(_costs_tried))
    for row in [r for r in rows
                if (r["asset_id"], r["account_id"], r["auction_id"]) not in _costs_tried][:limit]:
        key = (row["asset_id"], row["account_id"], row["auction_id"])
        _costs_tried.add(key)
        try:
            raw = adapter.fetch_bid_state(*key)
        except Exception as e:  # noqa: BLE001 — a purged lot must not stop the batch
            if verbose:
                print(f"[tracking] costs {key}: {type(e).__name__}: {e}")
            continue
        if not raw:
            continue
        tracking_store.record_costs(key[0], key[1], bidbox_costs(raw))
        filled += 1
    return filled
