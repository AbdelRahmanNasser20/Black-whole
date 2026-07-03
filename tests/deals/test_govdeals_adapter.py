import json
from pathlib import Path
from unittest.mock import patch
from deals.adapters.govdeals import GovDealsAdapter
from deals.models import Lot

FIXTURE = json.loads(Path("tests/deals/fixtures/maestro_page.json").read_text())

def test_discover_yields_lots_from_a_page():
    a = GovDealsAdapter()
    # first call returns the fixture, second returns [] so pagination stops
    with patch.object(a, "_search_page", side_effect=[FIXTURE, []]):
        lots = list(a.discover(category_ids="372"))
    assert lots and all(isinstance(l, Lot) for l in lots)
    assert all(l.native_category_id == "372" for l in lots)

def test_discover_stops_at_end_before():
    a = GovDealsAdapter()
    with patch.object(a, "_search_page", side_effect=[FIXTURE, []]):
        lots = list(a.discover(category_ids="372",
                    end_before=min(
                        __import__("deals.mapping",fromlist=["asset_to_lot"]).asset_to_lot(x).end_utc
                        for x in FIXTURE)))
    assert lots == []                       # every fixture lot closes at/after the earliest -> all excluded

def test_refetch_maps_present_keys_and_omits_missing():
    a = GovDealsAdapter()
    first = FIXTURE[0]
    key = (int(first["assetId"]), int(first["accountId"]), int(first.get("auctionId") or 0))
    with patch.object(a, "_search_page", side_effect=[[first], []]):
        snaps = a.refetch([key, (999999, 1, 1)])
    from deals.models import lot_key
    assert lot_key(*key) in snaps
    assert lot_key(999999,1,1) not in snaps      # missing => dropped/closed

def test_discover_skips_malformed_record_without_aborting():
    a = GovDealsAdapter()
    good = FIXTURE[0]
    bad = dict(good); bad.pop("currentBid", None)   # would raise in asset_to_lot
    with patch.object(a, "_search_page", side_effect=[[bad, good], []]):
        lots = list(a.discover(category_ids="372"))
    assert len(lots) == 1                            # bad one skipped, good one kept
