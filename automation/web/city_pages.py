"""City landing pages — `/chairs/{city-slug}` (SEO city pages, 2026-10-07).

Why: Search Console already shows impressions for "bulk seating", "banquet
chairs wholesale" and city-flavoured variants, but the only pages that name
a city are individual lot pages that come and go. A city page is the stable
URL for "used banquet chairs Atlanta": every live lot there, the sold
archive as proof, the lots within driving distance, and the pickup/freight
facts — server-rendered, so it reads the same to a crawler and a buyer.

Read model only. Rows come from inventory.list_public() /
list_sold_showcase() (the same gates as /listings and the sitemap), points
from public_map.all_points() (allow-listed, city-level). A lot that sits in
several cities (`locations`) appears on each of its city pages.
`storage_note` is never read.

Indexability: a city with at least one live lot is in the sitemap; a city
with only sold lots still renders (anyone with the link gets the archive
and the nearby lots) but carries noindex so it can't become a thin page.
"""

from __future__ import annotations

import zlib
from typing import Any

from automation import inventory, lot_urls
from automation.alerts import geo
from automation.web import public_map, readcache, seo_copy

CACHE_TTL = 300
NEARBY_MILES = 300
NEARBY_LIMIT = 8


def _places(row: dict) -> list[tuple[str, str]]:
    """(city, state) for the primary location plus every extra `locations`
    entry, de-duplicated, primary first."""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(city: Any, state: Any) -> None:
        c = str(city or "").strip()
        s = str(state or "").strip()
        if not c:
            return
        key = lot_urls.city_slug(c, s)
        if key and key not in seen:
            seen.add(key)
            out.append((c, s))

    add(row.get("city"), row.get("state"))
    locs = row.get("locations")
    if isinstance(locs, str):
        try:
            locs = inventory.parse_locations(locs)
        except ValueError:
            locs = None
    for loc in locs or []:
        if isinstance(loc, dict):
            add(loc.get("city"), loc.get("state"))
    return out


@readcache.cached(ttl=CACHE_TTL)
def index() -> dict[str, dict]:
    """slug → city page record. Memoised; any admin write drops the memo."""
    cities: dict[str, dict] = {}

    def bucket(row: dict, key: str) -> None:
        for city, state in _places(row):
            slug = lot_urls.city_slug(city, state)
            rec = cities.setdefault(slug, {
                "slug": slug, "city": city, "state": state,
                "label": f"{city}, {state}" if state else city,
                "path": lot_urls.city_path(city, state),
                "live": [], "sold": [],
            })
            rec[key].append(row)

    for row in inventory.list_public():
        bucket(row, "live")
    for row in inventory.list_sold_showcase():
        bucket(row, "sold")

    for rec in cities.values():
        rec["chairs"] = sum(int(r.get("quantity_remaining") or 0) for r in rec["live"])
        rec["moved"] = sum(int(r.get("quantity_original") or 0) for r in rec["sold"])
        prices = [float(r["price_per_chair"]) for r in rec["live"]
                  if r.get("price_per_chair") and float(r["price_per_chair"]) > 0]
        rec["min_price"] = min(prices) if prices else None
        rec["indexable"] = bool(rec["live"])
        rec["kinds"] = sorted({seo_copy.kind(r) for r in rec["live"]} or {"banquet"})
    return cities


def listing(*, indexable_only: bool = False) -> list[dict]:
    """Cities for the sitemap / the map page list — most live chairs first."""
    recs = [r for r in index().values() if r["indexable"] or not indexable_only]
    return sorted(recs, key=lambda r: (-r["chairs"], -r["moved"], r["label"]))


def nearby(city: str, state: str, *, exclude_slug: str,
           miles: float = NEARBY_MILES, limit: int = NEARBY_LIMIT) -> list[dict]:
    """Unsold map points within `miles` of the city, one per lot, nearest first."""
    lat, lng, _ = geo.resolve_place(city, state, None)
    if lat is None:
        return []
    best: dict[str, dict] = {}
    for p in public_map.all_points():
        if p["bucket"] == "sold" or p.get("lot_id") is None:
            continue
        if lot_urls.city_slug(p.get("city"), p.get("state")) == exclude_slug:
            continue
        d = geo.haversine_miles(lat, lng, p["lat"], p["lng"])
        if d > miles:
            continue
        item = {**p, "distance_mi": round(d, 1)}
        prior = best.get(p["lot_id"])
        if prior is None or item["distance_mi"] < prior["distance_mi"]:
            best[p["lot_id"]] = item
    items = sorted(best.values(), key=lambda p: p["distance_mi"])
    return items[:limit]


def _pick(seed: str, options: tuple[str, ...]) -> str:
    return options[zlib.crc32(seed.encode("utf-8")) % len(options)]


def intro(rec: dict) -> str:
    label = rec["label"]
    kinds = " and ".join(f"{seo_copy._KIND_WORD[k]} chairs" for k in rec["kinds"][:2])
    if rec["live"]:
        n = len(rec["live"])
        lots = f"{n} lot{'s' if n != 1 else ''}"
        price = f" from ${rec['min_price']:,.0f} per chair" if rec.get("min_price") else ""
        return _pick(rec["slug"], (
            f"Used {kinds} in {label}: {lots} on the floor right now, {rec['chairs']:,} chairs{price}. "
            f"Every lot is one matched set from a single venue — counted, photographed as it sits, "
            f"and priced by the chair. Pick up in {label} for free, or we palletise and ship.",
            f"{rec['chairs']:,} commercial-grade chairs are staged in {label} today across {lots}{price}. "
            f"These are matched sets pulled from hotels, universities and event halls, sold by the lot "
            f"at liquidation prices. Local pickup is free; freight is quoted per ZIP on each lot page.",
            f"Looking for bulk {kinds} near {label}? {lots} are on the floor ({rec['chairs']:,} chairs{price}). "
            f"One finish, one frame, one pickup — no mixing catalog orders to seat a hall. "
            f"Drive up free, or get a freight estimate from the lot page.",
        ))
    moved = rec.get("moved") or 0
    return (
        f"Nothing is on the floor in {label} at the moment"
        + (f" — {moved:,} chairs have moved through here already" if moved else "")
        + ". The lots below are the closest sets we have today, and the alert list hears about the "
          f"next {label} lot before it goes public."
    )


def faq(rec: dict) -> list[tuple[str, str]]:
    label = rec["label"]
    city = rec["city"]
    return [
        (f"Can I pick up chairs in {city}?",
         f"Yes — pickup in {label} is free. Bring a box truck, trailer or a few vans; we load with you. "
         "The exact pickup address comes with your confirmation."),
        (f"Do you deliver outside {city}?",
         "Yes. We palletise and shrink-wrap the chairs and ship LTL or by dedicated truck anywhere in the "
         "continental US. Each lot page has a freight form that prices your ZIP before you commit."),
        ("Can I buy part of a lot?",
         "Usually. Lots split by the pallet for local pickup; for freight the minimum is whatever keeps the "
         "shipping sensible — ask and we will tell you straight."),
        (f"How do I hear about the next {city} lot?",
         "Join the alert list at the bottom of the inventory page, or send a note through the contact form "
         "with the count you need — we flag matching lots before they are listed."),
    ]


def page(slug: str) -> dict | None:
    """Everything city.html needs, or None for an unknown city."""
    rec = index().get(slug)
    if rec is None:
        return None
    rec = dict(rec)
    try:
        rec["nearby"] = nearby(rec["city"], rec["state"], exclude_slug=slug)
    except Exception:  # noqa: BLE001 — the page must render without geo
        rec["nearby"] = []
    rec["intro"] = intro(rec)
    rec["faq"] = faq(rec)
    rec["faq_jsonld"] = seo_copy.faq_jsonld(rec["faq"])
    rec["title"] = f"Bulk Banquet Chairs for Sale in {rec['label']} | Black Whole"
    kinds = " and ".join(f"{seo_copy._KIND_WORD[k]} chairs" for k in rec["kinds"][:2])
    rec["description"] = (
        f"Used {kinds} in {rec['label']} sold by the lot — "
        + (f"{rec['chairs']:,} chairs on the floor, " if rec["live"] else "")
        + "free local pickup or nationwide freight. Matched commercial sets at liquidation prices."
    )[:155]
    return rec
