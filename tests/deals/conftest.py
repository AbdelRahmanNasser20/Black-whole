import pytest


@pytest.fixture(autouse=True)
def _no_live_relist_scan(monkeypatch):
    """run_discovery hooks deals.relist.scan_for_relists, which reads/writes the
    live deal_lots table. Unit tests must stay hermetic — the hook's behavior is
    covered by tests/deals/test_relist.py against the pure functions."""
    import deals.relist
    monkeypatch.setattr(deals.relist, "scan_for_relists", lambda now=None: 0)
