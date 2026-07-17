# tests/deals/test_geo.py
import pytest
from deals.geo import haversine_miles, distance_from_home

def test_known_distance_dc_to_nyc():
    # Washington DC (38.9072, -77.0369) to NYC (40.7128, -74.0060) ≈ 204 mi
    assert haversine_miles(38.9072, -77.0369, 40.7128, -74.0060) == pytest.approx(204, abs=5)

def test_zero_distance():
    assert haversine_miles(38.9, -77.0, 38.9, -77.0) == 0.0

def test_home_unset_returns_none():
    assert distance_from_home(38.9, -77.0, {}) is None

def test_missing_lot_coords_returns_none():
    env = {"DEALS_HOME_LAT": "38.9", "DEALS_HOME_LNG": "-77.0"}
    assert distance_from_home(None, None, env) is None

def test_home_set_computes():
    env = {"DEALS_HOME_LAT": "38.9072", "DEALS_HOME_LNG": "-77.0369"}
    assert distance_from_home(40.7128, -74.0060, env) == pytest.approx(204, abs=5)
