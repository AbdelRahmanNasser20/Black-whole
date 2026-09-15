"""Chair freight estimator — turn a quantity + a lane into an honest cost RANGE.

The storefront's lot page asks this module "what would it cost to truck N chairs
to ZIP X?" and gets an LTL (and, for big orders, a partial/volume) **range**,
plus lane miles and a transit estimate. Never a single invented number, never a
bookable rate: the chair price is quoted separately and firm, freight rides on
top and is re-confirmed at ship time.

**Provenance.** Vendored from the CRM repo's `BWCRM-19-feature-freight-quote-engine`
branch tip (`freight_quote.py` + `config/chair_freight.py` + `state_zips.py` +
`geo_utils.haversine_miles`), merged into this one self-contained module the way
`lot_images.py` centralizes photo resolution. That branch is 50+ commits behind
its own main and is NOT being rebased — this copy is the storefront's source of
truth. The chair-count/linear-feet bug in `_map_carrier_rates` was already fixed
on the branch tip and the fix is carried here, with its regression test.

**What changed on the way in** (deliberate, do not "restore"):

1. *No pgeocode.* The CRM geocoded ZIPs with `pgeocode`, which drags in pandas +
   numpy and downloads ~10 MB on first use. Here `zip_to_latlon` reads the
   committed 3-digit-prefix table in `automation.zip_centroids` — stdlib only,
   zero network, ±30 mi (≈ ±$10, inside the range spread).
2. *Warp only, no Estes.* The Estes adapter needed a carrier account that was
   rejected; it and its key-minting CLI are dropped. Provider precedence is
   Warp (if `WARP_API_KEY`) → estimator.
3. *A carrier failure falls back to the estimator instead of raising.* In the
   CRM a Warp HTTP/parse error meant "hand off to the seller", which is right
   for a DM thread with a human behind it. On the storefront a buyer typing a
   ZIP into a web form must never lose a quotable lane to a flaky third-party
   API, so `get_freight_estimate` retries with `EstimatorProvider` and reports
   `provider == "estimator"`. **Lane failures still raise** — an unresolvable
   ZIP, an international destination, an offshore/Alaska destination, or a
   non-positive quantity all produce `FreightUnavailable` and the caller shows
   "we'll quote this one by hand". The hard rule is unchanged: never fabricate
   a number.
4. *Alaska (`995`–`999`) joins the offshore set.* Ground-LTL math is simply
   wrong for AK (barge/air legs); those lanes get hand-quoted.

Stdlib-only at runtime and **no DB access anywhere** — logging a quote to the
`freight_quotes` table is the caller's job. That keeps this unit-testable with
no fixtures and safe to import from any process.

CLI:
    python -m automation.freight_estimate --origin 01608 --dest 83702 --qty 150
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional, Protocol

from automation.zip_centroids import PREFIX_CENTROIDS


# ===========================================================================
# Per-lot physical calibration  (was config/chair_freight.py)
# ===========================================================================
@dataclass(frozen=True)
class LotCalibration:
    """Physical constants for one lot's chairs, calibrated on how they ACTUALLY
    load — measured empirically, NOT derived from stack geometry (which kept
    overstating density).

    Abdel's ground truth (2026-06): **~600 chairs filled a 26' U-Haul box truck**
    (~317" usable length) and **300 chairs took ~half** of it. Both ⇒ ~23 chairs
    per linear foot, and ⇒ ~2.78 ft³/chair (600 chairs ≈ the box's ~1,670 ft³),
    i.e. density ~4.7 lb/ft³ → freight class 175.

    Note a 53' freight trailer is ~8" wider + ~9-11" taller than that box truck
    (cross-section ~17% bigger), so it fits a bit MORE per linear foot — these
    box-truck numbers make the linear-feet/partial estimate conservative, which
    is the safe direction for a quote."""

    lot_id: str
    origin_zip: str
    lbs_per_chair: float
    lbs_per_chair_estimated: bool = True       # True until a chair is actually weighed
    chairs_per_linear_foot: float = 23.0       # empirical (600 chairs ≈ 26' box-truck floor)
    cube_ft3_per_chair: float = 2.78           # empirical (600 chairs ≈ 1,670 ft³ box volume)


# The Boise lot (inventory.lot_id = '31225', zip 83702) is the default standard.
BOISE = LotCalibration(
    lot_id="31225",
    origin_zip="83702",
    lbs_per_chair=13.0,           # TODO: WEIGH — placeholder, lbs_per_chair_estimated=True
    lbs_per_chair_estimated=True,
    chairs_per_linear_foot=23.0,
    cube_ft3_per_chair=2.78,
)

# Per-lot overrides keyed by inventory.lot_id. Lots not listed fall back to the
# Boise standard via ``calibration_for_lot``.
LOT_CALIBRATIONS: dict[str, LotCalibration] = {
    BOISE.lot_id: BOISE,
}

DEFAULT_CALIBRATION = BOISE


def calibration_for_lot(lot_id: str | None) -> LotCalibration:
    """Calibration for an inventory lot. Unknown / None → the Boise standard,
    but carrying the real ``origin_zip`` is the caller's job (the standard's
    rack geometry is reused; only the chairs differ lot to lot, rarely)."""
    if lot_id and str(lot_id) in LOT_CALIBRATIONS:
        return LOT_CALIBRATIONS[str(lot_id)]
    return DEFAULT_CALIBRATION


# ---------------------------------------------------------------------------
# Mode selector thresholds (by LINEAR FEET of trailer the load occupies)
# ---------------------------------------------------------------------------
# Real carrier "linear foot rule": a load over ~12 ft is no longer cheap LTL.
# ≤ 12 ft → standard LTL. ≥ 14 ft → partial/volume (floor load). Between → BOTH
# (quote each, recommend the cheaper). Driven by actual floor space, not a
# guessed chair count.
LTL_MAX_LINEAR_FT = 12.0
PARTIAL_MIN_LINEAR_FT = 14.0


# ---------------------------------------------------------------------------
# Density → NMFC freight class (post-Jul-2025 density-based NMFC, 11-class scale)
# ---------------------------------------------------------------------------
# (low_density_inclusive, high_density_exclusive, class). Lower density ⇒ bulkier
# ⇒ higher (more expensive) class. Stacked chairs are bulky-light, so they land
# in the high classes — which is correct and why chair freight isn't cheap.
DENSITY_NMFC_BANDS: list[tuple[float, float, float]] = [
    (0.0, 1.0, 400),
    (1.0, 2.0, 300),
    (2.0, 4.0, 250),
    (4.0, 6.0, 175),
    (6.0, 8.0, 125),
    (8.0, 10.0, 110),
    (10.0, 12.0, 92.5),
    (12.0, 15.0, 85),
    (15.0, 22.5, 70),
    (22.5, 30.0, 65),
    (30.0, float("inf"), 60),
]


def density_to_nmfc_class(density_lb_ft3: float) -> float:
    """Map a density (lb/ft³) to its NMFC freight class via ``DENSITY_NMFC_BANDS``."""
    for low, high, cls in DENSITY_NMFC_BANDS:
        if low <= density_lb_ft3 < high:
            return cls
    return 400  # absurdly light — shouldn't happen, but quote the worst class


# ---------------------------------------------------------------------------
# Pricing knobs (tunable; all rating math reads these — nothing hard-coded in
# the provider). Calibrated so the live test lane Worcester MA 01608 → Boise ID
# 83702, 150 chairs lands in the ~$1,000–$1,800 LTL ballpark. See
# tests/test_freight_estimate.py for the pinned live-lane assertion.
# ---------------------------------------------------------------------------
ROAD_CIRCUITY = 1.20            # great-circle miles → road miles multiplier

# LTL: base + (billable hundredweight × $/cwt for the class) + (road miles × $/mi)
LTL_BASE_USD = 250.0
LTL_PER_MILE_USD = 0.25
# Carrier "linear foot rule": a low-density shipment occupying more than ~12 ft
# of trailer (or 6+ pallet positions) gets a capacity-load surcharge per foot
# over the cap — otherwise a bulky-light load is billed far too low. Kicks in at
# 3+ racks (the gray-zone / partial boundary); ≤2-rack LTL loads are unaffected.
LTL_LINEAR_FT_CAP = 12.0
LTL_LINEAR_FT_SURCHARGE_USD = 60.0
LTL_PER_CWT_USD_BY_CLASS: dict[float, float] = {
    400: 20.0,
    300: 16.0,
    250: 13.0,
    175: 10.0,
    125: 8.0,
    110: 7.0,
    92.5: 6.0,
    85: 5.5,
    70: 4.5,
    65: 4.0,
    60: 3.5,
}

# Partial / volume (floor-load) — priced on space (linear feet) + distance, not
# class. Only used for the partial mode (or the gray zone).
PARTIAL_BASE_USD = 350.0
PARTIAL_PER_MILE_USD = 0.45
PARTIAL_PER_LINEAR_FOOT_USD = 28.0

# Accessorials — defaulted ON for Marketplace/storefront buyers (residential
# delivery + liftgate) unless a dock/forklift is confirmed. Quoting without
# these to a house lowballs the buyer badly.
RESIDENTIAL_FEE_USD = 120.0
LIFTGATE_FEE_USD = 90.0

# The quoted range is the point estimate × these spreads (then rounded to $10).
RANGE_SPREAD_LOW = 0.85
RANGE_SPREAD_HIGH = 1.20

# Transit: ground LTL moves ~this many miles/day, + 1 day dock/handling.
TRANSIT_MILES_PER_DAY = 500

# How long a quoted range stays valid before it should be re-confirmed (days).
QUOTE_VALID_DAYS = 7


# ---------------------------------------------------------------------------
# Geometry helpers (pure — quantity + a LotCalibration → physical shipment math)
# Empirically anchored: linear feet from chairs-per-linear-foot, volume from
# cube-per-chair (both measured off Abdel's real U-Haul load, see LotCalibration).
# ---------------------------------------------------------------------------
def total_weight_lb(quantity: int, cal: LotCalibration) -> float:
    return max(0, quantity) * cal.lbs_per_chair


def volume_ft3(quantity: int, cal: LotCalibration) -> float:
    """Cubic feet the chairs occupy (empirical cube/chair × quantity)."""
    return max(0, quantity) * cal.cube_ft3_per_chair


def linear_feet(quantity: int, cal: LotCalibration) -> float:
    """Trailer floor length the load consumes (empirical chairs/linear-foot)."""
    if quantity <= 0 or cal.chairs_per_linear_foot <= 0:
        return 0.0
    return quantity / cal.chairs_per_linear_foot


def density_lb_ft3(quantity: int, cal: LotCalibration) -> float:
    """Load density — constant per chair (lbs/chair ÷ cube/chair); drives class."""
    if cal.cube_ft3_per_chair <= 0:
        return 0.0
    return cal.lbs_per_chair / cal.cube_ft3_per_chair


def handling_units(quantity: int, cal: LotCalibration) -> int:
    """Approx palletized handling units for a carrier API (~1 pallet / 4 linear ft)."""
    if quantity <= 0:
        return 0
    return max(1, math.ceil(linear_feet(quantity, cal) / 4.0))


def select_mode(linear_ft: float) -> str:
    """'ltl' | 'partial' | 'both' by the linear feet of trailer the load uses."""
    if linear_ft <= LTL_MAX_LINEAR_FT:
        return "ltl"
    if linear_ft >= PARTIAL_MIN_LINEAR_FT:
        return "partial"
    return "both"


# ===========================================================================
# State centers  (was state_zips.py — only the ZIP map is needed here)
# ===========================================================================
# Used by callers that know a lot's state but not its ZIP: the origin falls back
# to the state capital's ZIP so a lane is still measurable.
STATE_CENTER_ZIP = {
    "AL": "36104",  # Montgomery
    "AK": "99801",  # Juneau
    "AZ": "85007",  # Phoenix
    "AR": "72201",  # Little Rock
    "CA": "95814",  # Sacramento
    "CO": "80202",  # Denver
    "CT": "06103",  # Hartford
    "DE": "19901",  # Dover
    "DC": "20001",  # Washington
    "FL": "32301",  # Tallahassee
    "GA": "30303",  # Atlanta
    "HI": "96813",  # Honolulu
    "ID": "83702",  # Boise
    "IL": "62701",  # Springfield
    "IN": "46204",  # Indianapolis
    "IA": "50309",  # Des Moines
    "KS": "66603",  # Topeka
    "KY": "40601",  # Frankfort
    "LA": "70802",  # Baton Rouge
    "ME": "04330",  # Augusta
    "MD": "21401",  # Annapolis
    "MA": "02108",  # Boston
    "MI": "48933",  # Lansing
    "MN": "55102",  # Saint Paul
    "MS": "39201",  # Jackson
    "MO": "65101",  # Jefferson City
    "MT": "59601",  # Helena
    "NE": "68508",  # Lincoln
    "NV": "89701",  # Carson City
    "NH": "03301",  # Concord
    "NJ": "08608",  # Trenton
    "NM": "87501",  # Santa Fe
    "NY": "12207",  # Albany
    "NC": "27601",  # Raleigh
    "ND": "58501",  # Bismarck
    "OH": "43215",  # Columbus
    "OK": "73102",  # Oklahoma City
    "OR": "97301",  # Salem
    "PA": "17101",  # Harrisburg
    "RI": "02903",  # Providence
    "SC": "29201",  # Columbia
    "SD": "57501",  # Pierre
    "TN": "37219",  # Nashville
    "TX": "78701",  # Austin
    "UT": "84111",  # Salt Lake City
    "VT": "05602",  # Montpelier
    "VA": "23219",  # Richmond
    "WA": "98501",  # Olympia
    "WV": "25301",  # Charleston
    "WI": "53703",  # Madison
    "WY": "82001",  # Cheyenne
}


# ===========================================================================
# Errors + lane resolution
# ===========================================================================
class FreightUnavailable(Exception):
    """Raised when a quote cannot be produced (bad/unresolvable lane, intl,
    offshore/Alaska, non-positive quantity). The caller hands the lane to the
    operator to quote by hand — never a guess."""


def _resolve_zip(z: Optional[str]) -> Optional[str]:
    """Normalize to a 5-digit US zip string, or None."""
    if not z:
        return None
    s = str(z).strip().split("-")[0].zfill(5)
    return s if len(s) == 5 and s.isdigit() else None


def zip_to_latlon(zip_code) -> Optional[tuple[float, float]]:
    """ZIP → (lat, lon) via the committed 3-digit-prefix centroid table.

    Replaces the CRM's `geo_utils.zip_to_latlon` (pgeocode). Prefix-level
    resolution is ±30 mi, which is immaterial next to a range that already
    spans -15%/+20%. Unknown prefix → None (the caller refuses to quote)."""
    z = _resolve_zip(zip_code)
    if z is None:
        return None
    return PREFIX_CENTROIDS.get(z[:3])


def haversine_miles(a, b) -> float:
    """Great-circle distance in miles between two (lat, lon) pairs."""
    if not a or not b:
        return float("inf")
    lat1, lon1 = a
    lat2, lon2 = b
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 3958.7613 * math.asin(math.sqrt(h))


def lane_miles(origin_zip: str, dest_zip: str) -> float:
    """Road miles between two zips: great-circle × road circuity. Raises
    :class:`FreightUnavailable` if either zip can't be located — we will not
    quote a zero-mile / fabricated lane."""
    o = zip_to_latlon(origin_zip)
    d = zip_to_latlon(dest_zip)
    if not o or not d:
        raise FreightUnavailable(
            f"could not resolve lane {origin_zip!r}->{dest_zip!r} to coordinates"
        )
    gc = haversine_miles(o, d)
    if not math.isfinite(gc):
        raise FreightUnavailable(f"non-finite distance for {origin_zip!r}->{dest_zip!r}")
    return gc * ROAD_CIRCUITY


def transit_days(miles: float) -> int:
    """Rough ground-LTL transit: ~TRANSIT_MILES_PER_DAY/day + 1 day handling."""
    return int(math.ceil(miles / TRANSIT_MILES_PER_DAY) + 1)


def _round10(x: float) -> float:
    return float(round(x / 10.0) * 10)


def _is_international(dest_zip: Optional[str]) -> bool:
    """A non-US-zip destination (Canada postal, 'Nigeria', etc.) — or a US zip
    that's offshore — is out of scope here.

    Offshore = PR/VI (006–009), Hawaii/Guam/Pacific (967–969) and **Alaska
    (995–999)**. Alaska is a storefront addition: ground-LTL cost-per-mile math
    is meaningless once a barge or air leg is involved, so those lanes get
    quoted by hand rather than estimated badly."""
    z = _resolve_zip(dest_zip)
    if z is None:
        return True
    prefix = z[:3]
    return prefix in {
        "006", "007", "008", "009",          # Puerto Rico / US Virgin Islands
        "967", "968",                          # Hawaii
        "969",                                 # Guam / Marshall Is. / Micronesia
        "995", "996", "997", "998", "999",   # Alaska — barge/air, hand-quote it
    }


# ===========================================================================
# Providers
# ===========================================================================
class FreightProvider(Protocol):
    name: str

    def quote(
        self, origin_zip: str, dest_zip: str, quantity: int, delivery_env: str
    ) -> dict:
        ...


def _accessorials(delivery_env: str) -> dict:
    """Residential + liftgate default ON for storefront buyers; a confirmed
    dock/forklift ('dock') drops them. 'storage' = a self-storage unit
    (no dock, no forklift): liftgate stays ON and carriers price it as a
    limited-access / mini-storage delivery rather than residential."""
    env = (delivery_env or "").lower()
    dock = env == "dock"
    storage = env == "storage"
    return {
        "residential": not dock and not storage,
        "liftgate": not dock,
        "dock": dock,
        "storage": storage,
    }


class EstimatorProvider:
    """Default, key-free grounded estimator (density + NMFC class + lane miles)."""

    name = "estimator"

    def quote(self, origin_zip, dest_zip, quantity, delivery_env):
        cal = calibration_for_lot(None)  # rack geometry standard (Boise)
        miles = lane_miles(origin_zip, dest_zip)
        acc = _accessorials(delivery_env)
        acc_fee = (RESIDENTIAL_FEE_USD if acc["residential"] else 0.0) + (
            LIFTGATE_FEE_USD if acc["liftgate"] else 0.0
        )

        weight = total_weight_lb(quantity, cal)
        lf = linear_feet(quantity, cal)
        density = density_lb_ft3(quantity, cal)
        nmfc = density_to_nmfc_class(density)
        mode = select_mode(lf)

        ltl_low = ltl_high = partial_low = partial_high = None
        if mode in ("ltl", "both"):
            ltl_low, ltl_high = self._ltl_range(weight, miles, nmfc, acc_fee, lf)
        if mode in ("partial", "both"):
            partial_low, partial_high = self._partial_range(lf, miles, acc_fee)

        recommended = mode
        if mode == "both":
            # Recommend whichever mode is cheaper at the midpoint; keep both.
            ltl_mid = (ltl_low + ltl_high) / 2
            par_mid = (partial_low + partial_high) / 2
            recommended = "ltl" if ltl_mid <= par_mid else "partial"

        return {
            "ltl_low": ltl_low,
            "ltl_high": ltl_high,
            "partial_low": partial_low,
            "partial_high": partial_high,
            "recommended_mode": recommended,
            "mode": mode,
            "miles": int(round(miles)),
            "transit_days": transit_days(miles),
            "valid_until": (date.today() + timedelta(days=QUOTE_VALID_DAYS)).isoformat(),
            "accessorials": acc,
            "raw": {
                "provider": self.name,
                "weight_lb": round(weight, 1),
                "linear_feet": round(lf, 1),
                "chairs_per_linear_foot": cal.chairs_per_linear_foot,
                "cube_ft3_per_chair": cal.cube_ft3_per_chair,
                "density_lb_ft3": round(density, 2),
                "nmfc_class": nmfc,
                "lbs_per_chair": cal.lbs_per_chair,
                "lbs_per_chair_estimated": cal.lbs_per_chair_estimated,
                "accessorial_fee_usd": acc_fee,
            },
        }

    @staticmethod
    def _ltl_range(weight, miles, nmfc, acc_fee, linear_feet=0.0):
        cwt = weight / 100.0
        rate = LTL_PER_CWT_USD_BY_CLASS.get(nmfc, LTL_PER_CWT_USD_BY_CLASS[400])
        point = LTL_BASE_USD + cwt * rate + miles * LTL_PER_MILE_USD + acc_fee
        if linear_feet > LTL_LINEAR_FT_CAP:
            # Linear-foot capacity surcharge — a bulky load over the cap is no
            # longer cheap LTL (this is why partial usually wins the gray zone).
            point += (linear_feet - LTL_LINEAR_FT_CAP) * LTL_LINEAR_FT_SURCHARGE_USD
        return _round10(point * RANGE_SPREAD_LOW), _round10(point * RANGE_SPREAD_HIGH)

    @staticmethod
    def _partial_range(linear_feet, miles, acc_fee):
        point = (
            PARTIAL_BASE_USD
            + miles * PARTIAL_PER_MILE_USD
            + linear_feet * PARTIAL_PER_LINEAR_FOOT_USD
            + acc_fee
        )
        return _round10(point * RANGE_SPREAD_LOW), _round10(point * RANGE_SPREAD_HIGH)


class _CarrierProviderBase:
    """Shared HTTP plumbing for the real carrier adapters. Subclasses map the
    carrier JSON into the same contract dict and raise FreightUnavailable on any
    HTTP / parse error (never a half-filled quote)."""

    name = "carrier"
    timeout = 20

    def __init__(self, api_key: str):
        self.api_key = api_key

    def _post_json(self, url: str, headers: dict, payload: dict) -> dict:
        try:
            import json as _json
            import urllib.request

            req = urllib.request.Request(
                url,
                data=_json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json", **headers},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return _json.loads(resp.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001 — any failure ⇒ estimator fallback
            raise FreightUnavailable(f"{self.name} API error: {e}") from e


class WarpProvider(_CarrierProviderBase):
    """Warp multi-carrier freight API (LTL + partial/volume), the one real
    adapter we keep. Endpoint POST /api/v1/ltl/quote, Bearer ``WARP_API_KEY``.

    Sandbox vs production base URL switches on ``WARP_ENV`` (sandbox default).
    The response shape is mapped to the contract dict; anything unexpected
    raises FreightUnavailable, which on the storefront means "fall back to the
    estimator" (see the module docstring), not "no quote".
    """

    name = "warp"
    SANDBOX_BASE = "https://api.sandbox.wearewarp.com"
    PROD_BASE = "https://api.wearewarp.com"

    def __init__(self, api_key: str, env: Optional[str] = None):
        super().__init__(api_key)
        self.env = (env or os.environ.get("WARP_ENV") or "sandbox").lower()
        self.base = self.PROD_BASE if self.env == "production" else self.SANDBOX_BASE

    def quote(self, origin_zip, dest_zip, quantity, delivery_env):
        cal = calibration_for_lot(None)
        acc = _accessorials(delivery_env)
        payload = {
            "origin": {"zipcode": origin_zip},
            "destination": {"zipcode": dest_zip, "residential": acc["residential"]},
            "accessorials": [k for k in ("residential", "liftgate") if acc[k]],
            "items": [
                {
                    "quantity": handling_units(quantity, cal),
                    "packaging": "PALLET",
                    "weight": total_weight_lb(quantity, cal),
                    "length": 48,
                    "width": 40,
                    "height": 72,
                    "freightClass": density_to_nmfc_class(
                        density_lb_ft3(quantity, cal)
                    ),
                }
            ],
        }
        data = self._post_json(
            f"{self.base}/api/v1/ltl/quote",
            {"Authorization": f"Bearer {self.api_key}"},
            payload,
        )
        return _map_carrier_rates(self.name, data, origin_zip, dest_zip, quantity, acc)


def _first_number(obj, keys):
    """Best-effort scan for the first numeric value under any of ``keys`` in a
    nested dict/list. Carrier JSON shapes drift; this keeps the adapter from
    breaking on a renamed field (still raises upstream if nothing is found)."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in keys and isinstance(v, (int, float)):
                return float(v)
            found = _first_number(v, keys)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _first_number(item, keys)
            if found is not None:
                return found
    return None


def _map_carrier_rates(provider, data, origin_zip, dest_zip, quantity, acc) -> dict:
    """Map a carrier response into the contract dict. Pulls a total charge and
    spreads it into a ± range (carriers return a firm number; we still present a
    range, re-confirmed at ship time). Raises FreightUnavailable if no rate."""
    total = _first_number(
        data,
        {"totalCharge", "total", "netCharge", "amount", "rate", "price", "grandTotal"},
    )
    if total is None or total <= 0:
        raise FreightUnavailable(f"{provider}: no usable rate in response")
    miles = lane_miles(origin_zip, dest_zip)
    # select_mode takes linear FEET of trailer, not the chair count.
    cal = calibration_for_lot(None)
    mode = select_mode(linear_feet(quantity, cal))
    low = _round10(total * 0.95)
    high = _round10(total * 1.15)
    is_partial = mode == "partial"
    return {
        "ltl_low": None if is_partial else low,
        "ltl_high": None if is_partial else high,
        "partial_low": low if mode in ("partial", "both") else None,
        "partial_high": high if mode in ("partial", "both") else None,
        "recommended_mode": "partial" if is_partial else "ltl",
        "mode": mode,
        "miles": int(round(miles)),
        "transit_days": transit_days(miles),
        "valid_until": (date.today() + timedelta(days=QUOTE_VALID_DAYS)).isoformat(),
        "accessorials": acc,
        "raw": {"provider": provider, "carrier_total": total, "response": data},
    }


# ===========================================================================
# Provider selection + public entry point
# ===========================================================================
def select_provider() -> FreightProvider:
    """Pick the live provider by env, precedence Warp → estimator."""
    warp = os.environ.get("WARP_API_KEY")
    if warp:
        return WarpProvider(warp)
    return EstimatorProvider()


def get_freight_estimate(
    origin_zip: str,
    dest_zip: str,
    quantity: int,
    delivery_env: str = "residential",
) -> dict:
    """Compute a freight cost RANGE for shipping ``quantity`` chairs origin→dest.

    Returns the contract dict::

        {ltl_low, ltl_high, partial_low, partial_high, recommended_mode, mode,
         miles, transit_days, valid_until, accessorials, provider, raw}

    ``delivery_env`` is "residential" (default; residential + liftgate ON),
    "dock" (forklift/dock confirmed → those accessorials dropped), or
    "storage" (self-storage unit → liftgate + limited-access, not residential).

    Raises :class:`FreightUnavailable` for an international/offshore/Alaska
    destination, an unresolvable zip, or a non-positive quantity — the caller
    offers a hand quote and never invents a number. A **carrier** failure is
    different: it falls back to :class:`EstimatorProvider` (the returned
    ``provider`` says which one answered) so a flaky third-party API can't cost
    the storefront a quotable lane.
    """
    if not quantity or quantity <= 0:
        raise FreightUnavailable("quantity must be a positive number of chairs")
    o = _resolve_zip(origin_zip)
    if o is None:
        raise FreightUnavailable(f"unresolvable origin zip {origin_zip!r}")
    if _is_international(dest_zip):
        raise FreightUnavailable(f"international/offshore destination {dest_zip!r}")
    d = _resolve_zip(dest_zip)
    provider = select_provider()
    try:
        quote = provider.quote(o, d, int(quantity), delivery_env)
    except FreightUnavailable:
        if isinstance(provider, EstimatorProvider):
            raise           # the lane itself is unquotable — hand it off
        provider = EstimatorProvider()
        quote = provider.quote(o, d, int(quantity), delivery_env)
    quote.setdefault("provider", provider.name)
    return quote


# Kept for the CRM, whose call sites (and PR #43) use the older name. Same dict.
get_freight_quote = get_freight_estimate


if __name__ == "__main__":
    import argparse
    import json as _json

    ap = argparse.ArgumentParser(description="Quote a chair-freight lane.")
    ap.add_argument("--origin", help="origin zip")
    ap.add_argument("--dest", help="destination zip")
    ap.add_argument("--qty", type=int, help="chair quantity")
    ap.add_argument(
        "--env", default="residential", choices=["residential", "dock", "storage"]
    )
    args = ap.parse_args()
    if args.origin and args.dest and args.qty:
        print(
            _json.dumps(
                get_freight_estimate(args.origin, args.dest, args.qty, args.env),
                indent=2,
            )
        )
    else:
        ap.print_help()
