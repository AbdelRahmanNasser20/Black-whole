from datetime import datetime, timezone

import pytest


@pytest.fixture
def make_lot():
    """Factory for a fully-populated Lot; override any field (incl. site/native_id)."""
    from deals.models import Lot

    def _make(**over):
        kw = dict(asset_id=984, account_id=6466, auction_id=2, title="t", description="d",
            native_category_id="372", native_category_name="Furniture and Furnishings",
            canonical_category="seating_furniture",
            end_utc=datetime(2026, 7, 3, 13, tzinfo=timezone.utc),
            bid_count=0, opening_bid=10.0, current_bid=10.0, currency_code="USD",
            high_bidder=0, has_reserve=False, reserve_not_met=False, reserve_price=None,
            is_free=False, seller="City", city="Warren", state="ME", zip="04864",
            lat=44.1, lng=-69.2, hero_image_url="http://x/y.jpg", status="STA",
            is_sold=False, raw={"a": 1})
        kw.update(over)
        return Lot(**kw)
    return _make


@pytest.fixture(autouse=True)
def _no_live_relist_scan(monkeypatch):
    """run_discovery hooks deals.relist.scan_for_relists, which reads/writes the
    live deal_lots table. Unit tests must stay hermetic — the hook's behavior is
    covered by tests/deals/test_relist.py against the pure functions."""
    import deals.relist
    monkeypatch.setattr(deals.relist, "scan_for_relists", lambda now=None: 0)
