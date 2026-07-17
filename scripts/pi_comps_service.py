"""eBay sold-comps microservice — runs on the Pi (residential egress).

GET /comps?q=<query>            → cached-or-live sold-listing comps as JSON
GET /health                     → status + cache stats

Auth: X-Comps-Key header must equal COMPS_KEY from ~/comps/.env.
Fetch discipline (learned 2026-07-17, do not "optimize" away):
  - impersonate="safari_ios" (mobile fingerprint on a T-Mobile IP; desktop
    chrome got challenged instantly)
  - fresh sessions warm up: homepage → active search → sold search, with
    referer chain and 5-9s jitter; jumping straight to the sold URL trips
    the "Pardon Our Interruption" challenge
  - one session serves up to SESSION_MAX searches / SESSION_TTL seconds
  - on challenge: drop session, cool down COOLDOWN seconds, serve 503s
"""

import json
import os
import random
import re
import sqlite3
import threading
import time

from curl_cffi import requests as cffi_requests
from fastapi import FastAPI, Header, HTTPException, Query

from ebay_parse import parse_sold_page

BASE = os.path.expanduser("~/comps")
DB_PATH = os.path.join(BASE, "cache.db")
CACHE_TTL = int(os.environ.get("COMPS_CACHE_TTL", 7 * 24 * 3600))
SESSION_MAX = 25
SESSION_TTL = 1800
COOLDOWN = int(os.environ.get("COMPS_COOLDOWN", 900))
MIN_GAP = 5.0
MAX_GAP = 9.0

COMPS_KEY = os.environ.get("COMPS_KEY", "")

app = FastAPI(title="comps")

_lock = threading.Lock()
_session = None
_session_uses = 0
_session_born = 0.0
_last_url = "https://www.ebay.com/"
_blocked_until = 0.0


def _db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS cache "
        "(q TEXT PRIMARY KEY, fetched_at REAL, payload TEXT)"
    )
    return conn


def _norm(q: str) -> str:
    return re.sub(r"\s+", " ", q.strip().lower())


def _jitter():
    time.sleep(random.uniform(MIN_GAP, MAX_GAP))


def _new_session():
    global _session, _session_uses, _session_born, _last_url
    s = cffi_requests.Session(impersonate="safari_ios")
    r = s.get("https://www.ebay.com/", timeout=20)
    if r.status_code != 200:
        raise RuntimeError(f"warmup status {r.status_code}")
    _session, _session_uses, _session_born = s, 0, time.time()
    _last_url = "https://www.ebay.com/"


def _challenge(body: str) -> bool:
    return "Pardon Our Interruption" in body or len(body) < 50_000


def _fetch_sold(query: str) -> dict:
    """Fetch one sold-search page. Caller holds _lock."""
    global _session, _session_uses, _last_url, _blocked_until

    if time.time() < _blocked_until:
        raise HTTPException(503, detail={"error": "cooling_down",
                                         "retry_at": _blocked_until})

    fresh = False
    if (_session is None or _session_uses >= SESSION_MAX
            or time.time() - _session_born > SESSION_TTL):
        _new_session()
        fresh = True

    try:
        if fresh:
            _jitter()
            r = _session.get("https://www.ebay.com/sch/i.html",
                             params={"_nkw": query},
                             headers={"Referer": _last_url}, timeout=25)
            _last_url = str(r.url)
        _jitter()
        r = _session.get("https://www.ebay.com/sch/i.html",
                         params={"_nkw": query, "LH_Sold": "1",
                                 "LH_Complete": "1", "_ipg": "60"},
                         headers={"Referer": _last_url}, timeout=25)
    except Exception as exc:
        _session = None
        raise HTTPException(502, detail={"error": "fetch_failed", "exc": str(exc)})

    _session_uses += 1
    _last_url = str(r.url)
    body = r.text
    if r.status_code != 200 or _challenge(body):
        _session = None
        _blocked_until = time.time() + COOLDOWN
        raise HTTPException(503, detail={"error": "challenged",
                                         "retry_at": _blocked_until})
    return parse_sold_page(body)


def _auth(key):
    if not COMPS_KEY or key != COMPS_KEY:
        raise HTTPException(401, detail="bad key")


@app.get("/health")
def health():
    conn = _db()
    n = conn.execute("SELECT COUNT(*) FROM cache").fetchone()[0]
    conn.close()
    return {"ok": True, "cached_queries": n,
            "cooling_down": time.time() < _blocked_until}


@app.get("/comps")
def comps(q: str = Query(..., min_length=3, max_length=200),
          refresh: bool = False,
          x_comps_key: str = Header(default="")):
    _auth(x_comps_key)
    qn = _norm(q)
    conn = _db()
    row = conn.execute("SELECT fetched_at, payload FROM cache WHERE q=?",
                       (qn,)).fetchone()
    if row and not refresh and time.time() - row[0] < CACHE_TTL:
        payload = json.loads(row[1])
        payload.update({"query": qn, "cached": True, "fetched_at": row[0]})
        conn.close()
        return payload

    with _lock:
        data = _fetch_sold(qn)
    now = time.time()
    conn.execute("INSERT OR REPLACE INTO cache VALUES (?,?,?)",
                 (qn, now, json.dumps(data)))
    conn.commit()
    conn.close()
    data.update({"query": qn, "cached": False, "fetched_at": now})
    return data
