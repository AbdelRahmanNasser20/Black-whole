"""Phase 1.4 — the sync engine. Pure planning (`desired_state`, `payload_hash`,
`plan`) plus `run_once`, exercised with every collaborator monkeypatched:
`inventory.list_all`, `store.*`, `site_settings.channel_enabled/get_all`, and
`registry.get`. Nothing here touches a DB, a browser, or the network.

Rules defended (from the master plan, Phase 1.4):
  * live iff status in CATALOG_FEED_STATUSES and qty > 0 and not fake_sold_out.
  * hash tracks the fields a channel renders, ignores bookkeeping (updated_at).
  * plan: off→list, live+drift→update, live+same→noop, live→delist when the lot
    stops being sellable, pending_approval waits.
  * approval channels get `pending_approval`, never an adapter call, until the
    operator approves (state `queued`).
  * a disabled channel is skipped and counted, never acted on.
  * browser channels honour the daily cap and spacing.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from automation import channels, inventory
from automation.channels import store, sync
from automation.channels.sync import Action

NOW = datetime(2026, 9, 20, 15, 0, tzinfo=timezone.utc)


def lot(**over) -> dict:
    row = {
        "lot_id": "31225", "title": "Brown banquet chairs", "status": "owned",
        "quantity_remaining": 1200, "price_per_chair": 20, "city": "Atlanta", "state": "GA",
        "hero_image": "https://r2.example/hero.jpg", "chair_type": "banquet", "description": "stackable",
        "fake_sold_out": False, "updated_at": "2026-09-20T10:00:00", "storage_note": "gate 1234",
    }
    row.update(over)
    return row


def cur(lot_id, channel, state, **over) -> dict:
    return {"lot_id": lot_id, "channel": channel, "state": state, "url": None, "payload_hash": None, **over}


# ─────────────────────────── desired_state ───────────────────────────

@pytest.mark.parametrize("status", inventory.CATALOG_FEED_STATUSES)
def test_sellable_statuses_are_live(status):
    assert sync.desired_state(lot(status=status), "site") == "live"


@pytest.mark.parametrize("over", [
    {"status": "sold_out"}, {"status": "lost_sold_out"}, {"status": "hidden"}, {"status": "draft"},
    {"quantity_remaining": 0}, {"quantity_remaining": None}, {"fake_sold_out": True},
])
def test_unsellable_rows_are_delisted(over):
    assert sync.desired_state(lot(**over), "fb_catalog") == "delisted"


# ─────────────────────────── payload_hash ───────────────────────────

def test_hash_changes_with_price_not_with_updated_at():
    base = sync.payload_hash(lot(), "site")
    assert sync.payload_hash(lot(updated_at="2027-01-01T00:00:00"), "site") == base
    assert sync.payload_hash(lot(price_per_chair=25), "site") != base
    assert sync.payload_hash(lot(quantity_remaining=1199), "site") != base
    assert len(base) == 40   # sha1 hex


def test_hash_ignores_storage_note():
    assert sync.payload_hash(lot(storage_note="other"), "site") == sync.payload_hash(lot(), "site")


def test_hash_is_per_channel():
    assert sync.payload_hash(lot(), "site") != sync.payload_hash(lot(), "ebay")


# ─────────────────────────── plan ───────────────────────────

def _ops(actions, channel):
    return {a.lot_id: a.op for a in actions if a.channel == channel}


def test_plan_lists_when_desired_live_and_current_off():
    acts = sync.plan([lot()], {})
    assert Action("31225", "site", "list") in acts
    assert all(a.op == "list" for a in acts)
    assert {a.channel for a in acts} == set(channels.CHANNELS)


def test_plan_updates_when_live_and_hash_differs_noop_when_equal():
    row = lot()
    same = cur("31225", "site", "live", payload_hash=sync.payload_hash(row, "site"))
    drift = cur("31225", "ebay", "live", payload_hash="stale")
    acts = sync.plan([row], {("31225", "site"): same, ("31225", "ebay"): drift})
    assert _ops(acts, "site") == {"31225": "noop"}
    assert _ops(acts, "ebay") == {"31225": "update"}


def test_plan_delists_a_live_row_when_lot_stops_being_sellable():
    acts = sync.plan([lot(quantity_remaining=0)], {("31225", "site"): cur("31225", "site", "live")})
    assert _ops(acts, "site") == {"31225": "delist"}
    # a channel it was never on stays a noop — nothing to take down
    assert _ops(acts, "ebay") == {"31225": "noop"}


def test_plan_delists_pending_and_queued_rows_too():
    current = {("31225", "fb_marketplace"): cur("31225", "fb_marketplace", "pending_approval"),
               ("31225", "craigslist"): cur("31225", "craigslist", "queued")}
    acts = sync.plan([lot(fake_sold_out=True)], current)
    assert _ops(acts, "fb_marketplace") == {"31225": "delist"}
    assert _ops(acts, "craigslist") == {"31225": "delist"}


def test_plan_waits_on_pending_approval_and_retries_error():
    current = {("31225", "fb_marketplace"): cur("31225", "fb_marketplace", "pending_approval"),
               ("31225", "ebay"): cur("31225", "ebay", "error")}
    acts = sync.plan([lot()], current)
    assert _ops(acts, "fb_marketplace") == {"31225": "noop"}
    assert _ops(acts, "ebay") == {"31225": "list"}


def test_plan_lists_an_approved_queued_row():
    acts = sync.plan([lot()], {("31225", "fb_marketplace"): cur("31225", "fb_marketplace", "queued")})
    assert _ops(acts, "fb_marketplace") == {"31225": "list"}


# ─────────────────────────── run_once ───────────────────────────

class _Adapter:
    """A sync-callable push adapter (what Phase 3's eBay API adapter will be)."""
    platform = "ebay"

    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def publish_lot(self, row):
        self.calls.append(("publish", row["lot_id"]))
        if self.fail:
            raise RuntimeError("token expired")
        return {"url": "https://ebay.com/itm/1", "external_id": "1"}

    def unpublish_lot(self, row, current):
        self.calls.append(("unpublish", row["lot_id"]))


@pytest.fixture
def world(monkeypatch):
    """Offline collaborators. `w.rows` / `w.current` / `w.enabled` / `w.adapters` are knobs;
    `w.upserts` / `w.states` record what the engine wrote."""
    class W:
        rows = [lot()]
        current: dict = {}
        enabled = {c: True for c in channels.CHANNELS}
        settings = {"browser_channel_daily_cap": 4, "browser_channel_spacing_s": 1200}
        adapters: dict = {}
        recent: list = []
        upserts: list = []
        states: list = []

    w = W()
    monkeypatch.setattr(sync.inventory, "list_all", lambda: list(w.rows))
    monkeypatch.setattr(sync.store, "current_map", lambda: dict(w.current))
    monkeypatch.setattr(sync.store, "recent_synced_at", lambda channel, since: list(w.recent))

    def fake_upsert(lot_id, channel, *, state, **kw):
        w.upserts.append((lot_id, channel, state, kw))
        return {"lot_id": lot_id, "channel": channel, "state": state, **kw}

    def fake_set_state(lot_id, channel, state, *, last_error=None):
        w.states.append((lot_id, channel, state, last_error))
        return {"lot_id": lot_id, "channel": channel, "state": state, "last_error": last_error}

    monkeypatch.setattr(sync.store, "upsert", fake_upsert)
    monkeypatch.setattr(sync.store, "set_state", fake_set_state)
    monkeypatch.setattr(sync.site_settings, "channel_enabled", lambda c: bool(w.enabled.get(c)))
    monkeypatch.setattr(sync.site_settings, "get_all", lambda: dict(w.settings))
    monkeypatch.setattr(sync.registry, "get", lambda key: w.adapters.get(key))
    return w


def _writes(w, channel):
    return [(u[0], u[2], u[3]) for u in w.upserts if u[1] == channel]


def test_feed_channels_just_record_state(world):
    world.enabled = {c: c in channels.FEED_CHANNELS for c in channels.CHANNELS}
    rep = sync.run_once(now=NOW)
    for c in channels.FEED_CHANNELS:
        (lot_id, state, kw), = _writes(world, c)
        assert (lot_id, state) == ("31225", "live")
        assert kw["payload_hash"] == sync.payload_hash(lot(), c)
    assert _writes(world, "site")[0][2]["url"].endswith("/listings/31225")
    assert rep["applied"] == 3
    assert rep["skipped"]["disabled"] == 3
    assert world.states == []


def test_feed_update_writes_new_hash(world):
    world.enabled = {c: c == "site" for c in channels.CHANNELS}
    world.current = {("31225", "site"): cur("31225", "site", "live", payload_hash="stale")}
    sync.run_once(now=NOW)
    (_, state, kw), = _writes(world, "site")
    assert state == "live" and kw["payload_hash"] == sync.payload_hash(lot(), "site")


def test_feed_delist_when_sold(world):
    world.enabled = {c: c == "fb_catalog" for c in channels.CHANNELS}
    world.rows = [lot(status="sold_out")]
    world.current = {("31225", "fb_catalog"): cur("31225", "fb_catalog", "live")}
    sync.run_once(now=NOW)
    assert _writes(world, "fb_catalog")[0][1] == "delisted"


def test_approval_channel_queues_instead_of_calling_adapter(world):
    world.enabled = {c: c == "fb_marketplace" for c in channels.CHANNELS}
    fb = _Adapter(); fb.platform = "fb"
    world.adapters = {"fb": fb}
    sync.run_once(now=NOW)
    assert fb.calls == []
    assert world.states == [("31225", "fb_marketplace", "pending_approval", None)]
    assert world.upserts == []


def test_approved_queued_row_calls_adapter_and_goes_live(world):
    world.enabled = {c: c == "fb_marketplace" for c in channels.CHANNELS}
    fb = _Adapter(); fb.platform = "fb"
    world.adapters = {"fb": fb}
    world.current = {("31225", "fb_marketplace"): cur("31225", "fb_marketplace", "queued")}
    sync.run_once(now=NOW)
    assert fb.calls == [("publish", "31225")]
    (_, state, kw), = _writes(world, "fb_marketplace")
    assert state == "live" and kw["url"] == "https://ebay.com/itm/1" and kw["external_id"] == "1"


def test_push_adapter_success_and_failure(world):
    world.enabled = {c: c == "ebay" for c in channels.CHANNELS}
    world.adapters = {"ebay": _Adapter()}
    rep = sync.run_once(now=NOW)
    (_, state, kw), = _writes(world, "ebay")
    assert state == "live" and kw["url"] == "https://ebay.com/itm/1" and kw["payload_hash"]
    assert rep["applied"] == 1 and rep["errors"] == 0

    world.upserts.clear()
    world.adapters = {"ebay": _Adapter(fail=True)}
    rep = sync.run_once(now=NOW)
    (_, state, kw), = _writes(world, "ebay")
    assert state == "error" and "token expired" in kw["last_error"]
    assert rep["errors"] == 1


def test_push_without_adapter_records_error_not_silence(world):
    world.enabled = {c: c == "ebay" for c in channels.CHANNELS}
    world.adapters = {}
    sync.run_once(now=NOW)
    (_, state, kw), = _writes(world, "ebay")
    assert state == "error" and "adapter" in kw["last_error"]


def test_push_delist_uses_unpublish_or_flags_manual(world):
    world.enabled = {c: c in ("ebay", "craigslist") for c in channels.CHANNELS}
    world.rows = [lot(quantity_remaining=0)]
    world.current = {("31225", "ebay"): cur("31225", "ebay", "live"),
                     ("31225", "craigslist"): cur("31225", "craigslist", "live")}
    ebay = _Adapter()
    world.adapters = {"ebay": ebay}          # no craigslist adapter
    sync.run_once(now=NOW)
    assert ebay.calls == [("unpublish", "31225")]
    assert _writes(world, "ebay")[0][1] == "delisted"
    assert ("31225", "craigslist", "delisted", "manual delist required") in world.states


def test_disabled_channel_is_skipped_entirely(world):
    world.enabled = {c: False for c in channels.CHANNELS}
    world.adapters = {"ebay": _Adapter()}
    rep = sync.run_once(now=NOW)
    assert world.upserts == [] and world.states == []
    assert rep["applied"] == 0
    assert rep["skipped"]["disabled"] == len(channels.CHANNELS)


def test_master_off_means_every_channel_disabled(world):
    """`channel_enabled` already folds in the master switch — the engine asks it, not the raw dict."""
    world.enabled = {c: False for c in channels.CHANNELS}
    world.settings = {**world.settings, "channels_master_enabled": 0, "channel_site_enabled": 1}
    rep = sync.run_once(now=NOW)
    assert rep["applied"] == 0


def test_dry_run_plans_but_writes_nothing(world):
    world.adapters = {"ebay": _Adapter()}
    rep = sync.run_once(now=NOW, dry_run=True)
    assert rep["planned"] == len(channels.CHANNELS)
    assert rep["applied"] == 0
    assert world.upserts == [] and world.states == []
    assert world.adapters["ebay"].calls == []


def test_browser_daily_cap_is_honoured(world):
    world.enabled = {c: c == "craigslist" for c in channels.CHANNELS}
    cl = _Adapter(); cl.platform = "craigslist"
    world.adapters = {"craigslist": cl}
    world.settings["browser_channel_daily_cap"] = 2
    world.recent = [NOW - timedelta(hours=3), NOW - timedelta(hours=5)]   # two already today
    rep = sync.run_once(now=NOW)
    assert cl.calls == []
    assert rep["skipped"]["daily_cap"] == 1
    assert world.upserts == []


def test_browser_spacing_is_honoured(world):
    world.enabled = {c: c == "craigslist" for c in channels.CHANNELS}
    cl = _Adapter(); cl.platform = "craigslist"
    world.adapters = {"craigslist": cl}
    world.settings["browser_channel_spacing_s"] = 1200
    world.recent = [NOW - timedelta(seconds=600)]   # last post 10 min ago, need 20
    rep = sync.run_once(now=NOW)
    assert cl.calls == []
    assert rep["skipped"]["spacing"] == 1


def test_browser_cap_counts_this_runs_posts_too(world):
    """Two lots, cap 1: the second list in the same tick must wait for tomorrow."""
    world.enabled = {c: c == "craigslist" for c in channels.CHANNELS}
    cl = _Adapter(); cl.platform = "craigslist"
    world.adapters = {"craigslist": cl}
    world.settings["browser_channel_daily_cap"] = 1
    world.rows = [lot(), lot(lot_id="9006")]
    rep = sync.run_once(now=NOW)
    assert len(cl.calls) == 1
    assert rep["skipped"]["daily_cap"] == 1


def test_browser_pacing_does_not_gate_delists(world):
    """Taking a sold lot down is never rate-limited."""
    world.enabled = {c: c == "craigslist" for c in channels.CHANNELS}
    cl = _Adapter(); cl.platform = "craigslist"
    world.adapters = {"craigslist": cl}
    world.settings["browser_channel_daily_cap"] = 0
    world.rows = [lot(quantity_remaining=0)]
    world.current = {("31225", "craigslist"): cur("31225", "craigslist", "live")}
    sync.run_once(now=NOW)
    assert cl.calls == [("unpublish", "31225")]


def test_approved_fb_marketplace_uses_post_to_facebook_when_no_sync_adapter(world, monkeypatch):
    """The registry's `fb` adapter is Playwright-async; an approved row falls back to
    the same `lot_channels.post_to_facebook` the /list-lot skill uses."""
    from automation import lot_channels
    world.enabled = {c: c == "fb_marketplace" for c in channels.CHANNELS}
    world.adapters = {}
    world.current = {("31225", "fb_marketplace"): cur("31225", "fb_marketplace", "queued")}
    calls = []

    def fake_post(lot_id, **kw):
        calls.append(lot_id)
        return "https://www.facebook.com/marketplace/item/777/", None

    monkeypatch.setattr(lot_channels, "post_to_facebook", fake_post)
    sync.run_once(now=NOW)
    assert calls == ["31225"]
    (_, state, kw), = _writes(world, "fb_marketplace")
    assert state == "live" and kw["external_id"] == "777"


def test_fb_marketplace_post_failure_lands_in_error(world, monkeypatch):
    from automation import lot_channels
    world.enabled = {c: c == "fb_marketplace" for c in channels.CHANNELS}
    world.current = {("31225", "fb_marketplace"): cur("31225", "fb_marketplace", "queued")}
    monkeypatch.setattr(lot_channels, "post_to_facebook", lambda lot_id, **kw: (None, "no Chrome profile"))
    rep = sync.run_once(now=NOW)
    (_, state, kw), = _writes(world, "fb_marketplace")
    assert state == "error" and "no Chrome profile" in kw["last_error"]
    assert rep["errors"] == 1


def test_unapproved_fb_marketplace_never_posts(world, monkeypatch):
    """Switch on, but the row is `off`: it parks in pending_approval; post_to_facebook is not called."""
    from automation import lot_channels
    world.enabled = {c: c == "fb_marketplace" for c in channels.CHANNELS}
    monkeypatch.setattr(lot_channels, "post_to_facebook", lambda *a, **k: pytest.fail("must not post"))
    sync.run_once(now=NOW)
    assert world.states == [("31225", "fb_marketplace", "pending_approval", None)]


def test_run_once_never_hands_storage_note_to_an_adapter(world):
    world.enabled = {c: c == "ebay" for c in channels.CHANNELS}
    seen = {}

    class Spy(_Adapter):
        def publish_lot(self, row):
            seen["row"] = row
            return super().publish_lot(row)

    world.adapters = {"ebay": Spy()}
    sync.run_once(now=NOW)
    assert "storage_note" not in seen["row"]
