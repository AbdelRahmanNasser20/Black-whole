"""Site registry: one SiteSpec per auction marketplace the deal tracker knows.

Every lot-page URL in deals/ is built here — call-sites must never rebuild the
f-string by hand (GovDeals arg order is asset-then-account; swapped = HTTP 204).
Ordinals are permanent (they feed synth_ids account_id = -ordinal):
1=govdeals, 2=publicsurplus (reserved), 3=bidspotter (reserved), 4=marknet.
"""
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Callable

from deals.models import Lot


@dataclass(frozen=True)
class SiteSpec:
    key: str; name: str; ordinal: int
    make_adapter: Callable[[], object]
    lot_url: Callable[[Lot], str]
    enabled: bool = False


def _govdeals():
    from deals.adapters.govdeals import GovDealsAdapter
    return GovDealsAdapter()


SITES: dict[str, SiteSpec] = {
    "govdeals": SiteSpec("govdeals", "GovDeals", 1, _govdeals,
        lambda l: f"https://www.govdeals.com/en/asset/{l.asset_id}/{l.account_id}", enabled=True),
}


def get_adapter(key: str):
    return SITES[key].make_adapter()


def lot_url(lot) -> str:
    """Accepts a Lot or a fetch_all dict row (digest/relist/alerts build from rows)."""
    if isinstance(lot, dict):
        lot = SimpleNamespace(site=lot.get("site") or "govdeals",
                              **{k: v for k, v in lot.items() if k != "site"})
    return SITES[lot.site].lot_url(lot)


def enabled_sites() -> list[str]:
    return [k for k, s in SITES.items() if s.enabled]
