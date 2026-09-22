"""Keep the storefront honest about auctions we are still bidding on.

**Why this exists.** A lot we are bidding on lives in the ledger as
`status = 'active_bid'` and shows on black-whole.com as incoming stock. When the
GovDeals auction closes and we did not win, nothing told the site — so buyers
kept asking about chairs we never had. And when an unsold lot RELISTS (the
common case: a no-bid lot goes up again at the opening price), nothing told us
either, so the second chance to buy it went by unnoticed.

This module closes both directions:

  auction closed  → `lot_channels.remove_lot` → fake sold out, off the site,
                    off the FB catalog feed, not offerable by the CRM
  auction live again → `lot_channels.restore_lot` → back on the site, plus a
                    Telegram ping

**The one rule that matters.** An endpoint that will not answer is *not* an
answer. Maestro serves HTTP 204 with an empty body for assets it has purged
(verified 2026-09-15 on 53677/357, both id orders). Any lot whose state cannot
be read is recorded as an error and left exactly as it was: flipping a live lot
to SOLD because the API blinked is the only unrecoverable failure here, and the
cost of the opposite is one stale lot until the next pass.

**Which signal decides.** `assetStatusCd` — `STA` means still taking bids,
anything else means over. `deals/tracking.py` already made that call for the
bid-history poller, so `is_live` delegates to `tracking.is_closed` rather than
re-deciding it; the two pollers can then never disagree about what "closed"
means. The close *time* comes from the bidbox's `assetAuctionEndDateUTC`, never
from the detail endpoint's `assetAuctionEndDate` — that one carries no timezone
and is US/Eastern, so reading it as UTC puts every close 4 hours late (asset
420/9312, 2026-09-15: detail `17:00:05`, bidbox `21:00:05Z`).

Pure functions live above the I/O divider and are unit-tested without a
database or a network (`tests/test_auction_sync.py`).

Plan: `docs/superpowers/plans/2026-09-15-auction-expiry-sync.md`.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from . import auction_watch_store as watch_store
from . import lot_channels

# The site the Telegram message points a buyer back at.
SITE_BASE = lot_channels.SITE_BASE

# Close times are shown in the operator's zone, not UTC — "closes Sep 17, 2:00
# PM MST" is actionable, "2026-09-17T21:00:05Z" is a lookup.
ALERT_TZ = os.getenv("ALERT_TIMEZONE", "America/Phoenix")

# What a lot goes back to when its auction returns. A row we expired remembers
# its own prior status; this is the fallback for one that does not.
DEFAULT_RESTORE_STATUS = "active_bid"

ACTION_NOOP = "noop"
ACTION_EXPIRE = "expire"
ACTION_RELIST = "relist"
ACTION_UNKNOWN = "unknown"

Log = Callable[[str], None]


@dataclass(frozen=True)
class AuctionState:
    """What GovDeals says about one asset right now.

    Field names match `deals.bidders.BidState` where they overlap (`status`,
    `end_utc`) so `deals.tracking.is_closed` accepts either.
    """
    asset_id: int
    account_id: int
    auction_id: int | None
    status: str | None          # assetStatusCd: 'STA' = live
    end_utc: datetime | None
    city: str | None = None
    state: str | None = None


# ───────────────────────────── pure helpers ─────────────────────────────

def lot_ref(row: dict) -> tuple[int, int] | None:
    """`(asset_id, account_id)` from an inventory row's `govdeals_url`.

    `/en/asset/{assetId}/{accountId}` — asset FIRST. The order is the whole
    point: swapped ids do not error, they return an empty 204 body, which would
    read as "cannot resolve" forever. Returns None for a missing or non-GovDeals
    URL so one bad row cannot end a pass.
    """
    url = (row or {}).get("govdeals_url") or ""
    try:
        return lot_channels.parse_govdeals_url(url)
    except ValueError:
        return None


def detail_auction_id(detail: dict | None) -> int | None:
    """The auction the asset is in *right now*, off the detail endpoint.

    This is the only call that answers that question without already knowing an
    auction id, which is exactly what relist detection needs: the relist is a
    new auction id under the same asset.
    """
    try:
        return int((detail or {}).get("auctionId") or 0) or None
    except (TypeError, ValueError):
        return None


def bidbox_to_auction_state(raw: dict | None, asset_id: int, account_id: int,
                            auction_id: int | None) -> AuctionState | None:
    """Map a bidbox payload to `AuctionState`. None when there is nothing to map.

    Unlike `deals.bidders.bidbox_to_state` this does not require `currentBid`:
    we are asking "is this auction open", not "what is it worth", and a payload
    that carries a status but no price still answers the question.
    """
    if not raw:
        return None
    # Same tolerant ISO/Z parser the bid-history tracker uses, so the two
    # pollers read GovDeals' timestamps identically.
    from deals.bidders import _utc

    status = raw.get("assetStatusCd")
    return AuctionState(
        asset_id=asset_id,
        account_id=account_id,
        auction_id=(_int(raw.get("auctionId")) or auction_id),
        status=(status.strip() if isinstance(status, str) else None) or None,
        # `assetAuctionEndDateUTC` ONLY. The un-suffixed `assetAuctionEndDate`
        # is a naive US/Eastern timestamp; `_utc` would read it as system-local
        # and land the close hours off. No close time beats a wrong one — the
        # decision rides on `assetStatusCd` anyway.
        end_utc=_utc(raw.get("assetAuctionEndDateUTC")),
        city=(raw.get("city") or None),
        state=(raw.get("state") or None),
    )


def _int(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def is_live(state: AuctionState | None, now: datetime) -> bool | None:
    """True / False / None-for-unknown.

    Delegates to `deals.tracking.is_closed`, which owns `LIVE_STATUS` and the
    15-minute `CLOSE_GRACE` that absorbs GovDeals' anti-snipe extension. A state
    with no `assetStatusCd` is unknown, never "closed" — see the module
    docstring.
    """
    if state is None or not state.status:
        return None
    from deals import tracking
    return not tracking.is_closed(state, now)


def is_new_auction(known_auction_id: int | None, current_auction_id: int | None) -> bool:
    """A relist proper: the asset is live under an auction id we have not seen.

    An unsold lot relists under the SAME asset with a NEW auction id, which is
    why the watch list is keyed by asset. Same id back live means we called the
    close ~15 minutes early and the extension ran — still worth putting the lot
    back, but it is not a new sale.
    """
    if current_auction_id is None or known_auction_id is None:
        return True
    return current_auction_id != known_auction_id


def decide(*, watch_state: str | None, live: bool | None) -> str:
    """The whole state machine, in one place and with no I/O.

    | watch state      | GovDeals now | action  |
    |------------------|--------------|---------|
    | live / unseen    | live         | noop    |
    | live / unseen    | closed       | expire  |
    | expired          | closed       | noop    |
    | expired          | live         | relist  |
    | any              | unreadable   | unknown |
    """
    if live is None:
        return ACTION_UNKNOWN
    expired = watch_state == watch_store.STATE_EXPIRED
    if live:
        return ACTION_RELIST if expired else ACTION_NOOP
    return ACTION_NOOP if expired else ACTION_EXPIRE


def format_close(end_utc: datetime | None, tz_name: str = ALERT_TZ) -> str:
    """"Sep 17, 2:00 PM MST" — the operator's zone, not UTC.

    Falls back to UTC (and then to "an unknown time") rather than raising: a
    missing tz database must not cost us the alert.
    """
    if end_utc is None:
        return "an unknown time"
    dt = end_utc if end_utc.tzinfo else end_utc.replace(tzinfo=timezone.utc)
    try:
        from zoneinfo import ZoneInfo
        dt = dt.astimezone(ZoneInfo(tz_name))
    except Exception:  # noqa: BLE001 — no tzdata is not a reason to skip the ping
        dt = dt.astimezone(timezone.utc)
    # %-d / %-I drop the leading zero (glibc + BSD): "Sep 7, 2:00 PM MST".
    return dt.strftime("%b %-d, %-I:%M %p %Z")


def lot_location(row: dict, state: AuctionState | None = None) -> str:
    """"Las Vegas, NV" — the ledger's city first, GovDeals' as the fallback."""
    city = (row or {}).get("city") or (state.city if state else None) or ""
    st = (row or {}).get("state") or (state.state if state else None) or ""
    return ", ".join(p for p in (str(city).strip(), str(st).strip()) if p) or "location unknown"


def relist_message(row: dict, state: AuctionState | None, *, new_auction: bool,
                   site_base: str = SITE_BASE) -> str:
    """The Telegram body. Plain text, no Markdown — one stray underscore in a
    lot title breaks Telegram's parser silently (same rule as `_format_alert`).

    "BACK LIVE" rather than "RELISTED" when the auction id did not change, so
    the operator can tell a genuine second listing from a close we called early.
    """
    title = (row.get("title") or row.get("lot_id") or "Untitled lot").strip()
    head = "RELISTED" if new_auction else "BACK LIVE"
    where = lot_location(row, state)
    when = format_close(state.end_utc if state else None)
    link = f"{site_base.rstrip('/')}/listings/{row.get('lot_id')}"
    return f"{head}: {title} — {where} — closes {when} — back on {link}"


# ───────────────────────────── I/O below this line ───────────────────────────

def _default_adapter():
    from deals.adapters.govdeals import GovDealsAdapter
    return GovDealsAdapter()


def fetch_state(adapter, asset_id: int, account_id: int) -> AuctionState | None:
    """Two calls: detail for the current auction id, bidbox for status + close.

    Raises whatever the adapter raises — `sync_once` isolates per lot. Returns
    None when maestro has nothing to say (204 / empty body).
    """
    detail = adapter.fetch_detail(asset_id, account_id) or {}
    auction_id = detail_auction_id(detail)
    if auction_id is None:
        return None
    raw = adapter.fetch_bid_state(asset_id, account_id, auction_id) or {}
    return bidbox_to_auction_state(raw, asset_id, account_id, auction_id)


def sync_candidates(lot_ids: list[str] | None = None) -> list[dict]:
    """Every lot this sync is responsible for, newest ledger edit first.

    Two halves, because neither alone is the whole set:
      * `status = 'active_bid'` with a `govdeals_url` — lots currently offered
        as incoming stock, the ones that can go stale.
      * whatever `inventory_auction_watch` still lists as `expired` — those no
        longer carry `active_bid`, and dropping them would mean a relist is
        never noticed.
    """
    from . import db

    if lot_ids:
        rows = db.fetch_all(
            "SELECT * FROM inventory WHERE lot_id = ANY(%s) ORDER BY lot_id",
            (list(lot_ids),))
        return [r for r in rows if r.get("govdeals_url")]

    watched = watch_store.expired_lot_ids() if watch_store.schema_ready() else []
    rows = db.fetch_all(
        """SELECT * FROM inventory
           WHERE govdeals_url IS NOT NULL
             AND (status = 'active_bid' OR lot_id = ANY(%s))
           ORDER BY updated_at DESC""",
        (list(watched),))
    return rows


def _notify(text: str, log: Log) -> None:
    """Best-effort Telegram. An unconfigured bot is a log line, not a failure —
    the ledger change has already happened and is the part that matters."""
    from .telegram_alerts import send_message_sync
    try:
        ok, err = send_message_sync(text, topic="deals")
    except Exception as e:  # noqa: BLE001
        ok, err = False, f"{type(e).__name__}: {e}"
    log(f"  telegram: {'sent' if ok else 'NOT sent (' + str(err) + ')'}")


def sync_once(*, adapter=None, now: datetime | None = None, dry_run: bool = False,
              lot_ids: list[str] | None = None, log: Log = print) -> dict:
    """One pass over every watched lot. Per-lot error isolation throughout.

    Returns a report dict: checked / expired / relisted / unchanged / unresolved
    / errors, plus `actions` — one entry per lot that moved, which is what the
    CLI prints and `--dry-run` exists to show.
    """
    now = now or datetime.now(timezone.utc)
    adapter = adapter or _default_adapter()
    report: dict[str, Any] = {"checked": 0, "expired": 0, "relisted": 0, "unchanged": 0,
                              "unresolved": 0, "errors": 0, "dry_run": bool(dry_run),
                              "actions": []}

    ready = watch_store.schema_ready()
    if not ready:
        msg = ("inventory_auction_watch is missing — apply "
               "scripts/sql/012_inventory_auction_watch.sql "
               "(.venv/bin/python scripts/apply_sql.py scripts/sql/012_inventory_auction_watch.sql)")
        if not dry_run:
            report["error"] = msg
            log(f"[auction-sync] {msg}")
            return report
        log(f"[auction-sync] WARNING: {msg} — dry run continues with no stored history")

    for row in sync_candidates(lot_ids):
        lot_id = row["lot_id"]
        ref = lot_ref(row)
        if not ref:
            report["unresolved"] += 1
            log(f"[auction-sync] {lot_id}: no parseable govdeals_url — skipped")
            continue
        asset_id, account_id = ref
        try:
            watch = watch_store.get(lot_id) if ready else None
            state = fetch_state(adapter, asset_id, account_id)
            live = is_live(state, now)
            action = decide(watch_state=(watch or {}).get("state"), live=live)
            report["checked"] += 1

            if action == ACTION_UNKNOWN:
                report["unresolved"] += 1
                why = "empty response from maestro" if state is None else "no assetStatusCd"
                log(f"[auction-sync] {lot_id} ({asset_id}/{account_id}): unresolved — {why}")
                if ready and not dry_run:
                    watch_store.mark_error(lot_id, asset_id=asset_id, account_id=account_id,
                                           error=why, now=now)
                continue

            if action == ACTION_NOOP:
                report["unchanged"] += 1
                if ready and not dry_run:
                    watch_store.mark_checked(lot_id, asset_id=asset_id, account_id=account_id,
                                             auction_id=state.auction_id, end_utc=state.end_utc,
                                             now=now, live=bool(live))
                continue

            if action == ACTION_EXPIRE:
                report["expired"] += 1
                reason = f"assetStatusCd={state.status}"
                report["actions"].append({"lot_id": lot_id, "action": ACTION_EXPIRE,
                                          "reason": reason, "auction_id": state.auction_id})
                log(f"[auction-sync] {lot_id} ({asset_id}/{account_id}): EXPIRED ({reason}) "
                    f"→ fake sold out")
                if dry_run:
                    continue
                prior_status = row.get("status")
                # The ledger path the admin Launcher and /list-lot already use:
                # status → a sold status + fake_sold_out=true + crm_offerable
                # off. 'fb' is left out on purpose — Marketplace needs a browser
                # and an operator, never a background loop.
                lot_channels.remove_lot(lot_id, channels=("site", "business"),
                                        log=lambda m: log(f"  {m.strip()}"))
                if ready:
                    watch_store.mark_expired(lot_id, asset_id=asset_id, account_id=account_id,
                                             auction_id=state.auction_id, reason=reason,
                                             end_utc=state.end_utc, now=now,
                                             prior_status=prior_status)
                continue

            # ACTION_RELIST
            report["relisted"] += 1
            fresh = is_new_auction((watch or {}).get("auction_id"), state.auction_id)
            reason = (f"live again under auction {state.auction_id}" if fresh
                      else f"auction {state.auction_id} still live (close called early)")
            report["actions"].append({"lot_id": lot_id, "action": ACTION_RELIST,
                                      "reason": reason, "auction_id": state.auction_id,
                                      "new_auction": fresh})
            log(f"[auction-sync] {lot_id} ({asset_id}/{account_id}): RELISTED — {reason}")
            if dry_run:
                log(f"  would send: {relist_message(row, state, new_auction=fresh)}")
                continue
            restore_status = (watch or {}).get("prior_status") or DEFAULT_RESTORE_STATUS
            lot_channels.restore_lot(lot_id, status=restore_status,
                                     log=lambda m: log(f"  {m.strip()}"))
            # Point the row at the auction that is actually live now. Through
            # the ledger — `set_fields` is the whitelisted write path.
            from . import inventory
            fresh_row = inventory.set_fields(
                lot_id, govdeals_url=lot_channels.govdeals_url(asset_id, account_id)) or row
            if ready:
                watch_store.mark_relisted(lot_id, asset_id=asset_id, account_id=account_id,
                                          auction_id=state.auction_id, reason=reason,
                                          end_utc=state.end_utc, now=now)
            _notify(relist_message(fresh_row, state, new_auction=fresh), log)

        except Exception as e:  # noqa: BLE001 — one bad lot must not end the pass
            report["errors"] += 1
            log(f"[auction-sync] {lot_id} ({asset_id}/{account_id}): {type(e).__name__}: {e}")
            if ready and not dry_run:
                try:
                    watch_store.mark_error(lot_id, asset_id=asset_id, account_id=account_id,
                                           error=f"{type(e).__name__}: {e}", now=now)
                except Exception:  # noqa: BLE001
                    pass
    return report
