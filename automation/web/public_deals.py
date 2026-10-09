"""Server-side read model for the PUBLIC /deals page ("Surplus Radar").

Two halves, one shape:

* **GovDeals** rows come from `deal_lots` (comps, quantity, outcomes, the
  archived-lot viewer) — paged and filtered in SQL.
* **Every other recorder source** (AllSurplus, GSA, Purple Wave, Public
  Surplus, Municibid, MiBid) comes from `listing_snapshots` through
  `deals_sources` — a 5-min memo of normalised rows, filtered in memory.

`fetch_page` / `fetch_pins` / `fetch_facets` UNION the two. Three rules,
enforced so no caller can forget them:

1. **Chair-buyer isolation.** The operator resells seating. A chair buyer who
   finds this page must never see the lots he is bidding on, so we exclude:
   the seating category, any seating word in the title, and every lot in
   `tracked_lots` / `auction_favorites` / `deal_list_items` (the three places
   the operator marks interest). Override lists via env, never by editing SQL.
   The SQL form (`exclusion_where`) guards `deal_lots`; the table-agnostic form
   (`OperatorPicks` + `is_excluded`) guards the snapshot half.
2. **No source photos, no verdicts, no home distance.** `PUBLIC_COLS` is the
   allow-list; the copyright-bearing image columns, the LLM verdict join, and
   `DEALS_HOME_*` distance never reach a public response.
3. **One connection per request; facets/pins cached 5 min.** The pooler
   handshake is ~1.3 s, so rows+count share a connection and the expensive
   whole-table stats are memoized in-process.
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from typing import Any

from .. import db
from . import deals_query
from deals.fees import fee_model_from_env
from deals.quantity import lot_quantity, unit_price

# ── policy (env-overridable, comma lists) ────────────────────────────────────
_DEFAULT_CATS = "seating_furniture"
_DEFAULT_WORDS = ("chair,chairs,seating,stool,stools,bench,benches,pew,pews,"
                  "barstool,barstools,sofa,sofas,couch,couches,banquet")


def _csv(name: str, default: str) -> list[str]:
    raw = os.getenv(name) or default
    return [x.strip() for x in raw.split(",") if x.strip()]


EXCLUDED_CATEGORIES: frozenset[str] = frozenset(_csv("PUBLIC_DEALS_EXCLUDE_CATEGORIES", _DEFAULT_CATS))
_WORDS_ALT = "|".join(re.escape(w) for w in _csv("PUBLIC_DEALS_EXCLUDE_KEYWORDS", _DEFAULT_WORDS))
# Two dialects of the same whole-word regex. Python `re` spells a word boundary
# `\b`; Postgres ARE spells it `\y` — and reads `\b` as BACKSPACE, so the
# Python form matches nothing in SQL (found on the 2026-09-04 live smoke:
# "Salon Chairs" sailed through). Never hand the Python one to the DB.
EXCLUDED_TITLE_RE: str = r"\b(?:" + _WORDS_ALT + r")\b"
EXCLUDED_TITLE_SQL_RE: str = r"\y(?:" + _WORDS_ALT + r")\y"
_TITLE_RX = re.compile(EXCLUDED_TITLE_RE, re.I)

PUBLIC_COLS = (
    "asset_id, account_id, auction_id, title, canonical_category, "
    "native_category_name, city, state, bid_count, current_bid, currency_code, "
    "end_utc, outcome, final_bid, final_bid_count, outcome_complete, "
    "first_seen_at, lat, lng"
)
PUBLIC_SORTS = {"ends": "end_utc", "newest": "first_seen_at",
                "bid": "current_bid", "bids": "bid_count"}
PER_PAGE_CHOICES = (25, 50, 100)
MAX_PAGE = 400
PINS_CAP = 5000
CACHE_TTL = 300
GOVDEALS = "govdeals"

_CACHE: dict[str, tuple[float, Any]] = {}


def clear_cache() -> None:
    _CACHE.clear()


def _cached(key: str, loader):
    now = time.monotonic()
    hit = _CACHE.get(key)
    if hit and now - hit[0] < CACHE_TTL:
        return hit[1]
    value = loader()
    _CACHE[key] = (now, value)
    return value


# ── exclusion policy ─────────────────────────────────────────────────────────

def exclusion_where() -> tuple[str, list]:
    """SQL fragment (and args) that hides every lot a chair buyer must not see."""
    where = (
        "(canonical_category IS NULL OR canonical_category <> ALL(%s)) "
        "AND COALESCE(title, '') !~* %s "
        "AND NOT EXISTS (SELECT 1 FROM tracked_lots t "
        "  WHERE t.asset_id = deal_lots.asset_id AND t.account_id = deal_lots.account_id) "
        "AND NOT EXISTS (SELECT 1 FROM auction_favorites f "
        "  WHERE f.asset_id = deal_lots.asset_id::text || '/' || deal_lots.account_id::text) "
        "AND NOT EXISTS (SELECT 1 FROM deal_list_items li "
        "  WHERE li.asset_id = deal_lots.asset_id AND li.account_id = deal_lots.account_id "
        "    AND li.auction_id = deal_lots.auction_id)"
    )
    return where, [sorted(EXCLUDED_CATEGORIES), EXCLUDED_TITLE_SQL_RE]


def is_excluded(row: dict) -> bool:
    """Pure half of the policy (category + title). Membership needs the DB —
    see `is_operator_lot` (deal_lots keys) or `is_operator_pick` (any source)."""
    if (row.get("canonical_category") or "") in EXCLUDED_CATEGORIES:
        return True
    return bool(_TITLE_RX.search(row.get("title") or ""))


def is_operator_lot(asset_id: int, account_id: int, auction_id: int) -> bool:
    row = db.fetch_one(
        "SELECT EXISTS (SELECT 1 FROM tracked_lots WHERE asset_id=%s AND account_id=%s) "
        "OR EXISTS (SELECT 1 FROM auction_favorites WHERE asset_id=%s) "
        "OR EXISTS (SELECT 1 FROM deal_list_items WHERE asset_id=%s AND account_id=%s AND auction_id=%s) "
        "AS hit",
        (asset_id, account_id, f"{asset_id}/{account_id}", asset_id, account_id, auction_id),
    )
    return bool(row and row["hit"])


# ── operator picks, for public read models that are NOT `deal_lots` ──────────
# `exclusion_where()` joins on deal_lots' integer keys. A read model built on
# another table (the recorder's `listing_snapshots`, behind `deals_sources`)
# cannot use it, so the same rule lives here in a table-agnostic form: load the
# three pick tables once (they are tiny), then test each lot by maestro id pair,
# by native id, by URL and by title. Matching is deliberately greedy — hiding a
# lot the operator did not pick costs nothing, showing one he did is the leak.
PICKS_ROW_CAP = 5000
_MAESTRO_KEY_RE = re.compile(r"^(\d+)/(\d+)(?:/\d+)?$")
_ASSET_URL_RE = re.compile(r"/asset/(\d+)/(\d+)")


def _norm_title(s: Any) -> str:
    return re.sub(r"\W+", " ", str(s or "").lower()).strip()


def _norm_url(u: Any) -> str:
    s = str(u or "").strip().lower().split("#", 1)[0]
    s = re.sub(r"^https?://(www\.)?", "", s)
    return s.rstrip("/")


class OperatorPicks:
    """Everything the operator has starred / tracked / listed, as lookup sets."""
    __slots__ = ("pairs", "ids", "urls", "titles")

    def __init__(self, pairs=(), ids=(), urls=(), titles=()):
        self.pairs = frozenset(pairs)
        self.ids = frozenset(i for i in ids if i)
        self.urls = frozenset(u for u in urls if u)
        self.titles = frozenset(t for t in titles if t)


def load_operator_picks(conn) -> OperatorPicks:
    """Read tracked_lots + auction_favorites + deal_list_items on `conn`.
    Raises on failure — a caller that cannot load the picks must not publish."""
    pairs: set[tuple[int, int]] = set()
    ids: set[str] = set()
    urls: set[str] = set()
    titles: set[str] = set()

    def _link(link: Any) -> None:
        if not link:
            return
        urls.add(_norm_url(link))
        m = _ASSET_URL_RE.search(str(link))
        if m:
            pairs.add((int(m.group(1)), int(m.group(2))))

    for r in conn.execute("SELECT asset_id, account_id, title, url FROM tracked_lots LIMIT %s",
                          (PICKS_ROW_CAP,)).fetchall():
        pairs.add((int(r["asset_id"]), int(r["account_id"])))
        titles.add(_norm_title(r.get("title")))
        _link(r.get("url"))
    for r in conn.execute("SELECT asset_id, link, title FROM auction_favorites LIMIT %s",
                          (PICKS_ROW_CAP,)).fetchall():
        key = str(r.get("asset_id") or "").strip()
        ids.add(key)
        ids.add(key.split(":", 1)[-1])          # "bs:<uuid>" → "<uuid>"
        m = _MAESTRO_KEY_RE.match(key)
        if m:
            pairs.add((int(m.group(1)), int(m.group(2))))
        titles.add(_norm_title(r.get("title")))
        _link(r.get("link"))
    for r in conn.execute("SELECT asset_id, account_id FROM deal_list_items LIMIT %s",
                          (PICKS_ROW_CAP,)).fetchall():
        pairs.add((int(r["asset_id"]), int(r["account_id"])))
    return OperatorPicks(pairs, ids, urls, titles)


def is_operator_pick(picks: OperatorPicks, *, source_lot_id: Any,
                     title: Any = None, url: Any = None) -> bool:
    """True when a lot from ANY source is one the operator marked interest in."""
    key = str(source_lot_id or "").strip()
    if key and (key in picks.ids or key.split(":", 1)[-1] in picks.ids):
        return True
    m = _MAESTRO_KEY_RE.match(key)
    if m and (int(m.group(1)), int(m.group(2))) in picks.pairs:
        return True
    if url:
        if _norm_url(url) in picks.urls:
            return True
        m = _ASSET_URL_RE.search(str(url))
        if m and (int(m.group(1)), int(m.group(2))) in picks.pairs:
            return True
    t = _norm_title(title)
    return bool(t and t in picks.titles)


# ── query building ───────────────────────────────────────────────────────────

def build_public_where(*, q=None, category=None, state=None, max_bids=None,
                       ending_within=None, status="active", min_price=None,
                       max_price=None, bbox=None) -> tuple[str, list]:
    where, args = deals_query.build_where(
        q=q, category=category, state=state, max_bids=max_bids,
        ending_within=ending_within, status=status, min_price=min_price,
        max_price=max_price, bbox=bbox, search_fields=("title",),
    )
    ex_where, ex_args = exclusion_where()
    return f"{where} AND {ex_where}", [*args, *ex_args]


def public_sort(sort: str, direction: str | None) -> tuple[str, str]:
    col = PUBLIC_SORTS.get(sort) or PUBLIC_SORTS["ends"]
    if direction not in ("asc", "desc"):
        direction = "asc" if col == "end_utc" else "desc"
    return col, direction


def public_order(sort: str, direction: str | None) -> str:
    col, direction = public_sort(sort, direction)
    return f"ORDER BY {col} {direction.upper()} NULLS LAST"


def clamp_page(page: int, per_page: int) -> tuple[int, int]:
    per_page = per_page if per_page in PER_PAGE_CHOICES else PER_PAGE_CHOICES[0]
    return max(1, min(int(page or 1), MAX_PAGE)), per_page


def enrich_public(row: dict, fees) -> dict:
    return deals_query.enrich(row, fees)  # PUBLIC_COLS already excludes private fields


def enrich_snapshot(row: dict, fees=None) -> dict:
    """Quantity + $/unit for a non-GovDeals row. No landed cost: the fee model
    is GovDeals' buyer premium and we will not invent another site's."""
    qty, src = lot_quantity(row.get("title"))
    row["quantity"] = qty
    row["quantity_source"] = src
    row["unit_bid"] = unit_price(row.get("current_bid"), qty)
    row["landed_cost"] = None
    row["unit_landed"] = None
    return row


def govdeals_half(site: str | None) -> bool:
    """Does the `deal_lots` half take part under this `site` filter?"""
    return not site or site == GOVDEALS


def _merge_sorted(rows: list[dict], col: str, direction: str) -> list[dict]:
    """ORDER BY col direction NULLS LAST, across both halves."""
    def val(r):
        v = r.get(col)
        if isinstance(v, datetime):
            return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
        return float(v) if v is not None else None
    present = [r for r in rows if val(r) is not None]
    present.sort(key=val, reverse=(direction == "desc"))
    return present + [r for r in rows if val(r) is None]


def _snapshot_rows(*, status, q, category, state, max_bids, ending_within,
                   min_price, max_price, bbox, site, now) -> list[dict]:
    from . import deals_sources
    if site and site == GOVDEALS:
        return []
    return deals_sources.apply_filters(
        deals_sources.lots(status, now=now), q=q, category=category, state=state,
        max_bids=max_bids, ending_within=ending_within, min_price=min_price,
        max_price=max_price, bbox=bbox, site=site, now=now)


# ── reads ────────────────────────────────────────────────────────────────────

def fetch_page(*, q=None, category=None, state=None, max_bids=None,
               ending_within=None, status="active", min_price=None,
               max_price=None, bbox=None, site=None, sort="ends", dir=None,
               page=1, per_page=25) -> dict:
    page, per_page = clamp_page(page, per_page)
    col, direction = public_sort(sort, dir)
    now = datetime.now(timezone.utc)
    fees = fee_model_from_env()
    gd_rows: list[dict] = []
    gd_total = 0
    if govdeals_half(site):
        where, args = build_public_where(
            q=q, category=category, state=state, max_bids=max_bids,
            ending_within=ending_within, status=status, min_price=min_price,
            max_price=max_price, bbox=bbox)
        order = f"ORDER BY {col} {direction.upper()} NULLS LAST"
        # Top-N (N = page × per_page) from SQL is enough: anything in the true
        # top N of the union is either in deal_lots' top N or in the snapshot set.
        with db.connect() as conn:
            rows = conn.execute(
                f"SELECT {PUBLIC_COLS} FROM deal_lots WHERE {where} {order} LIMIT %s",
                (*args, page * per_page),
            ).fetchall()
            gd_total = conn.execute(
                f"SELECT count(*) AS c FROM deal_lots WHERE {where}", tuple(args)
            ).fetchone()["c"]
        gd_rows = [enrich_public(dict(r), fees) for r in rows]
    snap = [enrich_snapshot(r, fees) for r in _snapshot_rows(
        status=status, q=q, category=category, state=state, max_bids=max_bids,
        ending_within=ending_within, min_price=min_price, max_price=max_price,
        bbox=bbox, site=site, now=now)]
    merged = _merge_sorted(gd_rows + snap, col, direction)
    total = gd_total + len(snap)
    start = (page - 1) * per_page
    return {
        "rows": merged[start:start + per_page],
        "total": total, "page": page, "per_page": per_page,
        "pages": max(1, -(-total // per_page)),
    }


def fetch_pins(*, q=None, category=None, state=None, max_bids=None,
               ending_within=None, status="active", min_price=None,
               max_price=None, site=None) -> dict:
    params = dict(q=q, category=category, state=state, max_bids=max_bids,
                  ending_within=ending_within, status=status,
                  min_price=min_price, max_price=max_price, site=site)
    key = "pins:" + json.dumps(params, sort_keys=True, default=str)

    def load():
        now = datetime.now(timezone.utc)
        points: list[dict] = []
        if govdeals_half(site):
            where, args = build_public_where(**{k: v for k, v in params.items() if k != "site"})
            points = db.fetch_all(
                "SELECT asset_id, account_id, auction_id, title, current_bid, bid_count, "
                "end_utc, city, state, lat, lng FROM deal_lots "
                f"WHERE {where} AND lat IS NOT NULL AND lng IS NOT NULL "
                "ORDER BY end_utc ASC LIMIT %s",
                (*args, PINS_CAP + 1),
            )
            for p in points:
                p["source"] = GOVDEALS
                p["govdeals_url"] = f"https://www.govdeals.com/en/asset/{p['asset_id']}/{p['account_id']}"
                p["url"] = p["govdeals_url"]
                p["viewer_url"] = f"/deals/{p['asset_id']}/{p['account_id']}/{p['auction_id']}"
        for r in _snapshot_rows(status=status, q=q, category=category, state=state,
                                max_bids=max_bids, ending_within=ending_within,
                                min_price=min_price, max_price=max_price, bbox=None,
                                site=site, now=now):
            if r["lat"] is None or r["lng"] is None:
                continue
            points.append({k: r.get(k) for k in (
                "asset_id", "account_id", "auction_id", "source_lot_id", "title", "current_bid", "bid_count",
                "end_utc", "city", "state", "lat", "lng", "source", "source_name", "url", "viewer_url")})
        points = _merge_sorted(points, "end_utc", "asc")
        capped = len(points) > PINS_CAP
        return {"points": points[:PINS_CAP], "capped": capped}

    return _cached(key, load)


def fetch_facets() -> dict:
    def load():
        from . import deals_sources
        where, args = build_public_where(status="active")
        with db.connect() as conn:
            cats = conn.execute(
                "SELECT canonical_category AS value, count(*) AS count FROM deal_lots "
                f"WHERE {where} AND canonical_category IS NOT NULL GROUP BY 1 ORDER BY 2 DESC",
                tuple(args)).fetchall()
            states = conn.execute(
                "SELECT state AS value, count(*) AS count FROM deal_lots "
                f"WHERE {where} AND state IS NOT NULL GROUP BY 1 ORDER BY 2 DESC",
                tuple(args)).fetchall()
            gd_public = conn.execute(
                f"SELECT count(*) AS c FROM deal_lots WHERE {where}", tuple(args)).fetchone()["c"]
            stats = conn.execute(
                "SELECT count(*) AS tracked, "
                "count(*) FILTER (WHERE outcome_complete IS NOT TRUE AND end_utc > now()) AS active, "
                "count(*) FILTER (WHERE outcome_complete IS TRUE) AS closed, "
                "count(*) FILTER (WHERE outcome = 'no_bid') AS no_bid, "
                "count(DISTINCT state) FILTER (WHERE outcome_complete IS NOT TRUE AND end_utc > now()) AS states, "
                "min(first_seen_at) AS since FROM deal_lots").fetchone()
        cats = [dict(c) for c in cats]
        states = [dict(s) for s in states]
        stats = dict(stats)
        sites: dict[str, int] = {GOVDEALS: int(gd_public)}
        # The snapshot half: a failed read keeps the GovDeals facets rather than
        # failing the page — the lots endpoint reports its own error.
        try:
            live = deals_sources.lots("active")
            closed = deals_sources.lots("closed")
        except Exception:
            live, closed = [], []
        _bump(cats, (r["canonical_category"] for r in live))
        _bump(states, (r["state"] for r in live))
        for r in live:
            sites[r["source"]] = sites.get(r["source"], 0) + 1
        stats["active"] = int(stats.get("active") or 0) + len(live)
        stats["tracked"] = int(stats.get("tracked") or 0) + len(live) + len(closed)
        stats["closed"] = int(stats.get("closed") or 0) + len(closed)
        stats["no_bid"] = int(stats.get("no_bid") or 0) + sum(1 for r in closed if r["outcome"] == "no_bid")
        stats["states"] = len({s["value"] for s in states})
        site_rows = [{"value": k, "name": deals_sources.SITES.get(k, k), "count": n}
                     for k, n in sites.items() if n]
        site_rows.sort(key=lambda s: (-s["count"], s["value"]))
        return {"categories": cats, "states": states, "sites": site_rows,
                "stats": stats, "cached_at": time.time()}

    return _cached("facets", load)


def _bump(facet: list[dict], values) -> None:
    """Add in-memory counts to a `[{value, count}]` facet list, in place."""
    index = {f["value"]: f for f in facet}
    for v in values:
        if not v:
            continue
        if v in index:
            index[v]["count"] = int(index[v]["count"]) + 1
        else:
            index[v] = {"value": v, "count": 1}
            facet.append(index[v])
    facet.sort(key=lambda f: (-int(f["count"]), str(f["value"])))
