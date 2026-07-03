import os
from dataclasses import dataclass

@dataclass
class FeeModel:
    buyer_premium_pct: float = 0.0     # e.g. 0.125 = 12.5%
    tax_pct: float = 0.0
    freight: float = 0.0               # flat per-lot pickup/freight estimate

@dataclass
class LandedCost:
    total: float
    per_unit: float

def landed_cost(current_bid: float, qty: int, fees: FeeModel) -> LandedCost:
    with_premium = current_bid * (1 + fees.buyer_premium_pct)
    with_tax = with_premium * (1 + fees.tax_pct)
    total = with_tax + fees.freight
    per_unit = total / qty if qty and qty > 0 else total
    return LandedCost(total=total, per_unit=per_unit)

def fee_model_from_env() -> FeeModel:
    """FeeModel from DEALS_* env vars; defaults match the digest's 12.5% premium."""
    return FeeModel(
        buyer_premium_pct=float(os.getenv("DEALS_BUYER_PREMIUM_PCT", "0.125")),
        tax_pct=float(os.getenv("DEALS_TAX_PCT", "0")),
        freight=float(os.getenv("DEALS_FREIGHT", "0")),
    )
