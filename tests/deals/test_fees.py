import pytest
from deals.fees import FeeModel, landed_cost

def test_landed_cost_adds_premium_tax_and_freight():
    fees = FeeModel(buyer_premium_pct=0.125, tax_pct=0.07, freight=100.0)
    lc = landed_cost(current_bid=1000.0, qty=10, fees=fees)
    # 1000*1.125 = 1125; +7% tax on 1125 = 1203.75; +100 freight = 1303.75
    assert round(lc.total, 2) == 1303.75
    assert round(lc.per_unit, 3) == 130.375

def test_per_unit_guards_zero_quantity():
    lc = landed_cost(1000.0, qty=0, fees=FeeModel())
    assert lc.per_unit == lc.total          # qty<=0 -> treat as single unit, never divide by zero

def test_zero_fees_is_just_bid():
    lc = landed_cost(500.0, qty=5, fees=FeeModel())
    assert lc.total == 500.0 and lc.per_unit == 100.0

def test_fee_model_from_env_defaults(monkeypatch):
    from deals.fees import fee_model_from_env
    for k in ("DEALS_BUYER_PREMIUM_PCT", "DEALS_TAX_PCT", "DEALS_FREIGHT"):
        monkeypatch.delenv(k, raising=False)
    fm = fee_model_from_env()
    assert fm.buyer_premium_pct == 0.125
    assert fm.tax_pct == 0.0
    assert fm.freight == 0.0

def test_fee_model_from_env_overrides(monkeypatch):
    from deals.fees import fee_model_from_env
    monkeypatch.setenv("DEALS_BUYER_PREMIUM_PCT", "0.18")
    monkeypatch.setenv("DEALS_TAX_PCT", "0.07")
    monkeypatch.setenv("DEALS_FREIGHT", "40")
    fm = fee_model_from_env()
    assert fm.buyer_premium_pct == 0.18
    assert fm.tax_pct == 0.07
    assert fm.freight == 40.0
