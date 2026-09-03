"""Server-side read model for the PUBLIC /deals page ("Surplus Radar").

Everything the public may see about `deal_lots` passes through here. Three
rules, enforced in SQL so no caller can forget them:

1. **Chair-buyer isolation.** The operator resells seating. A chair buyer who
   finds this page must never see the lots he is bidding on, so we exclude:
   the seating category, any seating word in the title, and every lot in
   `tracked_lots` / `auction_favorites` / `deal_list_items` (the three places
   the operator marks interest). Override lists via env, never by editing SQL.
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
from typing import Any

from .. import db
from . import deals_query
from deals.fees import fee_model_from_env

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
    see `is_operator_lot`."""
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


def public_order(sort: str, direction: str | None) -> str:
    col = PUBLIC_SORTS.get(sort) or PUBLIC_SORTS["ends"]
    if direction not in ("asc", "desc"):
        direction = "asc" if col == "end_utc" else "desc"
    return f"ORDER BY {col} {direction.upper()} NULLS LAST"


def clamp_page(page: int, per_page: int) -> tuple[int, int]:
    per_page = per_page if per_page in PER_PAGE_CHOICES else PER_PAGE_CHOICES[0]
    return max(1, min(int(page or 1), MAX_PAGE)), per_page


def enrich_public(row: dict, fees) -> dict:
    return deals_query.enrich(row, fees)  # PUBLIC_COLS already excludes private fields


# ── reads ────────────────────────────────────────────────────────────────────

def fetch_page(*, q=None, category=None, state=None, max_bids=None,
               ending_within=None, status="active", min_price=None,
               max_price=None, bbox=None, sort="ends", dir=None,
               page=1, per_page=25) -> dict:
    page, per_page = clamp_page(page, per_page)
    where, args = build_public_where(
        q=q, category=category, state=state, max_bids=max_bids,
        ending_within=ending_within, status=status, min_price=min_price,
        max_price=max_price, bbox=bbox)
    order = public_order(sort, dir)
    with db.connect() as conn:
        rows = conn.execute(
            f"SELECT {PUBLIC_COLS} FROM deal_lots WHERE {where} {order} LIMIT %s OFFSET %s",
            (*args, per_page, (page - 1) * per_page),
        ).fetchall()
        total = conn.execute(
            f"SELECT count(*) AS c FROM deal_lots WHERE {where}", tuple(args)
        ).fetchone()["c"]
    fees = fee_model_from_env()
    return {
        "rows": [enrich_public(dict(r), fees) for r in rows],
        "total": total, "page": page, "per_page": per_page,
        "pages": max(1, -(-total // per_page)),
    }


def fetch_pins(*, q=None, category=None, state=None, max_bids=None,
               ending_within=None, status="active", min_price=None,
               max_price=None) -> dict:
    params = dict(q=q, category=category, state=state, max_bids=max_bids,
                  ending_within=ending_within, status=status,
                  min_price=min_price, max_price=max_price)
    key = "pins:" + json.dumps(params, sort_keys=True, default=str)

    def load():
        where, args = build_public_where(**params)
        points = db.fetch_all(
            "SELECT asset_id, account_id, auction_id, title, current_bid, bid_count, "
            "end_utc, city, state, lat, lng FROM deal_lots "
            f"WHERE {where} AND lat IS NOT NULL AND lng IS NOT NULL "
            "ORDER BY end_utc ASC LIMIT %s",
            (*args, PINS_CAP + 1),
        )
        capped = len(points) > PINS_CAP
        points = points[:PINS_CAP]
        for p in points:
            p["govdeals_url"] = f"https://www.govdeals.com/en/asset/{p['asset_id']}/{p['account_id']}"
        return {"points": points, "capped": capped}

    return _cached(key, load)


def fetch_facets() -> dict:
    def load():
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
            stats = conn.execute(
                "SELECT count(*) AS tracked, "
                "count(*) FILTER (WHERE outcome_complete IS NOT TRUE AND end_utc > now()) AS active, "
                "count(*) FILTER (WHERE outcome_complete IS TRUE) AS closed, "
                "count(*) FILTER (WHERE outcome = 'no_bid') AS no_bid, "
                "count(DISTINCT state) FILTER (WHERE outcome_complete IS NOT TRUE AND end_utc > now()) AS states, "
                "min(first_seen_at) AS since FROM deal_lots").fetchone()
        return {"categories": cats, "states": states, "stats": stats, "cached_at": time.time()}

    return _cached("facets", load)
