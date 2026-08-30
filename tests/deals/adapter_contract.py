# tests/deals/adapter_contract.py
"""Every site adapter's fixture test calls check_lots(). An adapter an agent wrote
is trusted exactly as far as this contract."""
from deals.models import Lot


def check_lots(lots, *, site):
    assert lots, "adapter yielded nothing from its fixture"
    seen = set()
    for l in lots:
        assert isinstance(l, Lot) and l.site == site
        assert l.native_id and l.native_id not in seen; seen.add(l.native_id)
        assert l.title.strip(), f"empty title {l.native_id}"
        assert l.end_utc.tzinfo is not None, f"naive end_utc {l.native_id}"
        assert isinstance(l.current_bid, float), f"price must be float, got {type(l.current_bid)}"
        assert l.current_bid >= 0 and l.bid_count >= 0
        assert l.state == "" or len(l.state) == 2, f"bad state {l.state!r}"
        assert l.hero_image_url.startswith(("http", "")), "hero url malformed"
