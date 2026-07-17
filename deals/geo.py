# deals/geo.py
"""Great-circle distance for the deal browser's distance filter.
Distance is a filter knob, never a hard exclusion (spec decision)."""
import math
import os

EARTH_RADIUS_MILES = 3958.8

def haversine_miles(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return EARTH_RADIUS_MILES * 2 * math.asin(math.sqrt(a))

def distance_from_home(lat: float | None, lng: float | None,
                       env: dict | None = None) -> float | None:
    env = env if env is not None else os.environ
    hlat, hlng = env.get("DEALS_HOME_LAT"), env.get("DEALS_HOME_LNG")
    if not hlat or not hlng or lat is None or lng is None:
        return None
    return round(haversine_miles(float(hlat), float(hlng), lat, lng), 1)
