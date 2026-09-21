"""Phase 1.7 — `inventory.set_platform_url` mirrors marketplace URLs into `listing_channels`
(facebook → fb_marketplace, ebay → ebay). Offline: the ledger connection is a stub and the store
is monkeypatched. A store failure never breaks the ledger write."""
from __future__ import annotations

import pytest

from automation import inventory
from automation.channels import store


class _Conn:
    def __init__(self, log):
        self.log = log

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.log.append(sql)

    def commit(self):
        pass


@pytest.fixture
def _ledger(monkeypatch):
    sqls: list[str] = []
    monkeypatch.setattr(inventory, "connect", lambda *a, **k: _Conn(sqls))
    monkeypatch.setattr(inventory, "get", lambda lot_id: {"lot_id": lot_id})
    return sqls


@pytest.mark.parametrize("platform,channel", [("facebook", "fb_marketplace"), ("ebay", "ebay")])
def test_marketplace_url_mirrors_as_live(_ledger, monkeypatch, platform, channel):
    seen = []
    monkeypatch.setattr(store, "upsert", lambda *a, **k: seen.append((a, k)) or {})
    monkeypatch.setattr(store, "set_state", lambda *a, **k: pytest.fail("a url is a live listing, not a state flip"))
    inventory.set_platform_url("gd-1-2", platform, "https://x/item/1")
    assert seen == [(("gd-1-2", channel), {"state": "live", "url": "https://x/item/1"})]
    assert any("UPDATE inventory SET" in s for s in _ledger), "the ledger write still happens"


def test_clearing_the_url_turns_the_channel_off(_ledger, monkeypatch):
    seen = []
    monkeypatch.setattr(store, "set_state", lambda *a, **k: seen.append(a) or {})
    monkeypatch.setattr(store, "upsert", lambda *a, **k: pytest.fail("no url → nothing is live"))
    inventory.set_platform_url("gd-1-2", "facebook", None)
    assert seen == [("gd-1-2", "fb_marketplace", "off")]


def test_promotional_platforms_do_not_touch_channels(_ledger, monkeypatch):
    monkeypatch.setattr(store, "upsert", lambda *a, **k: pytest.fail("fb_business / ad are not channels"))
    monkeypatch.setattr(store, "set_state", lambda *a, **k: pytest.fail("fb_business / ad are not channels"))
    inventory.set_platform_url("gd-1-2", "fb_business", "https://facebook.com/page/post/1")
    inventory.set_platform_url("gd-1-2", "ad", "https://facebook.com/ads/1")


def test_store_failure_never_breaks_the_ledger_write(_ledger, monkeypatch, capsys):
    def boom(*a, **k):
        raise RuntimeError("relation listing_channels does not exist")
    monkeypatch.setattr(store, "upsert", boom)
    out = inventory.set_platform_url("gd-1-2", "ebay", "https://ebay.com/itm/1")
    assert out == {"lot_id": "gd-1-2"}
    assert "listing_channels" in capsys.readouterr().err
