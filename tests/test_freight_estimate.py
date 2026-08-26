"""Offline tests for the vendored chair-freight estimator (`automation.freight_estimate`).

Ported from the CRM branch `BWCRM-19-feature-freight-quote-engine`
(`tests/test_freight_estimator.py`). Two things changed on the way in:

* The live-lane assertions are **no longer skippable**. The branch skipped them
  when the pgeocode dataset was missing; this repo commits its own ZIP-prefix
  centroid table (`automation/zip_centroids.py`), so the pinned Worcester→Boise
  lane is a hard gate — if the calibration or the centroids drift, CI fails.
* Estes is gone, and a carrier failure now falls back to the estimator rather
  than raising (the storefront must never lose a quotable lane to a flaky API).
  Lane failures — international, offshore, Alaska, bad ZIP, bad quantity — still
  raise, because inventing a number is the one thing this module may never do.

No network, no DB, no fixtures.
"""
from __future__ import annotations

import pytest

from automation import freight_estimate as fq

BOISE = fq.BOISE


@pytest.fixture(autouse=True)
def _no_carrier_keys(monkeypatch):
    """Default every test to the key-free estimator, whatever the dev's shell
    or .env happens to carry. Tests that want a carrier set the key themselves."""
    monkeypatch.delenv("WARP_API_KEY", raising=False)
    monkeypatch.delenv("WARP_ENV", raising=False)


# --------------------------------------------------------------------------- #
# Geometry / physics (pure)
# --------------------------------------------------------------------------- #
def test_density_is_constant_class_175():
    # Empirical: 13 lb / 2.78 ft^3 ≈ 4.7 lb/ft^3 -> class 175, independent of qty.
    d = fq.density_lb_ft3(150, BOISE)
    assert 4.0 < d < 6.0
    assert fq.density_lb_ft3(500, BOISE) == d   # density is per-chair, qty-independent
    assert fq.density_to_nmfc_class(d) == 175


def test_density_to_nmfc_class_bands():
    assert fq.density_to_nmfc_class(0.9) == 400
    assert fq.density_to_nmfc_class(3.5) == 250
    assert fq.density_to_nmfc_class(4.68) == 175
    assert fq.density_to_nmfc_class(9.0) == 110
    assert fq.density_to_nmfc_class(25.0) == 65
    assert fq.density_to_nmfc_class(50.0) == 60


def test_mode_selector_by_linear_feet():
    assert fq.select_mode(5.0) == "ltl"
    assert fq.select_mode(12.0) == "ltl"
    assert fq.select_mode(13.0) == "both"
    assert fq.select_mode(14.0) == "partial"
    assert fq.select_mode(20.0) == "partial"


def test_linear_feet_matches_uhaul_anchor():
    # Empirical: ~23 chairs/linear ft. 600 chairs ≈ 26 ft (full box truck);
    # 300 ≈ 13 ft (~half). Both are Abdel's directly observed loads.
    assert fq.linear_feet(600, BOISE) == pytest.approx(26.1, abs=0.5)
    assert fq.linear_feet(300, BOISE) == pytest.approx(13.0, abs=0.3)
    assert fq.linear_feet(150, BOISE) == pytest.approx(6.5, abs=0.2)


# --------------------------------------------------------------------------- #
# ZIP centroids — the committed table stands in for pgeocode
# --------------------------------------------------------------------------- #
def test_zip_prefix_centroids_land_on_the_right_cities():
    """Guard for regenerating `zip_centroids.py`: a prefix centroid must sit
    within ~0.5° of the city its ZIPs belong to."""
    worcester = fq.zip_to_latlon("01608")
    boise = fq.zip_to_latlon("83702")
    assert worcester == pytest.approx((42.3, -71.8), abs=0.5)
    assert boise == pytest.approx((43.6, -116.2), abs=0.5)
    assert fq.zip_to_latlon("not-a-zip") is None
    # Every state-center ZIP must resolve, or a state-only lot can't be quoted.
    unresolved = [s for s, z in fq.STATE_CENTER_ZIP.items() if fq.zip_to_latlon(z) is None]
    assert unresolved == []


# --------------------------------------------------------------------------- #
# Pinned live lane — NOT skippable
# --------------------------------------------------------------------------- #
def test_live_lane_worcester_to_boise():
    q = fq.get_freight_estimate("01608", "83702", 150, "residential")
    assert q["recommended_mode"] == "ltl"
    assert q["partial_low"] is None and q["partial_high"] is None
    # Ticket target ~$1,000–$1,800 for this cross-country lane (loose-load calibration).
    assert 1000 <= q["ltl_low"] <= 1300
    assert 1450 <= q["ltl_high"] <= 1800
    assert q["ltl_low"] < q["ltl_high"]
    assert 5 <= q["transit_days"] <= 8
    assert q["provider"] == "estimator"
    assert q["raw"]["nmfc_class"] == 175          # empirical density ~4.7 -> class 175
    assert q["raw"]["lbs_per_chair_estimated"] is True   # placeholder flagged


def test_dock_drops_accessorials():
    res = fq.get_freight_estimate("01608", "83702", 150, "residential")
    dock = fq.get_freight_estimate("01608", "83702", 150, "dock")
    assert dock["accessorials"] == {
        "residential": False, "liftgate": False, "dock": True, "storage": False,
    }
    assert dock["ltl_low"] < res["ltl_low"]   # cheaper without residential+liftgate


def test_accessorials_storage_env():
    acc = fq._accessorials("storage")
    assert acc["storage"] and acc["liftgate"] and not acc["residential"]


def test_both_mode_quotes_two_ranges():
    # ~300 chairs -> ~13 linear ft -> the LTL/partial gray zone.
    q = fq.get_freight_estimate("01608", "83702", 300, "residential")
    assert q["mode"] == "both"
    assert q["ltl_low"] is not None and q["partial_low"] is not None
    assert q["recommended_mode"] in ("ltl", "partial")


def test_get_freight_quote_is_an_alias():
    """The CRM's call sites use the older name; both must be the same function."""
    assert fq.get_freight_quote is fq.get_freight_estimate


# --------------------------------------------------------------------------- #
# Failure paths — never fabricate a number
# --------------------------------------------------------------------------- #
def test_international_raises():
    with pytest.raises(fq.FreightUnavailable):
        fq.get_freight_estimate("83702", "Nigeria", 1000, "residential")


def test_offshore_zip_raises():
    # Puerto Rico prefix (006xx) is treated as offshore/international.
    with pytest.raises(fq.FreightUnavailable):
        fq.get_freight_estimate("83702", "00601", 200, "residential")


def test_alaska_zip_is_hand_quoted():
    """Storefront addition: ground-LTL cost-per-mile math is meaningless once a
    barge or air leg is involved, so 995–999 hands off instead of estimating."""
    for ak_zip in ("99501", "99801", "99901"):   # Anchorage, Juneau, Ketchikan
        assert fq._is_international(ak_zip) is True
        with pytest.raises(fq.FreightUnavailable):
            fq.get_freight_estimate("83702", ak_zip, 150, "residential")
    # ...but the lower-48 neighbours of that prefix band are still quotable.
    assert fq._is_international("98501") is False   # Olympia WA


def test_nonpositive_quantity_raises():
    with pytest.raises(fq.FreightUnavailable):
        fq.get_freight_estimate("83702", "01608", 0, "residential")


def test_unresolvable_origin_raises():
    with pytest.raises(fq.FreightUnavailable):
        fq.get_freight_estimate("notazip", "01608", 150, "residential")


def test_unlocatable_origin_zip_raises():
    """A syntactically valid ZIP whose prefix isn't in the centroid table is a
    lane failure, not a $0 lane."""
    with pytest.raises(fq.FreightUnavailable):
        fq.get_freight_estimate("00100", "01608", 150, "residential")


# --------------------------------------------------------------------------- #
# Carrier mapping
# --------------------------------------------------------------------------- #
def test_map_carrier_rates_selects_mode_by_linear_feet():
    """Regression (BWCRM-19 parked bug, fixed on the branch tip):
    _map_carrier_rates passed the raw chair COUNT to select_mode(), which takes
    linear feet — 300 chairs (~13 lf, gray zone) misclassified as 'partial'-by-
    count on every carrier response. 150 chairs ≈ 6.5 lf must rate as LTL;
    600 chairs ≈ 26 lf as partial."""
    cal = fq.calibration_for_lot(None)
    data = {"totalCharge": 1500}

    small = fq._map_carrier_rates("warp", data, "01608", "83702", 150, {})
    assert small["mode"] == fq.select_mode(fq.linear_feet(150, cal))
    assert small["ltl_low"] is not None  # 150 chairs is an LTL-sized load

    big = fq._map_carrier_rates("warp", data, "01608", "83702", 600, {})
    assert big["mode"] == fq.select_mode(fq.linear_feet(600, cal))
    assert big["recommended_mode"] == "partial"  # full box truck of chairs


def test_map_carrier_rates_without_a_rate_raises():
    with pytest.raises(fq.FreightUnavailable):
        fq._map_carrier_rates("warp", {"messages": ["no service"]},
                              "01608", "83702", 150, {})


# --------------------------------------------------------------------------- #
# Provider precedence: Warp -> estimator (Estes dropped on vendoring)
# --------------------------------------------------------------------------- #
def test_provider_precedence(monkeypatch):
    assert isinstance(fq.select_provider(), fq.EstimatorProvider)

    # The CRM's Estes key must no longer select anything — that adapter is gone.
    monkeypatch.setenv("FREIGHT_API_KEY", "estes-key")
    assert isinstance(fq.select_provider(), fq.EstimatorProvider)

    monkeypatch.setenv("WARP_API_KEY", "wak_live_x")
    assert isinstance(fq.select_provider(), fq.WarpProvider)


def test_carrier_error_falls_back_to_estimator(monkeypatch):
    """DIVERGENCE from the CRM: a carrier HTTP failure must NOT lose the lane.
    The CRM raised FreightUnavailable (seller hand-off, fine in a DM thread);
    a buyer typing a ZIP into the storefront gets the estimator's range instead."""
    monkeypatch.setenv("WARP_API_KEY", "wak_live_x")
    import urllib.request

    def boom(*a, **k):
        raise OSError("HTTP 503")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    q = fq.get_freight_estimate("01608", "83702", 150, "residential")
    assert q["provider"] == "estimator"
    assert 1000 <= q["ltl_low"] <= 1300


def test_carrier_error_still_raises_on_a_bad_lane(monkeypatch):
    """The fallback is for carrier failures only — an unquotable lane stays
    unquotable no matter which provider is configured."""
    monkeypatch.setenv("WARP_API_KEY", "wak_live_x")
    import urllib.request

    monkeypatch.setattr(
        urllib.request, "urlopen", lambda *a, **k: (_ for _ in ()).throw(OSError("503"))
    )
    with pytest.raises(fq.FreightUnavailable):
        fq.get_freight_estimate("83702", "00601", 150, "residential")
