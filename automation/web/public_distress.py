"""Read model for the /distress page (public) and /api/distress/cases (operator).

Same shape as `public_deals.py`: every public read of `distress_cases` passes
through here, the allow-list is SQL, and the whole-table facets are memoised.

Rules:
1. **Contact details are operator-only.** `PUBLIC_COLS` never selects
   `trustee`, `attorneys` or `parties`; `ADMIN_COLS` does. A public caller gets
   the case, the court, the docket, the industry and the filing — enough to
   know a lead exists, not enough to work it.
2. **Near = ZIP radius on stored coordinates only.** `near` resolves through
   the committed 3-digit ZIP centroid table (no network); rows without
   `lat`/`lng` (most CourtListener dockets — petitions are scans) are dropped
   from a near-filtered page and counted in `unlocated`, never silently shown
   as "nearby".
3. Sort columns come only from `SORTS`; every value is bound.
"""
from __future__ import annotations

import json
import math
import time
from typing import Any

from .. import db

PUBLIC_COLS = ("id, source, docket_id, case_name, court_id, docket_number, date_filed, chapter, "
               "debtor_type, naics, industry_tag, city, state, lat, lng, petition_url, "
               "sale_noticed_at, sale_url, employees_affected, effective_date, first_seen_at")
ADMIN_COLS = PUBLIC_COLS + ", trustee, attorneys, parties, zip_code, last_seen_at"

SORTS = {"filed": "date_filed", "sale": "sale_noticed_at", "newest": "first_seen_at", "name": "case_name"}
TABS = ("leads", "sales")
CHAPTERS = ("7", "11")
SOURCES = ("courtlistener", "warn")
PER_PAGE_CHOICES = (25, 50, 100)
MAX_PAGE = 200
CACHE_TTL = 300
EARTH_MI = 3958.8

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


# ── near / radius ────────────────────────────────────────────────────────────

def resolve_near(zip_code: str | None) -> dict | None:
    """None when nothing typed; `resolved: False` when the ZIP has no centroid
    (so the UI can say so rather than show an empty, wrong page)."""
    if not zip_code or not str(zip_code).strip():
        return None
    from deals.distress import zip_latlng
    z = "".join(ch for ch in str(zip_code) if ch.isdigit())[:5]
    lat, lng = zip_latlng(z)
    if lat is None:
        return {"zip": z, "resolved": False, "lat": None, "lng": None}
    return {"zip": z, "resolved": True, "lat": lat, "lng": lng}


def _bbox(lat: float, lng: float, radius_mi: float) -> tuple[float, float, float, float]:
    dlat = radius_mi / 69.0
    dlng = radius_mi / max(1e-6, 69.0 * math.cos(math.radians(lat)))
    return lat - dlat, lng - dlng, lat + dlat, lng + dlng


def haversine_mi(lat1, lng1, lat2, lng2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return EARTH_MI * 2 * math.asin(math.sqrt(a))


# ── query building ───────────────────────────────────────────────────────────

def build_where(*, q=None, industry=None, state=None, chapter=None, source=None,
                tab="leads", origin: dict | None = None, radius_mi: float | None = None,
                since=None) -> tuple[str, list]:
    where: list[str] = ["TRUE"]
    args: list = []
    if tab == "sales":
        where.append("sale_noticed_at IS NOT NULL")
    if q:
        where.append("case_name ILIKE %s"); args.append(f"%{q}%")
    if industry:
        where.append("industry_tag = %s"); args.append(industry)
    if state:
        where.append("state = %s"); args.append(str(state).upper())
    if chapter:
        where.append("chapter = %s"); args.append(str(chapter))
    if source:
        where.append("source = %s"); args.append(source)
    if since:
        where.append("date_filed >= %s"); args.append(since)
    if origin and origin.get("resolved") and radius_mi:
        s, w, n, e = _bbox(origin["lat"], origin["lng"], float(radius_mi))
        where.append("lat BETWEEN %s AND %s AND lng BETWEEN %s AND %s")
        args += [s, n, w, e]
    return " AND ".join(where), args


def order_clause(sort: str, direction: str | None, tab: str = "leads") -> str:
    col = SORTS.get(sort) or (SORTS["sale"] if tab == "sales" else SORTS["filed"])
    if direction not in ("asc", "desc"):
        direction = "asc" if col == "case_name" else "desc"
    return f"ORDER BY {col} {direction.upper()} NULLS LAST, id DESC"


def clamp_page(page: int, per_page: int) -> tuple[int, int]:
    per_page = per_page if per_page in PER_PAGE_CHOICES else PER_PAGE_CHOICES[0]
    return max(1, min(int(page or 1), MAX_PAGE)), per_page


def enrich(row: dict, origin: dict | None = None) -> dict:
    row = dict(row)
    if row.get("docket_id"):
        row["docket_url"] = f"https://www.courtlistener.com/docket/{row['docket_id']}/"
    if origin and origin.get("resolved") and row.get("lat") is not None and row.get("lng") is not None:
        row["distance_mi"] = round(haversine_mi(origin["lat"], origin["lng"], row["lat"], row["lng"]), 1)
    return row


# ── reads ────────────────────────────────────────────────────────────────────

def fetch_page(*, q=None, industry=None, state=None, chapter=None, source=None, tab="leads",
               near=None, radius_mi=None, since=None, sort="filed", dir=None,
               page=1, per_page=25, admin: bool = False) -> dict:
    page, per_page = clamp_page(page, per_page)
    origin = resolve_near(near)
    where, args = build_where(q=q, industry=industry, state=state, chapter=chapter, source=source,
                              tab=tab, origin=origin, radius_mi=radius_mi, since=since)
    order = order_clause(sort, dir, tab)
    cols = ADMIN_COLS if admin else PUBLIC_COLS
    unlocated = 0
    with db.connect() as conn:
        rows = conn.execute(
            f"SELECT {cols} FROM distress_cases WHERE {where} {order} LIMIT %s OFFSET %s",
            (*args, per_page, (page - 1) * per_page)).fetchall()
        total = conn.execute(f"SELECT count(*) AS c FROM distress_cases WHERE {where}",
                             tuple(args)).fetchone()["c"]
        if origin and origin.get("resolved") and radius_mi:
            # how many matching rows have no coordinates (dropped by the radius box)
            base_where, base_args = build_where(q=q, industry=industry, state=state, chapter=chapter,
                                                source=source, tab=tab, since=since)
            unlocated = conn.execute(
                f"SELECT count(*) AS c FROM distress_cases WHERE {base_where} AND lat IS NULL",
                tuple(base_args)).fetchone()["c"]
    out_rows = [enrich(dict(r), origin) for r in rows]
    if origin and origin.get("resolved") and radius_mi:
        out_rows = [r for r in out_rows if r.get("distance_mi") is None or r["distance_mi"] <= float(radius_mi)]
    return {"rows": out_rows, "total": total, "page": page, "per_page": per_page,
            "pages": max(1, -(-total // per_page)), "near": origin, "unlocated": unlocated}


def fetch_facets() -> dict:
    def load():
        with db.connect() as conn:
            industries = conn.execute(
                "SELECT industry_tag AS value, count(*) AS count FROM distress_cases "
                "WHERE industry_tag IS NOT NULL GROUP BY 1 ORDER BY 2 DESC").fetchall()
            states = conn.execute(
                "SELECT state AS value, count(*) AS count FROM distress_cases "
                "WHERE state IS NOT NULL GROUP BY 1 ORDER BY 2 DESC").fetchall()
            chapters = conn.execute(
                "SELECT chapter AS value, count(*) AS count FROM distress_cases "
                "WHERE chapter IS NOT NULL GROUP BY 1 ORDER BY 1").fetchall()
            stats = conn.execute(
                "SELECT count(*) AS cases, "
                "count(*) FILTER (WHERE source = 'courtlistener') AS bankruptcies, "
                "count(*) FILTER (WHERE source = 'warn') AS closures, "
                "count(*) FILTER (WHERE chapter = '7') AS chapter_7, "
                "count(*) FILTER (WHERE sale_noticed_at IS NOT NULL) AS sales, "
                "count(DISTINCT state) AS states, "
                "max(date_filed) AS latest, min(first_seen_at) AS since FROM distress_cases").fetchone()
            try:
                runs = conn.execute("SELECT source, last_run_at FROM distress_sync_state").fetchall()
            except Exception:  # noqa: BLE001 — table from the same PENDING migration
                runs = []
        return {"industries": industries, "states": states, "chapters": chapters, "stats": stats,
                "last_run": {r["source"]: r["last_run_at"] for r in runs}, "cached_at": time.time()}
    return _cached("facets", load)


def params_key(**kw) -> str:
    return json.dumps(kw, sort_keys=True, default=str)
