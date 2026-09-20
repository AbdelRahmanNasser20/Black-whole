"""The sync engine: `inventory` in, channel actions out.

    desired_state(row, channel)  what a lot SHOULD be on a channel ('live' | 'delisted')
    payload_hash(row, channel)   sha1 of the fields that channel renders
    plan(rows, current)          diff desired vs `listing_channels` → [Action]
    run_once()                   plan + apply, honouring switches and browser pacing

Rules (master plan, Phase 1.4):
  * live iff `status in inventory.CATALOG_FEED_STATUSES` and qty > 0 and not
    `fake_sold_out` — the same gate the FB catalog feed uses, so an archived lot
    can never be offered as stock on any channel.
  * Feed channels (site, fb_catalog, google) are pull-based CSVs: list/delist
    only write state, update writes the new hash.
  * Push channels go through an adapter from `automation.publish.registry`.
    Phase 1 defines the *sync-callable* entry points an adapter may expose:
        publish_lot(row) -> {"url": ..., "external_id": ...}
        update_lot(row, current) -> {"url"?: ..., "external_id"?: ...}   (optional)
        unpublish_lot(row, current) -> None                             (optional)
    The existing Playwright adapters (`fb`, `ebay`) only have the async
    browser `publish(ctx, request)`, so until Phase 3/4 land a real one the
    engine records `error` "adapter has no publish_lot" — visible in the
    Channels tab, never silent. Those switches ship OFF anyway.
  * Approval channels (`fb_marketplace`): a `list` parks the row in
    `pending_approval` — NO adapter call — until the operator approves
    (state `queued`), which is the only path to a real post.
  * A channel whose switch (or the master switch) is off is skipped entirely.
  * Browser channels: at most `browser_channel_daily_cap` list actions per UTC
    day, `browser_channel_spacing_s` apart, counted from `last_synced_at`.
    Delists are never paced — taking a sold lot down is always allowed.
  * `storage_note` is stripped before a row reaches an adapter or a hash.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .. import inventory, site_settings
from ..catalog_feed import site_base_url
from ..publish import registry
from . import APPROVAL_CHANNELS, BROWSER_CHANNELS, CHANNELS, FEED_CHANNELS
from . import store

# Fields a channel renders. A change here is a real listing change (price, qty,
# copy, photo, location); anything else on the row is bookkeeping.
HASH_FIELDS: tuple[str, ...] = (
    "title", "price_per_chair", "quantity_remaining", "city", "state",
    "hero_image", "chair_type", "description",
)

# channel key → adapter key in automation.publish.registry
ADAPTER_KEYS: dict[str, str] = {
    "ebay": "ebay",
    "fb_marketplace": "fb",
    "craigslist": "craigslist",
}

# Never leaves the backend — not to an adapter, not into a hash, not into a log.
_PRIVATE_FIELDS: frozenset[str] = frozenset({"storage_note", "govdeals_username", "govdeals_password"})


@dataclass(frozen=True)
class Action:
    lot_id: str
    channel: str
    op: str  # 'list' | 'update' | 'delist' | 'noop'


# ───────────────────────────── pure planning ─────────────────────────────

def desired_state(row: dict, channel: str) -> str:
    """'live' when the lot is sellable stock, else 'delisted'. Channel-independent
    today; the argument exists so a channel can narrow the rule later (D2)."""
    if row.get("status") not in inventory.CATALOG_FEED_STATUSES:
        return "delisted"
    if (row.get("quantity_remaining") or 0) <= 0:
        return "delisted"
    if row.get("fake_sold_out"):
        return "delisted"
    return "live"


def payload_hash(row: dict, channel: str) -> str:
    payload = {k: row.get(k) for k in HASH_FIELDS}
    payload["_channel"] = channel
    blob = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()


def _op_for(row: dict, channel: str, current: dict | None) -> str:
    want = desired_state(row, channel)
    have = (current or {}).get("state") or "off"
    if want == "live":
        if have == "live":
            return "update" if (current or {}).get("payload_hash") != payload_hash(row, channel) else "noop"
        if have == "pending_approval":
            return "noop"  # waiting on the operator
        return "list"      # off / delisted / error (retry) / queued (approved)
    # want delisted
    if have in ("live", "queued", "pending_approval"):
        return "delist"
    return "noop"


def plan(rows: list[dict], current: dict[tuple[str, str], dict]) -> list[Action]:
    out: list[Action] = []
    for row in rows:
        lot_id = row["lot_id"]
        for channel in CHANNELS:
            out.append(Action(lot_id, channel, _op_for(row, channel, current.get((lot_id, channel)))))
    return out


# ───────────────────────────── applying ─────────────────────────────

def _public_row(row: dict) -> dict:
    return {k: v for k, v in row.items() if k not in _PRIVATE_FIELDS}


def _site_url(lot_id: str) -> str:
    return f"{site_base_url()}/listings/{lot_id}"


class _Pacer:
    """Per-channel budget for the browser channels within one run."""

    def __init__(self, now: datetime, settings: dict[str, Any]):
        self.now = now
        self.cap = int(settings.get("browser_channel_daily_cap", 0) or 0)
        self.spacing = timedelta(seconds=int(settings.get("browser_channel_spacing_s", 0) or 0))
        self._stamps: dict[str, list[datetime]] = {}

    def _load(self, channel: str) -> list[datetime]:
        if channel not in self._stamps:
            day_start = self.now.replace(hour=0, minute=0, second=0, microsecond=0)
            stamps = []
            for ts in store.recent_synced_at(channel, day_start):
                if ts is not None and ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                if ts is not None:
                    stamps.append(ts)
            self._stamps[channel] = stamps
        return self._stamps[channel]

    def blocked_reason(self, channel: str) -> str | None:
        stamps = self._load(channel)
        if len(stamps) >= self.cap:
            return "daily_cap"
        if stamps and (self.now - max(stamps)) < self.spacing:
            return "spacing"
        return None

    def record(self, channel: str) -> None:
        self._load(channel).append(self.now)


def _apply_feed(action: Action, row: dict) -> None:
    h = payload_hash(row, action.channel)
    url = _site_url(action.lot_id) if action.channel == "site" else None
    if action.op in ("list", "update"):
        store.upsert(action.lot_id, action.channel, state="live", url=url, payload_hash=h)
    elif action.op == "delist":
        store.upsert(action.lot_id, action.channel, state="delisted", payload_hash=h)


def _publish_fb_marketplace(row: dict) -> dict:
    """The one built-in publisher: an APPROVED FB Marketplace list runs the same
    `lot_channels.post_to_facebook` the /list-lot skill uses (family-account
    Chrome profile, non-headless, one attempt). Only reachable when the row is
    `queued` (operator clicked Approve) AND `channel_fb_marketplace_enabled` is
    on — both ship off. Raises on failure so the row lands in `error`."""
    from .. import lot_channels  # lazy: pulls Playwright-adjacent modules
    url, err = lot_channels.post_to_facebook(row["lot_id"])
    if not url:
        raise RuntimeError(err or "post_to_facebook returned no url")
    return {"url": url, "external_id": url.rstrip("/").rsplit("/", 1)[-1]}


# channel → sync-callable publisher used when the registry adapter has no
# `publish_lot`. Phase 3 (eBay API) / Phase 4 (Craigslist) add theirs to the
# registry instead.
_BUILTIN_PUBLISHERS: dict[str, Any] = {"fb_marketplace": _publish_fb_marketplace}


def _record_error(action: Action, exc: BaseException) -> str:
    store.upsert(action.lot_id, action.channel, state="error",
                 last_error=f"{type(exc).__name__}: {exc}"[:500])
    return "error"


def _apply_push(action: Action, row: dict, current: dict | None, pacer: _Pacer,
                skipped: dict[str, int]) -> str:
    """Returns 'written' | 'error' | 'skipped'. Never raises: an adapter failure
    is recorded on the row as state `error`, never thrown into the loop."""
    channel = action.channel
    have = (current or {}).get("state") or "off"
    adapter = registry.get(ADAPTER_KEYS.get(channel, channel))
    public = _public_row(row)

    if action.op == "list":
        if channel in APPROVAL_CHANNELS and have != "queued":
            # Park it for the operator. No adapter, no browser, no pacing.
            store.set_state(action.lot_id, channel, "pending_approval")
            return "written"
        if channel in BROWSER_CHANNELS:
            reason = pacer.blocked_reason(channel)
            if reason:
                skipped[reason] = skipped.get(reason, 0) + 1
                return "skipped"
        fn = getattr(adapter, "publish_lot", None) or _BUILTIN_PUBLISHERS.get(channel)
        if fn is None:
            store.upsert(action.lot_id, channel, state="error",
                         last_error="adapter has no publish_lot (channel-sync entry point) — see channels.md")
            return "error"
        try:
            res = fn(public) or {}
        except Exception as e:  # noqa: BLE001
            return _record_error(action, e)
        if channel in BROWSER_CHANNELS:
            pacer.record(channel)
        store.upsert(action.lot_id, channel, state="live", url=res.get("url"),
                     external_id=res.get("external_id"), payload_hash=payload_hash(row, channel))
        return "written"

    if action.op == "update":
        fn = getattr(adapter, "update_lot", None)
        if fn is None:
            # Nothing can push the edit yet; leave the stale hash so the drift stays visible.
            skipped["no_update_adapter"] = skipped.get("no_update_adapter", 0) + 1
            return "skipped"
        try:
            res = fn(public, current) or {}
        except Exception as e:  # noqa: BLE001
            return _record_error(action, e)
        store.upsert(action.lot_id, channel, state="live", url=res.get("url"),
                     external_id=res.get("external_id"), payload_hash=payload_hash(row, channel))
        return "written"

    if action.op == "delist":
        if have in ("queued", "pending_approval"):
            # Never went out — just drop it from the queue.
            store.set_state(action.lot_id, channel, "delisted")
            return "written"
        fn = getattr(adapter, "unpublish_lot", None)
        if fn is None:
            store.set_state(action.lot_id, channel, "delisted", last_error="manual delist required")
            return "written"
        try:
            fn(public, current)
        except Exception as e:  # noqa: BLE001
            return _record_error(action, e)
        store.upsert(action.lot_id, channel, state="delisted")
        return "written"

    return "skipped"


def run_once(*, now: datetime | None = None, dry_run: bool = False) -> dict:
    """One pass over every inventory row × channel.

    Returns {"planned": n, "applied": n, "errors": n, "skipped": {reason: n}}.
    `planned` counts non-noop actions; `applied` counts the ones that wrote a
    row (an `error` row counts as applied AND as an error — a recorded failure
    is still bookkeeping). Never raises for a single bad lot; a failure of the
    collaborators themselves (DB down) propagates to the caller, which logs it.
    """
    now = now or datetime.now(timezone.utc)
    rows = inventory.list_all()
    current = store.current_map()
    actions = [a for a in plan(rows, current) if a.op != "noop"]
    by_lot = {r["lot_id"]: r for r in rows}
    enabled = {c: site_settings.channel_enabled(c) for c in CHANNELS}
    pacer = _Pacer(now, site_settings.get_all())

    applied = errors = 0
    skipped: dict[str, int] = {}
    for action in actions:
        if not enabled.get(action.channel):
            skipped["disabled"] = skipped.get("disabled", 0) + 1
            continue
        if dry_run:
            continue
        row = by_lot[action.lot_id]
        if action.channel in FEED_CHANNELS:
            _apply_feed(action, row)
            applied += 1
            continue
        outcome = _apply_push(action, row, current.get((action.lot_id, action.channel)), pacer, skipped)
        if outcome != "skipped":
            applied += 1
        if outcome == "error":
            errors += 1
    return {"planned": len(actions), "applied": applied, "errors": errors, "skipped": skipped}
