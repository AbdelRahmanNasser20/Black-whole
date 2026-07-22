"""Supabase-backed read layer for the Auctions tab.

The auction scrape archive now lives in Supabase (`auction_listings`, populated
by `scripts/transfer_listings_to_supabase.py`). This module mirrors
`auction_extractors.top_chairs.get_top_chairs` but sources rows from Postgres
instead of the local SQLite cache, so `/api/auctions` reads one shared DB.

Only the *data loader* changes — ranking, category classification, the
active/expired filter, and the optional condition-scoring LLM are reused
verbatim from the upstream module, so card output is byte-for-byte identical.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Literal

from . import db

# Reuse the pure (non-SQLite) helpers from the upstream package so behavior
# stays in lockstep with the original SQLite path.
from auction_extractors.top_chairs import (  # noqa: E402
    CATEGORIES,
    _SANE_MAX_QUANTITY,
    TRUSTED_QUANTITY_SOURCES,
    _classify,
    _enrich_via_llm,
    _is_active,
    _is_non_chair_lot,
    _price_to_float,
)

Source = Literal["gd", "ps", "bs"]

_SOURCE_FRAG = {"gd": "govdeals.com", "ps": "publicsurplus.com", "bs": "bidspotter.com"}

_SELECT_COLS = (
    "asset_id, link, title, description, quantity, quantity_source, "
    "quantity_confidence, price, location, pickup_zip, contact_email, "
    "contact_phone, end_date, time_left, image_url, last_seen_at"
)


def _load_from_supabase(source: Source, min_quantity: int) -> list[dict]:
    """Mirror of top_chairs._load_from_cache, reading `auction_listings`."""
    frag = _SOURCE_FRAG[source]
    rows = db.fetch_all(
        f"""
        SELECT {_SELECT_COLS}
        FROM auction_listings
        WHERE quantity >= %s AND quantity <= %s
          AND quantity_source = ANY(%s)
          AND link ILIKE %s
        ORDER BY quantity DESC
        """,
        (min_quantity, _SANE_MAX_QUANTITY, list(TRUSTED_QUANTITY_SOURCES), f"%{frag}%"),
    )
    # last_seen_at comes back as a datetime (timestamptz); the upstream
    # _is_active helper expects an ISO string. Normalize so it parses.
    for r in rows:
        ls = r.get("last_seen_at")
        if isinstance(ls, datetime):
            r["last_seen_at"] = ls.isoformat()
    rows.sort(
        key=lambda x: (-int(x.get("quantity") or 0), _price_to_float(x.get("price")))
    )
    return rows


def get_top_chairs(
    source: Source = "gd",
    n: int = 15,
    min_quantity: int = 50,
    include_condition: bool = True,
    active_only: bool = True,
    max_stale_days: int = 2,
    category: str | None = None,
) -> list[dict]:
    """Supabase equivalent of auction_extractors.get_top_chairs.

    Same signature, same return shape (rank, quantity, title, raw_title,
    price, end_date, time_left, link, image_url, location, pickup_zip,
    contact_email, contact_phone, category, category_keyword, condition,
    condition_note).
    """
    if source not in _SOURCE_FRAG:
        raise ValueError(f"source must be one of {sorted(_SOURCE_FRAG)}, got {source!r}")
    if category is not None and category not in CATEGORIES:
        raise ValueError(f"category must be one of {CATEGORIES} or None, got {category!r}")

    items = _load_from_supabase(source, min_quantity=max(1, int(min_quantity)))

    for it in items:
        cat, kw = _classify(it.get("title"), it.get("description"))
        it["category"] = cat
        it["category_keyword"] = kw

    # Read-time relevance filter — the served list must match the scraper's
    # _keep: drop non-chair lots (scales/stools/recliners/…) and gate medical
    # behind INCLUDE_MEDICAL. Without this, junk stored in auction_listings is
    # served even though the scraper's post-store _keep dropped it from alerts.
    include_medical = os.getenv("INCLUDE_MEDICAL") == "1"
    items = [
        it for it in items
        if (include_medical if it["category"] == "medical"
            else not _is_non_chair_lot(it.get("title")))
    ]

    if category is not None:
        items = [it for it in items if it["category"] == category]

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
            print(
                f"[auctions_supabase] condition LLM unavailable ({e!r}); "
                "falling back to raw titles without condition score",
                file=sys.stderr,
            )
            enrich = None
    if enrich is None:
        enrich = [
            {"title": it.get("title") or "", "condition": None, "condition_note": None}
            for it in top
        ]

    out = []
    for i, (it, en) in enumerate(zip(top, enrich), start=1):
        out.append(
            {
                "rank": i,
                "quantity": int(it.get("quantity") or 0),
                "title": en["title"],
                "raw_title": it.get("title") or "",
                "price": it.get("price") or "",
                "end_date": it.get("end_date") or "",
                "time_left": it.get("time_left") or "",
                "link": it.get("link") or "",
                "image_url": it.get("image_url") or "",
                "location": it.get("location") or "",
                "pickup_zip": it.get("pickup_zip") or "",
                "contact_email": it.get("contact_email") or "",
                "contact_phone": it.get("contact_phone") or "",
                "category": it.get("category") or "other",
                "category_keyword": it.get("category_keyword") or "",
                "condition": en["condition"] if include_condition else None,
                "condition_note": en["condition_note"] if include_condition else None,
            }
        )
    return out


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
                 ELSE 'other'
               END AS src,
               count(*) AS n,
               max(last_seen_at) AS newest
        FROM auction_listings GROUP BY 1
        """,
        (f"%{_SOURCE_FRAG['gd']}%", f"%{_SOURCE_FRAG['ps']}%", f"%{_SOURCE_FRAG['bs']}%"),
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
