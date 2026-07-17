# deals/valuation.py
"""Pure resale-valuation math. No LLM, no network, no DB.

The bid is not the cost (landed_cost adds premium/tax/freight) and the LLM
is never the price: comp-grounded valuations require >= 3 judged comps."""
import os
import statistics
from dataclasses import dataclass
from deals.comps import Comp
from deals.fees import FeeModel, landed_cost

BULK_QTY_THRESHOLD = 5
PIECE_OUT_FACTOR = 0.8
MIN_COMPS = 3
HIGH_COMPS = 8

@dataclass
class Valuation:
    method: str
    per_unit: float | None
    recovery_tier: float
    est_resale: float
    piece_out_ceiling: float | None
    landed_cost: float
    margin: float
    margin_pct: float
    confidence: str

def bulk_recovery_tier(quantity: int, env: dict | None = None) -> float:
    env = env if env is not None else os.environ
    if quantity <= BULK_QTY_THRESHOLD:
        return 1.0
    return float(env.get("DEALS_BULK_RECOVERY", "0.4"))

def _finish(method, per_unit, tier, est, ceiling, current_bid, quantity,
            fees, confidence) -> Valuation:
    lc = landed_cost(current_bid, quantity, fees).total
    margin = est - lc
    margin_pct = (margin / lc * 100) if lc > 0 else 0.0
    return Valuation(method, per_unit, tier, round(est, 2),
                     round(ceiling, 2) if ceiling is not None else None,
                     round(lc, 2), round(margin, 2), round(margin_pct, 1),
                     confidence)

def value_from_comps(kept: list[Comp], quantity: int, current_bid: float,
                     fees: FeeModel, env: dict | None = None) -> Valuation | None:
    if len(kept) < MIN_COMPS:
        return None
    per_unit = float(statistics.median(c.price for c in kept))
    tier = bulk_recovery_tier(quantity, env)
    est = per_unit * quantity * tier
    ceiling = per_unit * quantity * PIECE_OUT_FACTOR if tier < 1.0 else None
    confidence = "high" if len(kept) >= HIGH_COMPS else "medium"
    return _finish("comps", per_unit, tier, est, ceiling, current_bid,
                   quantity, fees, confidence)

def value_from_estimate(est_resale: float, quantity: int, current_bid: float,
                        fees: FeeModel) -> Valuation:
    return _finish("llm_estimate", None, 1.0, est_resale, None, current_bid,
                   quantity, fees, "low")
