"""Site registry: one SiteSpec per auction marketplace the deal tracker knows.

Every lot-page URL in deals/ is built here — call-sites must never rebuild the
f-string by hand (GovDeals arg order is asset-then-account; swapped = HTTP 204).
Ordinals are permanent (they feed synth_ids account_id = -ordinal):
1=govdeals, 2=publicsurplus, 3=bidspotter (reserved), 4=marknet,
5=gsa, 6=hibid, 7=municibid, 8=purplewave (assigned in the Phase 2 plan),
9=allsurplus (same maestro API as GovDeals, businessId "GI"; recorder-only
for now — enabled=False keeps it out of the deals crons), 10=txauction
(Gaston & Sheehan, SSR __APOLLO_STATE__ pages; native_id "<auction>/<lot>").
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


def _allsurplus():
    from deals.adapters.govdeals import GovDealsAdapter
    return GovDealsAdapter(business_id="GI")


def _txauction():
    from deals.adapters.txauction import TXAuctionAdapter
    return TXAuctionAdapter()


def _tx_lot_url(l) -> str:
    from deals.adapters.txauction import lot_url
    return lot_url(l.native_id)


def _publicsurplus():
    from deals.adapters.publicsurplus import PublicSurplusAdapter
    return PublicSurplusAdapter()


SITES: dict[str, SiteSpec] = {
    "govdeals": SiteSpec("govdeals", "GovDeals", 1, _govdeals,
        lambda l: f"https://www.govdeals.com/en/asset/{l.asset_id}/{l.account_id}", enabled=True),
    "publicsurplus": SiteSpec("publicsurplus", "Public Surplus", 2, _publicsurplus,
        lambda l: f"https://www.publicsurplus.com/sms/auction/view?auc={l.native_id}", enabled=False),
    "allsurplus": SiteSpec("allsurplus", "AllSurplus", 9, _allsurplus,
        lambda l: f"https://www.allsurplus.com/en/asset/{l.asset_id}/{l.account_id}", enabled=False),
    "txauction": SiteSpec("txauction", "TXAuction", 10, _txauction, _tx_lot_url, enabled=True),
}


def get_adapter(key: str):
    return SITES[key].make_adapter()


def _site_by_account(account_id) -> str | None:
    """Foreign rows carry account_id = -ordinal (models.synth_ids); a row
    without a `site` column (the deal_candidates view) is still attributable."""
    try:
        acct = int(account_id)
    except (TypeError, ValueError):
        return None
    if acct >= 0:
        return None
    return next((k for k, s in SITES.items() if s.ordinal == -acct), None)


def lot_url(lot) -> str:
    """Accepts a Lot or a fetch_all dict row (digest/relist/alerts build from rows)."""
    if isinstance(lot, dict):
        site = lot.get("site") or _site_by_account(lot.get("account_id")) or "govdeals"
        lot = SimpleNamespace(site=site, **{k: v for k, v in lot.items() if k != "site"})
    return SITES[lot.site].lot_url(lot)


def enabled_sites() -> list[str]:
    return [k for k, s in SITES.items() if s.enabled]
