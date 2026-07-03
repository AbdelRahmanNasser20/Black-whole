from typing import Protocol, Iterator, runtime_checkable
from datetime import datetime
from deals.models import Lot, Snapshot

@runtime_checkable
class SiteAdapter(Protocol):
    site: str
    def discover(self, *, category_ids: str = "", search_text: str = "",
                 max_pages: int = 60, end_before: datetime | None = None) -> Iterator[Lot]: ...
    def refetch(self, keys: list[tuple[int, int, int]]) -> dict[str, Snapshot]: ...
    def fetch_gallery(self, asset_id: int, account_id: int) -> list[str]: ...
