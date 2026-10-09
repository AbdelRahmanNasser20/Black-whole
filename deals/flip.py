# deals/flip.py
"""GovAuctions-style flip analysis. Pure math, no LLM, no network, no DB.

Reconstructs the "Lot Analyst" card GovAuctions Pro shows per lot (their exact
math is paywalled; this is reverse-engineered from a UI capture): comp
final-bid spread P25/median/P75, "max bid for est. X% margin" at the 25/50/100
targets, a demand read, a projected closing price, and a 0-100 Flip Score.

Our iterations beyond GovAuctions:
- est_resale arrives already bulk-discounted (deals/valuation.py applies the
  bulk recovery tier — they price single items, we buy 300-chair lots).
- Max bid inverts the FULL landed-cost model (buyer premium × tax + flat
  freight from deals/fees.py), not just a flat ~10% buyer premium.
- flip_score hard-caps method='llm_estimate' at LLM_SCORE_CAP: the LLM is
  never the price, so an LLM guess must never look comp-grounded.
- projected_close is our own conservative definition (GovAuctions blurs
  theirs): demand picks the spread anchor, and it never dips below the
  current bid. The spread is per-comp (usually per-unit) final prices, so on
  a bulk lot it reads low against the whole-lot bid — treat it as a floor.
"""
import statistics
from dataclasses import dataclass

from deals.fees import FeeModel
from deals.valuation import HIGH_COMPS, MIN_COMPS

TARGET_MARGINS = (25, 50, 100)   # the GovAuctions target-margin picker
LLM_SCORE_CAP = 49               # llm_estimate verdicts never score comp-grounded

# demand thresholds (documented, deliberately simple)
HOT_BIDS = 8                     # that many bids = contested, whatever the clock
LATE_HOT_BIDS = 3                # ≥3 bids with <6h left also reads hot
LATE_HOURS = 6.0


@dataclass
class CompSpread:
    p25: float
    p50: float
    p75: float


def comp_spread(prices: list[float]) -> CompSpread | None:
    """P25/median/P75 of comp final prices; None under 3 prices.

    statistics.quantiles(..., method='inclusive') = classic linear
    interpolation over the sorted sample — deterministic."""
    if len(prices) < 3:
        return None
    q = statistics.quantiles(sorted(prices), n=4, method="inclusive")
    return CompSpread(round(q[0], 2), round(q[1], 2), round(q[2], 2))


def max_bid_for_margin(est_resale: float, target_margin_pct: float,
                       fees: FeeModel) -> float:
    """The bid at which landed cost leaves target_margin_pct of margin.

    Inverts fees.landed_cost: landed_max = est_resale / (1 + m), then
    bid = (landed_max − freight) / ((1+premium)(1+tax)), floored at 0.
    target_margin_pct is in percent (50 = 50%)."""
    landed_max = est_resale / (1 + target_margin_pct / 100.0)
    bid = (landed_max - fees.freight) / (
        (1 + fees.buyer_premium_pct) * (1 + fees.tax_pct))
    return round(max(bid, 0.0), 2)


def demand(bid_count: int, hours_left: float | None) -> str:
    """'cold' | 'warm' | 'hot'. cold = 0 bids; hot = ≥HOT_BIDS bids, or
    ≥LATE_HOT_BIDS bids with under LATE_HOURS h left; everything else warm."""
    if bid_count >= HOT_BIDS:
        return "hot"
    if (bid_count >= LATE_HOT_BIDS and hours_left is not None
            and hours_left < LATE_HOURS):
        return "hot"
    if bid_count == 0:
        return "cold"
    return "warm"


def projected_close(current_bid: float, comp: CompSpread | None,
                    demand: str) -> float | None:
    """Conservative projected closing price: demand picks the spread anchor
    (cold→p25, warm→p50, hot→p75), never below the current bid; None without
    a spread. Our own definition — GovAuctions blurs theirs."""
    if comp is None:
        return None
    anchor = {"cold": comp.p25, "warm": comp.p50, "hot": comp.p75}[demand]
    return round(max(current_bid, anchor), 2)


def _margin_curve(margin_pct: float) -> float:
    """Capped piecewise-linear margin term: 0% → 30, 100% → 70, 300%+ → 95.
    Negative margin slides down the same 0.4 slope and floors at 0."""
    if margin_pct <= 100:
        return max(0.0, 30.0 + margin_pct * 0.4)
    if margin_pct <= 300:
        return 70.0 + (margin_pct - 100) * 0.125
    return 95.0


def flip_score(margin_pct: float, comp_count: int, demand: str,
               method: str) -> int:
    """0-100 flip score. Weights: margin dominates (_margin_curve), + comps
    confidence bonus (≥HIGH_COMPS +5, ≥MIN_COMPS +2), − demand penalty (hot
    −10, warm −3 — hot lots get bid up). method='llm_estimate' is hard-capped
    at LLM_SCORE_CAP: the LLM is never the price."""
    bonus = 5 if comp_count >= HIGH_COMPS else 2 if comp_count >= MIN_COMPS else 0
    penalty = {"hot": 10, "warm": 3, "cold": 0}[demand]
    score = int(round(_margin_curve(margin_pct) + bonus - penalty))
    score = max(0, min(100, score))
    if method == "llm_estimate":
        score = min(score, LLM_SCORE_CAP)
    return score


@dataclass
class FlipAnalysis:
    score: int
    spread: CompSpread | None
    demand: str
    projected_close: float | None
    max_bids: dict[int, float]      # target margin % -> max bid

    def as_dict(self) -> dict:
        return {
            "score": self.score,
            "spread": ({"p25": self.spread.p25, "p50": self.spread.p50,
                        "p75": self.spread.p75} if self.spread else None),
            "demand": self.demand,
            "projected_close": self.projected_close,
            "max_bid": {str(m): b for m, b in self.max_bids.items()},
        }


def analyze_flip(*, est_resale: float, margin_pct: float, comp_count: int,
                 method: str, comp_prices: list[float], bid_count: int,
                 hours_left: float | None, current_bid: float,
                 fees: FeeModel) -> FlipAnalysis:
    """Bundle the whole card for one lot. est_resale/margin_pct come from the
    Valuation (already bulk-discounted); comp_prices are the kept comps."""
    spread = comp_spread(comp_prices)
    d = demand(bid_count, hours_left)
    return FlipAnalysis(
        score=flip_score(margin_pct, comp_count, d, method),
        spread=spread,
        demand=d,
        projected_close=projected_close(current_bid, spread, d),
        max_bids={m: max_bid_for_margin(est_resale, m, fees)
                  for m in TARGET_MARGINS},
    )
