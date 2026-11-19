"""Top deals for the /platform welcome page: the live lots whose bid is lowest
against what similar lots closed for (`deals/auction_comps.py`, no LLM).

Rules, in one place:

* **The request path never reads the DB.** `top()` answers from a memo and
  refreshes it in a daemon thread (same stance as `platform_api.stats()`).
  Nothing read yet, a failed read, or `FLIP_TOP_ENABLED=0` -> `None`, and the
  page renders without the section.
* **Public policy first.** Live lots are read through
  `public_deals.exclusion_where()`, so an operator pick never shows here.
* **Allow-list out.** Only the keys `_card` builds leave the server. The score
  rests on public closing prices; it is not the private verdict
  (`deal_verdicts`), and no verdict field is read here.
* **Light on memory.** Closed sales are streamed row by row into the matcher,
  which keeps a bounded heap per live lot. The read holds ONE pooled
  connection for tens of seconds: the session pooler caps clients at 15, so a
  private connection here could push a cron over the cap.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any

from deals import auction_comps
from deals.fees import fee_model_for_site

from .. import db
from . import deal_lot_urls, deals_sources, lot_pages, public_deals

log = logging.getLogger(__name__)

TTL = int(os.getenv("FLIP_TOP_INTERVAL_SEC", "3600"))
FAIL_BACKOFF = 300
MAX_AGE = 3 * TTL                 # reads keep failing: drop the list, never show old bids
READ_TIMEOUT = "120s"
LIMIT = 10
MIN_HOURS, MAX_HOURS = 1, 48      # closes soon, but not too late to act
MIN_BIDS = 2                      # an untouched opening bid is not a deal yet
MIN_VALUE = 150.0                 # a $20 lot is not worth a visitor's time
MIN_SCORE = 65
MAX_SPREAD = 4.0                  # p75 / p25: the comps must agree on a price
MIN_WORDS = 3                     # "Dell Laptops" names no item (a model year counts)
PER_CATEGORY = 3

_LIVE_SQL = (
    "SELECT site, asset_id, account_id, auction_id, native_id, title, "
    "canonical_category, city, state, bid_count, current_bid, end_utc, hero_image_url "
    "FROM deal_lots WHERE outcome_complete IS NOT TRUE "
    "AND end_utc > now() + make_interval(hours => %s) "
    "AND end_utc < now() + make_interval(hours => %s) "
    "AND bid_count >= %s AND current_bid > 0 AND currency_code = 'USD' AND {policy}"
)
_SOLD_SQL = (
    "SELECT title, canonical_category, final_bid, "
    "extract(epoch FROM now() - closed_at) / 86400.0 AS age_days "
    "FROM deal_lots WHERE final_bid > 0 AND outcome IN ('sold', 'low_bid') "
    "AND currency_code = 'USD' AND closed_at > now() - make_interval(days => %s) "
    "AND title IS NOT NULL AND canonical_category = ANY(%s)"
)


def enabled() -> bool:
    return os.getenv("FLIP_TOP_ENABLED", "1").strip() != "0"


def score_live(conn, live: list[dict]) -> dict:
    """{row index: CompResult} for live `deal_lots` rows, from one streamed
    pass over the closed sales in the same categories."""
    matcher = auction_comps.Matcher()
    for i, row in enumerate(live):
        matcher.add_lot(
            i, title=row["title"], category=row["canonical_category"],
            bid=float(row["current_bid"]),
            premium_pct=fee_model_for_site(row.get("site") or "govdeals").buyer_premium_pct)
    cats = sorted({r["canonical_category"] for r in live if r["canonical_category"]})
    if not cats:
        return {}
    with conn.cursor() as cur:
        for sale in cur.stream(_SOLD_SQL, (auction_comps.WINDOW_DAYS, cats)):
            matcher.add_sale(title=sale["title"], category=sale["canonical_category"],
                             price=float(sale["final_bid"]), age_days=float(sale["age_days"]))
    return matcher.results()


def pick(live: list[dict], results: dict, *, now: datetime, limit: int = LIMIT) -> list[tuple[dict, Any]]:
    """The best `limit` (row, CompResult) pairs: strong comps only, no damaged
    lot, one card per near-identical title, at most PER_CATEGORY per category."""
    ranked = []
    for i, res in results.items():
        row = live[i]
        if (res.comp_count < auction_comps.STRONG_COMPS or res.damaged
                or res.comp_value < MIN_VALUE or res.score < MIN_SCORE
                or res.p25 <= 0 or res.p75 / res.p25 > MAX_SPREAD):
            continue
        words, year = auction_comps.tokens(row["title"])
        if len(words) + (1 if year else 0) < MIN_WORDS:
            continue
        hours = (row["end_utc"] - now).total_seconds() / 3600.0
        rank = auction_comps.deal_rank(res.cost, res.comp_value, hours, int(row["bid_count"] or 0))
        if rank > 0 and deal_lot_urls.for_row(row):   # no public page, no card
            ranked.append((rank, i, row, res))
    ranked.sort(key=lambda x: (-x[0], x[1]))
    out, seen, per_cat = [], set(), {}
    for _rank, _i, row, res in ranked:
        sig = auction_comps.tokens(row["title"])[0]
        cat = row["canonical_category"] or ""
        if sig in seen or per_cat.get(cat, 0) >= PER_CATEGORY:
            continue
        seen.add(sig)
        per_cat[cat] = per_cat.get(cat, 0) + 1
        out.append((row, res))
        if len(out) >= limit:
            break
    return out


def _card(row: dict, res, photos: bool) -> dict | None:
    url = deal_lot_urls.for_row(row)
    if not url:
        return None
    return {
        "title": str(row["title"] or "")[:120],
        "url": url,
        "place": ", ".join(p for p in (row.get("city"), row.get("state")) if p),
        "bid": float(row["current_bid"]),
        "bid_count": int(row["bid_count"] or 0),
        "ends": row["end_utc"].astimezone(timezone.utc).isoformat(),
        "score": res.score,
        "comp_value": res.comp_value,
        "comp_count": res.comp_count,
        "pct_of_comp": round(100.0 * res.cost / res.comp_value),
        "photo_url": deals_sources.safe_photo_url(row.get("hero_image_url")) if photos else None,
    }


def compute(limit: int = LIMIT) -> dict:
    """One full read. Slow (streams every closed sale in the window): call it
    from a background thread or the CLI, never from a request."""
    policy, args = public_deals.exclusion_where()
    with db.connect() as conn:
        conn.execute(f"SET LOCAL statement_timeout = '{READ_TIMEOUT}'")
        live = conn.execute(_LIVE_SQL.format(policy=policy),
                            [MIN_HOURS, MAX_HOURS, MIN_BIDS, *args]).fetchall()
        results = score_live(conn, live) if live else {}
    photos = lot_pages.show_photos(False)
    now = datetime.now(timezone.utc)
    cards = [c for c in (_card(row, res, photos) for row, res in pick(live, results, now=now, limit=limit)) if c]
    return {"lots": cards, "as_of": now.isoformat(), "scanned": len(live), "scored": len(results)}


_lock = threading.Lock()
_memo: dict[str, Any] = {"at": 0.0, "data": None, "refreshing": False, "fail_until": 0.0}


def clear_cache() -> None:
    with _lock:
        _memo.update(at=0.0, data=None, refreshing=False, fail_until=0.0)


def _refresh() -> None:
    try:
        data = compute()
    except Exception:
        log.warning("flip top read failed", exc_info=True)
        data = None
    with _lock:
        _memo["refreshing"] = False
        if data is not None:
            _memo["data"], _memo["at"] = data, time.monotonic()
        else:
            _memo["fail_until"] = time.monotonic() + FAIL_BACKOFF


def _live_only(data: dict | None) -> dict | None:
    """Drop cards whose auction has ended since the read; None when none are left."""
    if not data:
        return None
    now = datetime.now(timezone.utc)
    lots = []
    for card in data["lots"]:
        left = (datetime.fromisoformat(card["ends"]) - now).total_seconds()
        if left > 0:
            lots.append({**card, "closes_in": _closes_in(left)})
    return {**data, "lots": lots} if lots else None


def _closes_in(seconds: float) -> str:
    if seconds < 3600:
        return f"{max(1, int(seconds // 60))} min"
    hours = int(seconds // 3600)
    return f"{hours} h" if hours < 48 else f"{hours // 24} d"


def top() -> dict | None:
    """The welcome page's top deals, or None. Never blocks and never raises."""
    if not enabled():
        return None
    with _lock:
        now = time.monotonic()
        if _memo["data"] is not None and now - _memo["at"] >= MAX_AGE:
            _memo["data"] = None
        stale = _memo["data"] is None or now - _memo["at"] >= TTL
        if not stale or _memo["refreshing"] or now < _memo["fail_until"]:
            return _live_only(_memo["data"])
        _memo["refreshing"] = True
        data = _memo["data"]
    try:
        threading.Thread(target=_refresh, name="flip-top", daemon=True).start()
    except Exception:
        log.warning("flip top thread did not start", exc_info=True)
        with _lock:
            _memo["refreshing"] = False
            _memo["fail_until"] = time.monotonic() + FAIL_BACKOFF
    return _live_only(data)
