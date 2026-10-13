# tests/deals/test_flip.py
import pytest
from deals.fees import FeeModel, fee_model_for_site, landed_cost
from deals.flip import (CompSpread, analyze_flip, comp_spread, demand,
                        flip_score, max_bid_for_margin, projected_close)

FEES = FeeModel(buyer_premium_pct=0.125, tax_pct=0.07, freight=100.0)

# ── comp_spread ──────────────────────────────────────────────────────────────

def test_spread_is_inclusive_linear_interpolation():
    s = comp_spread([40.0, 10.0, 30.0, 20.0])        # order must not matter
    assert (s.p25, s.p50, s.p75) == (17.5, 25.0, 32.5)

def test_spread_three_prices_and_under_three():
    s = comp_spread([10.0, 20.0, 30.0])
    assert (s.p25, s.p50, s.p75) == (15.0, 20.0, 25.0)
    assert comp_spread([10.0, 20.0]) is None
    assert comp_spread([]) is None

# ── max_bid_for_margin ───────────────────────────────────────────────────────

def test_max_bid_round_trips_through_landed_cost():
    for m in (25, 50, 100):
        bid = max_bid_for_margin(2000.0, m, FEES)
        lc = landed_cost(bid, 1, FEES).total
        assert lc * (1 + m / 100) == pytest.approx(2000.0, abs=0.05)

def test_max_bid_floors_at_zero():
    # est resale below freight alone -> no bid is profitable
    assert max_bid_for_margin(50.0, 50, FEES) == 0.0

def test_max_bid_without_fees_is_plain_margin_inversion():
    assert max_bid_for_margin(300.0, 50, FeeModel()) == 200.0

# ── demand ───────────────────────────────────────────────────────────────────

def test_demand_thresholds():
    assert demand(0, None) == "cold"
    assert demand(0, 1.0) == "cold"                   # 0 bids beats the clock
    assert demand(1, None) == "warm"
    assert demand(7, 48.0) == "warm"
    assert demand(8, None) == "hot"                   # many bids, any clock
    assert demand(3, 5.9) == "hot"                    # late action
    assert demand(3, 6.0) == "warm"
    assert demand(3, None) == "warm"                  # unknown clock stays warm

# ── projected_close ──────────────────────────────────────────────────────────

def test_projected_close_anchors_on_demand():
    s = CompSpread(p25=100.0, p50=200.0, p75=300.0)
    assert projected_close(10.0, s, "cold") == 100.0
    assert projected_close(10.0, s, "warm") == 200.0
    assert projected_close(10.0, s, "hot") == 300.0

def test_projected_close_never_below_current_bid():
    s = CompSpread(p25=100.0, p50=200.0, p75=300.0)
    assert projected_close(450.0, s, "cold") == 450.0

def test_projected_close_none_without_spread():
    assert projected_close(10.0, None, "cold") is None

# ── flip_score ───────────────────────────────────────────────────────────────

def test_flip_score_monotonic_in_margin():
    margins = [-100, -50, 0, 25, 50, 100, 200, 300, 500]
    scores = [flip_score(m, 5, "warm", "comps") for m in margins]
    assert scores == sorted(scores)
    assert all(0 <= s <= 100 for s in scores)

def test_flip_score_documented_anchors():
    assert flip_score(0, 0, "cold", "comps") == 30
    assert flip_score(100, 0, "cold", "comps") == 70
    assert flip_score(300, 0, "cold", "comps") == 95

def test_flip_score_comps_bonus_and_demand_penalty():
    base = flip_score(100, 0, "cold", "comps")
    assert flip_score(100, 3, "cold", "comps") == base + 2
    assert flip_score(100, 8, "cold", "comps") == base + 5
    assert flip_score(100, 0, "warm", "comps") == base - 3
    assert flip_score(100, 0, "hot", "comps") == base - 10

def test_llm_estimate_capped_at_49():
    assert flip_score(500, 0, "cold", "llm_estimate") == 49
    assert flip_score(0, 0, "hot", "llm_estimate") <= 49

# ── analyze_flip bundle ──────────────────────────────────────────────────────

def test_analyze_flip_as_dict_shape():
    fa = analyze_flip(est_resale=2000.0, margin_pct=250.0, comp_count=5,
                      method="comps", comp_prices=[80.0, 100.0, 120.0],
                      bid_count=0, hours_left=12.0, current_bid=10.0,
                      fees=FeeModel(buyer_premium_pct=0.125))
    d = fa.as_dict()
    assert set(d) == {"score", "spread", "demand", "projected_close", "max_bid"}
    assert set(d["max_bid"]) == {"25", "50", "100"}
    assert d["demand"] == "cold"
    assert d["spread"]["p50"] == 100.0
    assert d["projected_close"] == 90.0               # cold -> p25, above bid

def test_analyze_flip_degraded_without_comps():
    fa = analyze_flip(est_resale=2000.0, margin_pct=900.0, comp_count=0,
                      method="llm_estimate", comp_prices=[], bid_count=2,
                      hours_left=None, current_bid=10.0, fees=FeeModel())
    assert fa.spread is None and fa.projected_close is None
    assert fa.score <= 49

# ── fee_model_for_site ───────────────────────────────────────────────────────

def test_site_premium_defaults():
    assert fee_model_for_site("govdeals", {}).buyer_premium_pct == 0.125
    assert fee_model_for_site("publicsurplus", {}).buyer_premium_pct == 0.10
    assert fee_model_for_site("somethingnew", {}).buyer_premium_pct == 0.125

def test_site_premium_env_precedence():
    env = {"DEALS_BUYER_PREMIUM_PCT": "0.15",
           "DEALS_BUYER_PREMIUM_PCT_PUBLICSURPLUS": "0.08"}
    assert fee_model_for_site("publicsurplus", env).buyer_premium_pct == 0.08
    assert fee_model_for_site("govdeals", env).buyer_premium_pct == 0.15

def test_site_fee_model_keeps_global_tax_and_freight():
    fm = fee_model_for_site("govdeals", {"DEALS_TAX_PCT": "0.07",
                                         "DEALS_FREIGHT": "40"})
    assert (fm.tax_pct, fm.freight) == (0.07, 40.0)

# ── verdict row with/without the flip columns ────────────────────────────────

def test_verdict_row_with_and_without_flip_columns():
    from deals.verdict_store import FLIP_COLUMNS, VERDICT_COLUMNS, verdict_row
    v = {c: None for c in VERDICT_COLUMNS} | {
        "asset_id": 1, "flip": {"score": 82, "demand": "warm"},
        "flip_score": 82}
    base = verdict_row(v)
    assert len(base) == len(VERDICT_COLUMNS)          # flip keys ignored
    full = verdict_row(v, VERDICT_COLUMNS + FLIP_COLUMNS)
    assert len(full) == len(VERDICT_COLUMNS) + 2
    assert isinstance(full[-2], str)                  # flip json.dumps'd
    assert full[-1] == 82

def test_insert_verdict_gates_on_migration(monkeypatch):
    from deals import verdict_store
    executed = []
    monkeypatch.setattr(verdict_store.db, "execute",
                        lambda sql, params=None: executed.append((sql, params)))
    v = {c: None for c in verdict_store.VERDICT_COLUMNS} | {
        "flip": {"score": 1}, "flip_score": 1}

    monkeypatch.setattr(verdict_store, "_has_flip_columns", lambda: False)
    verdict_store.insert_verdict(v)
    assert "flip" not in executed[0][0]
    assert len(executed[0][1]) == len(verdict_store.VERDICT_COLUMNS)

    monkeypatch.setattr(verdict_store, "_has_flip_columns", lambda: True)
    verdict_store.insert_verdict(v)
    assert "flip,flip_score" in executed[1][0]
    assert len(executed[1][1]) == len(verdict_store.VERDICT_COLUMNS) + 2
