"""Admin-only reads over the private GovDeals lot archive (recorder/lot_archive.py).

Everything here is served under `/admin/...` or `/api/...`, both behind the
session cookie (auth.PROTECTED_PREFIXES). The archive holds undisguised
GovDeals photos, the seller's description and contact block — **never** route
any of it to a public template, `/deals`, the storefront or a catalog feed.

No GovDeals request is made at view time: the page is rebuilt from the
archive document, its photos (streamed from the store, never linked by a
public R2 URL) and the cached analysis. The only DB read is the optional
`lot_archive` index (migration 015, PENDING) for the list page; without it the
list is built from the `_meta/` sidecars.
"""
from __future__ import annotations

import json
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any

from recorder import lot_archive

_lock = threading.Lock()
_store_holder: dict[str, Any] = {}
_meta_cache: dict[str, dict] = {}
_doc_cache: "OrderedDict[str, dict]" = OrderedDict()
_DOC_CACHE_MAX = 128


class ArchiveUnavailable(RuntimeError):
    """No store configured (R2 unset and no LOT_ARCHIVE_STORE=local)."""


def store():
    with _lock:
        if "s" not in _store_holder:
            _store_holder["s"] = lot_archive.store_from_env()
        s = _store_holder["s"]
    if s is None:
        raise ArchiveUnavailable("lot archive store is not configured (R2_* or LOT_ARCHIVE_STORE=local)")
    return s


def reset() -> None:
    """Test seam."""
    with _lock:
        _store_holder.clear()
        _meta_cache.clear()
        _doc_cache.clear()


SOURCES = lot_archive.SOURCES
SOURCE_NAMES = {"govdeals": "GovDeals", "allsurplus": "AllSurplus"}


def valid_source(source: str) -> bool:
    """Route whitelist — anything else is a 404, never a store read."""
    return source in SOURCES


def lot_key(asset_id: int, account_id: int, auction_id: int) -> str:
    return f"{int(asset_id)}/{int(account_id)}/{int(auction_id)}"


def load_doc(key: str, source: str = "govdeals") -> dict | None:
    ck = f"{source}:{key}"
    with _lock:
        if ck in _doc_cache:
            _doc_cache.move_to_end(ck)
            return _doc_cache[ck]
    doc = lot_archive.load(store(), key, source)
    if doc is not None:
        with _lock:
            _doc_cache[ck] = doc
            while len(_doc_cache) > _DOC_CACHE_MAX:
                _doc_cache.popitem(last=False)
    return doc


def load_analysis(key: str, source: str = "govdeals") -> dict | None:
    from recorder import lot_analysis
    return lot_analysis.load(store(), key, source)


def photo_bytes(key: str, i: int, source: str = "govdeals") -> bytes | None:
    return store().get(lot_archive.photo_key(key, i, source))


# ───────────────────────────── list page ─────────────────────────────

def _metas_from_store() -> list[dict]:
    s = store()
    keys: list[tuple[str, str]] = []
    for src in SOURCES:
        keys += [(k, src) for k in s.list(lot_archive.prefix(src) + "/_meta/") if k.endswith(".json")]
    missing = [(k, src) for k, src in keys if k not in _meta_cache]

    def fetch(item: tuple[str, str]) -> tuple[str, dict | None]:
        k, src = item
        blob = s.get(k)
        m = json.loads(blob) if blob else None
        if m is not None:
            m.setdefault("source", src)   # sidecars written before 2026-10-02 carry no source
        return k, m

    if missing:
        with ThreadPoolExecutor(max_workers=8) as ex:
            for k, m in ex.map(fetch, missing):
                if m:
                    _meta_cache[k] = m
    return [_meta_cache[k] for k, _src in keys if k in _meta_cache]


def _metas_from_index() -> list[dict] | None:
    from automation import db
    try:
        if not (db.fetch_one("SELECT to_regclass('lot_archive') AS reg") or {}).get("reg"):
            return None
        # lot_archive.currency = migration 019 (APPLIED to prod 2026-10-02); without it every
        # row reads as USD-unknown (None) and the list shows no currency.
        has_cur = bool(db.fetch_one(
            "SELECT 1 AS ok FROM information_schema.columns "
            "WHERE table_name = 'lot_archive' AND column_name = 'currency'"))
        rows = db.fetch_all(
            """SELECT source, lot_key, title, canonical_category, category_name, city, state, seller,
                      closed_at, final_price, bid_count, outcome, status_code, photo_count,
                      completeness, archived_at""" + (", currency" if has_cur else "") + """
               FROM lot_archive WHERE source = ANY(%s)""", (list(SOURCES),))
    except Exception:  # noqa: BLE001 - index is optional; fall back to the store
        return None
    out = []
    for r in rows:
        r = dict(r)
        for k in ("closed_at", "archived_at"):
            if isinstance(r.get(k), datetime):
                r[k] = r[k].isoformat()
        if r.get("final_price") is not None:
            r["final_price"] = float(r["final_price"])
        out.append(r)
    return out


def _dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def filter_metas(metas: list[dict], *, q: str | None = None, category: str | None = None,
                 outcome: str | None = None, min_price: float | None = None,
                 max_price: float | None = None, since: str | None = None,
                 until: str | None = None, source: str | None = None) -> list[dict]:
    """Pure. Newest close first."""
    ql = (q or "").strip().lower()
    lo_dt, hi_dt = _dt(since), _dt(until)
    out = []
    for m in metas:
        if ql and ql not in (m.get("title") or "").lower() and ql not in (m.get("lot_key") or "") \
                and ql not in (m.get("seller") or "").lower():
            continue
        if source and (m.get("source") or "govdeals") != source:
            continue
        if category and (m.get("canonical_category") or "") != category:
            continue
        if outcome and (m.get("outcome") or "unknown") != outcome:
            continue
        price = m.get("final_price")
        if min_price is not None and (price is None or price < min_price):
            continue
        if max_price is not None and (price is None or price > max_price):
            continue
        closed = _dt(m.get("closed_at"))
        if lo_dt and (closed is None or closed < lo_dt):
            continue
        if hi_dt and (closed is None or closed > hi_dt):
            continue
        out.append(m)
    out.sort(key=lambda m: m.get("closed_at") or "", reverse=True)
    return out


def list_lots(*, q=None, category=None, outcome=None, min_price=None, max_price=None,
              since=None, until=None, page: int = 1, per_page: int = 50, source=None) -> dict:
    metas = _metas_from_index()
    index = "table" if metas is not None else "store"
    if metas is None:
        metas = _metas_from_store()
    facets_cat: dict[str, int] = {}
    facets_out: dict[str, int] = {}
    facets_src: dict[str, int] = {}
    for m in metas:
        facets_src[m.get("source") or "govdeals"] = facets_src.get(m.get("source") or "govdeals", 0) + 1
        facets_cat[m.get("canonical_category") or "other"] = facets_cat.get(m.get("canonical_category") or "other", 0) + 1
        facets_out[m.get("outcome") or "unknown"] = facets_out.get(m.get("outcome") or "unknown", 0) + 1
    hits = filter_metas(metas, q=q, category=category, outcome=outcome, min_price=min_price,
                        max_price=max_price, since=since, until=until, source=source)
    per_page = max(1, min(int(per_page or 50), 200))
    page = max(1, int(page or 1))
    start = (page - 1) * per_page
    return {"items": hits[start:start + per_page], "total": len(hits), "archived": len(metas),
            "page": page, "per_page": per_page, "pages": max(1, -(-len(hits) // per_page)),
            "index": index, "store": getattr(store(), "kind", None),
            "facets": {"category": dict(sorted(facets_cat.items(), key=lambda kv: -kv[1])),
                       "outcome": dict(sorted(facets_out.items(), key=lambda kv: -kv[1])),
                       "source": dict(sorted(facets_src.items(), key=lambda kv: -kv[1]))}}


# ───────────────────────────── rebuilt page ─────────────────────────────

def price_series(doc: dict) -> list[dict]:
    """(t, price, bids) points from our own timeline, plus the bidbox final.
    Pure; drops repeats so the chart shows movement only."""
    pts = []
    for p in doc.get("timeline") or []:
        if p.get("current_bid") is None or not p.get("t"):
            continue
        pts.append({"t": str(p["t"]), "price": float(p["current_bid"]),
                    "bids": p.get("bid_count"), "source": p.get("source")})
    s = doc.get("summary") or {}
    if s.get("final_price") is not None and s.get("closed_at"):
        pts.append({"t": s["closed_at"], "price": float(s["final_price"]),
                    "bids": s.get("bid_count"), "source": "final"})
    pts.sort(key=lambda p: _dt(p["t"]) or datetime.min.replace(tzinfo=timezone.utc))
    out = []
    for p in pts:
        if out and out[-1]["price"] == p["price"] and out[-1]["bids"] == p["bids"] and p["source"] != "final":
            continue
        out.append(p)
    return out


def chart_svg(points: list[dict], width: int = 640, height: int = 200, pad: int = 28) -> dict | None:
    """Pure. A step line over time → coordinates the template draws as SVG.
    None when there are fewer than two points (nothing to draw)."""
    if len(points) < 2:
        return None
    ts = [(_dt(p["t"]) or datetime.now(timezone.utc)).timestamp() for p in points]
    ys = [p["price"] for p in points]
    t0, t1 = min(ts), max(ts)
    y1 = max(ys) or 1.0
    span = (t1 - t0) or 1.0
    w, h = width - 2 * pad, height - 2 * pad

    def x(t): return round(pad + (t - t0) / span * w, 1)
    def y(v): return round(pad + h - (v / y1) * h, 1)

    path = []
    for i, (t, v) in enumerate(zip(ts, ys)):
        if i == 0:
            path.append(f"M{x(t)},{y(v)}")
        else:
            path.append(f"H{x(t)}V{y(v)}")
    dots = [{"x": x(t), "y": y(v), "price": v, "bids": p.get("bids"), "t": p["t"],
             "final": p.get("source") == "final"} for t, v, p in zip(ts, ys, points)]
    return {"width": width, "height": height, "path": " ".join(path), "dots": dots,
            "y_max": y1, "t_start": points[0]["t"], "t_end": points[-1]["t"],
            "baseline": pad + h, "pad": pad}


def currency_prefix(currency: str | None) -> str:
    """"$" for USD, else the ISO code ("EUR ") — never a $ on a euro price."""
    cur = (currency or "USD").upper()
    return "$" if cur == "USD" else f"{cur} "


def page_context(key: str, source: str = "govdeals") -> dict | None:
    doc = load_doc(key, source)
    if doc is None:
        return None
    analysis = load_analysis(key, source)
    series = price_series(doc)
    a, b, c = key.split("/")
    s = doc.get("summary") or {}
    return {"doc": doc, "s": s, "analysis": analysis, "series": series,
            "chart": chart_svg(series), "key": key, "source": source,
            "source_name": SOURCE_NAMES.get(source, source),
            "cur": currency_prefix(s.get("currency")),
            "photo_base": f"/api/archive/{source}/{a}/{b}/{c}/photo",
            "json_url": f"/api/archive/{source}/{a}/{b}/{c}"}


def lot_json(key: str, source: str = "govdeals") -> dict | None:
    doc = load_doc(key, source)
    if doc is None:
        return None
    return {"lot_key": key, "source": source, "summary": doc.get("summary"),
            "photos": len(doc.get("photos") or []),
            "archived_at": doc.get("archived_at"), "completeness": doc.get("completeness"),
            "series": price_series(doc), "analysis": load_analysis(key, source)}


def rerun_analysis(key: str, source: str = "govdeals") -> dict:
    from recorder import lot_analysis
    return lot_analysis.analyze_and_store(store(), key, source)
