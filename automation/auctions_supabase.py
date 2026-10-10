"""Supabase-backed read layer for the Auctions tab.

The auction scrape archive lives in Supabase (`auction_listings`, populated by
`scripts/transfer_listings_to_supabase.py`). This module is the profile-driven
loader behind `/api/auctions`: a research `Profile` (deals/profiles.py) says
which keywords/exclusions/quantity floor apply, the SQL does the filtering,
and the upstream ranking / active-window / condition-LLM helpers are reused
verbatim so card output keeps today's shape.

`get_top_chairs()` is the back-compat wrapper (category None/'banquet' ->
the `chairs` profile, 'medical' -> `medical`).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from . import db
from deals import profiles as _profiles
from deals.profiles import Profile

# Reuse the pure (non-SQLite) helpers from the upstream package so behavior
# stays in lockstep with the original SQLite path.
from auction_extractors.top_chairs import (  # noqa: E402
    CATEGORIES,
    _SANE_MAX_QUANTITY,
    TRUSTED_QUANTITY_SOURCES,
    UNSURE_CONFIDENCES,
    _enrich_via_llm,
    _is_active,
    _price_to_float,
    title_claimed_quantity,
)

Source = Literal["all", "gd", "ps", "bs", "tx"]

_SOURCE_FRAG = {"gd": "govdeals.com", "ps": "publicsurplus.com", "bs": "bidspotter.com",
                "tx": "txauction.com"}
SOURCE_NAMES = {"gd": "GovDeals", "ps": "Public Surplus", "bs": "BidSpotter", "tx": "TXAuction"}
SOURCES = ("all", *_SOURCE_FRAG)


def source_of_link(link: str | None) -> str:
    """'gd' | 'ps' | 'bs' | 'tx' | 'other' — which site a cached row came from."""
    low = (link or "").lower()
    for key, frag in _SOURCE_FRAG.items():
        if frag in low:
            return key
    return "other"


def _link_clause(source: str) -> tuple[str, tuple]:
    """`AND link ILIKE …` for one source; nothing for 'all'."""
    if source == "all":
        return "", ()
    return "AND link ILIKE %s", (f"%{_SOURCE_FRAG[source]}%",)


def _end_utc_iso(end_date) -> str:
    """Close time as ISO-8601 UTC ('…Z') for the card's readable countdown, via
    the one end-date parser (naive = US/Eastern). Unparseable → ''."""
    from .favorites import _parse_end_date
    dt = _parse_end_date(end_date)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if dt else ""

_SELECT_COLS = (
    "asset_id, link, title, description, quantity, quantity_source, "
    "quantity_confidence, price, location, pickup_zip, contact_email, "
    "contact_phone, end_date, time_left, image_url, last_seen_at"
)


def _load_from_supabase(profile: Profile, source: Source, min_quantity: int | None,
                        seen_within_days: int | None = None) -> list[dict]:
    """Rows from `auction_listings` matching the profile (keywords, exclusions,
    quantity floor) for one source, quantity-desc then price-asc.

    Plus the rows whose count is untrusted or low-confidence but whose title
    states a count over the floor (``title_claimed_quantity``). Those carry
    ``quantity_unverified=True`` + ``llm_quantity`` (what the LLM said), and
    ``quantity`` is the title's claim so they rank and render as the lot they
    say they are. Before this, "Two Hundred Ten (210) Banquet Hall Chairs"
    (LLM: 1, low) never reached the Auctions tab although it was starred.
    ``seen_within_days`` only narrows that second query.
    """
    link_sql, link_args = _link_clause(source)
    floor = max(1, profile.min_quantity if min_quantity is None else int(min_quantity))
    pwhere, pargs = _profiles.auction_listings_where(profile, min_quantity)
    rows = db.fetch_all(
        f"""
        SELECT {_SELECT_COLS}
        FROM auction_listings
        WHERE {pwhere}
          AND quantity <= %s
          AND quantity_source = ANY(%s)
          {link_sql}
        ORDER BY quantity DESC
        """,
        (*pargs, _SANE_MAX_QUANTITY, list(TRUSTED_QUANTITY_SOURCES), *link_args),
    )
    kwhere, kargs = _profiles.auction_listings_where(profile, quantity_floor=False)
    seen_sql, seen_args = "", ()
    if seen_within_days is not None:
        seen_sql = "AND last_seen_at >= now() - make_interval(days => %s)"
        seen_args = (int(seen_within_days) + 1,)
    unsure = db.fetch_all(
        f"""
        SELECT {_SELECT_COLS}
        FROM auction_listings
        WHERE {kwhere}
          AND (quantity IS NULL OR quantity < %s)
          AND (quantity_source <> ALL(%s) OR quantity_confidence = ANY(%s))
          AND title ~ '[0-9]'
          {link_sql}
          {seen_sql}
        """,
        (*kargs, floor, list(TRUSTED_QUANTITY_SOURCES), list(UNSURE_CONFIDENCES),
         *link_args, *seen_args),
    )
    for r in unsure:
        claim = title_claimed_quantity(r)
        if claim and floor <= claim[0] <= _SANE_MAX_QUANTITY:
            r["llm_quantity"] = r.get("quantity")
            r["quantity"] = claim[0]
            r["quantity_unverified"] = True
            rows.append(r)
    # last_seen_at comes back as a datetime (timestamptz); the upstream
    # _is_active helper expects an ISO string. Normalize so it parses.
    for r in rows:
        ls = r.get("last_seen_at")
        if isinstance(ls, datetime):
            r["last_seen_at"] = ls.isoformat()
    rows.sort(key=lambda x: (-int(x.get("quantity") or 0), _price_to_float(x.get("price"))))
    return rows


def get_top_lots(profile: Profile, source: Source = "all", n: int = 15,
                 min_quantity: int | None = None, include_condition: bool = True,
                 active_only: bool = True, max_stale_days: int = 2) -> list[dict]:
    """Top-n cached lots matching `profile`. Same row shape as get_top_chairs;
    `category` is the profile slug, `category_keyword` the keyword that hit."""
    if source not in SOURCES:
        raise ValueError(f"source must be one of {list(SOURCES)}, got {source!r}")
    items = _load_from_supabase(profile, source, min_quantity,
                                seen_within_days=max_stale_days if active_only else None)
    for it in items:
        it["category"] = profile.slug
        it["category_keyword"] = _profiles.matched_keyword(profile, it.get("title"), it.get("description"))
    if active_only:
        now = datetime.now(timezone.utc)
        items = [it for it in items if _is_active(it, now, max_stale_days)]
    top = items[: max(0, int(n))]
    if not top:
        return []
    enrich = None
    if include_condition:
        try:
            enrich = _enrich_via_llm(top)
        except Exception as e:  # LLM unreachable — degrade to raw titles.
            import sys
            print(f"[auctions_supabase] condition LLM unavailable ({e!r}); raw titles", file=sys.stderr)
    if enrich is None:
        enrich = [{"title": it.get("title") or "", "condition": None, "condition_note": None} for it in top]
    out = []
    for i, (it, en) in enumerate(zip(top, enrich), start=1):
        src = source_of_link(it.get("link"))
        out.append({
            "rank": i, "quantity": int(it.get("quantity") or 0),
            "title": en["title"], "raw_title": it.get("title") or "",
            "price": it.get("price") or "", "end_date": it.get("end_date") or "",
            "time_left": it.get("time_left") or "", "link": it.get("link") or "",
            "image_url": it.get("image_url") or "", "location": it.get("location") or "",
            "pickup_zip": it.get("pickup_zip") or "", "contact_email": it.get("contact_email") or "",
            "contact_phone": it.get("contact_phone") or "",
            "category": it["category"], "category_keyword": it["category_keyword"],
            "condition": en["condition"] if include_condition else None,
            "condition_note": en["condition_note"] if include_condition else None,
            "quantity_unverified": bool(it.get("quantity_unverified")),
            "llm_quantity": it.get("llm_quantity"),
            "source": src, "source_name": SOURCE_NAMES.get(src, "Other"),
            "end_utc": _end_utc_iso(it.get("end_date")),
        })
    return out


def get_top_chairs(source: Source = "gd", n: int = 15, min_quantity: int = 50,
                   include_condition: bool = True, active_only: bool = True,
                   max_stale_days: int = 2, category: str | None = None) -> list[dict]:
    """Back-compat wrapper: category None/'banquet' -> chairs, 'medical' -> medical."""
    if category is not None and category not in CATEGORIES:
        raise ValueError(f"category must be one of {CATEGORIES} or None, got {category!r}")
    slug = "medical" if category == "medical" else "chairs"
    profile = _profiles.load(slug) or _profiles.SEED_PROFILES[slug]
    return get_top_lots(profile, source=source, n=n, min_quantity=min_quantity,
                        include_condition=include_condition, active_only=active_only,
                        max_stale_days=max_stale_days)


def cache_stats() -> dict:
    """Roll-up for the Auctions tab header, from `auction_listings`."""
    total_row = db.fetch_one(
        "SELECT count(*) AS n, max(last_seen_at) AS newest, min(last_seen_at) AS oldest "
        "FROM auction_listings"
    )
    by_rows = db.fetch_all(
        """
        SELECT CASE
                 WHEN link ILIKE %s THEN 'gd'
                 WHEN link ILIKE %s THEN 'ps'
                 WHEN link ILIKE %s THEN 'bs'
                 WHEN link ILIKE %s THEN 'tx'
                 ELSE 'other'
               END AS src,
               count(*) AS n,
               max(last_seen_at) AS newest
        FROM auction_listings GROUP BY 1
        """,
        (f"%{_SOURCE_FRAG['gd']}%", f"%{_SOURCE_FRAG['ps']}%", f"%{_SOURCE_FRAG['bs']}%",
         f"%{_SOURCE_FRAG['tx']}%"),
    )

    def _iso(v):
        return v.isoformat() if isinstance(v, datetime) else v

    return {
        "total": (total_row or {}).get("n", 0) or 0,
        "newest_seen_at": _iso((total_row or {}).get("newest")),
        "oldest_seen_at": _iso((total_row or {}).get("oldest")),
        "by_source": {
            r["src"]: {"count": r["n"], "newest_seen_at": _iso(r["newest"])}
            for r in by_rows
        },
    }


# ── Listings DB tab: raw browser over auction_listings ──────────────────────
#
# The tab used to read auction_extractors/state/listings.db on the laptop. The
# scrape moved to the Render discovery cron (which writes /tmp/listings.db and
# pushes straight to Supabase), so that file stopped advancing on 2026-07-14
# and the tab silently showed a three-month-old cache — the Orlando lots
# starred on 2026-10-01 were in Supabase and missing here. Read the same table
# the Auctions tab reads.

# end_date is free text. ISO with no zone = GovDeals = US Eastern (the rule in
# auction_extractors/end_dates.py); ISO with Z/offset keeps its zone; anything
# else (old "April 20, 2026 01:00 PM EDT" strings) is NULL → "unknown".
_END_TS_SQL = r"""(CASE
  WHEN end_date ~ '^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d+)?)?$'
    THEN end_date::timestamp AT TIME ZONE 'America/New_York'
  WHEN end_date ~ '^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})$'
    THEN end_date::timestamptz
END)"""

_PRICE_SQL = (r"(CASE WHEN regexp_replace(price, '[^0-9.]', '', 'g') ~ '^\d+(\.\d+)?$' "
              r"THEN regexp_replace(price, '[^0-9.]', '', 'g')::numeric END)")

LISTINGS_SORTS = {
    "qty_desc":        "COALESCE(quantity, 0) DESC, last_seen_at DESC",
    "qty_asc":         "COALESCE(quantity, 0) ASC, last_seen_at DESC",
    "last_seen_desc":  "last_seen_at DESC",
    "first_seen_desc": "first_seen_at DESC",
    "price_low":       f"{_PRICE_SQL} ASC NULLS LAST, last_seen_at DESC",
}

_LISTINGS_COLS = (
    "asset_id, link, title, description, quantity, quantity_source, "
    "quantity_confidence, price, location, lot_number, end_date, time_left, "
    "description_fetched_at, first_seen_at, last_seen_at, image_url"
)


def browse_listings(*, source: str = "all", q: str = "", min_qty: int = 0,
                    max_qty: int = 99999, status: str = "all",
                    seen_within_days: int = 0, sort: str = "qty_desc",
                    limit: int = 50, offset: int = 0) -> tuple[int, list[dict]]:
    """Filtered page of raw `auction_listings` rows → ``(total, rows)``."""
    where: list[str] = ["COALESCE(quantity, 0) BETWEEN %s AND %s"]
    args: list = [min_qty, max_qty]
    if source in _SOURCE_FRAG:
        where.append("link ILIKE %s")
        args.append(f"%{_SOURCE_FRAG[source]}%")
    if q.strip():
        where.append("(title ILIKE %s OR description ILIKE %s)")
        like = f"%{q.strip()}%"
        args += [like, like]
    if seen_within_days > 0:
        where.append("last_seen_at >= now() - make_interval(days => %s)")
        args.append(int(seen_within_days))
    if status == "active":
        where.append(f"({_END_TS_SQL} >= now() OR ({_END_TS_SQL} IS NULL "
                     "AND COALESCE(time_left, '') <> ''))")
    elif status == "expired":
        where.append(f"{_END_TS_SQL} < now()")
    elif status == "unknown":
        where.append(f"({_END_TS_SQL} IS NULL AND COALESCE(time_left, '') = '')")
    where_sql = " AND ".join(where)
    order = LISTINGS_SORTS.get(sort, LISTINGS_SORTS["qty_desc"])

    total = (db.fetch_one(f"SELECT count(*) AS n FROM auction_listings WHERE {where_sql}",
                          tuple(args)) or {}).get("n", 0) or 0
    rows = db.fetch_all(
        f"SELECT {_LISTINGS_COLS} FROM auction_listings WHERE {where_sql} "
        f"ORDER BY {order} LIMIT %s OFFSET %s",
        (*args, limit, offset),
    )
    for r in rows:
        for k in ("description_fetched_at", "first_seen_at", "last_seen_at"):
            if isinstance(r.get(k), datetime):
                r[k] = r[k].isoformat()
    return total, rows
