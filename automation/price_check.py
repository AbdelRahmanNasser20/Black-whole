"""Price-check strip on the public lot page: what a comparable chair costs NEW.

"$25 a chair" means nothing to a buyer without a reference point. The strip
puts one or two verified NEW retail prices next to our used price, each
linked to its source — "same chair new: ~$169 · cheap new import: ~$47 —
ours: $25, already on the floor".

Rules (workspace + repo hard rules apply):
- **Never invent a number.** Live rows come from ``public.price_anchors``
  (migration 026), where a row is stored only when its URL and price appeared
  in the search tool's own results. The fallback is ``STATIC_ANCHORS`` — two
  prices verified by hand in the 2026-10-10 market study and already quoted
  in outreach emails.
- **Text + outbound link only.** Another seller's photo never renders here
  (their copies live in the PRIVATE bucket). Links carry
  ``rel="nofollow noopener"`` in the template.
- **Factual, never disparaging.** Label, price, source domain. Nothing else.
- Only anchors priced ABOVE our per-chair price render — the strip shows
  value; a stale anchor at or below our price would read as anti-selling.
- Degrades like ``attribution.columns_ready``: the table probe caches only a
  positive answer, and any DB failure falls back to the static anchors, so
  the lot page renders identically with no database at all.
"""
from __future__ import annotations

import logging
import time
from typing import Any
from urllib.parse import urlparse

from . import db

log = logging.getLogger(__name__)

# One DB read per TTL, not per page view. The anchors change once a day (the
# Render cron), so staleness here is invisible.
TTL_SECONDS = 600
LABEL_MAX = 70
MAX_ANCHORS = 2

# Hand-verified new-price references (direction/tasks/2026-10-10/price-anchors,
# re-checked in the us-chair-market study). Same shape as a live pick.
STATIC_ANCHORS: tuple[dict, ...] = (
    {
        "label": "MTS Seating 578 Omega II banquet chair — new",
        "brand": "MTS",
        "price": 169.0,
        "url": "https://www.todaysclassroom.com/mts-seating-578-omega-ii-banquet-stacking-chair-square-back-18-inch-seat-height/",
        "source": "todaysclassroom.com",
    },
    {
        "label": "Flash Furniture Hercules banquet chair — new import",
        "brand": "Flash Furniture",
        "price": 47.0,
        "url": "https://www.webstaurantstore.com/flash-furniture-ng-c01-hts-2201-sv-gg-hercules-burgundy-pattern-fabric-crown-back-stackable-banquet-chair-with-silver-vein-frame/354CRN2BPSLV.html",
        "source": "webstaurantstore.com",
    },
)


# ───────────────────────────── live rows (DB) ────────────────────────────────

_table_ready_flag = False
_memo: tuple[float, list[dict]] | None = None


def _table_ready() -> bool:
    """True once migration 026 created ``price_anchors``. Positive-only cache
    (same reasoning as ``attribution.columns_ready``): a DB blip must not pin
    the process to the static anchors forever."""
    global _table_ready_flag
    if _table_ready_flag:
        return True
    row = db.fetch_one(
        "SELECT 1 FROM information_schema.tables WHERE table_schema = 'public' "
        "AND table_name = 'price_anchors' LIMIT 1")
    if row is None:
        return False
    _table_ready_flag = True
    return True


def live_anchors(now=time.monotonic) -> list[dict]:
    """Active NEW-price rows from ``price_anchors``, memoised for TTL_SECONDS.

    `[]` on a missing table or any DB failure — the caller falls back to the
    static anchors either way. A failure is not memoised, so one blip never
    hides a working table for the whole TTL.
    """
    global _memo
    t = now()
    if _memo is not None and t - _memo[0] < TTL_SECONDS:
        return _memo[1]
    try:
        rows: list[dict] = []
        if _table_ready():
            rows = db.fetch_all(
                "SELECT title, brand, price_per_chair, listing_url "
                "FROM public.price_anchors "
                "WHERE status = 'active' AND tier = 'new' AND condition = 'new' "
                "AND currency = 'USD' "
                "ORDER BY price_per_chair DESC LIMIT 20")
    except Exception:  # noqa: BLE001 — the page renders on static anchors
        log.warning("price_check: live anchors unavailable, using static", exc_info=True)
        return []
    _memo = (t, rows)
    return rows


def reset_cache() -> None:
    """Tests only."""
    global _table_ready_flag, _memo
    _table_ready_flag = False
    _memo = None


# ───────────────────────────── selection (pure) ──────────────────────────────

def _clip_label(text: str) -> str:
    s = " ".join(str(text or "").split())
    if len(s) <= LABEL_MAX:
        return s
    return s[:LABEL_MAX].rsplit(" ", 1)[0].rstrip(" ,;:-") + "…"


def _as_anchor(row: dict) -> dict | None:
    """A display anchor from a ``price_anchors`` row, or None when unusable."""
    try:
        price = round(float(row["price_per_chair"]), 2)
    except (KeyError, TypeError, ValueError):
        return None
    url = str(row.get("listing_url") or "").strip()
    host = urlparse(url).netloc.lower().removeprefix("www.")
    if not host or price <= 0:
        return None
    label = _clip_label(row.get("title") or row.get("brand") or "")
    if not label:
        return None
    return {"label": f"{label} — new", "brand": row.get("brand") or "",
            "price": price, "url": url, "source": host}


def pick_anchors(live_rows: list[dict], *, ours: float, lot_text: str,
                 limit: int = MAX_ANCHORS) -> list[dict]:
    """Up to ``limit`` anchors worth showing next to a lot priced at ``ours``.

    Live rows first: the brand-matched one (the lot's own brand, new) plus the
    cheapest one (the budget import), then static anchors fill what's left.
    Everything at or below our price is dropped, so an empty list means
    "render no strip", never "render a bad comparison".
    """
    text = (lot_text or "").lower()
    pool = [a for a in (_as_anchor(r) for r in live_rows) if a and a["price"] > ours]
    picks: list[dict] = []

    def _take(anchor: dict | None) -> None:
        if anchor and anchor["url"] not in {p["url"] for p in picks} and len(picks) < limit:
            picks.append(anchor)

    matched = [a for a in pool
               if a["brand"] and a["brand"].split()[0].lower() in text]
    _take(max(matched, key=lambda a: a["price"], default=None)
          or max(pool, key=lambda a: a["price"], default=None))
    _take(min(pool, key=lambda a: a["price"], default=None))
    for a in STATIC_ANCHORS:
        if a["price"] > ours:
            _take(dict(a))
    picks.sort(key=lambda a: a["price"], reverse=True)
    return picks


# ───────────────────────────── page context ──────────────────────────────────

def strip_for(row: dict) -> dict[str, Any] | None:
    """Template context for the strip on one decorated lot row, or None.

    None whenever the comparison would be weak or wrong: a sold lot, a lot
    with no per-chair price, or no anchor priced above ours.
    """
    try:
        ours = float(row.get("price_per_chair") or 0)
    except (TypeError, ValueError):
        return None
    if ours <= 0 or row.get("is_sold"):
        return None
    lot_text = " ".join(str(row.get(k) or "") for k in ("title", "subtitle", "chair_type"))
    anchors = pick_anchors(live_anchors(), ours=ours, lot_text=lot_text)
    if not anchors:
        return None
    labels = row.get("location_labels") or []
    return {"ours": ours, "where": labels[0] if labels else None, "anchors": anchors}
