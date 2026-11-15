"""Read model for the public auction-lot page (`/deals/lot/...`).

One lot, any source. No single table holds every source, so `resolve` tries
three, in order, and returns one shape:

1. `deal_lots` by `(site, native_id)` — GovDeals, AllSurplus, TXAuction.
2. the `deals_sources` memo (newest `listing_snapshots` row per lot) — every
   other recorder source. Memo only (`deals_sources.find`): a cold memo is a
   miss and starts the background load; the heavy read never runs in a request.
3. `lot_archive` by `(source, lot_key)` — a closed lot that has left 1 and 2.

Rules:

* **Public policy first.** A lot the operator starred / tracked / listed is a
  404 on every path (`public_deals.exclusion_where`, the memo's own pick
  filter, `public_sold.base_where`). A hidden `deal_lots` row never falls
  through to another table.
* **"Sold for $X" is a confirmed sale only**: the `lot_archive` row says
  `outcome = 'sold'` with a `final_price` whose currency is exactly `USD`
  (the rule `public_sold.SOLD` uses; an unknown currency is not USD). Any
  other ended lot says the final price is not confirmed and is `noindex`.
* **A "$" is only ever a USD amount.** A live lot in another currency shows no
  price, no cost total and no Offer (`price_on_source`).
* **Photos are the source site's own links**, at most `MAX_PHOTOS`, https
  only. Never `archived_hero_url` / `gallery_urls` (our private copies).
  `photos_mode()` (site setting `deal_lot_photos`) can switch them off.
* Allow-list: seller, contact, bidder and verdict fields never enter the lot dict.
"""
from __future__ import annotations

import html
import re

import time
from datetime import datetime, timezone
from typing import Any

from .. import db, site_settings
from . import deal_lot_urls, deals_sources, lot_costs, public_deals, public_sold

MAX_PHOTOS = 3
SIMILAR = 6
DESC_MAX = 6000
PHOTOS_OFF, PHOTOS_SIGNED_IN, PHOTOS_ALL = 0, 1, 2
_MODE_TTL = 300
_mode: dict[str, Any] = {"at": 0.0, "value": None}

DEAL_LOT_SITES = ("govdeals", "allsurplus", "txauction")      # sources that live in deal_lots
ARCHIVE_SOURCES = ("govdeals", "allsurplus")                  # sources with confirmed closes
_MAESTRO_HOST = {"govdeals": "https://www.govdeals.com", "allsurplus": "https://www.allsurplus.com"}
SOURCE_NAMES = {**deals_sources.SITES, "txauction": "TXAuction"}

_DROP_BLOCKS = re.compile(r"<(script|style)\b.*?</\1\s*>", re.I | re.S)
_BREAK_TAGS = re.compile(r"<\s*(?:br\s*/?|/p|/li|/div|/tr|/h[1-6])\s*>", re.I)
_ANY_TAG = re.compile(r"<[^>]*>")


def plain_text(value) -> str:
    """A source description as words: listings store HTML, the page shows none."""
    text = _DROP_BLOCKS.sub("", str(value or ""))
    text = _ANY_TAG.sub("", _BREAK_TAGS.sub("\n", text))
    lines = (" ".join(line.split()) for line in html.unescape(text).splitlines())
    return "\n".join(line for line in lines if line)


_DL_COLS = ("site, native_id, asset_id, account_id, auction_id, title, description, "
            "canonical_category, native_category_name, city, state, lat, lng, bid_count, "
            "current_bid, currency_code, end_utc, outcome, final_bid_count, outcome_complete, "
            "first_seen_at, hero_image_url")
_ARCHIVE_COLS = "outcome, final_price, currency, closed_at, bid_count"


def clear_cache() -> None:
    _mode.update(at=0.0, value=None)


def photos_mode() -> int:
    """0 = off, 1 = signed-in visitors, 2 = everyone. Memoised 5 min; a failed
    read answers the default (`site_settings.get_all` never raises)."""
    now = time.monotonic()
    if _mode["value"] is None or now - _mode["at"] > _MODE_TTL:
        _mode["value"] = int(site_settings.get_all().get("deal_lot_photos", PHOTOS_ALL))
        _mode["at"] = now
    return _mode["value"]


def show_photos(signed_in: bool) -> bool:
    mode = photos_mode()
    return mode == PHOTOS_ALL or (mode == PHOTOS_SIGNED_IN and signed_in)


# ── helpers ──────────────────────────────────────────────────────────────────

def _aware(v: Any) -> datetime | None:
    if not isinstance(v, datetime):
        return None
    return v if v.tzinfo else v.replace(tzinfo=timezone.utc)


def _num(v: Any) -> float | None:
    return float(v) if v is not None else None


def _source_url(source: str, key: str) -> str | None:
    parts = key.split("/")
    if source in _MAESTRO_HOST:
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            return f"{_MAESTRO_HOST[source]}/en/asset/{parts[0]}/{parts[1]}"
        return None
    if source == "txauction":
        try:
            from deals.adapters.txauction import lot_url
            return lot_url(key)
        except Exception:  # noqa: BLE001 - a bad key has no link, the page still renders
            return None
    return None


def confirmed_sale(archive: dict | None) -> tuple[float | None, datetime | None]:
    """(price, closed_at) when the archive row is a confirmed USD sale, else (None, closed_at)."""
    if not archive:
        return None, None
    closed = _aware(archive.get("closed_at"))
    if (archive.get("outcome") == "sold" and archive.get("final_price") is not None
            and archive.get("currency") == public_sold.CURRENCY):       # NULL = unknown, never USD
        return float(archive["final_price"]), closed
    return None, closed


def _finish(lot: dict, archive: dict | None, now: datetime) -> dict:
    lot["source_name"] = SOURCE_NAMES.get(lot["source"], lot["source"])
    lot["sold_price"], closed_at = confirmed_sale(archive)
    lot["closed_at"] = closed_at or (lot["end_utc"] if lot.get("ended") else None)
    end = lot.get("end_utc")
    lot["ended"] = bool(lot.get("ended") or lot["sold_price"] is not None or (end and end <= now))
    lot["photos"] = [p for p in lot.get("photos") or [] if p][:MAX_PHOTOS]
    lot.setdefault("price_on_source", False)
    lot["path"] = deal_lot_urls.build(lot["source"], lot["key"], title=lot.get("title"), state=lot.get("state"))
    return lot


def _archive_row(conn, source: str, key: str) -> dict | None:
    if source not in ARCHIVE_SOURCES:
        return None
    try:
        row = conn.execute(f"SELECT {_ARCHIVE_COLS} FROM lot_archive WHERE source = %s AND lot_key = %s",
                           (source, key)).fetchone()
    except Exception:  # noqa: BLE001 - the page renders without a confirmed price
        return None
    return dict(row) if row else None


# ── the three backings ───────────────────────────────────────────────────────

def _from_deal_lots(source: str, key: str, now: datetime) -> dict | None | bool:
    """The lot, None when deal_lots has no such row, or False when the row
    exists and the public must not see it (the caller stops there)."""
    ex_where, ex_args = public_deals.exclusion_where()
    with db.connect() as conn:
        row = conn.execute(
            f"SELECT {_DL_COLS}, ({ex_where}) AS public_ok FROM deal_lots "
            "WHERE site = %s AND native_id = %s", (*ex_args, source, key)).fetchone()
        if not row:
            return None
        row = dict(row)
        if not row.pop("public_ok") or public_deals.is_excluded(row):
            return False
        archive = _archive_row(conn, source, key)
    # The bid is shown with a "$": only a USD amount may be. Any other currency
    # (an AllSurplus lot in CAD) shows no number; the source page has the price.
    usd = row.get("currency_code") == public_sold.CURRENCY
    return _finish({
        "source": source, "key": key, "backing": "deal_lots", "price_on_source": not usd,
        "title": row.get("title"), "description": plain_text(row.get("description"))[:DESC_MAX] or None,
        "category": row.get("canonical_category"), "native_category": row.get("native_category_name"),
        "city": row.get("city"), "state": row.get("state"), "lat": row.get("lat"), "lng": row.get("lng"),
        "current_bid": _num(row.get("current_bid")) if usd else None, "bid_count": row.get("bid_count"),
        "end_utc": _aware(row.get("end_utc")), "first_seen_at": _aware(row.get("first_seen_at")),
        "ended": bool(row.get("outcome_complete") or row.get("outcome")),
        "url": _source_url(source, key),
        "photos": [deals_sources.safe_photo_url(row.get("hero_image_url"))],
    }, archive, now)


def _from_snapshots(source: str, key: str, now: datetime) -> dict | None:
    if source not in deals_sources.SOURCES:
        return None
    hit = deals_sources.find(source, key, now=now)       # memo only; cold = miss
    if hit is None:
        return None
    archive = None
    if source in ARCHIVE_SOURCES:
        with db.connect() as conn:
            archive = _archive_row(conn, source, key)
    return _finish({
        "source": source, "key": key, "backing": "snapshot",
        "title": hit["title"], "description": None,
        "category": hit["canonical_category"], "native_category": hit["native_category_name"],
        "city": hit["city"], "state": hit["state"], "lat": hit["lat"], "lng": hit["lng"],
        "current_bid": hit["current_bid"], "bid_count": hit["bid_count"],
        "end_utc": hit["end_utc"], "first_seen_at": hit["first_seen_at"],
        "ended": bool(hit["outcome_complete"]), "url": hit["url"],
        "photos": [hit.get("photo_url")],
    }, archive, now)


def _from_archive(source: str, key: str, now: datetime) -> dict | None:
    if source not in ARCHIVE_SOURCES:
        return None
    where, args = public_sold.base_where()
    row = db.fetch_one(
        "SELECT source, lot_key, title, canonical_category, city, state, "
        f"{_ARCHIVE_COLS} FROM lot_archive WHERE {where} AND source = %s AND lot_key = %s",
        (*args, source, key))
    if not row:
        return None
    row = dict(row)
    return _finish({
        "source": source, "key": key, "backing": "archive",
        "title": row.get("title"), "description": None,
        "category": row.get("canonical_category"), "native_category": None,
        "city": row.get("city"), "state": row.get("state"), "lat": None, "lng": None,
        "current_bid": None, "bid_count": row.get("bid_count"),
        "end_utc": _aware(row.get("closed_at")), "first_seen_at": None,
        "ended": True, "url": _source_url(source, key), "photos": [],
    }, row, now)


def resolve(source: str, key: str, *, now: datetime | None = None) -> dict | None:
    """The public lot behind (source, native key), or None (= 404). Raises when
    a read fails; the route answers 503."""
    now = now or datetime.now(timezone.utc)
    if source in DEAL_LOT_SITES:
        lot = _from_deal_lots(source, key, now)
        if lot is False:
            return None
    else:
        lot = None
    lot = lot or _from_snapshots(source, key, now) or _from_archive(source, key, now)
    if lot is None or lot.get("path") is None or not (lot.get("title") or "").strip():
        return None
    return lot


# ── view model ───────────────────────────────────────────────────────────────

SIMILAR_POOL = 12          # rows memoised per (category, state); the page shows SIMILAR of them


def _similar_pool(category: str | None, state: str | None) -> list[dict]:
    """Memoised 5 min (`public_deals._cached`): every lot page of one category
    + state shares ONE count-less read, so a crawl cannot load the database."""
    return public_deals._cached(
        f"similar:{category or ''}:{state or ''}",
        lambda: public_deals.similar_rows(category=category, state=state, limit=SIMILAR_POOL))


def similar(lot: dict) -> list[dict]:
    """Up to SIMILAR live public lots in the same category (and state when
    there are enough). Never raises: a failed read is an empty list."""
    try:
        rows: list[dict] = []
        for state in ([lot["state"], None] if lot.get("state") else [None]):
            if not lot.get("category") and not state:
                break
            rows = [r for r in _similar_pool(lot.get("category"), state)
                    if r.get("viewer_url") and r["viewer_url"] != lot["path"]]
            if len(rows) >= 3:
                break
        return rows[:SIMILAR]
    except Exception:  # noqa: BLE001
        return []


def price_of(lot: dict) -> float | None:
    """The one price the page leads with: the confirmed sale, else the last bid seen."""
    return lot["sold_price"] if lot["sold_price"] is not None else lot.get("current_bid")


def robots(lot: dict) -> str:
    """Live and confirmed-sold pages are indexed; an ended lot with no confirmed price is not."""
    if lot["ended"] and lot["sold_price"] is None:
        return "noindex,follow"
    return "index,follow"


def place(lot: dict) -> str:
    return ", ".join(x for x in (lot.get("city"), lot.get("state")) if x)


def meta(lot: dict) -> dict:
    where = place(lot)
    title = f"{lot['title']}{' — ' + where if where else ''} | {lot['source_name']} auction"
    if lot["sold_price"] is not None:
        desc = f"Sold for ${lot['sold_price']:,.0f} on {lot['source_name']}"
    elif lot["ended"]:
        desc = f"This {lot['source_name']} auction has ended"
    elif lot.get("current_bid") is not None:
        desc = f"Current bid ${lot['current_bid']:,.0f} on {lot['source_name']}"
    else:
        desc = f"Live auction on {lot['source_name']}"
    if where:
        desc += f" in {where}"
    bids = lot.get("bid_count")
    if bids is not None:
        desc += f", {bids} bid{'' if bids == 1 else 's'}"
    return {"title": title[:150], "description": desc + ". Bid, time left and all-in cost in one page."}


def jsonld(lot: dict, base_url: str, *, photos: bool) -> list[dict]:
    """Product/Offer + BreadcrumbList. Price = the confirmed sale or the current bid; never both."""
    url = f"{base_url}{lot['path']}"
    product: dict[str, Any] = {"@context": "https://schema.org", "@type": "Product",
                               "name": lot["title"], "url": url}
    if lot.get("description"):
        product["description"] = lot["description"][:500]
    if lot.get("category"):
        product["category"] = lot["category"].replace("_", " ")
    if photos and lot["photos"]:
        product["image"] = lot["photos"]
    price = price_of(lot)
    # An ended lot with no confirmed sale has no price to offer: the last bid seen is not one.
    if price is not None and not (lot["ended"] and lot["sold_price"] is None):
        offer: dict[str, Any] = {
            "@type": "Offer", "price": f"{price:.2f}", "priceCurrency": public_sold.CURRENCY, "url": url,
            "availability": "https://schema.org/" + ("SoldOut" if lot["ended"] else "InStock"),
            "seller": {"@type": "Organization", "name": lot["source_name"]}}
        if not lot["ended"] and lot.get("end_utc"):
            offer["priceValidUntil"] = lot["end_utc"].date().isoformat()
        product["offers"] = offer
    crumbs = [("Auction deals", f"{base_url}/deals")]
    if lot.get("category"):
        crumbs.append((lot["category"].replace("_", " ").title(), f"{base_url}/deals?category={lot['category']}"))
    crumbs.append((lot["title"], url))
    trail = {"@context": "https://schema.org", "@type": "BreadcrumbList", "itemListElement": [
        {"@type": "ListItem", "position": i, "name": name, "item": href}
        for i, (name, href) in enumerate(crumbs, 1)]}
    return [product, trail]


def view(lot: dict, base_url: str, *, signed_in: bool = False) -> dict:
    """Template context for one resolved lot."""
    photos = show_photos(signed_in)
    return {
        "lot": lot, "place": place(lot), "price": price_of(lot),
        "cost": lot_costs.estimate(lot["source"], None if lot["ended"] else lot.get("current_bid")),
        "similar": similar(lot), "meta": meta(lot), "robots": robots(lot),
        "show_photos": photos and bool(lot["photos"]),
        "photos_need_sign_in": (not photos and photos_mode() == PHOTOS_SIGNED_IN and bool(lot["photos"])),
        "jsonld": jsonld(lot, base_url, photos=photos),      # the template writes it with |tojson
        "canonical_url": f"{base_url}{lot['path']}", "base_url": base_url,
    }
