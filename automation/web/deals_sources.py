"""Public /deals rows for every recorder source EXCEPT GovDeals.

GovDeals lots reach `/deals` through `deal_lots` (comps, quantity, outcomes,
the archived-lot viewer). Every other site the recorder observes lives only in
`listing_snapshots`, where title / city / state / category / url sit inside
the per-source `raw` jsonb. This module turns the newest snapshot of each
(source, source_lot_id) into a row shaped like a `deal_lots` public row, so
`public_deals` can UNION the two halves and the page codes against one shape.

Rules, enforced here so no caller can forget them:

1. **Allow-list, twice.** The SQL extracts a fixed set of fields from `raw`
   per source (it never selects `raw` itself; see `SOURCES`), and every row
   leaves through `ROW_KEYS`. Seller contact fields (GSA `coEmail`/`coPhone`,
   Purple Wave `*_contact`), street addresses, photos and the source's own
   lat/lng never reach a response.
2. **public_deals policy.** No seating lots (`public_deals.is_excluded` on
   title + category) and nothing the operator starred / tracked / listed
   (`public_deals.is_operator_pick`). If the pick tables cannot be read the
   whole read fails — nothing is published unfiltered.
3. **City-level pins.** `geo.resolve_place` on city + state only, memoised; a
   lot whose city does not resolve gets `lat`/`lng` null (no state centroid).
4. **USA / USD only.** AllSurplus is mostly EUR/GBP/ZAR; a foreign lot is left
   out, not shown as dollars. Sources with no country field are US sites.
5. **Bounded.** One query per status per `CACHE_TTL`: an observed_at window,
   a row cap, and index lookups — never a scan of `raw`. Filters run in
   memory on the cached set (a few thousand small dicts).

Adding a source = one `SourceSpec` entry in `SOURCES` (raw keys per field +
a url builder). `source='govdeals'` is refused here on purpose.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .. import db
from ..alerts import geo
from . import public_deals
from deals.categories import canonical_category

log = logging.getLogger(__name__)

GOVDEALS = "govdeals"             # served from deal_lots — never from here
CACHE_TTL = 300                   # seconds — one DB read per status per 5 min
FAIL_BACKOFF = 30                 # seconds — a failed read is not retried per page view
ROW_CAP = 4000                    # lots per status
OPEN_WINDOW_DAYS = 14             # a lot not observed for this long is not "open"
CLOSED_WINDOW_DAYS = 30           # how far back "closed" reaches

_KEY_RX = re.compile(r"^[A-Za-z0-9_]+$")
_SRC_RX = re.compile(r"^[a-z0-9_]+$")
_TOKEN_RX = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
_STATE_RX = re.compile(r"^[A-Z]{2}$")
_EMAIL_RX = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE_RX = re.compile(r"(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}\b")
_DIGITS_RX = re.compile(r"^\d{1,12}$")


# ── per-source raw-key maps ──────────────────────────────────────────────────
# Each field names the `raw` keys to try, in order (COALESCE). `url` builds the
# public lot link from (source_lot_id, row) where row carries ref_a / ref_b.
@dataclass(frozen=True)
class SourceSpec:
    name: str
    title: tuple[str, ...]
    city: tuple[str, ...] = ()
    state: tuple[str, ...] = ()
    state_const: str | None = None            # single-state site (MiBid = MI)
    category: tuple[str, ...] = ()
    category_code: tuple[str, ...] = ()       # maestro code → canonical bucket
    country: tuple[str, ...] = ()
    currency: tuple[str, ...] = ()
    ref_a: tuple[str, ...] = ()
    ref_b: tuple[str, ...] = ()
    url: Callable[[str, dict], str | None] = lambda lot_id, row: None


def _url_maestro(host: str) -> Callable[[str, dict], str | None]:
    def build(lot_id: str, row: dict) -> str | None:
        parts = lot_id.split("/")
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            return f"{host}/en/asset/{parts[0]}/{parts[1]}"
        return None
    return build


def _url_gsa(lot_id: str, row: dict) -> str | None:
    u = str(row.get("ref_a") or "")
    return u if re.match(r"^https://www\.gsaauctions\.gov/[\w/.-]+$", u) else None


def _url_purple_wave(lot_id: str, row: dict) -> str | None:
    auction, item = str(row.get("ref_a") or ""), str(row.get("ref_b") or "")
    if _TOKEN_RX.match(auction) and _TOKEN_RX.match(item):
        return f"https://www.purplewave.com/auction/{auction}/item/{item}"
    return None


def _url_public_surplus(lot_id: str, row: dict) -> str | None:
    return f"https://www.publicsurplus.com/sms/auction/view?auc={lot_id}" if _DIGITS_RX.match(lot_id) else None


def _url_municibid(lot_id: str, row: dict) -> str | None:
    return f"https://municibid.com/Listing/Details/{lot_id}" if _DIGITS_RX.match(lot_id) else None


_UUID_RX = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


def _url_mibid(lot_id: str, row: dict) -> str | None:
    return f"https://mibid.michigan.gov/AuctionBid/Index/{lot_id}" if _UUID_RX.match(lot_id) else None


SOURCES: dict[str, SourceSpec] = {
    # maestro search rows (recorder/sources/allsurplus.py → govdeals.py shape)
    "allsurplus": SourceSpec(
        name="AllSurplus", title=("assetShortDescription",), category=("categoryDescription",),
        category_code=("assetCategory",), city=("locationCity",), state=("locationState",),
        country=("country",), currency=("currencyCode",), url=_url_maestro("https://www.allsurplus.com")),
    # gsaauctions.gov list items. `locationCity`/`locationST` is the GSA sales
    # office, NOT where the property sits — use the property* fields only.
    "gsa": SourceSpec(
        name="GSA Auctions", title=("itemName",), city=("propertyCity",), state=("propertyState",),
        ref_a=("itemDescURL",), url=_url_gsa),
    # purplewave.com /v1/search/search items. `title` is the AUCTION's name
    # ("Tuesday August 04 Government Auction"); the lot is `first_line_description`.
    "purple_wave": SourceSpec(
        name="Purple Wave", title=("first_line_description",), category=("category",),
        city=("city",), state=("state_abbreviation",), ref_a=("auction",), ref_b=("item",),
        url=_url_purple_wave),
    # publicsurplus.com search cards: title + a 2-letter state, no city.
    "public_surplus": SourceSpec(
        name="Public Surplus", title=("title",), state=("location",), url=_url_public_surplus),
    # municibid.com srp-markers-data items.
    "municibid": SourceSpec(
        name="Municibid", title=("title",), city=("city",), state=("state",), url=_url_municibid),
    # mibid.michigan.gov rawAuctions items: `location` is a city, state is always MI.
    "mibid": SourceSpec(
        name="MiBid", title=("title",), city=("location",), state_const="MI", url=_url_mibid),
}
assert GOVDEALS not in SOURCES, "GovDeals rows come from deal_lots, never from listing_snapshots"
for _src, _spec in SOURCES.items():
    assert _SRC_RX.match(_src), _src
    for _f in ("title", "city", "state", "category", "category_code", "country", "currency", "ref_a", "ref_b"):
        for _k in getattr(_spec, _f):
            assert _KEY_RX.match(_k), (_src, _f, _k)

SITES: dict[str, str] = {GOVDEALS: "GovDeals", **{k: v.name for k, v in SOURCES.items()}}
MAESTRO = frozenset(("allsurplus",))        # sources whose category_code is a GovDeals bucket code

# Keys every row leaves through. Same names as a `deal_lots` public row so the
# page codes against one shape; `asset_id`/`account_id`/`auction_id` are None.
ROW_KEYS = (
    "id", "source", "source_name", "source_lot_id", "asset_id", "account_id", "auction_id",
    "title", "canonical_category", "native_category_name", "city", "state", "lat", "lng",
    "bid_count", "current_bid", "currency_code", "end_utc", "outcome", "final_bid",
    "final_bid_count", "outcome_complete", "first_seen_at", "url", "viewer_url",
)
_FIELDS = ("title", "category", "category_code", "city", "state", "country", "currency", "ref_a", "ref_b")


# ── the one query ────────────────────────────────────────────────────────────
# `latest` = each lot's newest snapshot inside the window (narrow columns only;
# `raw` is TOASTed and untouched). `picked` = the capped set. The LATERAL reads
# ONE discovery snapshot per picked lot through idx_listing_snapshots_lot and
# pulls only the per-source named keys out of `raw`. The final WHERE is rule 4
# (USA / USD) applied in the database so foreign AllSurplus lots are not
# shipped over the wire; `_normalise` re-checks.
def _field_expr(field: str) -> str:
    branches = []
    for src, spec in SOURCES.items():
        keys = getattr(spec, field)
        if not keys:
            continue
        expr = ", ".join(f"s.raw->'{k}'" for k in keys)
        expr = f"COALESCE({expr})" if len(keys) > 1 else expr
        branches.append(f"WHEN '{src}' THEN {expr}")
    return ("CASE s.source " + " ".join(branches) + " END") if branches else "NULL::jsonb"


def title_keys() -> list[str]:
    out: list[str] = []
    for spec in SOURCES.values():
        out += [k for k in spec.title if k not in out]
    return out


def extract(source: str, raw: dict) -> dict | None:
    """Pure twin of the SQL LATERAL: the same per-source COALESCE over `raw`,
    in Python, so tests can feed a recorded raw sample straight to `normalise`.
    None when the snapshot carries no title key (a poll/bidbox row)."""
    spec = SOURCES.get(source)
    if spec is None or not any(k in raw for k in spec.title):
        return None

    def first(keys):
        for k in keys:
            if raw.get(k) is not None:
                return raw[k]
        return None

    return {f: first(getattr(spec, f)) for f in _FIELDS}


def _build_sql() -> str:
    fields = ",\n           ".join(_field_expr(f) for f in _FIELDS)
    return f"""
WITH latest AS (
  SELECT DISTINCT ON (source, source_lot_id)
         source, source_lot_id, status, current_bid, bid_count, end_date
  FROM listing_snapshots
  WHERE source = ANY(%s) AND observed_at > now() - make_interval(days => %s)
  ORDER BY source, source_lot_id, observed_at DESC
), picked AS (
  SELECT * FROM latest WHERE {{pick}} LIMIT %s
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
           {fields}) AS f
  FROM listing_snapshots s
  WHERE s.source = p.source AND s.source_lot_id = p.source_lot_id
    AND s.raw ?| %s
  ORDER BY s.observed_at DESC LIMIT 1
) d ON true
WHERE COALESCE(d.f->>5, 'USA') = 'USA' AND COALESCE(d.f->>6, 'USD') = 'USD'
"""


_SQL = _build_sql()
_PICK = {
    "open": "status = 'active' AND end_date > now() ORDER BY end_date ASC",
    "closed": "status = 'closed' AND end_date > now() - make_interval(days => %s) ORDER BY end_date DESC",
}


def sources() -> list[str]:
    return list(SOURCES)


def _read(status: str) -> tuple[list[dict], public_deals.OperatorPicks]:
    """Rows + operator picks on one connection. Raises on any failure."""
    srcs = sources()
    if status == "closed":
        args: tuple = (srcs, CLOSED_WINDOW_DAYS + OPEN_WINDOW_DAYS, CLOSED_WINDOW_DAYS, ROW_CAP, title_keys())
    else:
        args = (srcs, OPEN_WINDOW_DAYS, ROW_CAP, title_keys())
    with db.connect() as conn:
        picks = public_deals.load_operator_picks(conn)
        rows = conn.execute(_SQL.format(pick=_PICK[status]), args).fetchall()
    return [dict(r) for r in rows], picks


# ── normalise (pure) ─────────────────────────────────────────────────────────
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


def _aware(dt: Any) -> datetime | None:
    if not isinstance(dt, datetime):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


_GEO: dict[tuple[str, str], tuple[float, float] | None] = {}
_GEO_CAP = 20000


def city_point(city: str | None, state: str | None) -> tuple[float, float] | None:
    """City centroid or None — never a state centroid. Memoised per process."""
    if not city or not state:
        return None
    key = (city, state)
    if key in _GEO:
        return _GEO[key]
    la, lo, prec = geo.resolve_place(city, state, None)
    hit = (round(la, 4), round(lo, 4)) if prec == "city" and la is not None else None
    if len(_GEO) >= _GEO_CAP:
        _GEO.clear()
    _GEO[key] = hit
    return hit


def normalise(row: dict, status: str, picks: public_deals.OperatorPicks) -> dict | None:
    """One DB row → one public record, or None when policy or quality drops it."""
    source = str(row.get("source") or "")
    lot_id = str(row.get("source_lot_id") or "")
    spec = SOURCES.get(source)
    if spec is None or not lot_id:
        return None
    if (row.get("country") or "USA") != "USA" or (row.get("currency") or "USD") != "USD":
        return None
    title = _text(row.get("title"))
    category = _text(row.get("category"), 80)
    city = _city(row.get("city"))
    state = str(spec.state_const or row.get("state") or "").strip().upper()
    state = state if _STATE_RX.match(state) else None
    if not title or not (city or state):
        return None
    canon = canonical_category(str(row.get("category_code") or "")) if source in MAESTRO else None
    if public_deals.is_excluded({"title": f"{title} {category or ''}", "canonical_category": canon}):
        return None
    url = spec.url(lot_id, row)
    if public_deals.is_operator_pick(picks, source_lot_id=lot_id, title=title, url=url):
        return None
    lat = lng = None
    pt = city_point(city, state)
    if pt:
        lat, lng = pt
    bid, bids = _num(row.get("current_bid")), row.get("bid_count")
    bids = int(bids) if bids is not None else None
    closed = status == "closed"
    outcome = None
    if closed:
        outcome = "no_bid" if bids == 0 else ("sold" if bids else None)
    return {
        "id": f"{source}:{lot_id}", "source": source, "source_name": spec.name,
        "source_lot_id": lot_id, "asset_id": None, "account_id": None, "auction_id": None,
        "title": title, "canonical_category": canon, "native_category_name": category,
        "city": city, "state": state, "lat": lat, "lng": lng,
        "bid_count": bids, "current_bid": bid, "currency_code": "USD",
        "end_utc": _aware(row.get("end_date")), "outcome": outcome,
        "final_bid": bid if (closed and bids and bid is not None) else None,
        "final_bid_count": bids if closed else None, "outcome_complete": closed,
        "first_seen_at": _aware(row.get("first_seen")), "url": url, "viewer_url": None,
    }


def build(rows: list[dict], status: str, picks: public_deals.OperatorPicks) -> list[dict]:
    out = []
    for row in rows:
        rec = normalise(row, status, picks)
        if rec is not None:
            out.append(rec)
    return out


# ── cache ────────────────────────────────────────────────────────────────────
_cache: dict[str, tuple[float, list[dict]]] = {}
_locks = {"open": threading.Lock(), "closed": threading.Lock()}
_fail_until: dict[str, float] = {}


def clear_cache() -> None:
    _cache.clear()
    _fail_until.clear()
    _GEO.clear()


def _lots(status: str) -> list[dict]:
    """Cached set for `open` / `closed`. One loader at a time (no cold-start
    stampede); a failure raises and is remembered for FAIL_BACKOFF so an
    outage is not re-queried on every page view."""
    now = time.monotonic()
    hit = _cache.get(status)
    if hit and now - hit[0] < CACHE_TTL:
        return hit[1]
    if now < _fail_until.get(status, 0.0):
        raise RuntimeError("deals sources read is backing off")
    with _locks[status]:
        hit = _cache.get(status)
        if hit and time.monotonic() - hit[0] < CACHE_TTL:
            return hit[1]
        try:
            rows, picks = _read(status)
        except Exception:
            _fail_until[status] = time.monotonic() + FAIL_BACKOFF
            raise
        built = build(rows, status, picks)
        _cache[status] = (time.monotonic(), built)
        return built


def lots(status: str = "active", *, now: datetime | None = None) -> list[dict]:
    """Normalised rows for a public status (`active` | `closed` | `all`).
    Open lots whose end passed while the memo was warm are dropped, never
    shown as live. Raises when the read fails (callers decide the fallback)."""
    now = now or datetime.now(timezone.utc)
    out: list[dict] = []
    if status in ("active", "all"):
        out += [r for r in _lots("open") if r["end_utc"] and r["end_utc"] > now]
    if status in ("closed", "all"):
        out += _lots("closed")
    return [dict(r) for r in out]


# ── in-memory filters (mirror deals_query.build_where for the snapshot half) ─
def apply_filters(items: list[dict], *, q=None, category=None, state=None, max_bids=None,
                  ending_within=None, min_price=None, max_price=None, bbox=None,
                  site=None, now: datetime | None = None) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    if site:
        items = [i for i in items if i["source"] == site]
    if q:
        needle = str(q).lower()
        items = [i for i in items if needle in (i["title"] or "").lower()]
    if category:
        items = [i for i in items if i["canonical_category"] == category]
    if state:
        s = str(state).strip().upper()
        items = [i for i in items if i["state"] == s]
    if max_bids is not None:        # NULL bid_count never passes, same as SQL `<=`
        items = [i for i in items if i["bid_count"] is not None and i["bid_count"] <= int(max_bids)]
    if ending_within is not None:
        edge = now + timedelta(hours=float(ending_within))
        items = [i for i in items if i["end_utc"] and i["end_utc"] <= edge]
    if min_price is not None:
        items = [i for i in items if i["current_bid"] is not None and i["current_bid"] >= float(min_price)]
    if max_price is not None:
        items = [i for i in items if i["current_bid"] is not None and i["current_bid"] <= float(max_price)]
    if bbox is not None:            # (south, west, north, east); unmapped lots drop out
        s, w, n, e = bbox
        items = [i for i in items if i["lat"] is not None and i["lng"] is not None
                 and s <= i["lat"] <= n and w <= i["lng"] <= e]
    return items
