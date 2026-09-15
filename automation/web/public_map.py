"""Public inventory map — the ONLY read model behind /map, the home map and the
listing mini-map (2026-09-15 plan).

Rules (tests enforce them):
- Allow-list. A point carries exactly POINT_KEYS. `storage_note`, zip, contact
  fields, auction links and raw GovDeals image URLs never leave this module.
- City-level pins: geo.resolve_place (city → zip → state) — never an address.
- Buckets: available (listed/owned/draft with stock) · incoming (won_pickup,
  active_bid, favorited auctions) · sold (sold_out/lost_sold_out/fake_sold_out).
- Favorites are redacted: title + quantity + city + clean photo. No link, no
  asset id, no bid, no close time. `#private` in notes = never shown.
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from typing import Iterable

from automation import favorites as favorites_mod
from automation import inventory, lot_channels, lot_images
from automation.alerts import geo
from automation.web import readcache

log = logging.getLogger(__name__)

BUCKETS = ("available", "incoming", "sold")
# Radius behind the listing page's "N other lots within N mi" block.
NEARBY_MILES = 200
INCOMING_STATUSES = frozenset({"won_pickup", "active_bid"})
AVAILABLE_STATUSES = frozenset({"listed", "owned", "draft"})
HIDDEN_STATUSES = frozenset({"hidden", "lost"})
POINT_KEYS = frozenset({
    "id", "kind", "lot_id", "title", "bucket", "quantity", "unit", "price_per_chair",
    "city", "state", "lat", "lng", "precision", "hero", "url",
})
CACHE_TTL = 300


def bucket(row: dict) -> str | None:
    row = row or {}
    status = row.get("status")
    if status in HIDDEN_STATUSES:
        # Off the map entirely — `fake_sold_out` must never resurrect a
        # deliberately hidden or lost lot as a "sold" trophy pin.
        return None
    if status in inventory.SOLD_STATUSES or row.get("fake_sold_out"):
        return "sold"
    if status in INCOMING_STATUSES:
        return "incoming"
    if status in AVAILABLE_STATUSES:
        qty = row.get("quantity_remaining")
        return "available" if qty is None or qty > 0 else None
    return None


def _places(row: dict) -> list[dict]:
    """[{city, state, quantity}] — primary city first, then `locations` extras."""
    out: list[dict] = []
    seen: dict[tuple[str, str], dict] = {}
    entries = inventory.parse_locations(row.get("locations")) or []
    primary = {"city": row.get("city"), "state": row.get("state"), "quantity": None}
    for e in [primary, *entries]:
        city, state = (e.get("city") or "").strip(), (e.get("state") or row.get("state") or "").strip()
        if not city:
            continue
        key = (city.lower(), state.upper())
        prior = seen.get(key)
        if prior is not None:
            # The primary city re-listed under `locations` carries the real
            # split for that city — take its quantity, keep one pin.
            if prior["quantity"] is None:
                prior["quantity"] = e.get("quantity")
            continue
        place = {"city": city, "state": state or None, "quantity": e.get("quantity")}
        seen[key] = place
        out.append(place)
    return out


def _point(**kw) -> dict:
    p = {k: kw.get(k) for k in POINT_KEYS}
    return p


def points_from_inventory(rows: Iterable[dict]) -> list[dict]:
    pts: list[dict] = []
    for row in rows:
        b = bucket(row)
        if b is None:
            continue
        places = _places(row)
        for i, place in enumerate(places):
            lat, lng, prec = geo.resolve_place(place["city"], place["state"],
                                               row.get("zip_code") if i == 0 else None)
            if lat is None:
                continue
            qty = place["quantity"] if place["quantity"] is not None else (
                row.get("quantity_original") if b == "sold" else row.get("quantity_remaining"))
            pts.append(_point(
                id=f"{row['lot_id']}#{i}", kind="lot", lot_id=row["lot_id"],
                title=row.get("title") or row["lot_id"], bucket=b, quantity=qty,
                unit=lot_channels.unit_word(row).upper(),
                price_per_chair=(float(row["price_per_chair"])
                                 if row.get("price_per_chair") is not None else None),
                city=place["city"], state=place["state"], lat=lat, lng=lng, precision=prec,
                hero=lot_images.hero_src(row), url=f"/listings/{row['lot_id']}",
            ))
    return pts


def points_from_favorites(favs: Iterable[favorites_mod.Favorite]) -> list[dict]:
    now = datetime.now(timezone.utc)
    pts: list[dict] = []
    for f in favs:
        if f.is_private:
            continue
        end = f.end_dt
        if end is not None and end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        if end is not None and end < now:
            continue
        city, state, _zip = geo.parse_place(f.location)
        lat, lng, prec = geo.resolve_place(city, state, None)
        if lat is None:
            continue
        # Opaque, stable pin id. A readable asset id would hand the public the
        # GovDeals URL for a lot we are still bidding on.
        key = hashlib.sha256(f.asset_id.encode()).hexdigest()[:12]
        pts.append(_point(
            id=f"fav-{key}", kind="favorite", lot_id=None,
            title=f.title or "Incoming lot", bucket="incoming", quantity=f.quantity,
            unit=lot_channels.unit_word(f.title or "").upper(),
            price_per_chair=None, city=city, state=state, lat=lat, lng=lng,
            precision=prec, hero=f.clean_hero_url or None, url="/#contact",
        ))
    return pts


@readcache.cached(ttl=CACHE_TTL)
def all_points() -> list[dict]:
    """Every public pin. Memoised; readcache drops it on any successful API write."""
    rows = [*inventory.list_public(), *inventory.list_sold_showcase()]
    seen: set[str] = set()
    uniq = [r for r in rows if not (r["lot_id"] in seen or seen.add(r["lot_id"]))]
    pts = points_from_inventory(uniq)
    try:
        pts += points_from_favorites(favorites_mod.list_all())
    except Exception as e:  # noqa: BLE001 — favorites trouble must not blank the map
        log.warning("public map: favorites unavailable: %r", e)
    return pts


def _resolve_near(text: str | None) -> dict | None:
    if not text or not text.strip():
        return None
    city, state, zip_code = geo.parse_place(text)
    lat, lng, prec = geo.resolve_place(city, state, zip_code)
    if lat is None:
        return None
    return {"lat": lat, "lng": lng, "label": text.strip(), "precision": prec}


def fetch_points(*, statuses: set[str] | None = None, near: str | None = None,
                 radius_mi: float | None = None) -> dict:
    pts = [dict(p) for p in all_points()]
    origin = _resolve_near(near)
    if origin:
        for p in pts:
            p["distance_mi"] = round(geo.haversine_miles(origin["lat"], origin["lng"], p["lat"], p["lng"]), 1)
        if radius_mi:
            pts = [p for p in pts if p["distance_mi"] <= radius_mi]
        pts.sort(key=lambda p: p["distance_mi"])
    # Counts are the legend: how many of each bucket sit in the visible area.
    # Area (near + radius) narrows them; the bucket checkboxes do not, or the
    # legend would only ever report the buckets already ticked.
    counts = {b: sum(1 for p in pts if p["bucket"] == b) for b in BUCKETS}
    if statuses:
        pts = [p for p in pts if p["bucket"] in statuses]
    return {"points": pts, "counts": counts, "near": origin}


def nearby(lot_id: str, *, miles: float = 200, limit: int = 6) -> dict:
    pts = all_points()
    mine = [p for p in pts if p["lot_id"] == lot_id]
    if not mine:
        return {"origin": None, "items": []}
    o = mine[0]
    # One entry per lot — a multi-location lot takes one of the `limit` slots
    # at its nearest pin, not one slot per warehouse.
    best: dict[str | None, dict] = {}
    loose: list[dict] = []
    for p in pts:
        if p["lot_id"] == lot_id or p["bucket"] == "sold":
            continue
        d = geo.haversine_miles(o["lat"], o["lng"], p["lat"], p["lng"])
        if d > miles:
            continue
        item = {**p, "distance_mi": round(d, 1)}
        if p["lot_id"] is None:  # favorites carry no lot_id — never collapse them
            loose.append(item)
            continue
        prior = best.get(p["lot_id"])
        if prior is None or item["distance_mi"] < prior["distance_mi"]:
            best[p["lot_id"]] = item
    items = [*best.values(), *loose]
    items.sort(key=lambda p: p["distance_mi"])
    return {"origin": {"lat": o["lat"], "lng": o["lng"], "precision": o["precision"]},
            "items": items[:limit]}
