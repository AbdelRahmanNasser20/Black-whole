"""discover --dry-run prints Lots and writes nothing — the runbook's live
verification step must be safe against prod."""
import sys

import pytest

import deals.cli
import deals.sites
import deals.store


class _FakeAdapter:
    site = "govdeals"

    def discover(self, **kw):
        yield from self._lots

    def __init__(self, lots):
        self._lots = lots


def _boom(*a, **kw):
    raise AssertionError("dry-run wrote to the store")


def test_dry_run_prints_and_writes_nothing(monkeypatch, capsys, make_lot):
    lots = [make_lot(asset_id=5, account_id=9, auction_id=2, title="Chairs", current_bid=25.0)]
    monkeypatch.setattr(deals.sites, "get_adapter", lambda key: _FakeAdapter(lots))
    monkeypatch.setattr(deals.store, "upsert_lot", _boom)
    monkeypatch.setattr(deals.store, "set_poll_schedule", _boom)
    monkeypatch.setattr(deals.store, "set_archived_images", _boom)
    monkeypatch.setattr(sys, "argv", ["deals.cli", "discover", "--site", "govdeals", "--dry-run"])
    deals.cli.main()
    out = capsys.readouterr().out
    assert "govdeals:5/9/2" in out and "Chairs" in out and "25" in out


def test_dry_run_respects_limit(monkeypatch, capsys, make_lot):
    lots = [make_lot(asset_id=i, account_id=9, auction_id=2) for i in range(1, 6)]
    monkeypatch.setattr(deals.sites, "get_adapter", lambda key: _FakeAdapter(lots))
    monkeypatch.setattr(deals.store, "upsert_lot", _boom)
    monkeypatch.setattr(sys, "argv",
                        ["deals.cli", "discover", "--site", "govdeals", "--dry-run", "--limit", "2"])
    deals.cli.main()
    out = capsys.readouterr().out
    assert "govdeals:2/9/2" in out and "govdeals:3/9/2" not in out
