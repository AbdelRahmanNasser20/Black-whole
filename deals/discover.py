from dataclasses import dataclass
from datetime import datetime, timedelta
from deals.adapters.base import SiteAdapter
from deals.classify import apply_classification
from deals.store import upsert_lot, set_poll_schedule
from deals.watcher_logic import schedule_lane, next_poll_delay
from deals.archive import archive_lot_images

@dataclass
class DiscoveryReport:
    discovered: int = 0; upserted: int = 0; classified: int = 0; archived: int = 0

def run_discovery(adapter: SiteAdapter, *, categories: list[str], classify: bool = True,
                  archive_candidates: bool = True, now: datetime | None = None) -> DiscoveryReport:
    now = now or datetime.now().astimezone()
    rep = DiscoveryReport()
    for category in categories:
        for lot in adapter.discover(category_ids=category):
            rep.discovered += 1
            if classify and (lot.canonical_category in ("general_merchandise", "other")):
                apply_classification(lot); rep.classified += 1
            upsert_lot(lot); rep.upserted += 1
            lane = schedule_lane(lot.end_utc, now)
            delay = next_poll_delay(lot.end_utc, now, lane)
            set_poll_schedule((lot.asset_id, lot.account_id, lot.auction_id),
                              now + timedelta(seconds=delay), lane.value)
            if archive_candidates and lot.bid_count == 0 and not lot.is_free \
               and lot.canonical_category == "seating_furniture":
                archive_lot_images(lot, adapter.fetch_gallery(lot.asset_id, lot.account_id))
                rep.archived += 1
    return rep
