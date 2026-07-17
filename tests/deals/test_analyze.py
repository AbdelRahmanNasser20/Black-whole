# tests/deals/test_analyze.py
from datetime import datetime, timezone
from deals.analyze import should_alert, format_verdict_alert, analyze_lot
from deals.comps import Comp, CompsResult, CompsUnavailable
from deals.fees import FeeModel
from deals.models import Lot

def _lot(**kw):
    base = dict(asset_id=1, account_id=2, auction_id=3, title="40 Steelcase Leap V2 Chairs",
        description="lot of 40", native_category_id="372", native_category_name="Furniture",
        canonical_category="seating_furniture",
        end_utc=datetime(2026, 7, 18, tzinfo=timezone.utc), bid_count=0, opening_bid=5.0,
        current_bid=5.0, currency_code="USD", high_bidder=0, has_reserve=False,
        reserve_not_met=False, reserve_price=None, is_free=False, seller="GSA",
        city="Richmond", state="VA", zip="23220", lat=37.5, lng=-77.4,
        hero_image_url="", status="active", is_sold=False)
    base.update(kw)
    return Lot(**base)

def test_should_alert_gates_margin_confidence_and_method():
    env = {"DEALS_ALERT_MIN_MARGIN_PCT": "100"}
    good = {"margin_pct": 250.0, "confidence": "medium", "method": "comps"}
    assert should_alert(good, env)
    assert not should_alert(good | {"margin_pct": 50.0}, env)
    assert not should_alert(good | {"confidence": "low"}, env)
    assert not should_alert(good | {"method": "llm_estimate"}, env)

def test_alert_text_has_essentials():
    v = {"est_resale": 1600.0, "margin": 1500.0, "margin_pct": 700.0,
         "landed_cost": 100.0, "confidence": "medium", "comp_count": 5,
         "comps": [{"url": "https://ebay.com/itm/9", "price": 100.0, "title": "leap"}]}
    text = format_verdict_alert(_lot(), v, distance=104.2)
    assert "Steelcase" in text and "700" in text and "104" in text
    assert "govdeals.com" in text and "ebay.com/itm/9" in text

class FakeComps:
    def __init__(self, result): self.result = result
    def fetch(self, q):
        if isinstance(self.result, Exception): raise self.result
        return self.result

def _ident(monkeypatch, qty=40):
    from deals import analyze, llm_steps
    ident = llm_steps.LotIdentity(brand="Steelcase", model="Leap V2",
        item_type="office chair", quantity=qty,
        queries=["steelcase leap v2"], est_resale_per_unit=150.0)
    monkeypatch.setattr(analyze, "extract_identity", lambda lot: ident)
    return ident

def test_comp_grounded_verdict(monkeypatch):
    from deals import analyze
    _ident(monkeypatch)
    comps = [Comp(str(i), "leap v2 chair", 100.0, None, "") for i in range(5)]
    monkeypatch.setattr(analyze, "judge_comps", lambda ident, c: comps)
    provider = FakeComps(CompsResult("q", 5, 100.0, comps, False))
    v = analyze_lot(_lot(), provider, FeeModel(), {})
    assert v["method"] == "comps" and v["comp_count"] == 5
    assert v["est_resale"] == 100.0 * 40 * 0.4

def test_degrades_when_comps_unavailable(monkeypatch):
    from deals import analyze
    _ident(monkeypatch)
    v = analyze_lot(_lot(), FakeComps(CompsUnavailable("down")), FeeModel(), {})
    assert v["method"] == "llm_estimate" and v["confidence"] == "low"
    assert v["est_resale"] == 150.0 * 40          # est_per_unit × qty (no discount claim)

def test_degrades_when_no_provider(monkeypatch):
    from deals import analyze
    _ident(monkeypatch)
    v = analyze_lot(_lot(), None, FeeModel(), {})
    assert v["method"] == "llm_estimate"
