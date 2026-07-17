import sys, os, uuid, requests
from datetime import datetime
from typing import Iterator
from deals.models import Lot, Snapshot, lot_key
from deals.mapping import asset_to_lot, photo_paths_to_urls

# reuse the proven maestro key-resolver + constants from the existing extractor
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "auction_extractors"))
import govdeals_chairs_extraction as _g   # noqa: E402

class GovDealsAdapter:
    site = "govdeals"

    def _headers(self) -> dict:
        return {"x-api-key": _g._resolve_maestro_key(), "x-user-id": "-1",
                "x-api-correlation-id": str(uuid.uuid4()), "Content-Type": "application/json",
                "Origin": "https://www.govdeals.com", "Referer": "https://www.govdeals.com/",
                "User-Agent": _g._BROWSER_UA}

    def _search_page(self, category_ids: str, search_text: str, page: int, rows: int = 120) -> list[dict]:
        body = {"categoryIds": category_ids, "searchText": search_text, "isQAL": False,
                "page": page, "displayRows": rows, "sortField": "auctionclose", "sortOrder": "asc",
                "requestType": "search", "responseStyle": "fullResponse", "facets": [], "facetsFilter": ""}
        r = requests.post(f"{_g.MAESTRO_URL}{_g.MAESTRO_SEARCH_PATH}", json=body,
                          headers=self._headers(), timeout=30)
        r.raise_for_status()
        return r.json().get("assetSearchResults") or []

    def discover(self, *, category_ids: str = "", search_text: str = "",
                 max_pages: int = 60, end_before: datetime | None = None) -> Iterator[Lot]:
        for page in range(1, max_pages + 1):
            batch = self._search_page(category_ids, search_text, page)
            if not batch:
                return
            for raw in batch:
                try:
                    lot = asset_to_lot(raw)
                except ValueError as e:
                    print(f"[discover] skipping bad record {raw.get('assetId')}: {e}", file=sys.stderr)
                    continue
                if end_before is not None and lot.end_utc >= end_before:
                    continue
                yield lot

    def refetch(self, keys: list[tuple[int, int, int]]) -> dict[str, Snapshot]:
        wanted = {lot_key(*k) for k in keys}
        found: dict[str, Snapshot] = {}
        # sweep the auctionclose-sorted firehose until we've matched all wanted keys or run dry
        for page in range(1, 60):
            batch = self._search_page("", "", page)
            if not batch:
                break
            for raw in batch:
                try:
                    lot = asset_to_lot(raw)
                except ValueError as e:
                    print(f"[refetch] skipping bad record {raw.get('assetId')}: {e}", file=sys.stderr)
                    continue
                k = lot_key(lot.asset_id, lot.account_id, lot.auction_id)
                if k in wanted and k not in found:
                    found[k] = Snapshot(lot.asset_id, lot.account_id, lot.auction_id,
                                        datetime.now().astimezone(),
                                        lot.bid_count, lot.current_bid, lot.end_utc, lot.status)
            if len(found) == len(wanted):
                break
        return found

    def fetch_detail(self, asset_id: int, account_id: int) -> dict:
        """Per-lot detail from maestro. The body {businessId, siteId} is load-bearing:
        without it the endpoint still 200s but returns assetPhotos=[]."""
        r = requests.post(f"{_g.MAESTRO_URL}/assets/{asset_id}/{account_id}/false",
                          json={"businessId": "GD", "siteId": 1},
                          headers=self._headers(), timeout=30)
        r.raise_for_status()
        return r.json()

    def fetch_gallery(self, asset_id: int, account_id: int) -> list[str]:
        try:
            detail = self.fetch_detail(asset_id, account_id)
        except Exception as e:
            print(f"[gallery] fetch failed for {asset_id}/{account_id}: {e}", file=sys.stderr)
            return []
        return photo_paths_to_urls(detail.get("assetPhotos") or [])
