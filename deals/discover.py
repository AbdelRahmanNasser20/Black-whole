import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable
from deals.adapters.base import SiteAdapter
from deals.models import Lot
from deals.classify import apply_classification
from deals.store import upsert_lot, set_poll_schedule, set_archived_images
from deals.watcher_logic import schedule_lane, next_poll_delay
from deals.archive import archive_lot_images

@dataclass
class DiscoveryReport:
    discovered: int = 0; upserted: int = 0; classified: int = 0; archived: int = 0; errors: int = 0
    classify_failed: int = 0

def _default_archive_gate(lot: Lot) -> bool:
    """Today's rule: only 0-bid seating lots get their images archived."""
    return lot.canonical_category == "seating_furniture"

def run_discovery(adapter: SiteAdapter, *, categories: list[str], classify: bool = True,
                  archive_candidates: bool = True, now: datetime | None = None,
                  max_pages: int = 60,
                  archive_predicate: Callable[[Lot], bool] | None = None) -> DiscoveryReport:
    """`archive_predicate` (research profile `matches`) replaces the seating
    gate for which 0-bid lots get archived; None = the seating rule."""
    now = now or datetime.now().astimezone()
    gate = archive_predicate or _default_archive_gate
    rep = DiscoveryReport()
    for category in categories:
        for lot in adapter.discover(category_ids=category, max_pages=max_pages):
            rep.discovered += 1
            try:
                if classify and (lot.canonical_category in ("general_merchandise", "other")):
                    apply_classification(lot)
                    # Count answers, not attempts. Counting attempts is how a
                    # dead API key reported "classified: 2,937" every night for
                    # weeks while storing nothing but nulls.
                    if lot.llm_category is not None:
                        rep.classified += 1
                    else:
                        rep.classify_failed += 1
                upsert_lot(lot); rep.upserted += 1
                lane = schedule_lane(lot.end_utc, now)
                delay = next_poll_delay(lot.end_utc, now, lane)
                set_poll_schedule((lot.asset_id, lot.account_id, lot.auction_id),
                                  now + timedelta(seconds=delay), lane.value)
                if archive_candidates and lot.bid_count == 0 and not lot.is_free and gate(lot):
                    stored = archive_lot_images(lot, adapter.fetch_gallery(lot.asset_id, lot.account_id))
                    if stored:
                        set_archived_images((lot.asset_id, lot.account_id, lot.auction_id), stored[0], stored[1:])
                    rep.archived += 1
            except Exception as e:
                rep.errors += 1
                print(f"[discover] error on lot {lot.asset_id}/{lot.account_id}: {e}", file=sys.stderr)
    try:
        from deals.relist import scan_for_relists
        scan_for_relists(now)
    except Exception as e:
        print(f"[discover] relist scan failed: {e}", file=sys.stderr)
    return rep
