"""Self-contained geo helpers for the alert matcher (BLACKWHOLE-10).

The CRM resolves a zip/state to lat-lon with `pgeocode` and measures distance
with a haversine (`geo_utils.zip_to_latlon` / `haversine_miles`). That code is
NOT vendored into this repo, so this module reimplements the small slice the
blast needs — with no hard new dependency:

  - `haversine_miles(...)` — great-circle distance, pure Python.
  - `resolve_latlon(zip_code, state)` — best-effort coordinates + a *precision*
    tag (`'zip' | 'state' | None`). Zip precision is used **only if `pgeocode`
    happens to be installed**; otherwise we fall back to a built-in state
    centroid table, so matching still works (at state precision) with zero
    extra packages. The matcher degrades honestly when precision is coarse
    (see `matcher.match_lot`).

Nothing here touches the network at import time. `pgeocode` (if present) reads a
bundled offline dataset.
"""
from __future__ import annotations

import math
from functools import lru_cache

# ── state centroids (approx geographic center, WGS84) ───────────────────────
# Enough for a state-precision fallback when a zip can't be resolved. Values are
# rounded; state-precision matching is intentionally coarse (±200 mi noise, per
# the PRD) and we never present these as exact.
STATE_CENTROIDS: dict[str, tuple[float, float]] = {
    "AL": (32.806, -86.791), "AK": (61.370, -152.404), "AZ": (33.729, -111.431),
    "AR": (34.970, -92.373), "CA": (36.116, -119.682), "CO": (39.059, -105.311),
    "CT": (41.598, -72.755), "DE": (39.319, -75.507), "DC": (38.897, -77.026),
    "FL": (27.766, -81.687), "GA": (33.040, -83.643), "HI": (21.094, -157.498),
    "ID": (44.240, -114.478), "IL": (40.349, -88.986), "IN": (39.849, -86.258),
    "IA": (42.011, -93.210), "KS": (38.526, -96.726), "KY": (37.668, -84.670),
    "LA": (31.169, -91.867), "ME": (44.693, -69.381), "MD": (39.064, -76.802),
    "MA": (42.230, -71.530), "MI": (43.326, -84.536), "MN": (45.694, -93.900),
    "MS": (32.741, -89.678), "MO": (38.456, -92.288), "MT": (46.921, -110.454),
    "NE": (41.125, -98.268), "NV": (38.313, -117.055), "NH": (43.452, -71.564),
    "NJ": (40.298, -74.521), "NM": (34.841, -106.248), "NY": (42.166, -74.948),
    "NC": (35.630, -79.806), "ND": (47.528, -99.784), "OH": (40.389, -82.764),
    "OK": (35.565, -96.929), "OR": (44.572, -122.071), "PA": (40.590, -77.209),
    "RI": (41.680, -71.512), "SC": (33.856, -80.945), "SD": (44.299, -99.438),
    "TN": (35.747, -86.692), "TX": (31.055, -97.563), "UT": (40.150, -111.862),
    "VT": (44.045, -72.710), "VA": (37.769, -78.170), "WA": (47.400, -121.490),
    "WV": (38.491, -80.954), "WI": (44.268, -89.616), "WY": (42.756, -107.302),
}

EARTH_RADIUS_MILES = 3958.7613


def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in miles between two lat-lon points."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * EARTH_RADIUS_MILES * math.asin(math.sqrt(a))


def _norm_state(state: str | None) -> str | None:
    s = (state or "").strip().upper()
    return s if s in STATE_CENTROIDS else None


@lru_cache(maxsize=1)
def _pgeocode_us():
    """Return a cached pgeocode US nominatim, or None if pgeocode is absent.

    Import is lazy + guarded: the package is optional. Absent => zip precision
    is simply unavailable and callers fall back to the state centroid.
    """
    try:
        import pgeocode  # type: ignore
    except Exception:
        return None
    try:
        return pgeocode.Nominatim("us")
    except Exception:
        return None


def _zip_latlon(zip_code: str | None) -> tuple[float, float] | None:
    z = (zip_code or "").strip()[:5]
    if len(z) != 5 or not z.isdigit():
        return None
    nomi = _pgeocode_us()
    if nomi is None:
        return None
    try:
        rec = nomi.query_postal_code(z)
        lat, lon = float(rec["latitude"]), float(rec["longitude"])
    except Exception:
        return None
    if math.isnan(lat) or math.isnan(lon):
        return None
    return (lat, lon)


def resolve_latlon(
    zip_code: str | None, state: str | None
) -> tuple[float | None, float | None, str | None]:
    """Best-effort coordinates for a zip/state pair.

    Returns ``(lat, lon, precision)`` where precision is:
      - ``'zip'``   — resolved from the 5-digit zip (needs `pgeocode`),
      - ``'state'`` — fell back to the state centroid,
      - ``None``    — nothing resolvable (no coords).

    Mirrors the CRM's `record_latlon` ladder (zip → state centroid → none).
    """
    hit = _zip_latlon(zip_code)
    if hit is not None:
        return (hit[0], hit[1], "zip")
    st = _norm_state(state)
    if st is not None:
        lat, lon = STATE_CENTROIDS[st]
        return (lat, lon, "state")
    return (None, None, None)
