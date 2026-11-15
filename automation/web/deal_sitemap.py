"""Sitemap for the public auction feed: `/deals`, `/deals/sold` and every lot page.

    /sitemap-index.xml          the storefront's /sitemap.xml + the children below
    /sitemaps/deals-{n}.xml     CHUNK lot URLs each

**A request never reads the database.** The URL list is a memo, rebuilt in a
daemon thread at most every `TTL` (the `public_sold.headline` pattern): a
stale memo starts one rebuild and the request answers with what it has. The
database is small ("Micro") and a crawler fetches these files often.

What goes in (all through the public policy, so an operator pick never does):

* live `deal_lots` rows (`public_deals.exclusion_where`);
* live rows of every other source (the `deals_sources` memo, already filtered);
* confirmed sales of the last `SOLD_DAYS` days from `lot_archive`
  (`public_sold.base_where` + `public_sold.SOLD`). An ended lot with no
  confirmed price is `noindex`, so it is not listed.
"""
from __future__ import annotations

import os
import threading
import time
from datetime import date, datetime
from typing import Any
from xml.sax.saxutils import escape

from .. import db
from . import deal_lot_urls, deals_sources, public_deals, public_sold

CHUNK = 10_000
TTL = 6 * 3600
RETRY = 300
SOLD_DAYS = 90
STATIC_PATHS = ("/deals", "/deals/sold")


def _cap() -> int:
    try:
        return max(1, int(os.getenv("DEAL_SITEMAP_MAX_URLS", "") or 50_000))
    except ValueError:
        return 50_000


_state: dict[str, Any] = {"at": 0.0, "urls": [], "busy": False, "built": False}
_lock = threading.Lock()


def clear_cache() -> None:
    with _lock:
        _state.update(at=0.0, urls=[], busy=False, built=False)


def _day(v: Any) -> str | None:
    if isinstance(v, datetime):
        return v.date().isoformat()
    return v.isoformat() if isinstance(v, date) else None


def collect() -> list[tuple[str, str | None]]:
    """(path, lastmod) for every public lot page. Reads the DB; runs off the
    request path only. Each source of URLs fails alone; all three failing raises."""
    cap = _cap()
    seen: dict[str, str | None] = {}
    failures = 0
    try:
        ex_where, ex_args = public_deals.exclusion_where()
        for r in db.fetch_all(
                "SELECT site, native_id, title, state, updated_at FROM deal_lots "
                f"WHERE outcome_complete IS NOT TRUE AND end_utc > now() AND {ex_where} "
                "ORDER BY end_utc ASC LIMIT %s", (*ex_args, cap)):
            path = deal_lot_urls.build(r.get("site"), r.get("native_id"), title=r.get("title"), state=r.get("state"))
            if path:
                seen.setdefault(path, _day(r.get("updated_at")))
    except Exception:  # noqa: BLE001
        failures += 1
    try:
        for r in deals_sources.lots("active"):
            if r.get("viewer_url"):
                seen.setdefault(r["viewer_url"], _day(r.get("first_seen_at")))
    except Exception:  # noqa: BLE001
        failures += 1
    try:
        where, args = public_sold.base_where()
        for r in db.fetch_all(
                "SELECT source, lot_key, title, state, closed_at FROM lot_archive "
                f"WHERE {where} AND {public_sold.SOLD} "
                "AND closed_at >= now() - make_interval(days => %s) "
                "ORDER BY closed_at DESC LIMIT %s", (*args, SOLD_DAYS, cap)):
            path = deal_lot_urls.build(r.get("source"), r.get("lot_key"), title=r.get("title"), state=r.get("state"))
            if path:
                seen.setdefault(path, _day(r.get("closed_at")))
    except Exception:  # noqa: BLE001
        failures += 1
    if failures == 3:
        raise RuntimeError("deal sitemap: every read failed")
    return list(seen.items())[:cap]


def _refresh() -> None:
    try:
        urls = collect()
    except Exception:  # noqa: BLE001 - keep the last good list
        urls = None
    with _lock:
        _state["busy"] = False
        if urls is not None:
            _state["urls"], _state["at"], _state["built"] = urls, time.monotonic(), True
        else:
            _state["at"] = time.monotonic() - TTL + RETRY


def _spawn() -> None:
    threading.Thread(target=_refresh, name="deal-sitemap", daemon=True).start()


def ready() -> bool:
    """Has the first build landed? Until it has, the files are valid but hold
    only the two list pages, and the routes answer `no-cache`."""
    return bool(_state["built"])


def warm() -> None:
    """Start the first build now (called once at web startup). Returns at once:
    the build runs in a daemon thread and never blocks startup."""
    urls()


def urls() -> list[tuple[str, str | None]]:
    """The memo. Never blocks, never reads the DB; a stale memo starts one rebuild."""
    with _lock:
        fresh = _state["at"] and time.monotonic() - _state["at"] < TTL
        if fresh or _state["busy"]:
            return _state["urls"]
        _state["busy"] = True
    _spawn()
    return _state["urls"]


def children() -> int:
    """How many /sitemaps/deals-{n}.xml files exist right now (always at least 1)."""
    return max(1, -(-len(urls()) // CHUNK))


def index_xml(base_url: str) -> str:
    locs = [f"{base_url}/sitemap.xml"] + [f"{base_url}/sitemaps/deals-{n}.xml" for n in range(1, children() + 1)]
    body = "".join(f"<sitemap><loc>{escape(u)}</loc></sitemap>" for u in locs)
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            f'<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{body}</sitemapindex>')


def child_xml(base_url: str, n: int) -> str | None:
    """Child `n` (1-based), or None when it does not exist. The first child also lists the two list pages."""
    if n < 1 or n > children():
        return None
    part = urls()[(n - 1) * CHUNK:n * CHUNK]
    rows = [f"<url><loc>{escape(base_url + p)}</loc></url>" for p in (STATIC_PATHS if n == 1 else ())]
    for path, lastmod in part:
        mod = f"<lastmod>{lastmod}</lastmod>" if lastmod else ""
        rows.append(f"<url><loc>{escape(base_url + path)}</loc>{mod}</url>")
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{"".join(rows)}</urlset>')
