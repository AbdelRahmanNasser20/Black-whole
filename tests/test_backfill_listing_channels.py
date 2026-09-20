"""Phase 1.8 — `scripts/backfill_listing_channels.py` seeds `listing_channels` from what
`inventory` already knows. Offline: inventory + store are monkeypatched. Properties:

  1. Feed channels (site, fb_catalog) take `sync.desired_state` — sellable stock is live,
     everything else delisted — and carry the payload hash so the first sync pass is a noop.
  2. fb_marketplace is live only for a real Marketplace item URL (`marketplace/item/`), never
     for a page post or an empty column; ebay is live wherever `ebay_url` is set.
  3. Dry-run (the default) writes nothing; `--apply` writes one upsert per planned row.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path("scripts/backfill_listing_channels.py")
spec = importlib.util.spec_from_file_location("backfill_listing_channels", SCRIPT)
bf = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = bf          # dataclasses resolve `from __future__ import annotations` via sys.modules
spec.loader.exec_module(bf)

LIVE = {"lot_id": "gd-1-2", "status": "owned", "quantity_remaining": 40, "fake_sold_out": False,
        "title": "Chairs", "price_per_chair": 15.0, "city": "Boise", "state": "ID",
        "facebook_url": "https://www.facebook.com/marketplace/item/123456789/", "ebay_url": None}
SOLD = {"lot_id": "gd-3-4", "status": "sold_out", "quantity_remaining": 0, "fake_sold_out": False,
        "title": "Gone", "facebook_url": "https://www.facebook.com/blackwhole/posts/999", "ebay_url": "https://ebay.com/itm/1"}


def _by_channel(plan):
    return {(p.lot_id, p.channel): p for p in plan}


def test_plan_feed_channels_follow_desired_state():
    got = _by_channel(bf.plan_rows([LIVE, SOLD]))
    assert got[("gd-1-2", "site")].state == "live"
    assert got[("gd-1-2", "site")].url.endswith("/listings/gd-1-2")
    assert got[("gd-1-2", "fb_catalog")].state == "live"
    assert got[("gd-3-4", "site")].state == "delisted"
    assert got[("gd-3-4", "fb_catalog")].state == "delisted"
    for p in got.values():
        if p.channel in ("site", "fb_catalog"):
            assert p.payload_hash, "hash stamped so the first sync pass is a noop"


def test_plan_marketplace_rows_only_for_real_item_urls():
    got = _by_channel(bf.plan_rows([LIVE, SOLD]))
    fb = got[("gd-1-2", "fb_marketplace")]
    assert fb.state == "live" and fb.external_id == "123456789" and fb.url == LIVE["facebook_url"]
    assert ("gd-3-4", "fb_marketplace") not in got, "a page post is not a Marketplace listing"
    assert ("gd-1-2", "ebay") not in got
    assert got[("gd-3-4", "ebay")].state == "live" and got[("gd-3-4", "ebay")].url == SOLD["ebay_url"]


def test_dry_run_writes_nothing(monkeypatch):
    monkeypatch.setattr(bf.inventory, "list_all", lambda: [LIVE, SOLD])
    monkeypatch.setattr(bf.store, "upsert", lambda *a, **k: pytest.fail("dry-run must not write"))
    summary = bf.run(apply=False)
    assert summary["planned"] == 6 and summary["written"] == 0


def test_apply_upserts_every_planned_row(monkeypatch):
    monkeypatch.setattr(bf.inventory, "list_all", lambda: [LIVE, SOLD])
    seen = []
    monkeypatch.setattr(bf.store, "upsert", lambda *a, **k: seen.append((a, k)) or {})
    summary = bf.run(apply=True)
    assert summary["planned"] == summary["written"] == len(seen) == 6
    args, kw = next(x for x in seen if x[0] == ("gd-1-2", "fb_marketplace"))
    assert kw["state"] == "live" and kw["external_id"] == "123456789"


def test_lot_filter(monkeypatch):
    monkeypatch.setattr(bf.inventory, "list_all", lambda: [LIVE, SOLD])
    monkeypatch.setattr(bf.store, "upsert", lambda *a, **k: {})
    summary = bf.run(apply=False, lot_id="gd-3-4")
    assert summary["planned"] == 3   # site, fb_catalog, ebay


def test_main_defaults_to_dry_run(monkeypatch, capsys):
    monkeypatch.setattr(bf.inventory, "list_all", lambda: [LIVE])
    monkeypatch.setattr(bf.store, "upsert", lambda *a, **k: pytest.fail("no --apply → no write"))
    assert bf.main([]) == 0
    out = capsys.readouterr().out
    assert "dry-run" in out and "--apply" in out
