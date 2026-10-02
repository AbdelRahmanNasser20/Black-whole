"""Real LTL carrier prices for one lane, from Warp's multi-carrier feed.

**Operator-only.** The number a buyer sees on a lot page comes from
`freight_estimate` and nothing else. This module answers a different question,
after the buyer already has their estimate: "what would real carriers charge for
that lane?" — so the Sales tab can show the cheapest carrier beside the range
we showed, and flag the ones where the two disagree.

**One endpoint: `POST /ltl/market-options`.** It returns every carrier's rate
for the lane (Averitt, XPO, ABF, Saia, FedEx, R+L …; 17-18 on the lanes probed
2026-10-02). It works without a key — the rates are then "indicative", priced on
a shared quote account — and takes a Bearer `WARP_API_KEY` when one is set.

**Never `POST /ltl/quote`.** On a lane Warp has no LTL service for, that
endpoint silently prices a DEDICATED TRUCK instead (`mode_substituted`): 72
chairs Las Vegas → Ohio came back $6,598 against a market of $846. A substituted
option is dropped here too, should the feed ever carry one.

Published contract: https://www.wearewarp.com/.well-known/openapi.json
Stdlib-only (urllib). Slow by design on Warp's side — typically ~20 s, up to
45 s — so callers run it in a worker thread, never on the event loop.
"""
from __future__ import annotations

import json
import math
import os
import urllib.error
import urllib.request
from datetime import date, timedelta

BASE_URL = "https://www.wearewarp.com/api/v1"
TIMEOUT_SEC = 60
MAX_RESPONSE_BYTES = 512 * 1024      # ~18 carriers is ~10 KB; anything huge is not our answer
USER_AGENT = "blackwhole-storefront/1.0"

# A standard 48x40 pallet. Height comes from the lot (85 in is Warp's LTL cap).
PALLET_LENGTH_IN = 48
PALLET_WIDTH_IN = 40
PALLET_TARE_LB = 40
MAX_PALLET_HEIGHT_IN = 85

# Past this a load is partial/volume or a full truck, not LTL: the feed's
# answer would be a number nobody would book. The row says "quote by hand".
MAX_LTL_PALLETS = 12

# How far the cheapest carrier may sit outside the range we showed the buyer
# before the Sales tab flags the row.
PRICE_CHECK_TOLERANCE = 0.25

OPTIONS_KEPT = 5

# Same default as the estimator: a storefront buyer has no dock.
DELIVERY_ACCESSORIALS = ("liftgate-delivery", "residential-delivery")


class WarpUnavailable(Exception):
    """The feed could not be reached or answered with something unusable."""


def enabled() -> bool:
    """Kill switch: `WARP_RATES_ENABLED=0` stops every outbound call."""
    return os.environ.get("WARP_RATES_ENABLED", "1").strip() != "0"


def pallets_for(quantity: int, chairs_per_pallet: float) -> int:
    """LTL pallets for a chair count (at least one)."""
    if quantity <= 0 or chairs_per_pallet <= 0:
        return 0
    return max(1, math.ceil(quantity / chairs_per_pallet))


def weight_per_pallet(quantity: int, pallets: int, lbs_per_chair: float) -> int:
    """Whole pounds on one pallet, chairs plus the pallet itself."""
    if pallets <= 0:
        return 0
    return int(round(quantity * lbs_per_chair / pallets)) + PALLET_TARE_LB


def pickup_date(today: date | None = None) -> str:
    """A plausible pickup day: three days out, never a weekend."""
    day = (today or date.today()) + timedelta(days=3)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day.isoformat()


def market_options(
    origin_zip: str,
    dest_zip: str,
    *,
    pallets: int,
    weight_lbs_per_pallet: int,
    height_in: int,
    pickup: str | None = None,
    api_key: str | None = None,
    timeout: float = TIMEOUT_SEC,
) -> list[dict]:
    """Every carrier's rate for the lane, as Warp returned them.

    Raises `WarpUnavailable` on a transport or HTTP error or a malformed body.
    An EMPTY list is a normal answer (Warp returns 200 with no options when its
    own aggregator fails, or when no carrier serves the lane).
    """
    body = {
        "origin_zip": str(origin_zip),
        "destination_zip": str(dest_zip),
        "pickup_date": pickup or pickup_date(),
        "pallets": int(pallets),
        "weight_lbs_per_pallet": int(weight_lbs_per_pallet),
        "length_in": PALLET_LENGTH_IN,
        "width_in": PALLET_WIDTH_IN,
        "height_in": min(int(height_in), MAX_PALLET_HEIGHT_IN),
        "accessorials": {"pickup": [], "delivery": list(DELIVERY_ACCESSORIALS)},
    }
    headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
    key = api_key if api_key is not None else os.environ.get("WARP_API_KEY")
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(
        f"{BASE_URL}/ltl/market-options",
        data=json.dumps(body).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise WarpUnavailable("warp: response too large")
            data = json.loads(raw.decode("utf-8"))
    except WarpUnavailable:
        raise
    except urllib.error.HTTPError as e:
        raise WarpUnavailable(f"warp HTTP {e.code}") from e
    except Exception as e:  # noqa: BLE001 — timeout, DNS, TLS, bad JSON
        raise WarpUnavailable(f"warp unreachable: {e}") from e
    if not isinstance(data, dict):
        raise WarpUnavailable("warp: unexpected response shape")
    options = data.get("market_options")
    if options is None:
        raise WarpUnavailable("warp: no market_options in response")
    if not isinstance(options, list):
        raise WarpUnavailable("warp: market_options is not a list")
    if data.get("mode_substituted"):
        # The lane was not priced as LTL at the response level. Warp's own entry
        # is then the dedicated truck; third-party LTL carriers are still LTL.
        options = [o for o in options if isinstance(o, dict) and not o.get("is_warp")]
    return [o for o in options if isinstance(o, dict)]


def _usable(option: dict) -> bool:
    if option.get("mode_substituted"):
        return False
    mode = str(option.get("mode") or "ltl").lower()
    if mode != "ltl":
        return False
    price = option.get("price_usd")
    return isinstance(price, (int, float)) and not isinstance(price, bool) and price > 0


def summarize(options: list[dict], limit: int = OPTIONS_KEPT) -> dict:
    """Boil a market-options list down to what `freight_quotes` stores.

    The cheapest usable price, who quoted it, how many carriers answered, and
    the ``limit`` cheapest options. Never the raw response.
    """
    usable = sorted((o for o in options if _usable(o)), key=lambda o: o["price_usd"])
    if not usable:
        return {
            "carrier_status": "none", "carrier_low": None, "carrier_name": None,
            "carrier_count": 0, "carrier_options": [],
        }
    kept = [
        {
            "carrier": str(o.get("carrier_name") or "unknown")[:80],
            "price_usd": round(float(o["price_usd"]), 2),
            "transit_days": o.get("transit_days") if isinstance(o.get("transit_days"), int) else None,
        }
        for o in usable[:limit]
    ]
    return {
        "carrier_status": "ok",
        "carrier_low": kept[0]["price_usd"],
        "carrier_name": kept[0]["carrier"],
        "carrier_count": len(usable),
        "carrier_options": kept,
    }


def unavailable(status: str) -> dict:
    """The summary for a check that produced no prices: 'too_big' | 'error'."""
    return {
        "carrier_status": status, "carrier_low": None, "carrier_name": None,
        "carrier_count": None, "carrier_options": [],
    }


def price_check(shown_low, shown_high, carrier_low) -> str | None:
    """How the range we showed the buyer compares with the cheapest carrier.

    'site_low'  — real carriers start well ABOVE what we showed (we under-quoted)
    'site_high' — real carriers start well BELOW it
    'ok'        — within `PRICE_CHECK_TOLERANCE`
    None        — nothing to compare
    """
    try:
        low, high, carrier = float(shown_low), float(shown_high), float(carrier_low)
    except (TypeError, ValueError):
        return None
    if low <= 0 or high <= 0 or carrier <= 0:
        return None
    if carrier > high * (1 + PRICE_CHECK_TOLERANCE):
        return "site_low"
    if carrier < low * (1 - PRICE_CHECK_TOLERANCE):
        return "site_high"
    return "ok"
