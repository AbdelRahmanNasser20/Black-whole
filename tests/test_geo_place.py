"""City → zip → state ladder for public map pins (no network: pgeocode is faked)."""
import pandas as pd
import pytest

from automation.alerts import geo

# Captured before the autouse fixture swaps `_pgeocode_us` for a stub — the
# retry test exercises the real constructor-cache.
_REAL_PGEOCODE = geo._pgeocode_us


class _FakeNominatim:
    def query_location(self, name, top_k=10):
        if name.lower() == "boise":
            return pd.DataFrame({
                "place_name": ["Boise", "Boise", "Boise"],
                "state_code": ["ID", "ID", "ID"],
                "latitude": [43.60, 43.63, 43.66],
                "longitude": [-116.27, -116.20, -116.25],
            })
        if name.lower() == "meridian":
            # pgeocode's query_location is fuzzy: right state, wrong city.
            return pd.DataFrame({
                "place_name": ["Boise", "Boise"],
                "state_code": ["ID", "ID"],
                "latitude": [43.60, 43.63],
                "longitude": [-116.27, -116.20],
            })
        if name.lower() == "charleston":
            # A name shared by many states: pgeocode ranks 30 other Charlestons
            # above the West Virginia one, which is the row we actually want.
            others = ["SC", "IL", "MO", "AR", "MS", "OH", "ME"] * 5
            return self._truncate(pd.DataFrame({
                "place_name": ["Charleston"] * 31,
                "state_code": others[:30] + ["WV"],
                "latitude": [32.0] * 30 + [38.35],
                "longitude": [-79.9] * 30 + [-81.63],
            }), top_k)
        return pd.DataFrame(columns=["place_name", "state_code", "latitude", "longitude"])

    @staticmethod
    def _truncate(df, top_k):
        """Real pgeocode returns at most top_k rows, best fuzzy score first."""
        return df.head(top_k)

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


def test_fuzzy_neighbour_is_not_a_city_hit():
    """A right-state/wrong-city fuzzy match must not be pinned as that city."""
    assert geo.city_latlon("Meridian", "ID") is None
    assert geo.resolve_place("Meridian", "ID", "83702")[2] == "zip"


def test_city_latlon_without_pgeocode(monkeypatch):
    monkeypatch.setattr(geo, "_pgeocode_us", lambda: None)
    geo.city_latlon.cache_clear()
    assert geo.city_latlon("Boise", "ID") is None


@pytest.mark.parametrize("text,expect", [
    ("Boise, ID", ("Boise", "ID", None)),
    ("83702", (None, None, "83702")),
    ("Boise ID 83702", ("Boise", "ID", "83702")),
    ("Pittsburgh, Pennsylvania", ("Pittsburgh", "PA", None)),
    # two-word state names must win over the one-word state hiding in their tail
    ("Charleston, West Virginia", ("Charleston", "WV", None)),
    ("West Virginia", (None, "WV", None)),
    ("New York, New York", ("New York", "NY", None)),
    ("", (None, None, None)),
])
def test_parse_place(text, expect):
    assert geo.parse_place(text) == expect


@pytest.mark.parametrize("raw,code", [
    ("ID", "ID"), ("id", "ID"), ("Idaho", "ID"), ("idaho", "ID"),
    ("  Idaho  ", "ID"), ("New York", "NY"), ("new york", "NY"),
    ("Freedonia", None), ("", None), (None, None),
])
def test_norm_state_accepts_full_names(raw, code):
    """inventory.state holds "Idaho" on some rows, "ID" on others."""
    assert geo._norm_state(raw) == code


def test_full_state_name_still_pins_the_city():
    assert geo.resolve_place("Boise", "Idaho", "83702")[2] == "city"


def test_full_state_name_falls_back_to_state_centroid():
    assert geo.resolve_latlon(None, "Pennsylvania")[2] == "state"


def test_shared_city_name_looks_past_the_first_25_candidates():
    """Charleston exists in a dozen states; WV must not fall off the candidate list."""
    lat, lng, prec = geo.resolve_place("Charleston", "WV", None)
    assert prec == "city"
    assert (round(lat, 2), round(lng, 2)) == (38.35, -81.63)


# ─── pgeocode construction cache (never memoise a failure) ───

class _FakeModule:
    """Stands in for `import pgeocode`: raises `fail_times` times, then works."""

    def __init__(self, fail_times):
        self.left, self.instance, self.calls = fail_times, _FakeNominatim(), 0

    def Nominatim(self, country):
        self.calls += 1
        if self.left > 0:
            self.left -= 1
            raise RuntimeError("GeoNames download failed")
        return self.instance


@pytest.fixture
def _real_pgeocode(monkeypatch):
    """Point the module at a fake `pgeocode` with a clean construction cache."""
    import sys
    fake = _FakeModule(fail_times=1)
    monkeypatch.setitem(sys.modules, "pgeocode", fake)
    monkeypatch.setattr(geo, "_NOMI", None)
    monkeypatch.setattr(geo, "_NOMI_FAILED_AT", 0.0)
    return fake


def test_pgeocode_failure_is_retried_not_memoised(_real_pgeocode, monkeypatch, caplog):
    """One flaky first call must not pin the whole process to state centroids."""
    with caplog.at_level("WARNING"):
        assert _REAL_PGEOCODE() is None
    assert "pgeocode unavailable" in caplog.text and "state centroids" in caplog.text
    # Inside the retry window the failure is not re-attempted (no hammering).
    assert _REAL_PGEOCODE() is None
    assert _real_pgeocode.calls == 1
    # Past it, the retry succeeds and the instance is cached from then on.
    monkeypatch.setattr(geo, "_NOMI_RETRY_SEC", 0.0)
    assert _REAL_PGEOCODE() is _real_pgeocode.instance
    assert _REAL_PGEOCODE() is _real_pgeocode.instance
    assert _real_pgeocode.calls == 2
