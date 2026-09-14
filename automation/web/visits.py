"""First-party page-view log for the public storefront.

Why: Apollo's website pixel only identifies *companies* by IP and the
sequences don't click-track, so "did the church emails bring anyone to the
site?" had no answer. Every public page view is appended to `site_visits`
with the UTM tags the Apollo sequences carry
(`?utm_source=apollo&utm_medium=email&utm_campaign=atl-churches`).

Rules (keep them):
- Never blocks or breaks a page. The insert runs in a worker thread, every
  error is swallowed after one log line, and a failing DB backs off for
  BACKOFF_SECONDS before the next attempt.
- No PII. No raw IP, no user-agent stored. `visitor` is a short salted hash
  of (IP, UA) so repeat views by one person can be counted once per day.
- Bots are dropped by user-agent before they reach the DB.
- Retention: rows older than SITE_VISITS_RETENTION_DAYS (90) are deleted at
  most once per process per day — the DB sits at the 500 MB free-tier line.
- Off switch: SITE_VISITS=off.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import threading
import time
from datetime import date
from typing import Any, Callable
from urllib.parse import urlsplit

from .. import db

log = logging.getLogger(__name__)

BACKOFF_SECONDS = 600
RETENTION_DAYS = int(os.getenv("SITE_VISITS_RETENTION_DAYS", "90") or 90)
_MAX = 64  # utm field length cap

_BOT_RE = re.compile(
    r"bot|crawl|spider|slurp|headless|python-requests|curl/|wget/|libwww|"
    r"facebookexternalhit|linkedinbot|twitterbot|whatsapp|telegram|preview|"
    r"monitor|uptime|pingdom|lighthouse|pagespeed|gtmetrix|scrapy|httpclient",
    re.I,
)
_TRACKED_EXACT = {"/", "/listings"}
_TRACKED_PREFIX = ("/listings/",)

_state: dict[str, Any] = {"down_until": 0.0, "retention_day": None}


def enabled() -> bool:
    return os.getenv("SITE_VISITS", "on").strip().lower() not in ("off", "0", "false", "no")


def should_track(path: str, user_agent: str | None, method: str = "GET") -> bool:
    if method.upper() != "GET":
        return False
    if path not in _TRACKED_EXACT and not path.startswith(_TRACKED_PREFIX):
        return False
    ua = user_agent or ""
    if not ua or _BOT_RE.search(ua):
        return False
    return True


def _clip(v: str | None, n: int = _MAX) -> str | None:
    v = (v or "").strip()
    return v[:n] if v else None


def _referer_host(ref: str | None) -> str | None:
    if not ref:
        return None
    try:
        host = urlsplit(ref).netloc.lower()
    except ValueError:
        return None
    return _clip(host, 120)


def _visitor_hash(ip: str | None, ua: str | None) -> str:
    salt = os.getenv("SITE_VISITS_SALT", "black-whole")
    raw = f"{salt}|{ip or ''}|{ua or ''}|{date.today().isoformat()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def build_row(path: str, query: dict, headers: dict, client_ip: str | None) -> dict:
    """Pure: turn one request into the row we store. No DB, no PII."""
    lot_id = path[len("/listings/"):] if path.startswith("/listings/") else None
    ua = headers.get("user-agent")
    # Render sits behind a proxy; prefer the forwarded client, else the socket.
    fwd = (headers.get("x-forwarded-for") or "").split(",")[0].strip() or None
    return {
        "path": _clip(path, 200),
        "lot_id": _clip(lot_id, 120),
        "utm_source": _clip(query.get("utm_source")),
        "utm_medium": _clip(query.get("utm_medium")),
        "utm_campaign": _clip(query.get("utm_campaign")),
        "referer_host": _referer_host(headers.get("referer")),
        "visitor": _visitor_hash(fwd or client_ip, ua),
        "country": _clip(headers.get("cf-ipcountry"), 2),
    }


def _insert(row: dict) -> None:
    now = time.time()
    if now < _state["down_until"]:
        return
    try:
        db.execute(
            "INSERT INTO site_visits (path, lot_id, utm_source, utm_medium, utm_campaign,"
            " referer_host, visitor, country) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            (row["path"], row["lot_id"], row["utm_source"], row["utm_medium"],
             row["utm_campaign"], row["referer_host"], row["visitor"], row["country"]),
        )
        _maybe_retention()
    except Exception as exc:  # noqa: BLE001 — tracking must never break a page
        _state["down_until"] = now + BACKOFF_SECONDS
        log.warning("site_visits insert failed; backing off %ss: %s", BACKOFF_SECONDS, exc)


def _maybe_retention() -> None:
    today = date.today()
    if _state["retention_day"] == today:
        return
    _state["retention_day"] = today
    db.execute("DELETE FROM site_visits WHERE ts < now() - (%s || ' days')::interval",
               (str(RETENTION_DAYS),))


def _run_in_background(fn: Callable[[dict], None], row: dict) -> None:
    """Default runner: a daemon thread so the response never waits on Supabase."""
    threading.Thread(target=fn, args=(row,), daemon=True).start()


_runner: Callable[[Callable[[dict], None], dict], None] = _run_in_background


def track(request) -> bool:
    """Record one public page view. Returns True if a row was scheduled."""
    if not enabled():
        return False
    path = request.url.path
    ua = request.headers.get("user-agent")
    if not should_track(path, ua, request.method):
        return False
    client_ip = request.client.host if request.client else None
    row = build_row(path, dict(request.query_params), dict(request.headers), client_ip)
    _runner(_insert, row)
    return True


def summary(days: int = 30) -> dict:
    """Admin rollup: views + unique visitors by campaign, by day, by lot, top referers."""
    days = max(1, min(int(days), 365))
    since = ("%s days",)
    where = "ts >= now() - (%s || ' days')::interval"
    p = (str(days),)
    by_campaign = db.fetch_all(
        f"SELECT coalesce(utm_source,'(direct)') AS source, coalesce(utm_campaign,'(none)') AS campaign,"
        f" count(*) AS views, count(DISTINCT visitor) AS visitors, max(ts) AS last_seen"
        f" FROM site_visits WHERE {where} GROUP BY 1,2 ORDER BY views DESC", p)
    by_day = db.fetch_all(
        f"SELECT ts::date AS day, count(*) AS views, count(DISTINCT visitor) AS visitors,"
        f" count(*) FILTER (WHERE utm_source='apollo') AS from_email"
        f" FROM site_visits WHERE {where} GROUP BY 1 ORDER BY 1", p)
    by_lot = db.fetch_all(
        f"SELECT lot_id, count(*) AS views, count(DISTINCT visitor) AS visitors,"
        f" count(*) FILTER (WHERE utm_source='apollo') AS from_email"
        f" FROM site_visits WHERE {where} AND lot_id IS NOT NULL GROUP BY 1 ORDER BY views DESC LIMIT 25", p)
    referers = db.fetch_all(
        f"SELECT referer_host, count(*) AS views FROM site_visits"
        f" WHERE {where} AND referer_host IS NOT NULL GROUP BY 1 ORDER BY views DESC LIMIT 15", p)
    total = db.fetch_one(
        f"SELECT count(*) AS views, count(DISTINCT visitor) AS visitors,"
        f" count(*) FILTER (WHERE utm_source='apollo') AS from_email FROM site_visits WHERE {where}", p) or {}
    return {"days": days, "total": total, "by_campaign": by_campaign,
            "by_day": by_day, "by_lot": by_lot, "referers": referers}
