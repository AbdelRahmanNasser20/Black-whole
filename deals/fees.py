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

# Per-site buyer-premium defaults (deals/sites.py keys).
SITE_BUYER_PREMIUM = {"govdeals": 0.125, "allsurplus": 0.125,
                      "publicsurplus": 0.10}

def fee_model_for_site(site: str, env: dict | None = None) -> FeeModel:
    """FeeModel with the buyer premium picked per site.

    Precedence: DEALS_BUYER_PREMIUM_PCT_<SITE> env var > an explicitly set
    global DEALS_BUYER_PREMIUM_PCT > the SITE_BUYER_PREMIUM table > the
    global 0.125 default. Tax/freight stay the global env knobs."""
    env = env if env is not None else os.environ
    premium = env.get(f"DEALS_BUYER_PREMIUM_PCT_{site.upper()}")
    if premium is None:
        premium = env.get("DEALS_BUYER_PREMIUM_PCT")
    if premium is None:
        premium = SITE_BUYER_PREMIUM.get(site, 0.125)
    return FeeModel(
        buyer_premium_pct=float(premium),
        tax_pct=float(env.get("DEALS_TAX_PCT", "0")),
        freight=float(env.get("DEALS_FREIGHT", "0")),
    )
