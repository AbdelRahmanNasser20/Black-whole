"""DMV sourcing matcher — BLACKWHOLE-19.

Pure Python over plain dicts (no DB, no network) so the whole radius filter is
unit-testable and the CLI / digest just feed it rows they already loaded. This
is the piece the existing saved-search runner (``deals.saved_search_alerts``,
single-``state`` filter only) cannot express: alert on any chair lot **within
100 mi of DC**, across DC/MD/VA.

Geo reuses the already-shipped haversine + zip/state resolver from the buyer
alert package (``automation.alerts.geo``) — same math, no new dependency, and
its state-centroid table already carries DC/MD/VA.

A lot matches when it is:
  1. a chair lot (``dmv.is_chair_text`` over title/category), AND
  2. within ``RADIUS_MILES`` of ``DC_ANCHOR`` — using the lot's own lat/lng if it
     carries them, else the zip → state-centroid ladder. When only state-precise
     coordinates are available (±200 mi noise), a lot in DC/MD/VA passes on an
     honest same-region degrade rather than a false distance test.

Each match carries an audit ``reason`` dict (distance, precision, why it passed)
mirroring the buyer matcher's ``match_reason`` so alerts stay explainable.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from automation.alerts.geo import haversine_miles, resolve_latlon

from . import dmv

# Field names a discovered lot row might use for coordinates, most precise first.
_LAT_KEYS = ("lat", "latitude", "lat_deg")
_LON_KEYS = ("lng", "lon", "longitude", "lng_deg", "lon_deg")


@dataclass
class SourcingMatch:
    lot: dict
    reason: dict = field(default_factory=dict)


@dataclass
class SourcingSkip:
    lot_key: Any
    reason: str


def _lot_key(lot: dict) -> Any:
    """Best-effort stable identifier for logging/audit."""
    for k in ("lot_id", "asset_id", "id", "url"):
        v = lot.get(k)
        if v not in (None, ""):
            return v
    return lot.get("title")


def _first(lot: dict, keys: tuple[str, ...]) -> Any:
    for k in keys:
        v = lot.get(k)
        if v not in (None, ""):
            return v
    return None


def _num(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None  # drop NaN


def resolve_lot_latlon(lot: dict) -> tuple[float | None, float | None, str | None]:
    """(lat, lon, precision) for a lot.

    Prefers coordinates the row already carries ('zip' precision), then falls
    back to the shared zip → state-centroid resolver. Precision is one of
    ``'exact' | 'zip' | 'state' | None``.
    """
    lat = _num(_first(lot, _LAT_KEYS))
    lon = _num(_first(lot, _LON_KEYS))
    if lat is not None and lon is not None:
        return lat, lon, "exact"
    zip_code = _first(lot, ("zip_code", "zip", "postal_code"))
    return resolve_latlon(zip_code, lot.get("state"))


def dmv_match(
    lot: dict,
    *,
    anchor: tuple[float, float] = dmv.DC_ANCHOR,
    radius_miles: int = dmv.RADIUS_MILES,
    states: frozenset[str] = dmv.DMV_STATES,
) -> tuple[bool, dict]:
    """Return (passes, reason). Geo-only — caller applies the chair-type gate."""
    lat, lon, precision = resolve_lot_latlon(lot)
    state = (lot.get("state") or "").strip().upper()

    if lat is None:
        # Can't localize by coordinate. Fall back to the region membership test:
        # a DC/MD/VA lot is in-scope; anything else we honestly can't place.
        if state in states:
            return True, {"distance_miles": None, "geo_precision": "state_only",
                          "in_dmv_state": True}
        return False, {"distance_miles": None, "geo_precision": "unresolved",
                       "in_dmv_state": False}

    dist = round(haversine_miles(anchor[0], anchor[1], lat, lon), 1)
    reason = {"distance_miles": dist, "geo_precision": precision,
              "radius_miles": radius_miles, "in_dmv_state": state in states}
    if dist <= radius_miles:
        return True, reason
    # Overshot. State-centroid coordinates carry ±200 mi of noise, so for a
    # coarse (state-precision) lot that still sits in DC/MD/VA, degrade honestly
    # to a same-region pass rather than drop a genuine DMV lot on centroid noise.
    if precision == "state" and state in states:
        return True, {**reason, "same_region_pass": True}
    return False, reason


def filter_dmv_lots(
    lots: list[dict],
    *,
    since: Any = None,
    anchor: tuple[float, float] = dmv.DC_ANCHOR,
    radius_miles: int = dmv.RADIUS_MILES,
    states: frozenset[str] = dmv.DMV_STATES,
) -> tuple[list[SourcingMatch], list[SourcingSkip]]:
    """Filter discovered auction ``lots`` to DMV chair lots within radius.

    ``since`` (optional) drops lots whose ``first_seen_at`` is not strictly after
    it — the "only NEW lots" contract, mirroring ``deals.saved_search_alerts``.
    Returns (matches, skips) with a coarse per-skip reason for the preview.
    """
    matches: list[SourcingMatch] = []
    skips: list[SourcingSkip] = []

    for lot in lots:
        key = _lot_key(lot)
        if since is not None:
            seen = lot.get("first_seen_at")
            if seen is not None and not (seen > since):
                skips.append(SourcingSkip(key, "not_new"))
                continue
        if not dmv.is_chair_text(lot.get("title"), lot.get("canonical_category")
                                 or lot.get("category")):
            skips.append(SourcingSkip(key, "not_chairs"))
            continue
        passed, reason = dmv_match(lot, anchor=anchor, radius_miles=radius_miles,
                                   states=states)
        if not passed:
            skips.append(SourcingSkip(key, "out_of_region"))
            continue
        matches.append(SourcingMatch(lot=lot, reason=reason))

    return matches, skips
