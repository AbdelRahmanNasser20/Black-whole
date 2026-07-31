"""Rival-bidder intelligence for GovDeals lots.

**Why this exists.** We keep losing chair lots and never learn to whom. The
search firehose (`deals/adapters/govdeals.py::discover`) carries a numeric
`highBidder` id but no name, and GovDeals publishes no bid *history* anywhere —
the per-lot bidbox endpoint is the only place a human-readable bidder appears,
and even there it is masked to the first two characters (`"ja*****"`).

That masking is what makes this worth storing rather than querying on demand.
An id alone is opaque; an id plus a two-letter stem plus the set of lots that
id chased is recognizable — "3939619 / `ja*****` bids on Gasser stacking chairs
in OK and TX, always in the last hour, never past $4/chair" is a rival profile
you can price against.

**The collection model is sampling, not history.** Nothing on GovDeals lets you
replay an auction after the fact, so a lead change only ever exists in the
moment it is live. This module writes one row per *observed change* of
`(bid_count, current_bid, high_bidder)` — change-gated exactly like
`deal_snapshots` — so a dense enough poll cadence reconstructs the bid history
we are otherwise never given. Rows we miss are gone for good, which is why the
watcher's hot lane (a lot near its close) matters more than sweep breadth here.

Pure functions live at the top and are unit-tested without a DB or network
(`tests/deals/test_bidders.py`); everything below `track_bidders` is I/O.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

# Non-GovDeals favorites are keyed with a source prefix by the Auctions tab
# (`ps:` Public Surplus, `bs:` BidSpotter). Neither site exposes a bidbox, so
# they are skipped rather than mis-parsed into bogus asset ids.
_FOREIGN_PREFIXES = ("ps:", "bs:")


@dataclass
class BidState:
    """One observation of a lot's live bid state.

    `high_bidder_username` is GovDeals' masked stem, kept verbatim — do not
    "clean" it. The trailing asterisks are load-bearing: they say how long the
    real handle is, which is the only length signal we ever get.
    """
    asset_id: int
    account_id: int
    auction_id: int
    observed_at: datetime
    bid_count: int
    current_bid: float
    currency_code: str
    high_bidder: int | None
    high_bidder_username: str | None
    bid_increment: float | None
    visitors: int | None
    hits: int | None
    watcher_count: int | None
    end_utc: datetime | None
    status: str | None


def _utc(s) -> datetime | None:
    if not s:
        return None
    if isinstance(s, datetime):
        return s.astimezone(timezone.utc)
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _int(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _float(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def parse_favorite_key(asset_id: str) -> tuple[int, int] | None:
    """Map an `auction_favorites.asset_id` to (asset_id, account_id).

    The Auctions tab stores GovDeals favorites as ``"17/28505"`` — the same
    asset-then-account order as the lot URL (`/en/asset/{asset}/{account}`).
    Returns None for other sources or anything unparseable, so a BidSpotter
    star can never be sent to a GovDeals endpoint.
    """
    if not asset_id:
        return None
    key = asset_id.strip()
    if key.lower().startswith(_FOREIGN_PREFIXES):
        return None
    parts = key.split("/")
    if len(parts) != 2:
        return None
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return None


def bidbox_to_state(raw: dict, key: tuple[int, int, int], observed_at: datetime) -> BidState:
    """Map a bidbox payload to a `BidState`.

    `currentBid` is parsed strictly for the same reason `mapping._price` is: a
    silent 0.0 would read as "nobody has bid" and could talk us into a lot that
    is actually contested. Every other field degrades to None — a bidbox that
    omits `visitors` is uninteresting, one that omits the price is a lie.
    """
    cb = raw.get("currentBid")
    if cb is None:
        raise ValueError(f"currentBid missing in bidbox for {key}")
    try:
        current_bid = float(cb)
    except (TypeError, ValueError) as e:
        raise ValueError(f"currentBid unparseable ({cb!r}) in bidbox for {key}") from e

    username = raw.get("highBidderUsername")
    return BidState(
        asset_id=key[0], account_id=key[1], auction_id=key[2],
        observed_at=observed_at,
        bid_count=_int(raw.get("bidCount")) or 0,
        current_bid=current_bid,
        currency_code=(raw.get("currencyCode") or "USD"),
        # highBidder is 0 on an un-bid lot; store NULL so the rollup doesn't
        # invent a rival named "0" who chases every no-bid lot on the site.
        high_bidder=(_int(raw.get("highBidder")) or None),
        high_bidder_username=(username.strip() or None) if isinstance(username, str) else None,
        bid_increment=_float(raw.get("assetBidIncrement")),
        visitors=_int(raw.get("visitors")),
        hits=_int(raw.get("hits")),
        watcher_count=_int(raw.get("watcherCount")),
        end_utc=_utc(raw.get("assetAuctionEndDateUTC") or raw.get("assetAuctionEndDate")),
        status=raw.get("assetStatusCd"),
    )


def is_bid_change(prev: BidState | None, new: BidState) -> bool:
    """True when `new` is worth a row.

    Gated on the three fields that define a bidding event. `visitors`/`hits`
    drift constantly and would otherwise write a row on every poll, burying the
    lead changes we actually came for.
    """
    if prev is None:
        return True
    return (prev.bid_count != new.bid_count
            or prev.current_bid != new.current_bid
            or prev.high_bidder != new.high_bidder)


def favorite_targets(adapter, *, verbose: bool = True) -> list[tuple[int, int, int]]:
    """Live auction keys for the GovDeals lots the operator has starred.

    A favorite is keyed by asset only, so the auction id has to be resolved
    every run — and it genuinely changes: a lot that doesn't sell gets relisted
    under the same asset with a new auction id (17/28505 ran as auction 3, then
    again as auction 4). Resolving from `deal_lots` first keeps this free;
    falling back to the detail endpoint covers a favorite the sweep hasn't
    reached yet.
    """
    from deals import store

    keys: list[tuple[int, int, int]] = []
    for raw_id in store.favorite_asset_ids():
        pair = parse_favorite_key(raw_id)
        if not pair:
            if verbose:
                print(f"[bidders] skipping non-GovDeals favorite {raw_id!r}")
            continue
        auction_id = store.live_auction_id(*pair)
        if auction_id is None:
            try:
                auction_id = int(adapter.fetch_detail(*pair).get("auctionId") or 0) or None
            except Exception as e:  # noqa: BLE001
                print(f"[bidders] can't resolve auction for {raw_id}: {type(e).__name__}: {e}")
                auction_id = None
        if auction_id is None:
            if verbose:
                print(f"[bidders] favorite {raw_id} has no live auction — skipping")
            continue
        keys.append((pair[0], pair[1], auction_id))
    return keys


def track_bidders(adapter, keys, *, now: datetime | None = None, verbose: bool = True) -> dict:
    """Sample the live bid state of each `(asset, account, auction)` key.

    Per-lot error isolation: one dead lot (relisted, pulled, 204'd) must not
    abort the sweep, because the lots most worth watching are the ones closest
    to closing.
    """
    from deals import store

    observed_at = now or datetime.now(timezone.utc)
    report = {"polled": 0, "recorded": 0, "unchanged": 0, "errors": 0}
    for key in keys:
        try:
            raw = adapter.fetch_bid_state(*key)
            report["polled"] += 1
            if not raw:
                report["errors"] += 1
                continue
            state = bidbox_to_state(raw, key, observed_at)
            if store.append_bid_observation(state):
                report["recorded"] += 1
                if verbose:
                    who = state.high_bidder_username or "—"
                    print(f"  {key[0]}/{key[1]}/{key[2]}  {state.bid_count} bids  "
                          f"${state.current_bid:,.2f}  high={who} ({state.high_bidder})")
            else:
                report["unchanged"] += 1
        except Exception as e:  # noqa: BLE001 — one bad lot must not end the sweep
            report["errors"] += 1
            print(f"[bidders] {key}: {type(e).__name__}: {e}")
    return report
