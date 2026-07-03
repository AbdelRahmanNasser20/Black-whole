from datetime import datetime, timezone
from deals.models import Lot
from deals.categories import canonical_category

IMAGE_BASE = "https://webassets.lqdt1.com/assets/photos/"

def _utc(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)

def _f(v) -> float:
    try: return float(v)
    except (TypeError, ValueError): return 0.0

def _price(raw: dict) -> float:
    """Parse the tracked price strictly. currentBid is the single most
    safety-critical field — a missing/unparseable value must fail loudly, never
    silently become 0.0 (which would also mislabel the lot as free)."""
    cb = raw.get("currentBid")
    if cb is None:
        raise ValueError(f"currentBid missing for asset {raw.get('assetId')!r}")
    try:
        return float(cb)
    except (TypeError, ValueError) as e:
        raise ValueError(f"currentBid unparseable ({cb!r}) for asset {raw.get('assetId')!r}") from e

def _hero_url(account_id: int, photo: str) -> str:
    # The CDN serves photos under an account-id subfolder:
    # /assets/photos/{account_id}/{photo}. Omitting the subfolder 404s
    # (verified live). Matches govdeals_chairs_extraction IMAGE_CDN_BASE usage.
    return f"{IMAGE_BASE}{account_id}/{photo}" if photo else ""

def asset_to_lot(raw: dict) -> Lot:
    current = _price(raw)
    photo = raw.get("photo") or ""
    return Lot(
        asset_id=int(raw["assetId"]), account_id=int(raw["accountId"]),
        auction_id=int(raw.get("auctionId") or 0),
        title=(raw.get("assetShortDescription") or "").strip(),
        description=(raw.get("assetLongDescription") or "").strip(),
        native_category_id=str(raw.get("assetCategory") or ""),
        native_category_name=(raw.get("categoryDescription") or "").strip(),
        canonical_category=canonical_category(str(raw.get("assetCategory") or "")),
        end_utc=_utc(raw["assetAuctionEndDateUtc"]),
        bid_count=int(raw.get("bidCount") or 0),
        opening_bid=_f(raw.get("assetBidPrice")),
        current_bid=current,
        currency_code=(raw.get("currencyCode") or "USD").strip().upper(),
        high_bidder=int(raw.get("highBidder") or 0),
        has_reserve=bool(raw.get("hasReservePrice")),
        reserve_not_met=bool(raw.get("isReserveNotMet")),
        reserve_price=(_f(raw["assetStrikePrice"]) if raw.get("assetStrikePrice") is not None else None),
        is_free=bool(raw.get("isFreeAsset")) or current <= 0,
        seller=(raw.get("companyName") or "").strip(),
        city=(raw.get("locationCity") or "").strip(),
        state=(raw.get("locationState") or "").strip(),
        zip=(raw.get("locationZip") or "").strip(),
        lat=raw.get("latitude"), lng=raw.get("longitude"),
        hero_image_url=_hero_url(int(raw["accountId"]), photo),
        status=(raw.get("assetStatusCd") or "").strip(),
        is_sold=bool(raw.get("isSoldAuction")),
        raw=raw,
    )
