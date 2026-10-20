"""One lane, every carrier — the shared core behind three callers.

  * the buyer's price on a lot page (`POST /freight-estimate` in `web/app.py`):
    `buyer_quote` turns the cheapest carrier + the operator's padding into the
    one number shown and stored;
  * the operator's Sales › Lane quote form (`POST /api/freight/lane-quote`);
  * the terminal: `scripts/lane_quote.py --origin 04270 --dest 28206 --qty 100`.

Everything here is pure arithmetic over the lot's calibration and a Warp
`market_options` answer — no DB. The only network call is `fetch_options`,
which the CLI uses directly; the web app fetches through its own budgeted,
memoised worker pool (`_fetch_carrier_summary` in `web/app.py`) and hands the
summary in. Operator decision 2026-10-20: buyers see a carrier-backed price;
the estimator (`freight_estimate`) is the comparison column and the fallback.
"""
from __future__ import annotations

import re
from dataclasses import replace

from . import freight_estimate, warp_rates

ZIP5 = re.compile(r"^\d{5}$")

# The buyer's drop: residential + liftgate. `dock` = neither (a forklift and a
# dock on site); liftgate only = a self-storage unit; residential only = the
# driver does not unload — the buyer hand-unloads off the truck.
DEFAULT_RESIDENTIAL = True
DEFAULT_LIFTGATE = True


class LaneInput(ValueError):
    """A ZIP, a quantity or a chair number that cannot be quoted."""


def clean_zip(value, label: str = "zip") -> str:
    """Exactly five digits, or LaneInput. Never zero-padded: ``3003`` is a
    typo for an Atlanta ZIP, not New Hampshire (the storefront rule)."""
    s = str(value or "").strip()
    if not ZIP5.match(s):
        raise LaneInput(f"{label} must be a 5-digit US ZIP, got {s!r}")
    return s


def clean_quantity(value) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise LaneInput("quantity must be a whole number of chairs") from None
    if n <= 0:
        raise LaneInput("quantity must be at least 1")
    return n


def calibration(
    row: dict | None = None,
    *,
    lbs_per_chair=None,
    chairs_per_pallet=None,
    pallet_height_in=None,
) -> freight_estimate.LotCalibration:
    """The lot's chair data (`calibration_from_row`) with explicit overrides on
    top — a blank override keeps the lot's (or the standard chair's) value."""
    cal = freight_estimate.calibration_from_row(row)
    changes: dict = {}
    for key, value in (
        ("lbs_per_chair", lbs_per_chair),
        ("chairs_per_pallet", chairs_per_pallet),
        ("pallet_height_in", pallet_height_in),
    ):
        if value in (None, ""):
            continue
        n = freight_estimate._positive(value)
        if n is None:
            raise LaneInput(f"{key} must be a positive number")
        changes[key] = int(round(n)) if key == "pallet_height_in" else n
    if "lbs_per_chair" in changes:
        changes["lbs_per_chair_estimated"] = False
    return replace(cal, **changes) if changes else cal


def shipment(quantity: int, cal: freight_estimate.LotCalibration) -> dict:
    """How the chairs go on the truck: pallets, pounds per pallet, height.
    ``too_big`` = more pallets than LTL takes (`warp_rates.MAX_LTL_PALLETS`)."""
    pallets = warp_rates.pallets_for(quantity, cal.chairs_per_pallet)
    height = min(int(cal.pallet_height_in), warp_rates.MAX_PALLET_HEIGHT_IN)
    return {
        "pallets": pallets,
        "weight_lbs_per_pallet": warp_rates.weight_per_pallet(quantity, pallets, cal.lbs_per_chair),
        "height_in": height,
        "chairs_per_pallet": cal.chairs_per_pallet,
        "lbs_per_chair": cal.lbs_per_chair,
        "lbs_per_chair_estimated": cal.lbs_per_chair_estimated,
        "too_big": pallets > warp_rates.MAX_LTL_PALLETS,
    }


def lane_key(origin_zip: str, dest_zip: str, ship: dict, delivery: tuple[str, ...]) -> tuple:
    """What makes two carrier asks the same ask (the web app's memo key)."""
    return (
        str(origin_zip), str(dest_zip), int(ship["pallets"]),
        int(ship["weight_lbs_per_pallet"]), int(ship["height_in"]), tuple(delivery),
    )


def delivery_env(*, residential: bool, liftgate: bool) -> str:
    """The estimator's `delivery_env` for a drop. It has no "residential
    without liftgate" mode, so that case is priced as residential (≈ $90
    high) — the comparison column, not the buyer's number."""
    if not residential and not liftgate:
        return "dock"
    if liftgate and not residential:
        return "storage"
    return "residential"


def estimator(
    origin_zip: str, dest_zip: str, quantity: int, cal, *,
    residential: bool = DEFAULT_RESIDENTIAL, liftgate: bool = DEFAULT_LIFTGATE,
) -> dict | None:
    """The in-house range for the lane, or None when it refuses (offshore,
    unresolvable) — never a guess."""
    try:
        return freight_estimate.get_freight_estimate(
            origin_zip, dest_zip, quantity,
            delivery_env(residential=residential, liftgate=liftgate), cal,
        )
    except freight_estimate.FreightUnavailable:
        return None


def fetch_options(
    origin_zip: str, dest_zip: str, quantity: int, cal, *,
    residential: bool = DEFAULT_RESIDENTIAL, liftgate: bool = DEFAULT_LIFTGATE,
    timeout: float = warp_rates.TIMEOUT_SEC,
) -> list[dict]:
    """Ask Warp for the lane (the CLI's path — no budget, no memo). Raises
    `warp_rates.WarpUnavailable`; a load too big for LTL raises LaneInput
    rather than asking for a number nobody would book."""
    ship = shipment(quantity, cal)
    if ship["pallets"] <= 0:
        raise LaneInput("nothing to ship")
    if ship["too_big"]:
        raise LaneInput(
            f"{ship['pallets']} pallets is more than LTL takes "
            f"({warp_rates.MAX_LTL_PALLETS}) — quote a partial/full truck by hand"
        )
    return warp_rates.market_options(
        origin_zip, dest_zip,
        pallets=ship["pallets"],
        weight_lbs_per_pallet=ship["weight_lbs_per_pallet"],
        height_in=ship["height_in"],
        timeout=timeout,
        delivery=warp_rates.delivery_accessorials(residential=residential, liftgate=liftgate),
    )


def carriers_table(options: list[dict]) -> list[dict]:
    """Every usable carrier, cheapest first — `{carrier, price_usd, transit_days}`."""
    return list(warp_rates.summarize(options, limit=None)["carrier_options"])


def result(
    origin_zip: str, dest_zip: str, quantity: int, cal, *,
    residential: bool, liftgate: bool, summary: dict | None, padding_pct: float,
    error: str | None = None,
) -> dict:
    """The operator-facing answer for one lane: the full carrier list (no
    padding), what the site WOULD show (padded), and the estimator beside it.
    ``summary`` is a `warp_rates.summarize(..., limit=None)` dict, or None when
    the feed was not asked (then ``error`` says why)."""
    ship = shipment(quantity, cal)
    est = estimator(origin_zip, dest_zip, quantity, cal, residential=residential, liftgate=liftgate)
    summary = summary or warp_rates.unavailable("too_big" if ship["too_big"] else "error")
    carriers = list(summary.get("carrier_options") or [])
    cheapest = carriers[0] if carriers else None
    return {
        "origin_zip": origin_zip,
        "dest_zip": dest_zip,
        "quantity": quantity,
        "residential": bool(residential),
        "liftgate": bool(liftgate),
        "delivery_accessorials": list(warp_rates.delivery_accessorials(
            residential=residential, liftgate=liftgate)),
        "shipment": ship,
        "carrier_status": summary.get("carrier_status"),
        "carrier_count": summary.get("carrier_count"),
        "carriers": carriers,
        "cheapest": cheapest,
        "padding_pct": padding_pct,
        "padded_price": warp_rates.padded_price(summary.get("carrier_low"), padding_pct),
        "estimator": None if est is None else {
            "mode": est.get("mode"),
            "recommended_mode": est.get("recommended_mode"),
            "ltl_low": est.get("ltl_low"), "ltl_high": est.get("ltl_high"),
            "partial_low": est.get("partial_low"), "partial_high": est.get("partial_high"),
            "miles": est.get("miles"),
            "transit_days": est.get("transit_days"),
        },
        "error": error,
    }


def buyer_quote(
    est: dict, summary: dict, padding_pct: float, *, no_liftgate: dict | None = None,
) -> dict | None:
    """The quote a buyer sees and `freight_quotes` stores when a carrier answered.

    Same keys as the estimator's dict (`freight_log` reads them unchanged) with
    the shown range collapsed to ONE padded, whole-dollar number
    (``ltl_low == ltl_high``), ``provider='warp'``, the carrier's own transit
    days, and the estimator's range kept under ``raw['estimator']`` for the
    audit trail. ``carrier`` is the summary `freight_log` writes to the
    `carrier_*` columns. ``without_liftgate`` is the padded price for a drop
    where the driver does not unload, only when it is actually cheaper.

    None when the summary carries no usable price — the caller falls back.
    """
    price = warp_rates.padded_price(summary.get("carrier_low"), padding_pct)
    if summary.get("carrier_status") != "ok" or price is None:
        return None
    cheapest = (summary.get("carrier_options") or [{}])[0]
    without = None
    if no_liftgate and no_liftgate.get("carrier_status") == "ok":
        candidate = warp_rates.padded_price(no_liftgate.get("carrier_low"), padding_pct)
        if candidate is not None and candidate < price:
            without = candidate
    transit = cheapest.get("transit_days")
    # Warp's quotes expire (1-3 days out). The buyer's "valid until" is the
    # cheapest option's expiry day; the estimator's 7-day window only when
    # Warp sent none.
    valid_until = warp_rates.expires_date(cheapest.get("expires_at")) or est.get("valid_until")
    raw = dict(est.get("raw") or {})
    raw.update({
        "provider": "warp",
        "padding_pct": padding_pct,
        "carrier_low": summary.get("carrier_low"),
        "carrier_name": summary.get("carrier_name"),
        "carrier_count": summary.get("carrier_count"),
        "carrier_expires_at": cheapest.get("expires_at"),
        "carrier_bookable": bool(cheapest.get("bookable")),
        "price_without_liftgate": without,
        "carrier_low_without_liftgate": (no_liftgate or {}).get("carrier_low"),
        "estimator": {
            k: est.get(k) for k in ("mode", "ltl_low", "ltl_high", "partial_low", "partial_high")
        },
    })
    return {
        "mode": "ltl",
        "recommended_mode": "ltl",
        "ltl_low": price,
        "ltl_high": price,
        "partial_low": None,
        "partial_high": None,
        "miles": est.get("miles"),
        "transit_days": transit if isinstance(transit, int) else est.get("transit_days"),
        "valid_until": valid_until,
        "accessorials": est.get("accessorials"),
        "provider": "warp",
        "carrier": summary,
        "without_liftgate": without,
        "raw": raw,
    }
