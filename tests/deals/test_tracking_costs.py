"""Landed cost on the Tracking tab: qty, all-in total (bid + premium + fees +
tax), and price per chair. Numbers below are real bidbox reads from 2026-09-28."""
import pytest

from deals.quantity import chair_quantity
from deals.tracking import bidbox_costs, landed_costs


class TestChairQuantity:
    @pytest.mark.parametrize("title,qty", [
        ("LOT: (199) BANQUET CHAIRS", 199),
        ("Lot of (250) Banquet Chairs", 250),
        ("Folding Chairs qty 224, Folding Chair Ca", 224),
        ("Lot: Approx: (66) Tables and (294) Chairs", 294),
        # the conservative title parser misses these; the chair-noun fallback catches them
        ("150 Student Chairs", 150),
        ("120 Banquet Stacking Chairs - B6", 120),
        ("LOT of ~2500 Wire Frame Linkable CHAIRS", 2500),
        ("225 Stackable Sled Chairs with 10 Wheeled Carts", 225),
    ])
    def test_counts(self, title, qty):
        assert chair_quantity(title)[0] == qty

    @pytest.mark.parametrize("title", ["Sled Chairs", "Metal folding chairs",
                                       "LOT: Metal Framed Stackable Event Chairs", None, ""])
    def test_unknown_is_none_not_one(self, title):
        # a made-up 1 would turn the lot total into a "per chair" price
        assert chair_quantity(title) == (None, "unknown")


def test_bidbox_costs_maps_the_fee_fields():
    raw = {"premiumPercent": 12.5, "adminFeeAmount": 0.0, "totalTaxAmount": 139.78,
           "grandTotalAmount": 2136.65, "state": "NC"}
    assert bidbox_costs(raw) == {"premium_pct": 12.5, "admin_fee": 0.0, "tax_total": 139.78,
                                 "grand_total": 2136.65, "lot_state": "NC"}


def _row(**over):
    base = dict(asset_id=1, account_id=1, title="Lot of (100) Chairs", quantity=None,
                closed_at=None, current_bid=None, final_bid=None, premium_pct=12.5,
                admin_fee=0, tax_total=0, grand_total=0, lot_state=None)
    return {**base, **over}


def test_sold_lot_uses_govdeals_exact_total():
    r = _row(asset_id=420, account_id=9312, title="LOT: (199) BANQUET CHAIRS",
             closed_at="x", final_bid=800, grand_total=900, lot_state="NV")
    c = landed_costs([r])[0]["cost"]
    assert c["basis"] == "exact"
    assert c["total"] == 900.0 and c["premium"] == 100.0 and c["tax"] == 0.0
    assert c["qty"] == 199 and c["per_chair"] == 4.52


def test_open_lot_borrows_the_same_sellers_tax_rate():
    sold = _row(asset_id=239, account_id=31465, closed_at="x", final_bid=1775,
                tax_total=139.78, grand_total=2136.65, lot_state="NC")
    live = _row(asset_id=999, account_id=31465, current_bid=1000, lot_state="NC")
    c = landed_costs([sold, live])[1]["cost"]
    assert c["basis"] == "est" and c["tax_from"] == "seller"
    # 1000 + 125 premium, taxed at NC's 7.0 %
    assert c["premium"] == 125.0
    assert c["tax"] == pytest.approx(78.75, abs=0.02)
    assert c["total"] == pytest.approx(1203.75, abs=0.02)
    assert c["per_chair"] == pytest.approx(12.04, abs=0.01)


def test_open_lot_falls_back_to_same_state_then_to_no_tax():
    sold = _row(asset_id=5282, account_id=3780, closed_at="x", final_bid=1725,
                tax_total=135.85, grand_total=2076.47, lot_state="PA")
    pa = _row(asset_id=2, account_id=7, current_bid=100, lot_state="PA")
    mo = _row(asset_id=3, account_id=8, current_bid=100, lot_state="MO")
    out = landed_costs([sold, pa, mo])
    assert out[1]["cost"]["tax_from"] == "state" and out[1]["cost"]["tax"] > 0
    mo_c = out[2]["cost"]
    assert mo_c["basis"] == "est_no_tax" and mo_c["tax"] is None
    assert mo_c["total"] == 112.5         # bid + premium only, flagged as missing tax


def test_manual_quantity_wins_and_unknown_qty_has_no_per_chair():
    r = _row(title="Sled Chairs", current_bid=500)
    assert landed_costs([r])[0]["cost"]["per_chair"] is None
    r["quantity"] = 50
    c = landed_costs([r])[0]["cost"]
    assert c["qty"] == 50 and c["qty_source"] == "manual" and c["per_chair"] == 11.25


def test_no_bid_lot_has_no_total():
    c = landed_costs([_row(closed_at="x", final_bid=None, current_bid=None)])[0]["cost"]
    assert c["total"] is None and c["per_chair"] is None


def test_missing_cost_columns_before_migration_still_estimates():
    # rows from a DB where 013 is not applied yet: none of the cost keys exist
    r = {"asset_id": 1, "account_id": 1, "title": "Lot of (100) Chairs",
         "closed_at": None, "current_bid": 200, "final_bid": None}
    c = landed_costs([r])[0]["cost"]
    assert c["premium"] == 25.0 and c["total"] == 225.0 and c["per_chair"] == 2.25


class TestCloseOutcome:
    @pytest.mark.parametrize("status,bids,want", [
        ("SOA", 54, "sold"), ("SOL", 1, "low_bid"), ("SOA", 0, "no_bid"),
        # 3357/527: 20 bids to $1,850, reserve not met — was stored as 'sold'
        ("RNM", 20, "reserve_not_met"), ("CNB", 20, "reserve_not_met"),
        ("CAN", 3, "cancelled"),
        ("STA", 5, "sold"),          # closed on the clock alone: status still live
    ])
    def test_status_decides_before_bid_count(self, status, bids, want):
        from deals.tracking import close_outcome
        assert close_outcome(status, bids) == want


@pytest.mark.parametrize("status,bids", [("RNM", 20), ("CAN", 3), ("SOA", 0)])
def test_unsold_closed_lot_has_no_all_in(status, bids):
    r = _row(closed_at="x", final_bid=1850, final_bid_count=bids, status=status, lot_state="MO")
    c = landed_costs([r])[0]["cost"]
    assert c["total"] is None and c["per_chair"] is None and c["basis"] == "not_sold"
