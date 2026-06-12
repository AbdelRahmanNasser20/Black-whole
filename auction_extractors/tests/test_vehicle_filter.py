"""Vehicle-lot filter guards for the chair scrape pipeline.

The maestro JSON API token-matches the search term "chair" against
"Wheelchair" / "Wheel Chair", so wheelchair-accessible vans (and other
vehicles) come back as chair results. `_is_vehicle_lot` flags them so
`scrape_listings` can drop them before they reach the DB / Auctions tab.

Pure / no network. Run standalone:  python tests/test_vehicle_filter.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from govdeals_chairs_extraction import _is_vehicle_lot, _drop_vehicle_lots

# Every True case below is a real title scraped into Supabase on 2026-06-10.
VEHICLES = [
    "2016 Ford Econoline",
    "2014 Ford Metrolite Wheelchair  ACCESSIBLE VAN (5173)",
    "2014 Ford Metrolite Wheelchair ACCESSIBLE VAN(5187)",
    "2010 Ford/Coach Phoenix DRW Wheelchair ACCESSIBLE VAN (5001)",
    "2011 Ford/Coach Phoenix DRW Wheelchair  ACCESSIBLE VAN (5141)",
    "2018 Ford Econoline Wheel Chair Accesible Van (5244)",
    "2001 Ford F-350 SD XLT SuperCab Short Bed 4WD",
    "2021 Chevrolet Suburban RST 4X4 Fully Loaded!!",
    "2018 Freightliner M2 106 Extended Cab Chipper Dump Truck",
    "Freightliner MT55 Step Van Mobile Clinic Office",
    "2006 Ford Econoline",
    # Scraped 2026-06-12: "Concorde" isn't in the make list and "motorhome"
    # wasn't in the noun list, so this slipped through with quantity 400
    # (from "19,400 miles").
    "2005 Concorde wheelchair accessible motorhome.  19,400 miles, wheelchair lift",
    "1999 Winnebago RV with wheelchair ramp",
]

NOT_VEHICLES = [
    "Blue Conference/Banquet Chairs (370)",
    "Lot of 100 Virco Kinder Height Student Chairs",
    "UMF Medical 8678 Power Phlebotomy Chair",
    "Approximately 200 Banquet Chairs",
    "Ritter 204 Power Exam Table",
    "Wheelchairs - Lot of 12",          # wheelchair equipment, not a vehicle
    "2014 Herman Miller Aeron Chairs",  # year + non-automotive brand
    "Folding Chairs and Tables",
]


def run() -> int:
    failures = []
    for title in VEHICLES:
        if not _is_vehicle_lot(title):
            failures.append((title, "expected vehicle, got not-vehicle"))
    for title in NOT_VEHICLES:
        if _is_vehicle_lot(title):
            failures.append((title, "expected not-vehicle, got vehicle"))

    cards = [{"title": t} for t in VEHICLES + NOT_VEHICLES]
    kept = _drop_vehicle_lots(cards)
    kept_titles = {c["title"] for c in kept}
    if kept_titles != set(NOT_VEHICLES):
        failures.append(("_drop_vehicle_lots", f"kept {sorted(kept_titles)}"))

    for title, why in failures:
        print(f"FAIL  {title!r}: {why}")
    total = len(VEHICLES) + len(NOT_VEHICLES) + 1
    print(f"\n{total - len(failures)}/{total} passed")
    return 1 if failures else 0


def test_vehicle_detection():
    bad = [t for t in VEHICLES if not _is_vehicle_lot(t)]
    bad += [t for t in NOT_VEHICLES if _is_vehicle_lot(t)]
    assert not bad, f"vehicle detection wrong for: {bad}"


def test_drop_vehicle_lots():
    cards = [{"title": t} for t in VEHICLES + NOT_VEHICLES]
    kept = {c["title"] for c in _drop_vehicle_lots(cards)}
    assert kept == set(NOT_VEHICLES)


if __name__ == "__main__":
    raise SystemExit(run())
