from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

class Outcome(str, Enum):
    NO_BID = "no_bid"; LOW_BID = "low_bid"; SOLD = "sold"; UNKNOWN = "unknown"

class Lane(str, Enum):
    COLD = "cold"; WARM = "warm"; HOT = "hot"; DONE = "done"

def lot_key(asset_id: int, account_id: int, auction_id: int) -> str:
    return f"{asset_id}/{account_id}/{auction_id}"

@dataclass
class Lot:
    asset_id: int; account_id: int; auction_id: int
    title: str; description: str
    native_category_id: str; native_category_name: str
    canonical_category: str
    end_utc: datetime
    bid_count: int; opening_bid: float; current_bid: float; currency_code: str
    high_bidder: int
    has_reserve: bool; reserve_not_met: bool; reserve_price: float | None
    is_free: bool
    seller: str; city: str; state: str; zip: str
    lat: float | None; lng: float | None
    hero_image_url: str; status: str; is_sold: bool
    raw: dict = field(default_factory=dict)
    llm_category: str | None = None
    llm_category_confidence: float | None = None
    category_agreement: bool | None = None

@dataclass
class Snapshot:
    asset_id: int; account_id: int; auction_id: int
    observed_at: datetime
    bid_count: int; current_bid: float
    end_utc: datetime; status: str
