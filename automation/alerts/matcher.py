"""Match new-inventory lots to alert subscribers (BLACKWHOLE-10, PRD §4).

Pure Python over plain dicts — no DB, no network — so the whole matching
engine is unit-testable and the blast orchestration (`blast.py`) just feeds it
rows it already loaded. Adapts the PRD's matcher to the *shipped* `subscribers`
schema (single `chair_type` / `quantity_wanted` columns, `city/state/zip_code`,
no interests child table, no stored lat-lon/radius):

  1. Channel — this is the **email** blast (Resend-class provider, PRD P0). A
     subscriber with no email is skipped (SMS/Twilio is a later ticket).
  2. Status — `unsubscribed` / `bounced` never match (filtered by the caller's
     query; `match_lot` also defends against it).
  3. Chair type — subscriber `chair_type` must equal the lot's, or be a
     wildcard (`None` / `''` / `'any'`).
  4. Quantity — if the subscriber asked for `quantity_wanted`, the lot must be
     able to cover it (`quantity_remaining >= quantity_wanted`); unknown on
     either side is treated as a pass.
  5. Geo — haversine(subscriber, lot) <= radius. `radius_miles = 0` means
     "anywhere" (interest-only). Coordinates resolve zip → state-centroid →
     none; when either side is only state-precise, a same-state pair also passes
     (centroid math is ±200 mi noise — degrade honestly, PRD §4.3). A subscriber
     with no resolvable location can't be geo-filtered, so it passes (they opted
     into "ping me", we can't localize them).

`match_lot` returns one `Match` per matched subscriber with an audit
`match_reason` dict destined for `alert_sends.match_reason`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .geo import haversine_miles, resolve_latlon

CHANNEL_EMAIL = "email"
_WILDCARD_CHAIR = {None, "", "any"}
_ELIGIBLE_STATUSES = {"new", "contacted", "matched"}  # NOT unsubscribed/bounced/invalid


@dataclass
class Match:
    subscriber_id: Any
    email: str
    channel: str = CHANNEL_EMAIL
    match_reason: dict = field(default_factory=dict)


@dataclass
class Skip:
    subscriber_id: Any
    reason: str


def _norm(v: str | None) -> str | None:
    s = (v or "").strip().lower()
    return s or None


def _chair_matches(sub_type: str | None, lot_type: str | None) -> bool:
    st = _norm(sub_type)
    if st in _WILDCARD_CHAIR:
        return True
    return st == _norm(lot_type)


def _qty_matches(quantity_wanted: Any, quantity_remaining: Any) -> bool:
    """Lot must be able to fill the wanted quantity. Unknown on either side
    passes (we'd rather over-notify than silently drop a bulk buyer)."""
    if quantity_wanted in (None, "", 0):
        return True
    if quantity_remaining in (None, ""):
        return True
    try:
        return int(quantity_remaining) >= int(quantity_wanted)
    except (TypeError, ValueError):
        return True


def _geo_result(
    sub: dict, lot_lat: float | None, lot_lon: float | None,
    lot_precision: str | None, radius_miles: int,
) -> tuple[bool, dict]:
    """Return (passes, geo_reason_fragment)."""
    if radius_miles == 0:
        return True, {"distance_miles": None, "geo_precision": "anywhere"}

    s_lat, s_lon, s_prec = resolve_latlon(sub.get("zip_code"), sub.get("state"))
    if s_lat is None or lot_lat is None:
        # Can't localize one side — include rather than silently drop.
        return True, {"distance_miles": None, "geo_precision": "unresolved"}

    dist = haversine_miles(s_lat, s_lon, lot_lat, lot_lon)
    precision = "zip" if (s_prec == "zip" and lot_precision == "zip") else "state"
    reason = {"distance_miles": round(dist, 1), "geo_precision": precision,
              "radius_miles": radius_miles}
    if dist <= radius_miles:
        return True, reason
    # Overshot the radius. `match_lot` applies the same-state honest-degrade for
    # coarse (state) precision — two state centroids carry ±200 mi of noise.
    return False, reason


def match_lot(
    lot: dict,
    subscribers: list[dict],
    *,
    default_radius_miles: int = 100,
) -> tuple[list[Match], list[Skip]]:
    """Match `subscribers` against one inventory `lot`.

    Returns (matches, skips). `skips` carries a coarse reason per non-match for
    the preview/audit ("no_email", "chair_type", "out_of_radius", ...).
    """
    lot_lat, lot_lon, lot_precision = resolve_latlon(
        lot.get("zip_code"), lot.get("state")
    )
    lot_state = (lot.get("state") or "").strip().upper()

    matches: list[Match] = []
    skips: list[Skip] = []

    for sub in subscribers:
        sid = sub.get("id")
        if (sub.get("status") or "new") not in _ELIGIBLE_STATUSES:
            skips.append(Skip(sid, "not_active"))
            continue
        email = (sub.get("email") or "").strip()
        if not email:
            skips.append(Skip(sid, "no_email"))  # email blast only (SMS later)
            continue
        if not _chair_matches(sub.get("chair_type"), lot.get("chair_type")):
            skips.append(Skip(sid, "chair_type"))
            continue
        if not _qty_matches(sub.get("quantity_wanted"), lot.get("quantity_remaining")):
            skips.append(Skip(sid, "quantity"))
            continue

        radius = sub.get("radius_miles")
        radius = int(radius) if radius not in (None, "") else default_radius_miles
        passed, geo_reason = _geo_result(sub, lot_lat, lot_lon, lot_precision, radius)
        # Same-state honest-degrade for coarse precision (see _geo_result note).
        if not passed and geo_reason.get("geo_precision") == "state":
            if lot_state and (sub.get("state") or "").strip().upper() == lot_state:
                passed = True
                geo_reason = {**geo_reason, "same_state_pass": True}
        if not passed:
            skips.append(Skip(sid, "out_of_radius"))
            continue

        reason = {
            "chair_type": _norm(sub.get("chair_type")) or "any",
            "quantity_wanted": sub.get("quantity_wanted"),
            **geo_reason,
        }
        matches.append(Match(subscriber_id=sid, email=email, match_reason=reason))

    return matches, skips
