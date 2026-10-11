"""AllSurplus (Liquidity Services' international marketplace) — `source='allsurplus'`.

Same maestro host, key and payload shapes as GovDeals (recorder/sources/
govdeals.py), with businessId "GI" (verified live 2026-10-02):

- search: a top-level `"businessId": "GI"` in the search body returns only GI
  lots — ~1,470 live, 13 pages of 120, USA/GBR/DEU/ZAF/POL/CHN/FRA, priced in
  USD/EUR/GBP/ZAR/CNY/AUD/BRL. `currencyCode` rides on every asset.
- detail: `POST /assets/{asset}/{account}/false` needs `{"businessId": "GI"}`
  (with "GD" it answers an empty shell).
- bidbox: `/bids/bidbox/{GI|GD}/{a}/{b}/{c}` ignores the segment;
  `premiumPercent` is 18 on the sample; `currencyCode` is null there, so the
  currency always comes from the search/detail payload.
- lot page: https://www.allsurplus.com/en/asset/{asset}/{account}

Everything else — bidbox finals, the 15-min grace, the absence corroboration,
the 7-day SOA re-check, the ≤1 req/s maestro throttle — is GovDealsSource's,
inherited unchanged. The whole GI catalog is small, so `discover` always sweeps
all of it (no furniture scope), ≤ RECORDER_ALLSURPLUS_MAX_PAGES (40) pages.

**Never aggregate final prices across currencies**: `raw.currencyCode` is the
lot's currency; comps and the `sold_comps` view (migration 019) filter on it.
"""
from __future__ import annotations

from recorder.models import Observation
from recorder.sources import govdeals as gd
from recorder.sources.base import SourceFetchFailed

SOURCE = gd.ALLSURPLUS_SOURCE
MAX_PAGES_DEFAULT = 40


def max_pages() -> int:
    return gd._env_int("RECORDER_ALLSURPLUS_MAX_PAGES", MAX_PAGES_DEFAULT)


class AllSurplusSource(gd.GovDealsSource):
    SOURCE = SOURCE
    BUSINESS_ID = "GI"

    def discover(self, scope_override: str | None = None) -> list[Observation]:
        adapter = self._make_adapter()
        pages = max_pages()
        lots, ok = gd._safe_discover(adapter, category_ids="", search_text="",
                                     max_pages=pages, label="allsurplus sweep")
        obs = {}
        for lot in lots:
            o = gd._lot_to_observation(lot)
            if o.source != SOURCE:   # never record a non-GI asset under allsurplus
                continue
            obs[o.source_lot_id] = o
        if not ok and not obs:
            print("[allsurplus] RECORDER ERROR: discover() aborted — sweep failed, 0 observations")
            raise SourceFetchFailed("discover() aborted — GI sweep failed")
        if not obs:
            print("[allsurplus] WARNING: discover() found 0 GI lots — check the maestro search for drift")
            return []
        reqs = getattr(adapter, "requests", None)
        print(f"[allsurplus] discover lots={len(obs)} requests={reqs if reqs is not None else '?'} "
              f"max_pages={pages}")
        if len(lots) >= pages * 120:
            print(f"[allsurplus] WARNING: sweep hit RECORDER_ALLSURPLUS_MAX_PAGES={pages} — raise the cap")
        return list(obs.values())
