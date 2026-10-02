"""Public read model behind /platform: one auctions view across every site the
recorder observes, plus an honest list of sites in scope.

Source table: `listing_snapshots` (the recorder). Rules, enforced here so no
caller can forget them:

1. **Allow-list, twice.** The SQL extracts a fixed set of fields from `raw`
   (it never selects `raw` itself), and every item leaves through `ITEM_KEYS`.
   Seller contact fields (GSA `coEmail`/`coPhone`, Purple Wave `*_contact`),
   street addresses, photos and the source's own lat/lng never reach a response.
2. **public_deals policy.** No seating lots (`public_deals.is_excluded` on title
   and category) and nothing the operator starred / tracked / listed
   (`public_deals.is_operator_pick`). If the pick tables cannot be read, the
   whole read fails — nothing is published unfiltered.
3. **City-level pins.** `geo.resolve_place` on city+state only; a lot whose city
   does not resolve gets `lat`/`lng` null (no state-centroid stand-in).
4. **USA / USD only.** The contract has no currency or country field, so a lot
   priced in EUR/GBP/ZAR (most of AllSurplus) is left out, not shown as dollars.
5. **Bounded.** One query per status per `CACHE_TTL`: a time window, a row cap,
   and index lookups — never a scan of `raw`. Filters, sort, paging and facets
   run in memory on the cached set (a few thousand small dicts).
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from .. import db
from ..alerts import geo
from . import public_deals, readcache
from deals.categories import canonical_category

log = logging.getLogger(__name__)

CACHE_TTL = 300              # seconds — one DB read per status per 5 min
FAIL_BACKOFF = 30            # seconds — a failed read is not retried per page view
ROW_CAP = 4000               # lots per status
OPEN_WINDOW_DAYS = 14        # a lot not observed for this long is not "open"
CLOSED_WINDOW_DAYS = 30      # how far back "closed" reaches
MAX_PER_PAGE = 100
LIVE_WINDOW = timedelta(hours=24)

# key → (display name, kind). Adapter-backed sites (recorder/sources/<key>.py).
SITES: dict[str, tuple[str, str]] = {
    "govdeals": ("GovDeals", "government"),
    "allsurplus": ("AllSurplus", "commercial"),
    "gsa": ("GSA Auctions", "government"),
    "purple_wave": ("Purple Wave", "commercial"),
    "public_surplus": ("Public Surplus", "government"),
    "municibid": ("Municibid", "government"),
    "mibid": ("MiBid", "government"),
}
# Roadmap, no adapter yet: docs/blackwhole-28/SPEC-DRAFT.md §3.2 "Ranked
# rollout", Tier 2. Nothing here is scraped by the recorder; it is labelled
# `planned` with `lots: null` and never anything else until an adapter exists.
PLANNED_SITES: dict[str, tuple[str, str]] = {
    "ibid_illinois": ("iBid Illinois", "government"),
    "propertyroom": ("PropertyRoom", "government"),
    "hibid": ("HiBid", "commercial"),
    "govplanet": ("GovPlanet", "government"),
    "bid4assets": ("Bid4Assets", "government"),
    "bidspotter": ("BidSpotter", "commercial"),
}
# Sources this module knows how to normalise. A source without a normaliser is
# never listed, whatever the env says.
_NORMALISED = ("govdeals", "allsurplus", "gsa", "purple_wave")
_MAESTRO_HOSTS = {"govdeals": "https://www.govdeals.com", "allsurplus": "https://www.allsurplus.com"}

ITEM_KEYS = (
    "id", "site", "site_name", "title", "category", "city", "state", "lat", "lng",
    "current_bid", "bid_count", "ends_at", "status", "final_price", "closed_at", "url",
)
SORTS = ("ending", "bid_low", "bids", "newest")
_SOURCES_DIR = Path(__file__).resolve().parents[2] / "recorder" / "sources"


def public_sources() -> list[str]:
    raw = os.getenv("PLATFORM_PUBLIC_SOURCES")
    want = [x.strip() for x in raw.split(",") if x.strip()] if raw else list(_NORMALISED)
    return [s for s in want if s in _NORMALISED]


# ── the one query ────────────────────────────────────────────────────────────
# `latest` = each lot's newest snapshot inside the window (seq scan of the
# narrow columns; `raw` is TOASTed and untouched). `picked` = the capped set.
# The LATERAL reads ONE discovery snapshot per picked lot through
# idx_listing_snapshots_lot and pulls only the named fields out of `raw`.
# The final WHERE is rule 4 (USA / USD) applied in the database so the ~1,000
# foreign AllSurplus lots are not shipped over the wire; `_normalise` re-checks.
_SQL = """
WITH latest AS (
  SELECT DISTINCT ON (source, source_lot_id)
         source, source_lot_id, status, current_bid, bid_count, end_date
  FROM listing_snapshots
  WHERE source = ANY(%s) AND observed_at > now() - make_interval(days => %s)
  ORDER BY source, source_lot_id, observed_at DESC
), picked AS (
  SELECT * FROM latest WHERE {pick} LIMIT %s
)
SELECT p.source, p.source_lot_id, p.current_bid, p.bid_count, p.end_date,
       (SELECT min(m.observed_at) FROM listing_snapshots m
         WHERE m.source = p.source AND m.source_lot_id = p.source_lot_id) AS first_seen,
       d.f->>0 AS title, d.f->>1 AS category, d.f->>2 AS category_code,
       d.f->>3 AS city, d.f->>4 AS state, d.f->>5 AS country, d.f->>6 AS currency,
       d.f->>7 AS ref_a, d.f->>8 AS ref_b
FROM picked p
JOIN LATERAL (
  SELECT jsonb_build_array(
           COALESCE(s.raw->'assetShortDescription', s.raw->'itemName', s.raw->'first_line_description'),
           COALESCE(s.raw->'categoryDescription', s.raw->'category'),
           s.raw->'assetCategory',
           COALESCE(s.raw->'locationCity', s.raw->'propertyCity', s.raw->'city'),
           COALESCE(s.raw->'locationState', s.raw->'propertyState', s.raw->'state_abbreviation'),
           s.raw->'country', s.raw->'currencyCode',
           COALESCE(s.raw->'itemDescURL', s.raw->'auction'), s.raw->'item') AS f
  FROM listing_snapshots s
  WHERE s.source = p.source AND s.source_lot_id = p.source_lot_id
    AND s.raw ?| array['assetShortDescription','itemName','first_line_description']
  ORDER BY s.observed_at DESC LIMIT 1
) d ON true
WHERE COALESCE(d.f->>5, 'USA') = 'USA' AND COALESCE(d.f->>6, 'USD') = 'USD'
"""
_PICK = {
    "open": "status = 'active' AND end_date > now() ORDER BY end_date ASC",
    "closed": "status = 'closed' AND end_date > now() - make_interval(days => %s) ORDER BY end_date DESC",
}


def _read(status: str) -> tuple[list[dict], public_deals.OperatorPicks]:
    """Rows + operator picks on one connection. Raises on any failure."""
    sources = public_sources()
    if status == "closed":
        args: tuple = (sources, CLOSED_WINDOW_DAYS + OPEN_WINDOW_DAYS, CLOSED_WINDOW_DAYS, ROW_CAP)
    else:
        args = (sources, OPEN_WINDOW_DAYS, ROW_CAP)
    with db.connect() as conn:
        picks = public_deals.load_operator_picks(conn)
        rows = conn.execute(_SQL.format(pick=_PICK[status]), args).fetchall()
    return [dict(r) for r in rows], picks


# ── normalise (pure) ─────────────────────────────────────────────────────────
_EMAIL_RX = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE_RX = re.compile(r"(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}\b")
_STATE_RX = re.compile(r"^[A-Z]{2}$")
_TOKEN_RX = re.compile(r"^[A-Za-z0-9_-]{1,40}$")


def _text(v: Any, limit: int = 200) -> str | None:
    """Whitespace-collapsed display text with any email / phone number removed."""
    s = _PHONE_RX.sub(" ", _EMAIL_RX.sub(" ", str(v or "")))
    s = re.sub(r"\s+", " ", s).strip(" -,:;")
    return s[:limit] or None


def _city(v: Any) -> str | None:
    s = _text(v, 80)
    if not s:
        return None
    return s.title() if s.isupper() else s


def _num(v: Any) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _iso(dt: Any) -> str | None:
    if not isinstance(dt, datetime):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _url(source: str, lot_id: str, row: dict) -> str | None:
    if source in _MAESTRO_HOSTS:
        parts = lot_id.split("/")
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            return f"{_MAESTRO_HOSTS[source]}/en/asset/{parts[0]}/{parts[1]}"
        return None
    if source == "gsa":
        u = str(row.get("ref_a") or "")
        return u if re.match(r"^https://www\.gsaauctions\.gov/[\w/.-]+$", u) else None
    if source == "purple_wave":
        auction, item = str(row.get("ref_a") or ""), str(row.get("ref_b") or "")
        if _TOKEN_RX.match(auction) and _TOKEN_RX.match(item):
            return f"https://www.purplewave.com/auction/{auction}/item/{item}"
    return None


def _normalise(row: dict, status: str, picks: public_deals.OperatorPicks) -> dict | None:
    """One DB row → one public record, or None when policy or quality drops it."""
    source = str(row.get("source") or "")
    lot_id = str(row.get("source_lot_id") or "")
    if source not in _NORMALISED or not lot_id:
        return None
    if (row.get("country") or "USA") != "USA" or (row.get("currency") or "USD") != "USD":
        return None
    title = _text(row.get("title"))
    category = _text(row.get("category"), 80)
    city = _city(row.get("city"))
    state = str(row.get("state") or "").strip().upper()
    state = state if _STATE_RX.match(state) else None
    if not title or not (city or state):
        return None
    canon = canonical_category(str(row.get("category_code") or "")) if source in _MAESTRO_HOSTS else None
    if public_deals.is_excluded({"title": f"{title} {category or ''}", "canonical_category": canon}):
        return None
    url = _url(source, lot_id, row)
    if public_deals.is_operator_pick(picks, source_lot_id=lot_id, title=title, url=url):
        return None
    lat = lng = None
    if city and state:
        la, lo, prec = geo.resolve_place(city, state, None)
        if prec == "city":
            lat, lng = round(la, 4), round(lo, 4)
    bid, bids = _num(row.get("current_bid")), row.get("bid_count")
    bids = int(bids) if bids is not None else None
    ends = _iso(row.get("end_date"))
    closed = status == "closed"
    return {
        "id": f"{source}:{lot_id}", "site": source, "site_name": SITES[source][0],
        "title": title, "category": category, "city": city, "state": state,
        "lat": lat, "lng": lng, "current_bid": bid, "bid_count": bids,
        "ends_at": ends, "status": status,
        "final_price": bid if (closed and bids and bid is not None) else None,
        "closed_at": ends if closed else None, "url": url,
        "_first_seen": _iso(row.get("first_seen")) or "",
    }


def _build(rows: list[dict], status: str, picks: public_deals.OperatorPicks) -> list[dict]:
    out = []
    for row in rows:
        rec = _normalise(row, status, picks)
        if rec is not None:
            out.append(rec)
    return out


# ── cache ────────────────────────────────────────────────────────────────────
@readcache.cached(ttl=CACHE_TTL)
def _lots_cached(status: str) -> tuple[str, list[dict]]:
    rows, picks = _read(status)
    return datetime.now(timezone.utc).isoformat(), _build(rows, status, picks)


_locks = {"open": threading.Lock(), "closed": threading.Lock()}
_fail_until: dict[str, float] = {}


def clear_cache() -> None:
    readcache.invalidate_all()
    _fail_until.clear()


def _lots(status: str) -> tuple[str, list[dict]]:
    """Cached set for a status. One loader at a time (no cold-start stampede);
    a failure raises and is remembered for FAIL_BACKOFF so an outage is not
    re-queried on every page view."""
    if time.monotonic() < _fail_until.get(status, 0.0):
        raise RuntimeError("platform auctions read is backing off")
    with _locks[status]:
        try:
            return _lots_cached(status)
        except Exception:
            _fail_until[status] = time.monotonic() + FAIL_BACKOFF
            raise


# ── endpoint 1: auctions ─────────────────────────────────────────────────────
def _facet(items: list[dict], field: str, names: Callable[[str], str]) -> list[dict]:
    counts: dict[str, int] = {}
    for it in items:
        v = it.get(field)
        if v:
            counts[v] = counts.get(v, 0) + 1
    return [{"key": k, "name": names(k), "count": n}
            for k, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]


def _sorted(items: list[dict], sort: str, status: str) -> list[dict]:
    big = float("inf")
    if sort == "bid_low":
        return sorted(items, key=lambda i: (i["current_bid"] is None, i["current_bid"] or 0.0))
    if sort == "bids":
        return sorted(items, key=lambda i: -(i["bid_count"] if i["bid_count"] is not None else -big))
    if sort == "newest":
        return sorted(items, key=lambda i: i["_first_seen"], reverse=True)
    # "ending": soonest first when open, most recently closed first when closed
    return sorted(items, key=lambda i: i["ends_at"] or ("" if status == "closed" else "9"),
                  reverse=(status == "closed"))


def auctions(*, q=None, site=None, category=None, state=None, status="open", no_bids=None,
             max_bid=None, ending=None, sort="ending", page=1, per_page=50) -> dict:
    """The /platform/api/auctions payload. Never raises: a failed read answers
    `ok: false` with the same shape and no items — never error text, and never
    closed lots standing in for open ones."""
    status = status if status in _PICK else "open"
    sort = sort if sort in SORTS else "ending"
    try:
        page = max(1, int(page or 1))
        per_page = max(1, min(int(per_page or 50), MAX_PER_PAGE))
    except (TypeError, ValueError):
        page, per_page = 1, 50
    now = datetime.now(timezone.utc)
    empty = {"ok": False, "as_of": now.isoformat(), "total": 0, "page": page, "per_page": per_page,
             "items": [], "facets": {"sites": [], "categories": [], "states": []}}
    try:
        as_of, lots = _lots(status)
    except Exception:
        log.warning("platform auctions read failed", exc_info=True)
        return empty

    now_iso = now.isoformat()
    if status == "open":          # the memo can be minutes old — drop what has ended since
        lots = [i for i in lots if i["ends_at"] and i["ends_at"] > now_iso]
    facets = {
        "sites": _facet(lots, "site", lambda k: SITES[k][0]),
        "categories": _facet(lots, "category", lambda k: k),
        "states": _facet(lots, "state", lambda k: k),
    }

    items = lots
    if site:
        items = [i for i in items if i["site"] == site]
    if category:
        c = category.strip().lower()
        items = [i for i in items if (i["category"] or "").lower() == c]
    if state:
        s = state.strip().upper()
        items = [i for i in items if i["state"] == s]
    if str(no_bids or "").lower() in ("1", "true", "yes"):
        items = [i for i in items if i["bid_count"] == 0]     # no-bid = bid_count==0 only
    cap = _num(max_bid)
    if cap is not None:
        items = [i for i in items if i["current_bid"] is not None and i["current_bid"] <= cap]
    span = {"24h": timedelta(hours=24), "7d": timedelta(days=7)}.get(str(ending or ""))
    if span is not None:
        if status == "open":
            edge = (now + span).isoformat()
            items = [i for i in items if i["ends_at"] and i["ends_at"] <= edge]
        else:
            edge = (now - span).isoformat()
            items = [i for i in items if i["closed_at"] and i["closed_at"] >= edge]
    terms = [t for t in str(q or "").lower().split() if t][:8]
    if terms:
        items = [i for i in items if all(t in i["title"].lower() for t in terms)]

    items = _sorted(items, sort, status)
    start = (page - 1) * per_page
    return {
        "ok": True, "as_of": as_of, "total": len(items), "page": page, "per_page": per_page,
        "items": [{k: i[k] for k in ITEM_KEYS} for i in items[start:start + per_page]],
        "facets": facets,
    }


# ── endpoint 2: sites ────────────────────────────────────────────────────────
def has_adapter(key: str) -> bool:
    return bool(re.match(r"^[a-z0-9_]+$", key)) and (_SOURCES_DIR / f"{key}.py").is_file()


def sites(source_rows: Callable[[], list[dict]]) -> list[dict]:
    """Every site in scope with an honest status. `source_rows` is the app's
    memoised grouped read of `listing_snapshots` (shared with
    /platform/api/sources). Raises when that read fails — the route answers 503
    rather than guessing which sites are live."""
    rows = {r["source"]: r for r in source_rows()}
    now = datetime.now(timezone.utc)
    out = []
    for key, (name, kind) in {**SITES, **PLANNED_SITES}.items():
        if not has_adapter(key):
            # No adapter = nothing scraped. Never a lot count, never "live".
            out.append({"key": key, "name": name, "kind": kind, "status": "planned",
                        "lots": None, "last_seen": None})
            continue
        row = rows.get(key) or {}
        seen = row.get("last_seen")
        if seen is not None and seen.tzinfo is None:
            seen = seen.replace(tzinfo=timezone.utc)
        live = bool(seen and now - seen <= LIVE_WINDOW)
        out.append({"key": key, "name": name, "kind": kind,
                    "status": "live" if live else "paused",
                    "lots": int(row.get("lots") or 0),
                    "last_seen": seen.isoformat() if seen else None})
    return out
