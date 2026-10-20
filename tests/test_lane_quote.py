"""Offline tests for `automation/lane_quote.py` and `scripts/lane_quote.py`.

No network, no DB: `warp_rates.market_options` is a recorder. What is pinned:
the shipment sizing the lot's chair data drives, the buyer's quote (cheapest
carrier + padding, whole dollars, one number, the carrier's transit days, the
estimator kept for the audit trail), the operator result (every carrier,
unpadded), and the CLI's arguments and exit codes.
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from automation import freight_estimate, lane_quote, site_settings, warp_rates

OPTIONS = [
    {"carrier_name": "Averitt Express", "price_usd": 589.51, "transit_days": 3, "is_warp": False},
    {"carrier_name": "Saia LTL Freight", "price_usd": 625.512, "transit_days": 2, "is_warp": False},
    {"carrier_name": "XPO", "price_usd": 701.0, "transit_days": 4, "is_warp": False},
    {"carrier_name": "Warp truck", "price_usd": 6598.0, "transit_days": 2, "mode_substituted": True},
    {"carrier_name": "no price", "price_usd": None, "transit_days": 2},
    {"carrier_name": "FedEx", "price_usd": 612.0, "transit_days": 2},
    {"carrier_name": "R+L", "price_usd": 640.0, "transit_days": 3},
    {"carrier_name": "ABF", "price_usd": 655.0, "transit_days": 3},
]
EST = {
    "mode": "ltl", "recommended_mode": "ltl", "ltl_low": 1080.0, "ltl_high": 1520.0,
    "partial_low": None, "partial_high": None, "miles": 912, "transit_days": 3,
    "valid_until": "2026-10-27", "accessorials": {"residential": True, "liftgate": True},
    "provider": "estimator", "raw": {"provider": "estimator", "nmfc_class": 175},
}
SUMMARY = warp_rates.summarize(OPTIONS, limit=None)


@pytest.fixture
def wire(monkeypatch):
    seen = {"calls": [], "options": OPTIONS, "raise": None}

    def fake(origin, dest, **kw):
        seen["calls"].append((origin, dest, kw))
        if seen["raise"]:
            raise seen["raise"]
        return list(seen["options"])

    monkeypatch.setattr(warp_rates, "market_options", fake)
    monkeypatch.setattr(site_settings, "get_all", lambda: {**site_settings.defaults(), "freight_padding_pct": 15.0})
    return seen


# ───────────────────────────── input + sizing ────────────────────────────────

@pytest.mark.parametrize("bad", ["3003", "", None, "K1A 0B1", "303033", " 1608"])
def test_zip_must_be_five_digits_never_padded(bad):
    with pytest.raises(lane_quote.LaneInput):
        lane_quote.clean_zip(bad)


def test_zip_accepts_leading_zeros():
    assert lane_quote.clean_zip(" 04270 ") == "04270"


@pytest.mark.parametrize("bad", [0, -1, "x", None, "1.5"])
def test_quantity_must_be_a_positive_whole_number(bad):
    with pytest.raises(lane_quote.LaneInput):
        lane_quote.clean_quantity(bad)


def test_calibration_overrides_sit_on_top_of_the_lot():
    row = {"lot_id": "x", "chair_weight_lb": 17, "chairs_per_pallet": 40, "pallet_height_in": 72}
    cal = lane_quote.calibration(row)
    assert (cal.lbs_per_chair, cal.chairs_per_pallet, cal.pallet_height_in) == (17, 40, 72)
    assert cal.lbs_per_chair_estimated is False
    cal = lane_quote.calibration(row, lbs_per_chair=11, pallet_height_in="80")
    assert (cal.lbs_per_chair, cal.chairs_per_pallet, cal.pallet_height_in) == (11, 40, 80)
    std = lane_quote.calibration(None)
    assert std == freight_estimate.DEFAULT_CALIBRATION
    with pytest.raises(lane_quote.LaneInput):
        lane_quote.calibration(None, chairs_per_pallet=-3)


def test_shipment_sizes_pallets_from_the_chair_data():
    ship = lane_quote.shipment(100, lane_quote.calibration(None))
    # 100 / 35 → 3 pallets; 100 × 13 / 3 + 40 = 473 lb; standard 80 in
    assert (ship["pallets"], ship["weight_lbs_per_pallet"], ship["height_in"]) == (3, 473, 80)
    assert ship["too_big"] is False and ship["lbs_per_chair_estimated"] is True
    ship = lane_quote.shipment(100, lane_quote.calibration(None, lbs_per_chair=11, chairs_per_pallet=40, pallet_height_in=90))
    assert (ship["pallets"], ship["weight_lbs_per_pallet"], ship["height_in"]) == (3, 407, 85)   # height clamped
    assert lane_quote.shipment(500, lane_quote.calibration(None))["too_big"] is True


def test_lane_key_is_the_ask():
    ship = lane_quote.shipment(100, lane_quote.calibration(None))
    key = lane_quote.lane_key("04270", "28206", ship, warp_rates.delivery_accessorials(liftgate=False))
    assert key == ("04270", "28206", 3, 473, 80, ("residential-delivery",))


@pytest.mark.parametrize("res, lift, env", [
    (True, True, "residential"), (False, False, "dock"), (False, True, "storage"), (True, False, "residential"),
])
def test_delivery_env_for_the_estimator(res, lift, env):
    assert lane_quote.delivery_env(residential=res, liftgate=lift) == env


# ─────────────────────────────── fetch + result ──────────────────────────────

def test_fetch_options_sends_the_sized_shipment_and_the_drop(wire):
    cal = lane_quote.calibration(None)
    out = lane_quote.fetch_options("04270", "28206", 100, cal, liftgate=False, timeout=7)
    assert out == OPTIONS
    ((origin, dest, kw),) = wire["calls"]
    assert (origin, dest) == ("04270", "28206")
    assert kw == {"pallets": 3, "weight_lbs_per_pallet": 473, "height_in": 80,
                  "timeout": 7, "delivery": ("residential-delivery",)}


def test_fetch_options_refuses_a_load_too_big_for_ltl(wire):
    with pytest.raises(lane_quote.LaneInput):
        lane_quote.fetch_options("04270", "28206", 500, lane_quote.calibration(None))
    assert wire["calls"] == []


def test_result_lists_every_usable_carrier_unpadded_with_the_estimator_beside():
    out = lane_quote.result("04270", "28206", 100, lane_quote.calibration(None),
                            residential=True, liftgate=True, summary=SUMMARY, padding_pct=15)
    assert [c["carrier"] for c in out["carriers"]] == [
        "Averitt Express", "FedEx", "Saia LTL Freight", "R+L", "ABF", "XPO"]   # 6 usable of 8
    assert out["carrier_count"] == 6 and out["carrier_status"] == "ok"
    assert out["cheapest"]["price_usd"] == 589.51
    assert out["padded_price"] == 678 and out["padding_pct"] == 15
    assert out["estimator"]["mode"] == "ltl" and out["estimator"]["ltl_low"] > 0
    assert out["estimator"]["miles"] > 500
    assert out["delivery_accessorials"] == ["liftgate-delivery", "residential-delivery"]
    assert out["error"] is None


def test_result_without_a_summary_says_why():
    out = lane_quote.result("04270", "28206", 500, lane_quote.calibration(None),
                            residential=False, liftgate=False, summary=None, padding_pct=15)
    assert out["carrier_status"] == "too_big" and out["carriers"] == [] and out["padded_price"] is None
    assert out["delivery_accessorials"] == []
    out = lane_quote.result("04270", "28206", 100, lane_quote.calibration(None),
                            residential=True, liftgate=True, summary=None, padding_pct=15,
                            error="warp HTTP 503")
    assert out["carrier_status"] == "error" and out["error"] == "warp HTTP 503"
    assert out["estimator"] is not None            # the comparison column still prints


def test_result_estimator_is_none_for_a_refused_lane():
    out = lane_quote.result("04270", "96801", 100, lane_quote.calibration(None),
                            residential=True, liftgate=True, summary=SUMMARY, padding_pct=15)
    assert out["estimator"] is None and out["carriers"]


# ──────────────────────────────── buyer quote ────────────────────────────────

def test_buyer_quote_is_one_padded_whole_dollar_number():
    q = lane_quote.buyer_quote(EST, SUMMARY, 15)
    assert q["provider"] == "warp"
    assert q["ltl_low"] == q["ltl_high"] == 678          # ceil(589.51 × 1.15)
    assert q["partial_low"] is None and q["mode"] == q["recommended_mode"] == "ltl"
    assert q["transit_days"] == 3                       # Averitt's
    assert q["miles"] == 912 and q["valid_until"] == "2026-10-27"
    assert q["carrier"] is SUMMARY
    assert q["without_liftgate"] is None
    assert q["raw"]["estimator"] == {"mode": "ltl", "ltl_low": 1080.0, "ltl_high": 1520.0,
                                     "partial_low": None, "partial_high": None}
    assert q["raw"]["carrier_low"] == 589.51 and q["raw"]["padding_pct"] == 15
    assert q["raw"]["nmfc_class"] == 175                # the estimator's internals stay


@pytest.mark.parametrize("pct, price", [(0, 590), (10, 649), (15, 678), (33.3, 786)])
def test_padding_rounds_up_to_the_dollar(pct, price):
    assert lane_quote.buyer_quote(EST, SUMMARY, pct)["ltl_low"] == price


def test_buyer_quote_is_none_without_a_usable_carrier():
    assert lane_quote.buyer_quote(EST, warp_rates.summarize([]), 15) is None
    assert lane_quote.buyer_quote(EST, warp_rates.unavailable("error"), 15) is None
    assert lane_quote.buyer_quote(EST, {"carrier_status": "ok", "carrier_low": 0}, 15) is None


def test_no_liftgate_price_only_when_cheaper():
    cheaper = warp_rates.summarize([{"carrier_name": "A", "price_usd": 480.0, "transit_days": 3}])
    q = lane_quote.buyer_quote(EST, SUMMARY, 15, no_liftgate=cheaper)
    assert q["without_liftgate"] == 552 and q["raw"]["price_without_liftgate"] == 552
    assert q["raw"]["carrier_low_without_liftgate"] == 480.0
    dearer = warp_rates.summarize([{"carrier_name": "A", "price_usd": 600.0, "transit_days": 3}])
    assert lane_quote.buyer_quote(EST, SUMMARY, 15, no_liftgate=dearer)["without_liftgate"] is None
    assert lane_quote.buyer_quote(EST, SUMMARY, 15, no_liftgate=warp_rates.summarize([]))["without_liftgate"] is None


# ─────────────────────────────────── CLI ────────────────────────────────────

def _cli():
    path = Path(__file__).resolve().parents[1] / "scripts" / "lane_quote.py"
    spec = importlib.util.spec_from_file_location("lane_quote_cli", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["lane_quote_cli"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_cli_quotes_a_lane_and_prints_every_carrier(wire, capsys):
    cli = _cli()
    assert cli.main(["--origin", "04270", "--dest", "28206", "--qty", "100"]) == 0
    out = capsys.readouterr().out
    assert "LANE 04270 → 28206 · 100 chairs · 3 pallets × 473 lb · 80 in · residential + liftgate" in out
    assert "CARRIERS (6)" in out and "Averitt Express" in out and "XPO" in out
    assert "$589.51" in out and "SITE WOULD SHOW $678 (+15%)" in out
    assert "ESTIMATOR $" in out
    assert "Warp truck" not in out                       # a substituted option is never listed
    ((_o, _d, kw),) = wire["calls"]
    assert kw["delivery"] == ("liftgate-delivery", "residential-delivery")
    assert kw["timeout"] == warp_rates.TIMEOUT_SEC


def test_cli_chair_and_drop_flags(wire):
    cli = _cli()
    cli.main(["--origin", "04270", "--dest", "28206", "--qty", "100",
              "--lb", "11", "--per-pallet", "40", "--height", "80", "--no-liftgate", "--timeout", "9"])
    ((_o, _d, kw),) = wire["calls"]
    assert kw == {"pallets": 3, "weight_lbs_per_pallet": 407, "height_in": 80,
                  "timeout": 9.0, "delivery": ("residential-delivery",)}
    cli.main(["--origin", "04270", "--dest", "28206", "--qty", "100", "--dock"])
    assert wire["calls"][-1][2]["delivery"] == ()


def test_cli_json_and_padding(wire, capsys):
    cli = _cli()
    assert cli.main(["--origin", "04270", "--dest", "28206", "--qty", "100", "--padding", "20", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["padded_price"] == 708 and data["padding_pct"] == 20.0
    assert len(data["carriers"]) == 6 and data["cheapest"]["carrier"] == "Averitt Express"


def test_cli_lot_prefills_origin_quantity_and_chair_data(wire, monkeypatch):
    from automation import inventory
    row = {"lot_id": "bs-1", "zip_code": "04270", "quantity_remaining": 360,
           "chair_weight_lb": 11, "chairs_per_pallet": 40, "pallet_height_in": 72}
    monkeypatch.setattr(inventory, "get", lambda lot_id: dict(row) if lot_id == "bs-1" else None)
    cli = _cli()
    assert cli.main(["--lot", "bs-1", "--dest", "28206"]) == 0
    ((origin, _d, kw),) = wire["calls"]
    assert origin == "04270"
    assert (kw["pallets"], kw["weight_lbs_per_pallet"], kw["height_in"]) == (9, 480, 72)
    assert cli.main(["--lot", "nope", "--dest", "28206"]) == 2


@pytest.mark.parametrize("argv", [
    ["--origin", "4270", "--dest", "28206", "--qty", "100"],      # 4-digit origin
    ["--origin", "04270", "--dest", "K1A0B1", "--qty", "100"],
    ["--origin", "04270", "--dest", "28206", "--qty", "0"],
    ["--dest", "28206", "--qty", "100"],                          # no origin and no lot
])
def test_cli_bad_input_is_exit_2_without_a_call(wire, argv, capsys):
    assert _cli().main(argv) == 2
    assert wire["calls"] == []
    assert "error:" in capsys.readouterr().err


def test_cli_no_carrier_is_exit_1_with_the_estimate(wire, capsys):
    wire["raise"] = warp_rates.WarpUnavailable("warp HTTP 503")
    assert _cli().main(["--origin", "04270", "--dest", "28206", "--qty", "100"]) == 1
    out = capsys.readouterr().out
    assert "CARRIERS  none — warp HTTP 503" in out and "ESTIMATOR $" in out
    wire["raise"] = None
    assert _cli().main(["--origin", "04270", "--dest", "28206", "--qty", "500"]) == 1   # too big: no call
    assert len(wire["calls"]) == 1


def test_cli_never_calls_the_single_quote_endpoint():
    src = (Path(__file__).resolve().parents[1] / "scripts" / "lane_quote.py").read_text()
    code = src.split('"""', 2)[2]                      # past the module docstring, which NAMES the rule
    assert "/ltl/quote" not in code and "urllib" not in code
    assert "market_options" not in code                # Warp is reached through lane_quote.fetch_options only
