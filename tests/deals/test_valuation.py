# tests/deals/test_valuation.py
import pytest
from deals.comps import Comp
from deals.fees import FeeModel
from deals.valuation import (Valuation, bulk_recovery_tier,
                             value_from_comps, value_from_estimate)

FEES = FeeModel(buyer_premium_pct=0.125, tax_pct=0.0, freight=0.0)

def _comps(prices):
    return [Comp(str(i), f"comp {i}", p, None, "") for i, p in enumerate(prices)]

def test_single_item_full_recovery():
    assert bulk_recovery_tier(1, {}) == 1.0
    assert bulk_recovery_tier(5, {}) == 1.0

def test_bulk_default_tier_and_env_override():
    assert bulk_recovery_tier(40, {}) == 0.4
    assert bulk_recovery_tier(40, {"DEALS_BULK_RECOVERY": "0.5"}) == 0.5

def test_median_times_qty_times_tier():
    v = value_from_comps(_comps([50, 100, 150]), 40, 10.0, FEES, {})
    assert v.per_unit == 100.0
    assert v.est_resale == pytest.approx(100.0 * 40 * 0.4)
    assert v.piece_out_ceiling == pytest.approx(100.0 * 40 * 0.8)
    assert v.method == "comps"
    # landed: 10 * 1.125 = 11.25 → margin ≈ 1588.75
    assert v.margin == pytest.approx(v.est_resale - 11.25)
    # margin_pct is rounded to 1 decimal by the impl — allow for that
    assert v.margin_pct == pytest.approx(v.margin / 11.25 * 100, abs=0.05)

def test_confidence_scales_with_comp_count():
    assert value_from_comps(_comps([50] * 3), 1, 10, FEES, {}).confidence == "medium"
    assert value_from_comps(_comps([50] * 8), 1, 10, FEES, {}).confidence == "high"

def test_too_few_comps_returns_none():
    assert value_from_comps(_comps([50, 60]), 1, 10, FEES, {}) is None

def test_estimate_is_always_low_confidence():
    v = value_from_estimate(200.0, 1, 10.0, FEES)
    assert (v.method, v.confidence) == ("llm_estimate", "low")
    assert v.per_unit is None

def test_free_bid_zero_margin_pct_guard():
    v = value_from_estimate(200.0, 1, 0.0, FEES)   # landed cost 0
    assert v.margin_pct == 0.0                     # no div-by-zero
