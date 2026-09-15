"""City → zip → state ladder for public map pins (no network: pgeocode is faked)."""
import pandas as pd
import pytest

from automation.alerts import geo


class _FakeNominatim:
    def query_location(self, name, top_k=10):
        if name.lower() == "boise":
            return pd.DataFrame({
                "place_name": ["Boise", "Boise", "Boise"],
                "state_code": ["ID", "ID", "ID"],
                "latitude": [43.60, 43.63, 43.66],
                "longitude": [-116.27, -116.20, -116.25],
            })
        return pd.DataFrame(columns=["place_name", "state_code", "latitude", "longitude"])

    def query_postal_code(self, code):
        return pd.Series({"latitude": 43.6322, "longitude": -116.2052}) if code == "83702" \
            else pd.Series({"latitude": float("nan"), "longitude": float("nan")})


@pytest.fixture(autouse=True)
def _fake_pgeocode(monkeypatch):
    monkeypatch.setattr(geo, "_pgeocode_us", lambda: _FakeNominatim())
    geo.city_latlon.cache_clear()


def test_city_ladder_prefers_city():
    lat, lng, prec = geo.resolve_place("Boise", "ID", "83702")
    assert prec == "city"
    assert abs(lat - 43.63) < 0.05 and abs(lng - (-116.24)) < 0.05


def test_city_ladder_falls_to_zip_then_state():
    assert geo.resolve_place("Nowhere", "ID", "83702")[2] == "zip"
    assert geo.resolve_place("Nowhere", "ID", None)[2] == "state"
    assert geo.resolve_place(None, None, None) == (None, None, None)


def test_city_requires_matching_state():
    assert geo.city_latlon("Boise", "GA") is None


def test_city_latlon_without_pgeocode(monkeypatch):
    monkeypatch.setattr(geo, "_pgeocode_us", lambda: None)
    geo.city_latlon.cache_clear()
    assert geo.city_latlon("Boise", "ID") is None


@pytest.mark.parametrize("text,expect", [
    ("Boise, ID", ("Boise", "ID", None)),
    ("83702", (None, None, "83702")),
    ("Boise ID 83702", ("Boise", "ID", "83702")),
    ("Pittsburgh, Pennsylvania", ("Pittsburgh", "PA", None)),
    ("", (None, None, None)),
])
def test_parse_place(text, expect):
    assert geo.parse_place(text) == expect
