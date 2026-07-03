from datetime import timezone
import pytest
from deals.mapping import asset_to_lot, IMAGE_BASE

def _raw(**over):
    base = dict(assetId=984, accountId=6466, auctionId=2,
        assetShortDescription="2000 Freightliner", assetLongDescription="long...",
        assetCategory="372", categoryDescription="Furniture and Furnishings",
        assetAuctionEndDateUtc="2026-07-03T13:08:09Z",
        bidCount=6, assetBidPrice=2500.0, currentBid=3555.0, currencyCode="USD",
        highBidder=99, hasReservePrice=False, isReserveNotMet=False, assetStrikePrice=None,
        isFreeAsset=False, companyName="City of X", locationCity="Warren",
        locationState="ME", locationZip="04864", latitude=44.1, longitude=-69.2,
        photo="7224_3559_abc.jpg?cb=1", assetStatusCd="STA", isSoldAuction=False)
    base.update(over); return base

def test_price_is_current_bid_not_opening():
    lot = asset_to_lot(_raw())
    assert lot.current_bid == 3555.0
    assert lot.opening_bid == 2500.0        # kept as reference, never used as price

def test_end_utc_is_tz_aware_utc():
    lot = asset_to_lot(_raw())
    assert lot.end_utc.tzinfo == timezone.utc
    assert lot.end_utc.hour == 13 and lot.end_utc.minute == 8

def test_zero_bid_lot_flags_no_bid_even_when_price_equals_opening():
    lot = asset_to_lot(_raw(bidCount=0, currentBid=10.0, assetBidPrice=10.0))
    assert lot.bid_count == 0               # no-bid comes from bid_count, not price equality

def test_free_or_zero_price_flagged():
    assert asset_to_lot(_raw(currentBid=0.0)).is_free is True
    assert asset_to_lot(_raw(isFreeAsset=True)).is_free is True
    assert asset_to_lot(_raw()).is_free is False

def test_non_usd_currency_preserved():
    assert asset_to_lot(_raw(currencyCode="CAD")).currency_code == "CAD"

def test_hero_image_url_prefixed():
    lot = asset_to_lot(_raw(photo="a_b_c.jpg?cb=1"))
    assert lot.hero_image_url == IMAGE_BASE + "a_b_c.jpg?cb=1"

def test_missing_photo_yields_empty_image():
    assert asset_to_lot(_raw(photo=None)).hero_image_url == ""

def test_canonical_category_assigned():
    assert asset_to_lot(_raw(assetCategory="47B")).canonical_category == "seating_furniture"

def test_missing_current_bid_raises():
    raw = _raw()
    del raw["currentBid"]
    with pytest.raises(ValueError):
        asset_to_lot(raw)

def test_nonnumeric_current_bid_raises():
    with pytest.raises(ValueError):
        asset_to_lot(_raw(currentBid="N/A"))

def test_reserve_price_zero_is_preserved_not_nulled():
    lot = asset_to_lot(_raw(assetStrikePrice=0.0))
    assert lot.reserve_price == 0.0
