"""FastAPI dashboard.

Routes:
  GET  /                  → single page with all three tabs
  GET  /api/runs/state    → snapshot of current/last run
  POST /api/runs/start    → kick off run.py with a GovDeals URL
  GET  /api/runs/stream   → SSE: progress + raw stdout lines
  GET  /api/drafts        → JSON list of listing folders + metadata
  GET  /image/{folder}/{name} → serve image from a listing folder
  GET  /screenshot/{folder}/{name} → serve a Playwright screenshot
  POST /subscribe             → public alerts signup → subscribers table
  POST /event                 → public click-only lead event (tel:/mailto:) → site_visits
  GET/PATCH/DELETE /api/subscribers[/{id}] → admin Subscribers tab

Streams stdout from run.py as Server-Sent Events. Parses
`<<<EVENT>>>{json}` lines emitted by automation.progress.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import os
import re
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sse_starlette.sse import EventSourceResponse

from ..config import (
    DOWNLOAD_ROOT,
    FACEBOOK_BUSINESS_URL,
    GOOGLE_SITE_VERIFICATION,
    PUBLIC_ADDRESS_CITY,
    PUBLIC_ADDRESS_POSTAL,
    PUBLIC_ADDRESS_REGION,
    PUBLIC_ADDRESS_STREET,
    PUBLIC_BASE_URL,
    PUBLIC_CONTACT_EMAIL,
    PUBLIC_CONTACT_PHONE,
)
from fastapi.exception_handlers import http_exception_handler as fastapi_http_exception_handler
from starlette.exceptions import HTTPException as StarletteHTTPException
from ..progress import EVENT_PREFIX, parse as parse_event
from .. import config as app_config
from .. import db
from .. import catalog_feed, google_feed, lot_channels
from .. import inventory
from .. import lot_images
from .. import lot_urls
from .. import favorite_images
from .. import favorites
from .. import telegram_alerts
from .. import deposits
from .. import site_settings
from .. import channels as channels_pkg
from ..channels import store as channel_store
from ..channels import sync as channel_sync
from .. import stripe_gateway
from .. import freight_estimate
from .. import freight_log
from .. import attribution
from .. import warp_rates
from ..alerts import blast as alerts_blast
from . import deals_query
from . import platform_api
from . import public_deals
from . import rate_limit
from . import public_map
from . import public_distress
from . import seo_copy
from . import city_pages
from . import auth as auth_svc
from . import readcache
from . import visits
from . import short_links
from . import lot_archive_view
from deals import profiles
from deals.fees import fee_model_from_env
from deals.geo import distance_from_home

try:
    # Auctions tab reads the shared Supabase `auction_listings` table. The
    # loader reuses the upstream ranking/condition helpers, so card output is
    # identical to the old SQLite path — only the data source changed.
    from ..auctions_supabase import (
        browse_listings, get_top_chairs, get_top_lots, cache_stats as _auctions_cache_stats,
    )
except Exception:  # pragma: no cover
    get_top_chairs = None  # unavailable; /api/auctions will 503
    get_top_lots = None
    browse_listings = None
    _auctions_cache_stats = None

log = logging.getLogger(__name__)

PKG_DIR = Path(__file__).parent
TEMPLATE_DIR = PKG_DIR / "templates"
STATIC_DIR = PKG_DIR / "static"
PROJECT_ROOT = PKG_DIR.parents[1]
AUCTION_EXTRACTORS_DIR = PROJECT_ROOT / "auction_extractors"

PHASES = ["scrape", "llm", "download", "dewatermark", "facebook", "ebay"]

app = FastAPI(title="listing_automation dashboard")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
templates.env.globals["asset_v"] = str(int(time.time()))  # cache-bust per process start
templates.env.globals["lot_url"] = lot_urls.public_path  # the one way templates build a lot link
from automation.web.ui_preview import router as _ui_preview_router  # noqa: E402
app.include_router(_ui_preview_router)

# "auth once" (BLACKWHOLE-14): gate /admin + /api/* behind a 365-day signed
# session cookie once ADMIN_PASSWORD is set (no-op otherwise). The public
# storefront stays open. Cookie/signing mechanics + the env-var contract live
# in automation/web/auth.py.
app.middleware("http")(auth_svc.session_auth_middleware)
# Any successful write through the API drops the admin read memo (readcache.py).
app.middleware("http")(readcache.invalidate_on_write_middleware)


@app.middleware("http")
async def _response_headers_middleware(request: Request, call_next):
    """Server-Timing on every response (so a slow load is diagnosable from the
    browser's Network tab), and long-lived caching for versioned static assets:
    `?v=<asset_v>` changes per process start, so a URL's bytes never change —
    the browser keeps them for a year instead of re-fetching every tab open."""
    t0 = time.perf_counter()
    response = await call_next(request)
    response.headers["Server-Timing"] = f"app;dur={(time.perf_counter() - t0) * 1000:.1f}"
    if request.url.path.startswith("/static/") and response.status_code == 200:
        if "v" in request.query_params:
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        else:
            response.headers["Cache-Control"] = "public, max-age=300"
    return response


@app.exception_handler(StarletteHTTPException)
async def _http_exception_handler(request: Request, exc: StarletteHTTPException):
    """A browser hitting a dead storefront URL (an old Facebook post, a feed
    link to a removed lot, a typo) gets a real page with the live lots on it
    instead of `{"detail": "..."}` — those visitors are buyers, and Google
    counts a JSON 404 as a soft error on the whole site. API callers and
    anything not asking for HTML keep the JSON contract unchanged."""
    wants_html = "text/html" in (request.headers.get("accept") or "")
    path = request.url.path
    storefront = not path.startswith(_NON_STOREFRONT_PREFIXES)
    if exc.status_code == 404 and wants_html and storefront:
        lots = await asyncio.to_thread(_not_found_lots)
        return templates.TemplateResponse(
            request, "404.html",
            _public_ctx({"lots": lots, "robots_noindex": True, "no_canonical": True}),
            status_code=404,
        )
    return await fastapi_http_exception_handler(request, exc)


# Paths whose 404 is never a buyer on a dead lot link: assets, JSON APIs,
# feeds, the local-photo fallback. They keep FastAPI's default response.
_NON_STOREFRONT_PREFIXES = ("/api/", "/static/", "/deals/api/", "/distress/api/", "/map/api/",
                            "/catalog/", "/image/", "/stripe/")


@readcache.cached(ttl=60)
def _not_found_lots() -> list[dict]:
    """Up to six live lots for the 404 page (threadpool: it touches the DB).
    Memoised like _landing_data — a scanner sweeping /wp-admin, /.env, …
    with a browser Accept header must not turn into a pooler round trip per
    probe."""
    try:
        rows = inventory.list_public()[:6]
    except Exception:  # noqa: BLE001 — the 404 page must render without the DB
        return []
    return [_decorate(r) for r in rows]


# ───────────────────────────── run state ─────────────────────────────

class RunState:
    """In-memory state for the most recent run.

    A single run at a time is the design contract — the user kicks one off
    from the launcher and watches it stream. We append every stdout line and
    every parsed event to ring buffers so a late-joining SSE client can catch
    up with replay before subscribing to live updates.
    """

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.proc: asyncio.subprocess.Process | None = None
        self.url: str | None = None
        self.started_at: float | None = None
        self.finished_at: float | None = None
        self.return_code: int | None = None
        self.lines: list[dict] = []          # {"t": ts, "stream": "stdout|stderr|event", "data": ...}
        self.phases: dict[str, dict] = {p: {"status": "pending"} for p in PHASES}
        self.run_status: str = "idle"        # idle | running | finished | error
        self.subscribers: list[asyncio.Queue] = []
        self.suggested_price: int | None = None
        self.confirmed_price: int | None = None
        # FIFO of pending runs queued by the user from the Auctions tab or
        # Launcher while another run is active. Each entry: {"url": str,
        # "extra_args": list[str]}. Drained by _start_next() when a run ends.
        self.pending: list[dict] = []

    def reset(self) -> None:
        self.proc = None
        self.url = None
        self.started_at = None
        self.finished_at = None
        self.return_code = None
        self.lines.clear()
        self.phases = {p: {"status": "pending"} for p in PHASES}
        self.run_status = "idle"
        self.suggested_price = None
        self.confirmed_price = None

    async def broadcast(self, msg: dict) -> None:
        self.lines.append(msg)
        # Cap memory — keep last 4k events
        if len(self.lines) > 4000:
            del self.lines[:1000]
        dead = []
        for q in self.subscribers:
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                dead.append(q)
        for q in dead:
            self.subscribers.remove(q)

    def snapshot(self) -> dict:
        return {
            "url": self.url,
            "status": self.run_status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "return_code": self.return_code,
            "phases": self.phases,
            "suggested_price": self.suggested_price,
            "confirmed_price": self.confirmed_price,
            "line_count": len(self.lines),
            "queue": [
                {"position": i + 1, "url": item["url"]}
                for i, item in enumerate(self.pending)
            ],
            "queue_length": len(self.pending),
        }


state = RunState()


# ───────────────────────────── subprocess ─────────────────────────────

async def _pump_stream(stream: asyncio.StreamReader, kind: str) -> None:
    """Read run.py's stdout/stderr line by line, parse <<<EVENT>>> markers."""
    while True:
        raw = await stream.readline()
        if not raw:
            break
        line = raw.decode("utf-8", errors="replace").rstrip("\n")
        ev = parse_event(line)
        if ev is not None:
            await _apply_event(ev)
            await state.broadcast({"t": time.time(), "stream": "event", "data": ev})
        else:
            await state.broadcast({"t": time.time(), "stream": kind, "data": line})


async def _apply_event(ev: dict) -> None:
    kind = ev.get("kind")
    if kind == "phase":
        phase = ev.get("phase")
        if phase in state.phases:
            entry = {k: v for k, v in ev.items() if k not in ("kind", "phase", "ts")}
            state.phases[phase] = entry
    elif kind == "run":
        st = ev.get("status")
        if st == "started":
            state.run_status = "running"
        elif st == "finished":
            state.run_status = "finished"
            readcache.invalidate_all()   # the run/scrape wrote rows the admin memo may hold
    elif kind == "price":
        state.suggested_price = ev.get("suggested")
        state.confirmed_price = ev.get("confirmed")


# What the Launcher's ▶ does. "channels" = scripts/lot_channels.py add — ledger
# row + photos on R2 + Marketplace post on the family account, with the site and
# the FB Business catalog following the row (2026-08-26). "pipeline" = the
# original run.py scrape→LLM→dewatermark→FB/eBay draft flow, now opt-in.
DEFAULT_LAUNCH_MODE = os.getenv("LISTING_LAUNCH_MODE", "channels")
LAUNCH_MODES = ("channels", "pipeline", "remove")


def _launch_cmd(mode: str, target: str, extra_args: list[str]) -> list[str]:
    project_root = Path(__file__).resolve().parents[2]
    if mode == "pipeline":
        return [sys.executable, "-u", str(project_root / "run.py"), target] + extra_args
    script = project_root / "scripts" / "lot_channels.py"
    verb = "remove" if mode == "remove" else "add"
    return [sys.executable, "-u", str(script), verb, target] + extra_args


async def _run_subprocess(url: str, extra_args: list[str], mode: str = "pipeline") -> None:
    """Spawn the launch command for `mode` and pump its output into the event stream."""
    project_root = Path(__file__).resolve().parents[2]
    cmd = _launch_cmd(mode, url, extra_args)
    env = os.environ.copy()
    env.setdefault("PYTHONUNBUFFERED", "1")
    # Give the dashboard up to 2 minutes to POST a confirmed price before run.py
    # auto-accepts the LLM suggestion. (run.py:_wait_for_price_confirmation)
    env.setdefault("LISTING_PRICE_CONFIRM_TIMEOUT", "120")

    state.proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.PIPE,
        env=env,
        cwd=str(project_root),
    )
    state.started_at = time.time()
    state.run_status = "running"

    await state.broadcast({
        "t": time.time(), "stream": "system",
        "data": f"$ {' '.join(cmd)}",
    })

    try:
        await asyncio.gather(
            _pump_stream(state.proc.stdout, "stdout"),
            _pump_stream(state.proc.stderr, "stderr"),
        )
        rc = await state.proc.wait()
    except Exception as e:
        await state.broadcast({"t": time.time(), "stream": "system",
                              "data": f"[runner error] {e!r}"})
        rc = -1

    state.return_code = rc
    state.finished_at = time.time()
    state.run_status = "finished" if rc == 0 else "error"
    readcache.invalidate_all()   # the run/scrape wrote rows the admin memo may hold
    await state.broadcast({"t": time.time(), "stream": "system",
                          "data": f"[exit {rc}]"})
    # Drain the next queued URL if any.
    await _start_next()


async def _broadcast_queue() -> None:
    """Push a snapshot-ish event when the pending queue changes."""
    await state.broadcast({
        "t": time.time(), "stream": "queue",
        "data": {"queue": [
            {"position": i + 1, "url": item["url"]}
            for i, item in enumerate(state.pending)
        ], "queue_length": len(state.pending)},
    })


async def _start_next() -> None:
    """If idle and something's pending, pop the head and start it."""
    if state.run_status == "running":
        return
    if not state.pending:
        return
    item = state.pending.pop(0)
    state.reset()
    state.url = item["url"]
    asyncio.create_task(_run_subprocess(item["url"], item.get("extra_args") or [],
                                        item.get("mode") or DEFAULT_LAUNCH_MODE))
    await _broadcast_queue()


# ───────────────────────────── HTTP routes ─────────────────────────────

@app.get("/admin", response_class=HTMLResponse)
async def admin(request: Request):
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "phases": PHASES,
            "now": int(time.time()),
            # Channels tab (Phase 1.6): one switch per channel is rendered server-side so a
            # channel can never be missing from the admin; channels.js only paints state.
            "channels": list(channels_pkg.CHANNELS),
            "approval_channels": sorted(channels_pkg.APPROVAL_CHANNELS),
        },
    )


# ───────────────────────────── auth (BLACKWHOLE-14) ─────────────────────────
# "Auth once": password → 365-day signed-cookie session, optional TOTP once per
# device. No-op until ADMIN_PASSWORD is set. See automation/web/auth.py.

@app.get("/admin/login", response_class=HTMLResponse)
async def admin_login_page(request: Request):
    # Already signed in (or auth disabled)? Skip the form.
    if not auth_svc.auth_enabled() or auth_svc.request_has_session(request):
        return RedirectResponse("/admin", status_code=303)
    return templates.TemplateResponse(request, "login.html", {})


@app.get("/api/auth/status")
async def auth_status(request: Request):
    """Lets the admin UI decide whether to show a sign-out control."""
    enabled = auth_svc.auth_enabled()
    return {
        "auth_enabled": enabled,
        "authenticated": (not enabled) or auth_svc.request_has_session(request),
        "totp_enabled": auth_svc.totp_enabled(),
    }


@app.post("/api/auth/login")
async def auth_login(payload: dict, request: Request, response: Response):
    """Password first factor; TOTP second factor once per device (env-gated).

    - 200 ``{ok: true}`` — session cookie set (and device cookie, if TOTP ran).
    - 200 ``{ok: false, totp_required: true}`` — password accepted but this
      device isn't trusted yet: re-submit with ``totp_code``.
    - 401 — wrong password or wrong TOTP code.
    - 400 — auth disabled (ADMIN_PASSWORD unset).
    """
    payload = payload or {}
    if not auth_svc.auth_enabled():
        raise HTTPException(400, "auth_disabled")
    password = str(payload.get("password") or "")
    import secrets as _secrets
    if not _secrets.compare_digest(password, auth_svc.admin_password()):
        raise HTTPException(401, "bad_credentials")

    if auth_svc.totp_enabled() and not auth_svc.request_has_trusted_device(request):
        code = str(payload.get("totp_code") or "").strip()
        if not code:
            return {"ok": False, "totp_required": True}
        if not auth_svc.verify_totp_code(code):
            raise HTTPException(401, "bad_totp")
        auth_svc.set_device_cookie(response)

    auth_svc.set_session_cookie(response)
    return {"ok": True, "totp_required": False}


@app.post("/api/auth/logout")
async def auth_logout(response: Response):
    """Clear the session cookie (the trusted-device cookie survives — TOTP
    stays 'once per device', not 'once per session')."""
    auth_svc.clear_session_cookie(response)
    return {"ok": True}


# ───────────────────────────── public pages ─────────────────────────────

def _reserve_enabled() -> bool:
    """The Reserve feature's single on/off switch — no Stripe key, no feature.

    Read at request time (not import time) so flipping the key doesn't need a
    redeploy and tests can monkeypatch it.
    """
    return stripe_gateway.enabled()


def _reservable(row: dict | None) -> bool:
    """Can a buyer put money down on this lot right now?

    Deliberately stricter than "is it visible": a lot with no price has no
    quote, and a lot at zero remaining has nothing to hold. Hidden/sold lots
    still render their detail page (BLACKWHOLE-29 sold archive) — they just
    don't get a Reserve button.
    """
    if not row or row.get("status") == "hidden" or inventory.is_sold(row):
        return False
    try:
        price = float(row.get("price_per_chair") or 0)
        remaining = int(row.get("quantity_remaining") or 0)
    except (TypeError, ValueError):
        return False
    return price > 0 and remaining > 0


def _public_ctx(extra: dict) -> dict:
    """Common context for every public-page template (footer link etc)."""
    return {
        "facebook_business_url": FACEBOOK_BUSINESS_URL or None,
        "base_url": PUBLIC_BASE_URL,
        "google_site_verification": GOOGLE_SITE_VERIFICATION or None,
        "reserve_enabled": _reserve_enabled(),
        "contact": PUBLIC_CONTACT,
        **extra,
    }


# Business identity for the footer + Organization JSON-LD (NAP). Built once;
# the phone key is simply absent until PUBLIC_CONTACT_PHONE is set.
PUBLIC_CONTACT: dict = {
    k: v for k, v in {
        "email": PUBLIC_CONTACT_EMAIL.strip() or None,
        "phone": PUBLIC_CONTACT_PHONE.strip() or None,
        "street": PUBLIC_ADDRESS_STREET.strip() or None,
        "city": PUBLIC_ADDRESS_CITY.strip() or None,
        "region": PUBLIC_ADDRESS_REGION.strip() or None,
        "postal": PUBLIC_ADDRESS_POSTAL.strip() or None,
    }.items() if v
}


# <title> budget. Google shows ~60 characters on desktop and truncates the
# rest with an ellipsis, so the brand goes on the short form and the scraped
# lot title gets trimmed at a word boundary to fit.
SEO_TITLE_MAX = 65
SEO_DESCRIPTION_MAX = 155
_BRAND_SHORT = "Black Whole"
# A leading number is a count ("~2,500 Wire Frame…", "Lot of 657 …") unless
# the next word is a unit of measure ("8 ft Rectangular Tables", "60 in
# Round Tables") — those are the Augusta table lots and the size stays.
_TITLE_QTY_PREFIX = re.compile(
    r"^\s*(?:lot\s+of\s+)?~?\s*\d[\d,]*\s*(?:×|x)?\s+"
    r"(?!(?:ft|feet|foot|in|inch|inches|cm|mm|m|lb|lbs|ga|gauge|pc|pcs|piece|pieces)\b)",
    re.I,
)
_TITLE_TRAILING_PAREN = re.compile(r"\s*\([^()]*\)\s*$")


def _short_title(title: str) -> str:
    """Scraped titles read like `~2,500 Wire Frame Stacking Chairs — Chrome
    Frame, Dark Plum Pad, Linkable (Pittsburgh, PA)`. Strip the leading count
    (we add our own) and the trailing `(City, ST)` (we add the location) so
    the words that are left are the ones a buyer searches for."""
    t = _TITLE_TRAILING_PAREN.sub("", (title or "").strip())
    t = _TITLE_QTY_PREFIX.sub("", t)
    return t.strip(" —–-·,") or (title or "").strip()


_SEGMENT_SPLIT = re.compile(r"(\s+[—–|·]\s+|,\s+|\s+-\s+)")


def _truncate_words(text: str, limit: int, ellipsis: str = "…") -> str:
    """Cut `text` to at most `limit` characters. Prefers dropping whole
    descriptor segments (`… — Chrome Frame, Dark Plum Pad`) so the part that
    names the product survives, keeping each segment's own separator;
    falls back to a word boundary."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    budget = max(limit - len(ellipsis), 0)
    parts = _SEGMENT_SPLIT.split(text)  # [seg, sep, seg, sep, …]
    kept = ""
    for i in range(0, len(parts), 2):
        sep = parts[i - 1] if i else ""
        candidate = f"{kept}{sep}{parts[i]}"
        if len(candidate) > budget:
            break
        kept = candidate
    if not kept:
        kept = text[:budget]
        kept = kept.rsplit(" ", 1)[0] if " " in kept else kept
    return kept.rstrip(" ,;:—–-·") + ellipsis


def _seo_title(qty, title: str, loc: str) -> str:
    """`{qty}× {short title} — {City, ST} | Black Whole`, trimmed to the
    budget by shortening the title part first (never the location or brand).
    A multi-city location keeps only its first city when it would not fit."""
    short = _short_title(title)
    suffix = f" | {_BRAND_SHORT}"
    prefix = f"{qty}× " if qty else ""
    for loc_part in (loc, loc.split(" · ")[0] if loc else ""):
        tail = f" — {loc_part}" if loc_part else ""
        room = SEO_TITLE_MAX - len(prefix) - len(tail) - len(suffix)
        if room >= 12 or not loc_part:
            body = short if len(short) <= room else _truncate_words(short, max(room, 12), ellipsis="")
            return f"{prefix}{body}{tail}{suffix}"
    return f"{prefix}{_truncate_words(short, 30, ellipsis='')}{suffix}"


def _absolute(url: str | None) -> str | None:
    """Make a site-relative image path absolute for og:image / JSON-LD."""
    if not url:
        return None
    return url if url.startswith("http") else f"{PUBLIC_BASE_URL}{url}"


def _location_str(row: dict) -> str:
    parts = [p for p in ((row.get("city") or "").strip(),
                         (row.get("state") or "").strip()) if p]
    return ", ".join(parts)


def _detail_seo(row: dict, hero: str | None, images: list[str]) -> dict:
    """Title / description / JSON-LD payload for a lot detail page."""
    sold = inventory.is_sold(row)
    qty = (row.get("quantity_original") if sold
           else (row.get("quantity_remaining") or row.get("quantity_original")))
    title = (row.get("title") or "Chair lot").strip()
    # A multi-location lot advertises every city it sat in, not just the first.
    loc = " · ".join(inventory.location_labels(row)) or _location_str(row)

    seo_title = _seo_title(qty, title, loc)

    desc_bits = []
    if qty:
        desc_bits.append(f"{qty} sold" if sold else f"{qty} available")
    if row.get("price_per_chair"):
        desc_bits.append(f"${row['price_per_chair']:.0f}/chair")
    if loc:
        desc_bits.append(("sourced from " if sold else "pickup in ") + loc)
    # The lead (count · price · every city) is the part that must survive —
    # the title only has room for the first city, so a multi-location lot's
    # other cities live here. The scraped title is shortened first and the
    # scraped description only fills whatever room is left.
    name = _truncate_words(_short_title(title), 50, ellipsis="")
    if sold:
        lead = f"{name} — this lot has sold" + (
            f" ({' · '.join(desc_bits)})." if desc_bits else "."
        )
        tail = " We buy sets like it every week; ask us about the next one."
        if len(lead) + len(tail) <= SEO_DESCRIPTION_MAX:
            lead += tail
    else:
        lead = f"{name} for sale in bulk" + (
            f" — {' · '.join(desc_bits)}." if desc_bits else "."
        )
    body = (row.get("description") or "").strip()
    room = SEO_DESCRIPTION_MAX - len(lead) - 1
    if body and room >= 20:
        lead += " " + _truncate_words(body, room)
    lead = _truncate_words(lead, SEO_DESCRIPTION_MAX)

    address = {
        k: v for k, v in {
            "addressLocality": (row.get("city") or "").strip() or None,
            "addressRegion": (row.get("state") or "").strip() or None,
            "postalCode": (row.get("zip_code") or "").strip() or None,
        }.items() if v
    }
    offer: dict = {
        "@type": "Offer",
        "priceCurrency": "USD",
        "availability": (
            "https://schema.org/InStock"
            if (row.get("quantity_remaining") or 0) > 0
            else "https://schema.org/SoldOut"
        ),
        "itemCondition": "https://schema.org/UsedCondition",
    }
    if row.get("price_per_chair"):
        offer["price"] = f"{row['price_per_chair']:.2f}"
    if address:
        offer["availableAtOrFrom"] = {
            "@type": "Place",
            "address": {"@type": "PostalAddress", "addressCountry": "US", **address},
        }
    product: dict = {
        "@context": "https://schema.org",
        "@type": "Product",
        "name": title,
        # The slug, never the ledger id: a `gd-{asset}-{account}` sku pastes
        # straight back into GovDeals and finds the auction.
        "sku": row.get("slug") or row.get("lot_id"),
        "offers": offer,
    }
    imgs = [u for u in (_absolute(hero), *map(_absolute, images)) if u]
    if imgs:
        product["image"] = list(dict.fromkeys(imgs))
    if body:
        product["description"] = body

    return {
        "seo_title": seo_title,
        "seo_description": lead,
        "og_image": _absolute(hero) or (imgs[0] if imgs else None),
        "product_jsonld": seo_copy.jsonld(product),
        # Visible trail + BreadcrumbList come from the _crumbs.html macro.
        "crumb_name": _truncate_words(_short_title(title), 60, ellipsis=""),
    }


def _public_indexable_status(row: dict) -> bool:
    """A live, public, in-stock lot — the sitemap's first half."""
    remaining = row.get("quantity_remaining")
    return (row.get("status") in inventory.PUBLIC_STATUSES
            and (remaining is None or remaining > 0))


def _indexable(row: dict, hero: str | None, images: list[str]) -> bool:
    """Should Google index this lot page? Mirrors the sitemap: a live public
    lot, or a sold lot that earns a showcase card (real headcount + a photo).
    Everything else (lost, hidden, half-imported folder rows) still renders
    for anyone holding a link, but carries noindex so it can't drag the site
    down as a pile of thin orphan pages."""
    if _public_indexable_status(row):
        return True
    if inventory.is_sold(row) and (row.get("quantity_original") or 0) > 0 and (hero or images):
        return True
    return False


def _canonical_twin(row: dict) -> str | None:
    """A `<lot>-sold` showcase row is the same chairs as `<lot>` (the operator
    keeps a sold copy next to a live lot for the archive strip). Point its
    canonical at the live page so Google sees one lot, not two near-identical
    ones — but only while that page is itself indexable (live, public, in
    stock); a canonical to a noindex page would drop both URLs."""
    lot_id = str(row.get("lot_id") or "")
    if not lot_id.endswith("-sold") or not inventory.is_sold(row):
        return None
    base = lot_id[: -len("-sold")]
    try:
        twin = inventory.get(base)
    except Exception:  # noqa: BLE001 — a lookup failure must not break the page
        return None
    if twin and _public_indexable_status(twin):
        return f"{PUBLIC_BASE_URL}{lot_urls.public_path(twin)}"
    return None


def _hero_src(row: dict) -> str | None:
    """Cover-image URL for a row: durable cloud URL first, local /image/ fallback.

    The deployed site has no local Desktop folder, so a populated
    `hero_image_url` is what actually renders there. Locally, an un-uploaded lot
    still shows via the /image/ route. Precedence lives in `lot_images` so the
    site and the CRM can't drift (BLACKWHOLE-31).
    """
    return lot_images.hero_src(row)


def _gallery_srcs(row: dict) -> list[str]:
    """Ordered gallery URLs: durable cloud list first, else local folder files."""
    return lot_images.gallery_srcs(row)


@readcache.cached(ttl=60)
def _landing_data() -> dict:
    """Counts + featured carousel for `/`. Memoised: this is the most-hit
    route on the site (every visitor, every bot) and the numbers change a
    few times a week."""
    counts = inventory.stats()
    # Featured carousel: Idaho lots lead (the Boise nationwide-ships
    # campaign), then the rest in ledger order.
    def _idaho_first(r: dict) -> int:
        state = (r.get("state") or "").strip().upper()
        city = (r.get("city") or "").strip().lower()
        return 0 if state in ("ID", "IDAHO") or "boise" in city else 1
    rows = inventory.list_public()
    featured = sorted(rows, key=_idaho_first)[:12]
    for r in featured:
        r["hero_src"] = _hero_src(r)
    # Floor price over EVERY public lot, not the 12 featured — it is the
    # number the homepage intent line quotes next to the full chair count.
    prices = [float(r["price_per_chair"]) for r in rows
              if r.get("price_per_chair") and float(r["price_per_chair"]) > 0]
    return {"counts": counts, "featured": featured,
            "min_price": min(prices) if prices else None}


@app.get("/", response_class=HTMLResponse)
def public_landing(request: Request):
    visits.track(request)
    try:
        data = _landing_data()
        counts, featured = data["counts"], data["featured"]
        min_price = data.get("min_price")
    except Exception:
        counts = {"lots": 0, "chairs": 0, "cities": 0, "moved": 0}
        featured = []
        min_price = None
    return templates.TemplateResponse(
        request, "landing.html",
        _public_ctx({
            "stats": counts, "featured": featured,
            "intent": _intent_line(counts, min_price),
            "site_faq": seo_copy.SITE_FAQ,
            "site_faq_jsonld": seo_copy.faq_jsonld(seo_copy.SITE_FAQ),
        }),
    )


def _intent_line(counts: dict, min_price: float | None) -> str | None:
    """The one sentence a "bulk seating" searcher wants above the fold:
    floor price, how many chairs, how many cities. Empty floor → no line."""
    chairs = int(counts.get("chairs") or 0)
    cities = int(counts.get("cities") or 0)
    if not chairs:
        return None
    bits = []
    if min_price:
        bits.append(f"Bulk seating from ${min_price:,.0f} per chair")
    else:
        bits.append("Bulk seating priced by the chair")
    bits.append(f"{chairs:,} chairs on the floor")
    if cities:
        bits.append(f"{cities} pickup cit{'y' if cities == 1 else 'ies'}")
    bits.append("local pickup free or nationwide freight")
    return " · ".join(bits) + "."


@app.get("/about", response_class=HTMLResponse)
def public_about(request: Request):
    visits.track(request)
    try:
        counts = _landing_data()["counts"]
    except Exception:  # noqa: BLE001 — the page reads fine without numbers
        counts = {"lots": 0, "chairs": 0, "cities": 0, "moved": 0}
    return templates.TemplateResponse(request, "about.html", _public_ctx({"stats": counts}))


def _short_link_redirect(code: str):
    def _go(lot_id: str | None = None):
        return RedirectResponse(short_links.target(code, lot_id), status_code=302)
    return _go


# Typed short links (`black-whole.com/cl`) → tagged landing/lot URL. See short_links.py.
for _code in short_links.CHANNELS:
    app.add_api_route(f"/{_code}", _short_link_redirect(_code), methods=["GET"], include_in_schema=False)
    app.add_api_route(f"/{_code}/{{lot_id}}", _short_link_redirect(_code), methods=["GET"], include_in_schema=False)


def _decorate(row: dict) -> dict:
    """Attach the derived fields every public card/page needs."""
    row["hero_src"] = _hero_src(row)
    row["location_labels"] = inventory.location_labels(row)
    row["is_sold"] = inventory.is_sold(row)
    # "CHAIR" on every card was fine until the Augusta round tables (2026-08-26).
    row["unit"] = lot_channels.unit_word(row).upper()
    return row


@app.get("/listings", response_class=HTMLResponse)
def public_listings(request: Request):
    visits.track(request)
    items = [_decorate(r) for r in inventory.list_public()]
    # Sold lots are shown too (BLACKWHOLE-29) — a buyer who sees 4,000 chairs
    # already moved trusts the 200 on the floor. They render in their own
    # archive strip, stamped SOLD, and are not filterable stock.
    sold_items = [_decorate(r) for r in inventory.list_sold_showcase()]
    cities = sorted({label for r in items for label in r["location_labels"]})
    chair_types = sorted({(r.get("chair_type") or "").strip()
                          for r in items if r.get("chair_type")})
    return templates.TemplateResponse(
        request, "listings.html",
        _public_ctx({"items": items, "sold_items": sold_items,
                     "cities": cities, "chair_types": chair_types,
                     "itemlist_jsonld": _itemlist_jsonld(items)}),
    )


def _itemlist_jsonld(items: list[dict], *, name: str = "Chair lots for sale") -> str:
    """ItemList of the live lots for /listings and the city pages — tells
    Google the page is a catalogue of these products, so lot pages are found
    from it, not only from the sitemap."""
    data = {
        "@context": "https://schema.org",
        "@type": "ItemList",
        "name": name,
        "numberOfItems": len(items),
        "itemListElement": [
            {"@type": "ListItem", "position": i,
             "url": f"{PUBLIC_BASE_URL}{lot_urls.public_path(r)}",
             "name": (r.get("title") or "Chair lot")}
            for i, r in enumerate(items, start=1)
        ],
    }
    return seo_copy.jsonld(data)


@app.get("/listings/{lot_id}", response_class=HTMLResponse)
def public_listing_detail(request: Request, lot_id: str):
    """`lot_id` is the slug (canonical) or the ledger id. An id hit on a row
    that has a slug 301s to the slug URL (query string kept, so feed UTM tags
    survive) — every Facebook post, feed row and short link ever sent keeps
    resolving."""
    row = inventory.get_public(lot_id)
    if not row or row.get("status") in ("hidden",):
        visits.track(request)
        raise HTTPException(404, "listing not found")
    if row.get("slug") and lot_id != row["slug"]:
        # Not tracked: the slug page that follows is the real view, and one
        # click must not count as two lots.
        target = lot_urls.public_path(row)
        if request.url.query:
            target += f"?{request.url.query}"
        return RedirectResponse(target, status_code=301)
    lot_id = str(row["lot_id"])
    visits.track(request, lot_id=lot_id)
    _decorate(row)
    hero = _hero_src(row)
    images = _gallery_srcs(row)
    try:
        near = public_map.nearby(lot_id, miles=public_map.NEARBY_MILES)
    except Exception:  # noqa: BLE001 — the page must render without the map
        log.exception("nearby lots failed for %s", lot_id)
        near = {"origin": None, "items": []}
    return templates.TemplateResponse(
        request, "listing_detail.html",
        _public_ctx({
            "item": row,
            "hero": hero,
            "images": images,
            # The freight widget only renders where it can actually answer: a
            # lot with a locatable origin that's still for sale. A zip-less or
            # sold lot keeps the plain pickup row instead of offering a form
            # that can only ever say "we'll quote it by hand".
            "freight": {
                "enabled": bool(_freight_origin_zip(row)) and not row["is_sold"],
                "default_qty": _freight_default_qty(row),
            },
            "nearby": near,
            "copy": seo_copy.build(row, sold=row["is_sold"]),
            "city_crumb": _city_crumb(row),
            "robots_noindex": not _indexable(row, hero, images),
            "canonical_url": _canonical_twin(row),
            **_detail_seo(row, hero, images),
        }),
    )


def _city_crumb(row: dict) -> tuple[str, str] | None:
    """(label, path) of the lot's primary city page, when that city has one."""
    city, state = (row.get("city") or "").strip(), (row.get("state") or "").strip()
    if not city:
        return None
    slug = lot_urls.city_slug(city, state)
    try:
        if slug in city_pages.index():
            return (f"{city}, {state}" if state else city, lot_urls.city_path(city, state))
    except Exception:  # noqa: BLE001 — a crumb is never worth a 500
        log.exception("city index failed")
    return None


@app.get("/map", response_class=HTMLResponse)
def public_map_page(request: Request, near: str | None = None, status: str | None = None,
                    radius: float | None = None):
    """Full-screen public map of our lots (plan 2026-09-15). The pins are JS
    (/map/api/points); the city list under the map is server-rendered so a
    crawler sees every pickup city and its page."""
    visits.track(request)
    try:
        cities = city_pages.listing()
    except Exception:  # noqa: BLE001 — the map must render without the list
        log.exception("city listing failed")
        cities = []
    return templates.TemplateResponse(request, "map.html", _public_ctx({
        "near": (near or "").strip(), "status": status or "available,incoming",
        "radius": radius or "", "cities": cities,
    }))


@app.get("/chairs", response_class=HTMLResponse)
def public_cities(request: Request):
    visits.track(request)
    return templates.TemplateResponse(
        request, "chairs_index.html", _public_ctx({"cities": city_pages.listing()}),
    )


@app.get("/chairs/{slug}", response_class=HTMLResponse)
def public_city(request: Request, slug: str):
    """City landing page: live lots there, sold proof, lots within driving
    distance, pickup/freight FAQ. Sold-only cities render with noindex."""
    visits.track(request)
    page = city_pages.page(slug)
    if page is None:
        raise HTTPException(404, "no such city")
    page["live"] = [_decorate(dict(r)) for r in page["live"]]
    page["sold"] = [_decorate(dict(r)) for r in page["sold"]]
    page["itemlist_jsonld"] = _itemlist_jsonld(page["live"], name=f"Chair lots in {page['label']}")
    return templates.TemplateResponse(
        request, "city.html",
        _public_ctx({"page": page, "robots_noindex": not page["indexable"]}),
    )


@app.get("/map/api/points")
def public_map_points(status: str | None = None, near: str | None = None,
                      radius: float | None = None):
    """Public JSON for every map surface. Allow-listed in public_map — never add
    columns here. Lives under /map/api/ (public), not /api/ (auth-gated)."""
    wanted = {s.strip() for s in (status or "available,incoming").split(",") if s.strip()}
    if not wanted <= set(public_map.BUCKETS):
        raise HTTPException(400, f"status must be a comma list of {','.join(public_map.BUCKETS)}")
    try:
        data = public_map.fetch_points(statuses=wanted, near=near, radius_mi=radius)
    except Exception as e:  # noqa: BLE001
        # The repr carries the DSN and local paths, and this route is public +
        # unauthenticated — the detail goes to the server log, never the body.
        log.warning("map points query failed: %r", e)
        raise HTTPException(503, "map temporarily unavailable")
    # Public, identical for everyone, and already memoised server-side for
    # CACHE_TTL — let the browser and any CDN in front of us hold it too.
    return JSONResponse(data, headers={"Cache-Control": "public, max-age=120"})


@app.get("/api/visits/summary")
async def api_visits_summary(days: int = Query(30, ge=1, le=365)):
    """Admin-only (session middleware gates /api/*): storefront views by
    campaign / day / lot for the last N days. Answers "did the Apollo church
    emails bring anyone to the site?" — see automation/web/visits.py."""
    return await asyncio.to_thread(visits.summary, days)


@app.get("/deals/{asset_id}/{account_id}/{auction_id}", response_class=HTMLResponse)
async def deal_listing(request: Request, asset_id: int, account_id: int, auction_id: int):
    """Archived-lot viewer. Public visitors get text only (no source photos —
    copyright) and never see a seating/operator lot (chair-buyer isolation);
    an operator session sees everything."""
    row = await asyncio.to_thread(
        db.fetch_one,
        """SELECT * FROM deal_lots
        WHERE asset_id=%s AND account_id=%s AND auction_id=%s""",
        (asset_id, account_id, auction_id))
    if not row:
        raise HTTPException(status_code=404, detail="lot not archived")
    operator = (not auth_svc.auth_enabled()) or auth_svc.request_has_session(request)
    if not operator and (
        public_deals.is_excluded(row)
        or await asyncio.to_thread(public_deals.is_operator_lot, asset_id, account_id, auction_id)
    ):
        raise HTTPException(status_code=404, detail="lot not archived")
    from deals import tracking, tracking_store
    history = await asyncio.to_thread(tracking_store.history, asset_id, account_id)
    fees = fee_model_from_env()
    lot = deals_query.enrich(dict(row), fees)
    return templates.TemplateResponse(request, "deal_listing.html", {
        "lot": lot, "history": history, "bidders": tracking.bidder_summary(history),
        "show_images": operator, "premium_pct": fees.buyer_premium_pct})


# ── Deals dashboard API (BLACKWHOLE-12) ─────────────────────────────────────

_DEALS_COLS = (
    "asset_id, account_id, auction_id, title, canonical_category, city, state, "
    "bid_count, current_bid, currency_code, end_utc, outcome, final_bid, "
    "outcome_complete, first_seen_at, hero_image_url, archived_hero_url, "
    "lat, lng"
)

_DEALS_ACTIVE = "outcome_complete IS NOT TRUE AND end_utc > now()"


def _parse_bbox(raw: str | None) -> tuple[float, float, float, float] | None:
    """Parse "south,west,north,east" into a float 4-tuple, or None."""
    if not raw:
        return None
    try:
        s, w, n, e = (float(x) for x in raw.split(","))
    except ValueError:
        raise HTTPException(400, "bbox must be 'south,west,north,east'")
    return (s, w, n, e)


def _profile_where(slug: str | None) -> tuple[str, list] | None:
    """Resolve ?profile=<slug> into a deal_lots SQL fragment. Empty → no filter."""
    if not slug:
        return None
    try:
        return profiles.deal_lots_where(profiles.resolve(slug))
    except KeyError:
        raise HTTPException(404, f"unknown profile {slug!r}")

# Latest-verdict join (alias `v`) — build_where's min_margin filter and the
# "margin" sort both reference v.margin_pct, so the same FROM clause is used
# for the row and count queries alike.
_DEALS_FROM = """FROM deal_lots
LEFT JOIN LATERAL (
    SELECT method, est_resale, margin_pct, confidence, comp_count, comps,
           rank_score, analyzed_at
    FROM deal_verdicts v0
    WHERE v0.asset_id = deal_lots.asset_id AND v0.account_id = deal_lots.account_id
      AND v0.auction_id = deal_lots.auction_id
    ORDER BY v0.analyzed_at DESC LIMIT 1) v ON TRUE"""


_DEALS_FACETS_TTL = 120
_DEALS_FACETS: dict[str, tuple[float, tuple]] = {}


def deals_facets_cache_clear() -> None:
    _DEALS_FACETS.clear()


def _deals_facets_and_stats() -> tuple[list, list, dict]:
    """Categories/states facets + headline stats for the admin Deals tab.

    These three queries don't depend on the filter set and each opened its own
    pooler connection (~1.3 s) on every page flip. 120 s cache."""
    hit = _DEALS_FACETS.get("v")
    if hit and time.monotonic() - hit[0] < _DEALS_FACETS_TTL:
        return hit[1]
    cats = db.fetch_all(
        "SELECT canonical_category AS value, count(*) AS count FROM deal_lots "
        f"WHERE {_DEALS_ACTIVE} AND canonical_category IS NOT NULL "
        "GROUP BY 1 ORDER BY count DESC"
    )
    states = db.fetch_all(
        "SELECT state AS value, count(*) AS count FROM deal_lots "
        f"WHERE {_DEALS_ACTIVE} AND state IS NOT NULL "
        "GROUP BY 1 ORDER BY count DESC"
    )
    stats = db.fetch_one(
        "SELECT (SELECT count(*) FROM deal_lots) AS total_lots, "
        "(SELECT count(*) FROM deal_candidates) AS candidates, "
        f"(SELECT count(*) FROM deal_lots WHERE {_DEALS_ACTIVE} "
        "AND end_utc <= now() + interval '24 hours') AS ending_24h"
    )
    value = (cats, states, stats)
    _DEALS_FACETS["v"] = (time.monotonic(), value)
    return value


@app.get("/api/deals")
async def list_deals(
    q: str | None = None,
    category: str | None = None,
    native: str | None = None,
    state: str | None = None,
    max_bids: int | None = None,
    ending_within: int | None = None,
    status: str = "active",
    sort: str = "ends",
    dir: str | None = None,
    limit: int = 50,
    offset: int = 0,
    min_margin: float | None = None,
    min_price: float | None = None,
    max_price: float | None = None,
    list_id: int | None = None,
    tag: str | None = None,
    max_distance: float | None = None,
    bbox: str | None = None,
    profile: str | None = None,
):
    """Search/filter/sort deal_lots for the admin Deals tab.

    Facets reflect the full active set (not the filtered subset) — v1 keeps
    the SQL simple; counts guide, not gate.

    `bbox` is "south,west,north,east" (map viewport). It filters in SQL, so
    the paged rows and `total` both reflect the viewport — unlike
    `max_distance`, which still trims post-LIMIT in Python.
    """
    if status not in ("active", "closed", "all"):
        raise HTTPException(400, "status must be active|closed|all")
    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset))
    where, args = deals_query.build_where(
        q=q, category=category, native=native, state=state, max_bids=max_bids,
        ending_within=ending_within, status=status,
        min_margin=min_margin, min_price=min_price, max_price=max_price,
        list_id=list_id, tag=tag,
        bbox=_parse_bbox(bbox),
        profile_where=_profile_where(profile),
    )
    order = deals_query.order_clause(sort, dir)

    def _fetch():
        rows = db.fetch_all(
            f"SELECT {_DEALS_COLS}, row_to_json(v.*) AS verdict "
            f"{_DEALS_FROM} WHERE {where} {order} "
            "LIMIT %s OFFSET %s",
            (*args, limit, offset),
        )
        total = db.fetch_one(
            f"SELECT count(*) AS c {_DEALS_FROM} WHERE {where}", tuple(args)
        )["c"]
        cats, states, stats = _deals_facets_and_stats()
        return rows, total, cats, states, stats

    try:
        rows, total, cats, states, stats = await asyncio.to_thread(_fetch)
    except Exception as e:  # DB down / view missing → 503, matches /api/auctions
        raise HTTPException(503, f"deals query failed: {e!r}")

    fees = fee_model_from_env()
    out_rows = []
    for r in rows:
        row = deals_query.enrich(dict(r), fees)
        row["distance_mi"] = distance_from_home(row.get("lat"), row.get("lng"))
        out_rows.append(row)
    if max_distance is not None:
        out_rows = [r for r in out_rows
                    if r["distance_mi"] is not None and r["distance_mi"] <= max_distance]
    return {
        "total": total,
        "rows": out_rows,
        "facets": {"categories": cats, "states": states},
        "stats": stats,
    }


@app.get("/api/deals/geo")
async def deals_geo(
    q: str | None = None,
    category: str | None = None,
    native: str | None = None,
    state: str | None = None,
    max_bids: int | None = None,
    ending_within: int | None = None,
    status: str = "active",
    min_margin: float | None = None,
    min_price: float | None = None,
    max_price: float | None = None,
    list_id: int | None = None,
    tag: str | None = None,
    profile: str | None = None,
):
    """All mappable lots for the current filter set — feeds the Deals map.

    Unpaged on purpose: the map clusters client-side over the whole filtered
    set (GovAuctions-style), while /api/deals stays the paged table source.
    Minimal columns keep ~20k active rows to a few MB of JSON.
    """
    if status not in ("active", "closed", "all"):
        raise HTTPException(400, "status must be active|closed|all")
    where, args = deals_query.build_where(
        q=q, category=category, native=native, state=state, max_bids=max_bids,
        ending_within=ending_within, status=status,
        min_margin=min_margin, min_price=min_price, max_price=max_price,
        list_id=list_id, tag=tag,
        profile_where=_profile_where(profile),
    )

    def _fetch():
        points = db.fetch_all(
            "SELECT asset_id, account_id, auction_id, title, current_bid, "
            "bid_count, end_utc, city, state, lat, lng "
            f"{_DEALS_FROM} WHERE {where} AND lat IS NOT NULL AND lng IS NOT NULL "
            "LIMIT 25000",
            tuple(args),
        )
        unmapped = db.fetch_one(
            f"SELECT count(*) AS c {_DEALS_FROM} WHERE {where} AND lat IS NULL",
            tuple(args),
        )["c"]
        return points, unmapped

    try:
        points, unmapped = await asyncio.to_thread(_fetch)
    except Exception as e:
        raise HTTPException(503, f"deals geo query failed: {e!r}")
    for p in points:
        p["govdeals_url"] = (
            f"https://www.govdeals.com/en/asset/{p['asset_id']}/{p['account_id']}"
        )
    return {"points": points, "unmapped": unmapped}


@app.get("/api/geo/zip")
async def geo_zip(zip: str):
    """Geocode a 5-digit ZIP so the admin map can center on it."""
    z = zip.strip()
    if not re.fullmatch(r"\d{5}", z):
        raise HTTPException(400, "zip must be 5 digits")
    # Lazy import: pgeocode loads a dataset — same pattern as _annotate_auction_geo.
    from automation.alerts.geo import resolve_latlon

    lat, lng, precision = await asyncio.to_thread(resolve_latlon, z, None)
    return {"zip": z, "lat": lat, "lng": lng, "precision": precision}


# ── Public deals surface ("Surplus Radar") — outside the /api/ auth prefix ──
# Policy + queries: automation/web/public_deals.py. Nothing here touches
# photos, verdicts, or the operator's home distance.

_PUBLIC_STATUSES = ("active", "closed", "all")


@app.get("/deals", response_class=HTMLResponse)
async def public_deals_page(request: Request):
    return templates.TemplateResponse(request, "deals_public.html", {
        "base_url": PUBLIC_BASE_URL, "now": int(time.time()),
        "per_page_choices": public_deals.PER_PAGE_CHOICES})


@app.get("/deals/api/lots")
async def public_deals_lots(
    q: str | None = None, category: str | None = None, state: str | None = None,
    max_bids: int | None = None, ending_within: int | None = None,
    status: str = "active", min_price: float | None = None,
    max_price: float | None = None, bbox: str | None = None,
    sort: str = "ends", dir: str | None = None, page: int = 1, per_page: int = 25,
):
    if status not in _PUBLIC_STATUSES:
        raise HTTPException(400, "status must be active|closed|all")
    try:
        return await asyncio.to_thread(
            public_deals.fetch_page, q=q, category=category, state=state,
            max_bids=max_bids, ending_within=ending_within, status=status,
            min_price=min_price, max_price=max_price, bbox=_parse_bbox(bbox),
            sort=sort, dir=dir, page=page, per_page=per_page)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(503, f"deals query failed: {e!r}")


@app.get("/deals/api/pins")
async def public_deals_pins(
    q: str | None = None, category: str | None = None, state: str | None = None,
    max_bids: int | None = None, ending_within: int | None = None,
    status: str = "active", min_price: float | None = None, max_price: float | None = None,
):
    if status not in _PUBLIC_STATUSES:
        raise HTTPException(400, "status must be active|closed|all")
    try:
        return await asyncio.to_thread(
            public_deals.fetch_pins, q=q, category=category, state=state,
            max_bids=max_bids, ending_within=ending_within, status=status,
            min_price=min_price, max_price=max_price)
    except Exception as e:
        raise HTTPException(503, f"pins query failed: {e!r}")


@app.get("/deals/api/facets")
async def public_deals_facets():
    try:
        return await asyncio.to_thread(public_deals.fetch_facets)
    except Exception as e:
        raise HTTPException(503, f"facets query failed: {e!r}")


# ── Distress cases (/distress) — bankruptcy + WARN closure leads ────────────
# Read model + the public/operator column split: automation/web/public_distress.py.
# Public JSON lives under /distress/api/ (outside the auth-walled /api/ prefix)
# and never carries trustee / attorney / party contacts; /api/distress/cases
# (session-gated) does. Writer: `python -m deals.cli distress-sync`.

def _distress_args(tab, chapter, source, industry, radius_mi):
    from deals.distress import INDUSTRY_TAGS
    if tab not in public_distress.TABS:
        raise HTTPException(400, "tab must be leads|sales")
    if chapter and chapter not in public_distress.CHAPTERS:
        raise HTTPException(400, "chapter must be 7|11")
    if source and source not in public_distress.SOURCES:
        raise HTTPException(400, "source must be courtlistener|warn")
    if industry and industry not in INDUSTRY_TAGS:
        raise HTTPException(400, f"industry must be one of {','.join(INDUSTRY_TAGS)}")
    if radius_mi is not None and not (0 < radius_mi <= 1000):
        raise HTTPException(400, "radius_mi must be 1-1000")


def _distress_page(admin: bool, **kw) -> dict:
    _distress_args(kw["tab"], kw["chapter"], kw["source"], kw["industry"], kw["radius_mi"])
    try:
        return public_distress.fetch_page(admin=admin, **kw)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(503, f"distress query failed: {e!r}")


@app.get("/distress", response_class=HTMLResponse)
async def public_distress_page(request: Request):
    return templates.TemplateResponse(request, "distress_public.html", {
        "base_url": PUBLIC_BASE_URL, "per_page_choices": public_distress.PER_PAGE_CHOICES})


@app.get("/distress/api/cases")
@readcache.cached(ttl=60)
def public_distress_cases(
    q: str | None = None, industry: str | None = None, state: str | None = None,
    chapter: str | None = None, source: str | None = None, tab: str = "leads",
    near: str | None = None, radius_mi: float | None = None,
    sort: str = "filed", dir: str | None = None, page: int = 1, per_page: int = 25,
):
    return _distress_page(False, q=q, industry=industry, state=state, chapter=chapter, source=source,
                          tab=tab, near=near, radius_mi=radius_mi, sort=sort, dir=dir,
                          page=page, per_page=per_page)


@app.get("/distress/api/facets")
def public_distress_facets():
    try:
        return public_distress.fetch_facets()
    except Exception as e:
        raise HTTPException(503, f"distress facets failed: {e!r}")


@app.get("/api/distress/cases")
@readcache.cached(ttl=30)
def admin_distress_cases(
    q: str | None = None, industry: str | None = None, state: str | None = None,
    chapter: str | None = None, source: str | None = None, tab: str = "leads",
    near: str | None = None, radius_mi: float | None = None,
    sort: str = "filed", dir: str | None = None, page: int = 1, per_page: int = 25,
):
    """Operator view: same filters + trustee, attorneys, parties, ZIP."""
    return _distress_page(True, q=q, industry=industry, state=state, chapter=chapter, source=source,
                          tab=tab, near=near, radius_mi=radius_mi, sort=sort, dir=dir,
                          page=page, per_page=per_page)


@app.get("/sources", response_class=HTMLResponse)
async def sources_page(request: Request):
    """Public "Where the lots come from" page — server-rendered from the
    (5-min cached) public facets; no photos, no client fetch."""
    try:
        facets = await asyncio.to_thread(public_deals.fetch_facets)
    except Exception as e:
        raise HTTPException(503, f"facets query failed: {e!r}")
    return templates.TemplateResponse(request, "sources.html", {
        "facets": facets, "premium_pct": fee_model_from_env().buyer_premium_pct,
        "base_url": PUBLIC_BASE_URL})


@app.get("/api/deals/tree")
async def deals_tree(status: str = "active", profile: str | None = None):
    """Category tree for the Deals tab explorer: canonical bucket (branch) →
    native GovDeals category (twig), each with lot / zero-bid / ending-24h
    counts. Same status semantics as /api/deals."""
    if status not in ("active", "closed", "all"):
        raise HTTPException(400, "status must be active|closed|all")
    where, args = deals_query.build_where(status=status,
                                          profile_where=_profile_where(profile))

    def _fetch():
        return db.fetch_all(
            "SELECT canonical_category, native_category_id, "
            "min(native_category_name) AS native_category_name, "
            "count(*) AS n, "
            "count(*) FILTER (WHERE bid_count = 0) AS zero_bid, "
            "count(*) FILTER (WHERE outcome_complete IS NOT TRUE "
            "  AND end_utc > now() AND end_utc <= now() + interval '24 hours') AS ending_24h "
            f"FROM deal_lots WHERE {where} AND canonical_category IS NOT NULL "
            "GROUP BY canonical_category, native_category_id",
            tuple(args),
        )

    try:
        rows = await asyncio.to_thread(_fetch)
    except Exception as e:
        raise HTTPException(503, f"deals tree query failed: {e!r}")

    branches: dict[str, dict] = {}
    for r in rows:
        b = branches.setdefault(r["canonical_category"], {
            "category": r["canonical_category"], "n": 0, "zero_bid": 0,
            "ending_24h": 0, "twigs": [],
        })
        b["n"] += r["n"]
        b["zero_bid"] += r["zero_bid"]
        b["ending_24h"] += r["ending_24h"]
        b["twigs"].append({
            "native_id": r["native_category_id"],
            "name": r["native_category_name"] or r["native_category_id"],
            "n": r["n"], "zero_bid": r["zero_bid"], "ending_24h": r["ending_24h"],
        })
    for b in branches.values():
        b["twigs"].sort(key=lambda t: -t["n"])
    tree = sorted(branches.values(), key=lambda b: -b["n"])
    return {"total": sum(b["n"] for b in tree), "branches": tree}


# ── Deals browser: lists / tags / saved searches (2026-07-17 spec, T12) ─────
# Thin db wrappers in the style of the /api/auctions/favorites handlers.


@app.get("/api/deals/lists")
@readcache.cached(ttl=30)
def deals_lists():
    return db.fetch_all(
        "SELECT dl.id, dl.name, count(li.list_id) AS count "
        "FROM deal_lists dl LEFT JOIN deal_list_items li ON li.list_id = dl.id "
        "GROUP BY dl.id, dl.name ORDER BY dl.name"
    )


@app.post("/api/deals/lists")
def deals_list_create(payload: dict):
    name = ((payload or {}).get("name") or "").strip()
    if not name:
        raise HTTPException(400, "name required")
    row = db.fetch_one(
        "INSERT INTO deal_lists (name) VALUES (%s) "
        "ON CONFLICT (name) DO UPDATE SET name = EXCLUDED.name RETURNING id, name",
        (name,),
    )
    return {"id": row["id"], "name": row["name"], "count": 0}


@app.delete("/api/deals/lists/{list_id}")
def deals_list_delete(list_id: int):
    n = db.execute("DELETE FROM deal_lists WHERE id=%s", (list_id,))
    if not n:
        raise HTTPException(404, "list not found")
    return {"ok": True}


@app.put("/api/deals/lists/{list_id}/items/{asset_id}/{account_id}/{auction_id}")
def deals_list_item_add(list_id: int, asset_id: int, account_id: int,
                              auction_id: int):
    db.execute(
        "INSERT INTO deal_list_items (list_id, asset_id, account_id, auction_id) "
        "VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
        (list_id, asset_id, account_id, auction_id),
    )
    return {"ok": True}


@app.delete("/api/deals/lists/{list_id}/items/{asset_id}/{account_id}/{auction_id}")
def deals_list_item_remove(list_id: int, asset_id: int, account_id: int,
                                 auction_id: int):
    n = db.execute(
        "DELETE FROM deal_list_items WHERE list_id=%s AND asset_id=%s "
        "AND account_id=%s AND auction_id=%s",
        (list_id, asset_id, account_id, auction_id),
    )
    if not n:
        raise HTTPException(404, "not in list")
    return {"ok": True}


@app.get("/api/deals/tags")
@readcache.cached(ttl=30)
def deals_tags():
    return db.fetch_all(
        "SELECT tag, count(*) AS count FROM deal_lot_tags "
        "GROUP BY tag ORDER BY count DESC, tag"
    )


@app.put("/api/deals/tags/{asset_id}/{account_id}/{auction_id}/{tag}")
def deals_tag_add(asset_id: int, account_id: int, auction_id: int, tag: str):
    tag = tag.strip()
    if not tag:
        raise HTTPException(400, "tag required")
    db.execute(
        "INSERT INTO deal_lot_tags (asset_id, account_id, auction_id, tag) "
        "VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
        (asset_id, account_id, auction_id, tag),
    )
    return {"ok": True}


@app.delete("/api/deals/tags/{asset_id}/{account_id}/{auction_id}/{tag}")
def deals_tag_remove(asset_id: int, account_id: int, auction_id: int, tag: str):
    n = db.execute(
        "DELETE FROM deal_lot_tags WHERE asset_id=%s AND account_id=%s "
        "AND auction_id=%s AND tag=%s",
        (asset_id, account_id, auction_id, tag),
    )
    if not n:
        raise HTTPException(404, "tag not on lot")
    return {"ok": True}


@app.get("/api/deals/searches")
@readcache.cached(ttl=30)
def deals_searches():
    return db.fetch_all(
        "SELECT id, name, params, alert, created_at, last_run_at "
        "FROM saved_searches ORDER BY name"
    )


@app.post("/api/deals/searches")
def deals_search_create(payload: dict):
    payload = payload or {}
    name = (payload.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "name required")
    params = payload.get("params") or {}
    if not isinstance(params, dict):
        raise HTTPException(400, "params must be an object")
    row = db.fetch_one(
        "INSERT INTO saved_searches (name, params, alert) VALUES (%s, %s, %s) "
        "ON CONFLICT (name) DO UPDATE SET params = EXCLUDED.params, "
        "alert = EXCLUDED.alert RETURNING id, name, params, alert",
        (name, json.dumps(params), bool(payload.get("alert"))),
    )
    return row


@app.delete("/api/deals/searches/{search_id}")
def deals_search_delete(search_id: int):
    n = db.execute("DELETE FROM saved_searches WHERE id=%s", (search_id,))
    if not n:
        raise HTTPException(404, "search not found")
    return {"ok": True}


# ── Research profiles: what we're hunting for (deals/profiles.py) ────────────

@app.get("/api/profiles")
@readcache.cached(ttl=60)
def profiles_list():
    rows = profiles.list_all(True)
    default = next((p.slug for p in rows if p.is_default), "chairs")
    return {"profiles": [p.to_row() for p in rows], "default": default}


@app.post("/api/profiles")
async def profiles_create(payload: dict):
    payload = payload or {}
    try:
        p = profiles.from_row({**payload, "slug": profiles.validate_slug(payload.get("slug", ""))})
        if not p.name.strip():
            raise ValueError("name required")
        saved = await asyncio.to_thread(profiles.upsert, p)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except profiles.ProfilesUnavailable as e:
        raise HTTPException(503, str(e))
    _AUCTIONS_CACHE.clear()
    readcache.invalidate_all()
    return saved.to_row()


@app.delete("/api/profiles/{slug}")
async def profiles_delete(slug: str):
    try:
        ok = await asyncio.to_thread(profiles.delete, slug)
    except ValueError as e:
        raise HTTPException(409, str(e))
    except profiles.ProfilesUnavailable as e:
        raise HTTPException(503, str(e))
    if not ok:
        raise HTTPException(404, "profile not found")
    _AUCTIONS_CACHE.clear()
    readcache.invalidate_all()
    return {"ok": True}


@app.get("/api/profiles/{slug}/outcomes")
async def profiles_outcomes(slug: str, days: int = 365):
    """Past results for a profile: closed lots + their sold comps. This is the
    'research the past' half — the Deals tab with status=closed is the table,
    this is the roll-up above it."""
    pw = _profile_where(slug)
    where, args = (pw or ("TRUE", []))
    days = max(1, min(int(days), 3650))

    def _fetch():
        roll = db.fetch_one(
            f"""SELECT count(*) AS closed,
                       count(*) FILTER (WHERE outcome = 'no_bid') AS no_bid,
                       count(*) FILTER (WHERE outcome = 'sold') AS sold,
                       percentile_cont(0.5) WITHIN GROUP (ORDER BY final_bid)
                         FILTER (WHERE final_bid > 0) AS median_final_bid,
                       max(closed_at) AS last_closed_at
                FROM deal_lots
                WHERE outcome_complete IS TRUE
                  AND closed_at >= now() - make_interval(days => %s)
                  AND ({where})""",
            (days, *args))
        comps = db.fetch_all(
            f"""SELECT v.asset_id, v.account_id, v.auction_id, v.analyzed_at, v.method,
                       v.comp_count, v.per_unit, v.margin_pct, v.comps, deal_lots.title
                FROM deal_verdicts v
                JOIN deal_lots ON deal_lots.asset_id = v.asset_id
                              AND deal_lots.account_id = v.account_id
                              AND deal_lots.auction_id = v.auction_id
                WHERE v.comp_count > 0 AND ({where})
                ORDER BY v.analyzed_at DESC LIMIT 20""",
            tuple(args))
        return roll or {}, comps

    try:
        roll, comps = await asyncio.to_thread(_fetch)
    except Exception as e:
        raise HTTPException(503, f"outcomes query failed: {e!r}")
    closed = int(roll.get("closed") or 0)
    no_bid = int(roll.get("no_bid") or 0)
    return {
        "profile": slug, "days": days, "closed": closed, "no_bid": no_bid,
        "sold": int(roll.get("sold") or 0),
        "no_bid_pct": round(100.0 * no_bid / closed, 1) if closed else 0.0,
        "median_final_bid": (None if roll.get("median_final_bid") is None
                             else round(float(roll["median_final_bid"]), 2)),
        "last_closed_at": (roll["last_closed_at"].isoformat()
                           if roll.get("last_closed_at") else None),
        "comps": [dict(c, analyzed_at=c["analyzed_at"].isoformat() if c.get("analyzed_at") else None)
                  for c in comps],
    }
# ── Tracking list: follow chosen lots through their close (deals/tracking.py) ─

_tracking_adapter = None


def _govdeals_adapter():
    global _tracking_adapter
    if _tracking_adapter is None:
        from deals import sites
        _tracking_adapter = sites.get_adapter("govdeals")
    return _tracking_adapter


@app.get("/api/tracking")
@readcache.cached()
def tracking_list(label: str | None = None):
    from deals import tracking, tracking_store
    # Costs over the whole list, then filter: an open lot borrows its tax rate
    # from a sold lot by the same seller even when that one is on another list.
    items = tracking.landed_costs(tracking_store.list_all())
    if label:
        items = [r for r in items if r["label"] == label]
    return {"items": items, "labels": tracking_store.labels()}


@app.post("/api/tracking")
async def tracking_add(payload: dict):
    from deals import tracking
    payload = payload or {}
    ref = (payload.get("ref") or "").strip()
    if not ref:
        raise HTTPException(400, "ref required (GovDeals URL or asset/account)")
    label = (payload.get("label") or "").strip() or "default"
    note = (payload.get("note") or "").strip() or None
    try:
        row = await asyncio.to_thread(
            tracking.add_tracked, _govdeals_adapter(), ref, label=label, note=note)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return row


@app.patch("/api/tracking/{asset_id}/{account_id}")
def tracking_patch(asset_id: int, account_id: int, payload: dict):
    from deals import tracking_store
    payload = payload or {}
    label = payload.get("label")
    row = tracking_store.patch(asset_id, account_id,
                               label=(label.strip() or "default") if isinstance(label, str) else None,
                               note=payload.get("note"))
    if row and "quantity" in payload:
        # null / "" / 0 clears the override back to the title parse
        q = payload.get("quantity")
        try:
            q = int(q) if q not in (None, "") else None
        except (TypeError, ValueError):
            raise HTTPException(400, "quantity must be a whole number")
        if q is not None and q < 0:
            raise HTTPException(400, "quantity must be positive")
        try:
            row = tracking_store.set_quantity(asset_id, account_id, q or None)
        except Exception as e:  # noqa: BLE001
            if type(e).__name__ == "UndefinedColumn":
                raise HTTPException(503, "apply scripts/sql/013_tracked_lots_costs.sql first")
            raise
    if not row:
        raise HTTPException(404, "not tracked")
    return row


@app.delete("/api/tracking/{asset_id}/{account_id}")
def tracking_remove(asset_id: int, account_id: int):
    from deals import tracking_store
    if not tracking_store.delete(asset_id, account_id):
        raise HTTPException(404, "not tracked")
    return {"ok": True}


@app.get("/api/tracking/{asset_id}/{account_id}/history")
def tracking_history(asset_id: int, account_id: int):
    """Bid timeline (every observed change), the bidders collapsed by id, and
    the other lots those same bidders have been seen leading."""
    from deals import tracking, tracking_store
    row = tracking_store.get(asset_id, account_id)
    observations = tracking_store.history(asset_id, account_id)
    bidders = tracking.bidder_summary(observations)
    rivals = tracking_store.rival_lots([b["bidder_id"] for b in bidders],
                                       exclude=(asset_id, account_id))
    return {"lot": row, "observations": observations, "bidders": bidders, "rivals": rivals}


@app.post("/api/tracking/sync")
async def tracking_sync_now():
    """Poll every open tracked lot right now, ignoring the schedule."""
    from deals import tracking, tracking_store
    adapter = _govdeals_adapter()
    adopted = await asyncio.to_thread(tracking.adopt_favorites, adapter)
    # Force everything due, then run the normal pass.
    await asyncio.to_thread(db.execute, "UPDATE tracked_lots SET next_poll_at = now() WHERE closed_at IS NULL")
    report = await asyncio.to_thread(tracking.sync_tracked, adapter)
    return {"adopted_favorites": adopted, **report}


@app.get("/api/deal-photos/{path:path}")
def deal_photo(path: str):
    """Operator-only proxy for scraped auction photos in the PRIVATE bucket
    (deals/archive.py). /api/ is session-walled; the key must match the exact
    shape the archiver mints, so nothing else in the bucket is reachable."""
    from deals import archive as deals_archive
    try:
        got = deals_archive.fetch_private_photo(path)
    except Exception as e:  # noqa: BLE001 - unconfigured / R2 down
        raise HTTPException(503, f"photo store unavailable: {type(e).__name__}")
    if got is None:
        raise HTTPException(404, "no such photo")
    data, ctype = got
    return Response(data, media_type=ctype,
                    headers={"Cache-Control": "private, max-age=604800",
                             "X-Robots-Tag": "noindex"})


def _deal_images(row: dict) -> list[str]:
    """Ordered image list for a lot: archived copies when we have them
    (admin-proxy URLs into the private bucket), else the GovDeals CDN hero."""
    imgs = [row.get("archived_hero_url") or row.get("hero_image_url")]
    imgs += row.get("gallery_urls") or []
    return [u for u in imgs if u]


@app.get("/api/deals/{asset_id}/{account_id}/{auction_id}")
def deal_lot_json(asset_id: int, account_id: int, auction_id: int):
    """Lot detail for the DealCard component (static/deal_card.js)."""
    row = db.fetch_one("""SELECT asset_id, account_id, auction_id, title, description,
        native_category_name, canonical_category, city, state, seller,
        bid_count, current_bid, opening_bid, currency_code, end_utc,
        outcome, final_bid, final_bid_count, images_archived,
        archived_hero_url, gallery_urls, hero_image_url
        FROM deal_lots WHERE asset_id=%s AND account_id=%s AND auction_id=%s""",
        (asset_id, account_id, auction_id))
    if not row:
        raise HTTPException(status_code=404, detail="lot not tracked")
    images = _deal_images(row)
    for k in ("archived_hero_url", "gallery_urls", "hero_image_url"):
        row.pop(k)
    return {**row, "images": images,
            "image_source": "archived" if row["images_archived"] else "cdn"}


# ── Lot archive (admin-only; recorder/lot_archive.py) ──────────────────────
# The private, permanent copy of every closed GovDeals lot. Every route sits
# under /admin or /api (session-walled) and none of it may ever reach a public
# page: photos are the seller's own, undisguised, streamed from the store.

def _archive_or_503(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except lot_archive_view.ArchiveUnavailable as e:
        raise HTTPException(503, str(e))


@app.get("/admin/archive")
def admin_archive_page():
    """The list lives in the admin shell as the Archive tab."""
    return RedirectResponse("/admin?tab=archive", status_code=303)


def _archive_source_or_404(source: str) -> str:
    """Whitelist ({govdeals, allsurplus}); anything else never reaches the store."""
    if not lot_archive_view.valid_source(source):
        raise HTTPException(404, "unknown archive source")
    return source


@app.get("/admin/archive/{source}/{asset_id}/{account_id}/{auction_id}", response_class=HTMLResponse)
def admin_archive_lot_page(request: Request, source: str, asset_id: int, account_id: int,
                           auction_id: int):
    _archive_source_or_404(source)
    key = lot_archive_view.lot_key(asset_id, account_id, auction_id)
    ctx = _archive_or_503(lot_archive_view.page_context, key, source)
    if ctx is None:
        raise HTTPException(404, "lot not archived")
    return templates.TemplateResponse(request, "archive_lot.html", ctx)


@app.get("/api/archive/lots")
@readcache.cached()
def archive_lots(q: str | None = None, category: str | None = None, outcome: str | None = None,
                 min_price: float | None = None, max_price: float | None = None,
                 since: str | None = None, until: str | None = None,
                 page: int = Query(1, ge=1), per_page: int = Query(50, ge=1, le=200),
                 source: str | None = None):
    if source:
        _archive_source_or_404(source)
    return _archive_or_503(lot_archive_view.list_lots, q=q, category=category, outcome=outcome,
                           min_price=min_price, max_price=max_price, since=since, until=until,
                           page=page, per_page=per_page, source=source)


@app.get("/api/archive/{source}/{asset_id}/{account_id}/{auction_id}")
@readcache.cached()
def archive_lot_json(source: str, asset_id: int, account_id: int, auction_id: int):
    _archive_source_or_404(source)
    key = lot_archive_view.lot_key(asset_id, account_id, auction_id)
    out = _archive_or_503(lot_archive_view.lot_json, key, source)
    if out is None:
        raise HTTPException(404, "lot not archived")
    return out


@app.get("/api/archive/{source}/{asset_id}/{account_id}/{auction_id}/photo/{i}")
def archive_lot_photo(source: str, asset_id: int, account_id: int, auction_id: int, i: int):
    _archive_source_or_404(source)
    key = lot_archive_view.lot_key(asset_id, account_id, auction_id)
    data = _archive_or_503(lot_archive_view.photo_bytes, key, i, source)
    if data is None:
        raise HTTPException(404, "no such photo")
    return Response(data, media_type="image/jpeg",
                    headers={"Cache-Control": "private, max-age=604800",
                             "X-Robots-Tag": "noindex"})


@app.post("/api/archive/{source}/{asset_id}/{account_id}/{auction_id}/analyze")
def archive_lot_analyze(source: str, asset_id: int, account_id: int, auction_id: int):
    """Re-run the LLM analysis now (one lot, a few seconds). An LLM failure is
    returned as status=unavailable, never a default."""
    _archive_source_or_404(source)
    key = lot_archive_view.lot_key(asset_id, account_id, auction_id)
    out = _archive_or_503(lot_archive_view.rerun_analysis, key, source)
    if out.get("error") == "lot is not archived":
        raise HTTPException(404, "lot not archived")
    return out


@app.get("/sell", response_class=HTMLResponse)
async def public_sell(request: Request):
    return templates.TemplateResponse(
        request, "sell.html", _public_ctx({}),
    )


@app.get("/platform", response_class=HTMLResponse)
def public_platform(request: Request):
    """The software behind the store, with a read-only demo (templates/platform.html).

    Deliberately unlinked: chair buyers should not land on a software pitch, so
    this is not in the storefront nav or the sitemap and the page is `noindex`.
    The handler reads nothing — the Deal finder tab calls the existing public
    `/deals/api/*` endpoints from the browser (policy: public_deals.py), the
    other two tabs load invented sample JSON from `static/site/platform/`, and
    the request-access form posts to the existing `/contact`.
    """
    return templates.TemplateResponse(
        request, "platform.html",
        _public_ctx({"platform_sources": list(_PLATFORM_SOURCE_NAMES.values())}),
    )


# Auction sites the recorder knows, in display order. The page renders these
# names with no DB read; /platform/api/sources adds the live numbers.
_PLATFORM_SOURCE_NAMES = {
    "govdeals": "GovDeals",
    "allsurplus": "AllSurplus",
    "gsa": "GSA Auctions",
    "purple_wave": "Purple Wave",
    "public_surplus": "Public Surplus",
    "municibid": "Municibid",
    "mibid": "MiBid",
}
_PLATFORM_SOURCES_TTL = 300        # seconds; one grouped read per 5 min, not per page view
_PLATFORM_LIVE_WINDOW_H = 24


@readcache.cached(ttl=_PLATFORM_SOURCES_TTL)
def _platform_source_rows() -> list[dict]:
    """One grouped read of `listing_snapshots`. Raises on failure, so a failed
    read is never memoised (readcache stores return values only)."""
    return db.fetch_all(
        "SELECT source, count(DISTINCT source_lot_id) AS lots, max(observed_at) AS last_seen "
        "FROM listing_snapshots GROUP BY source"
    )


@app.get("/platform/api/sources")
def public_platform_sources():
    """Auction sites tracked, for the strip under the /platform hero. Public and
    read-only (deliberately not under the auth-walled `/api/`). A site is `live`
    when the recorder observed it in the last 24 hours; anything else is shown
    as paused. A failed read answers names only — never error text."""
    names = dict(_PLATFORM_SOURCE_NAMES)
    try:
        rows = {r["source"]: r for r in _platform_source_rows()}
    except Exception:
        log.warning("platform sources read failed", exc_info=True)
        return {"ok": False, "sources": [{"key": k, "name": n} for k, n in names.items()]}
    for key in rows:
        names.setdefault(key, str(key).replace("_", " ").title())
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    out = []
    for key, name in names.items():
        row = rows.get(key) or {}
        seen = row.get("last_seen")
        if seen is not None and seen.tzinfo is None:
            seen = seen.replace(tzinfo=timezone.utc)
        live = bool(seen and now - seen <= timedelta(hours=_PLATFORM_LIVE_WINDOW_H))
        out.append({
            "key": key, "name": name, "live": live,
            "lots": int(row.get("lots") or 0),
            "last_seen": seen.isoformat() if seen else None,
        })
    return {"ok": True, "tracked": len(out), "live": sum(1 for s in out if s["live"]), "sources": out}


@app.get("/platform/api/sites")
def public_platform_sites():
    """Every auction site in scope: `live` / `paused` (an adapter exists) or
    `planned` (roadmap, nothing scraped, `lots: null`). Public, read-only, shares
    the memoised read behind /platform/api/sources. 503 when that read fails —
    never a guessed status. Policy: platform_api.py."""
    try:
        return platform_api.sites(_platform_source_rows)
    except Exception:
        log.warning("platform sites read failed", exc_info=True)
        return JSONResponse({"ok": False, "error": "unavailable"}, status_code=503)


@app.get("/platform/api/auctions")
def public_platform_auctions(
    q: str | None = None, site: str | None = None, category: str | None = None,
    state: str | None = None, status: str = "open", no_bids: str | None = None,
    max_bid: str | None = None, ending: str | None = None, sort: str = "ending",
    page: int = 1, per_page: int = 50,
):
    """One auctions view across every site the recorder observes. Public and
    read-only (deliberately not under the auth-walled `/api/`); the read model,
    its allow-list and the public_deals exclusions live in platform_api.py."""
    return platform_api.auctions(
        q=q, site=site, category=category, state=state, status=status, no_bids=no_bids,
        max_bid=max_bid, ending=ending, sort=sort, page=page, per_page=per_page)


# ── liquidator-facing pages (feat/liquidator-platform-ui) ─────────────────
# Same stance as /platform: direct URL only (noindex, not in the nav, footer or
# sitemap), handlers read nothing from the database. Every sample is labelled
# as sample in the server-rendered HTML; plain-English search is only "coming".
_BANKRUPTCY_SAMPLE_PATH = STATIC_DIR / "site" / "platform" / "bankruptcies.sample.json"


def _bankruptcy_samples() -> list[dict]:
    """The INVENTED bankruptcy fixture (static JSON, no DB). Read per call —
    it is one small file and the pages that use it are not hot."""
    try:
        data = json.loads(_BANKRUPTCY_SAMPLE_PATH.read_text())
        return list(data.get("filings") or [])
    except (OSError, ValueError):
        log.warning("bankruptcy sample fixture unreadable", exc_info=True)
        return []


def _platform_site_rows() -> list[dict]:
    """Every site name the platform read model knows, with no DB read: the
    adapter-backed ones (`live`/`paused` is decided in the browser from
    /platform/api/sites) and the planned ones, which never show as scraped."""
    rows = [{"key": k, "name": n, "kind": kind, "planned": False}
            for k, (n, kind) in platform_api.SITES.items()]
    rows += [{"key": k, "name": n, "kind": kind, "planned": True}
             for k, (n, kind) in platform_api.PLANNED_SITES.items()]
    return rows


@app.get("/liquidators", response_class=HTMLResponse)
def public_liquidators(request: Request):
    """Landing page for liquidation companies: find distressed businesses first
    (lead side) and sell lots to the buyer network (sell side). The sample
    lead table is the invented fixture, rendered server-side and labelled."""
    return templates.TemplateResponse(
        request, "liquidators.html",
        _public_ctx({"sample_filings": _bankruptcy_samples()[:6]}),
    )


@app.get("/platform/bankruptcies", response_class=HTMLResponse)
def public_platform_bankruptcies(request: Request):
    """Searchable SAMPLE bankruptcy table + filters + an "Ask AI" box that is
    UI only (it answers a coming-soon state, no network). Data: the static
    fixture, fetched by the browser; nothing here is a court record."""
    return templates.TemplateResponse(
        request, "platform_bankruptcies.html", _public_ctx({}),
    )


@app.get("/platform/deals", response_class=HTMLResponse)
def public_platform_deals(request: Request):
    """Unified deal feed across every auction site the recorder observes. The
    browser reads the policy-gated /platform/api/auctions (+ /platform/api/sites
    for each site's true status); the contract carries no photos (public_deals
    policy) so none can render. Planned sites are named and labelled planned."""
    return templates.TemplateResponse(
        request, "platform_deals.html", _public_ctx({"sites": _platform_site_rows()}),
    )


@app.get("/robots.txt", response_class=PlainTextResponse)
async def robots_txt():
    return (
        "User-agent: *\n"
        "Disallow: /admin\n"
        "Disallow: /api/\n"
        "Disallow: /deals\n"
        "Disallow: /reserve/\n"
        "Allow: /\n"
        "\n"
        f"Sitemap: {PUBLIC_BASE_URL}/sitemap.xml\n"
    )


def _sitemap_entry(loc: str, lastmod: str | None = None) -> str:
    tag = f"  <url>\n    <loc>{loc}</loc>\n"
    if lastmod:
        tag += f"    <lastmod>{lastmod}</lastmod>\n"
    return tag + "  </url>\n"


@app.get("/sitemap.xml")
def sitemap_xml():
    return Response(content=_sitemap_body(), media_type="application/xml")


@readcache.cached(ttl=300)
def _sitemap_body() -> str:
    """Crawlers re-fetch the sitemap constantly; two inventory queries per hit
    is wasted pooler time. Any write through the admin API drops the memo."""
    body = '<?xml version="1.0" encoding="UTF-8"?>\n'
    body += '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
    for path in ("/", "/listings", "/map", "/chairs", "/sell", "/about", "/terms", "/privacy"):
        body += _sitemap_entry(f"{PUBLIC_BASE_URL}{path}")
    # City pages with at least one live lot (sold-only cities are noindex).
    for rec in city_pages.listing(indexable_only=True):
        body += _sitemap_entry(f"{PUBLIC_BASE_URL}{rec['path']}")
    # Sold lots are indexable too (BLACKWHOLE-29): "500 banquet chairs Atlanta"
    # should land on our archive page and convert into a next-lot inquiry.
    for row in [*inventory.list_public(), *inventory.list_sold_showcase()]:
        if _canonical_twin(row):
            continue  # its page points Google at the live lot; don't submit it
        updated = row.get("updated_at")
        lastmod = None
        if updated is not None:
            # timestamptz comes back as datetime from Postgres; sitemap wants a date
            lastmod = updated.date().isoformat() if hasattr(updated, "date") else str(updated)[:10]
        body += _sitemap_entry(f"{PUBLIC_BASE_URL}{lot_urls.public_path(row)}", lastmod)
    body += "</urlset>\n"
    return body


@app.get("/catalog/facebook.csv")
def facebook_catalog_feed():
    """Facebook Business product-catalog feed (BLACKWHOLE-7).

    Public, read-only, no secrets — Commerce Manager pulls this URL on a
    schedule. One row per sellable lot (see `inventory.list_catalog_feed` for
    the status/quantity filter; `catalog_feed` drops rows FB would reject).
    Every product's `link` points back to `/listings/{lot_id}`. Operator setup
    is in docs/fb_catalog_feed_runbook.md.
    """
    body = catalog_feed.rows_to_csv(inventory.list_catalog_feed())
    return PlainTextResponse(body, media_type="text/csv; charset=utf-8")


@app.get("/catalog/google.csv")
def google_catalog_feed():
    """Google Merchant Center product feed (multichannel Phase 2, D7).

    Public, read-only, no secrets — Merchant Center fetches this URL on a
    daily schedule. Same status/quantity gate as the FB feed
    (`inventory.list_catalog_feed`); `google_feed` drops rows Google would
    reject. Operator setup: docs/google_merchant_runbook.md.
    """
    body = google_feed.rows_to_csv(inventory.list_catalog_feed())
    return PlainTextResponse(
        body,
        media_type="text/csv; charset=utf-8",
        headers={"Cache-Control": "public, max-age=900"},
    )


async def _notify_new_inquiry(row: dict) -> None:
    # Fire-and-forget: a customer reaching out is the highest-value ping we
    # get — but a lost Telegram send must never fail the contact form.
    try:
        kind = (row.get("kind") or "buy").strip()
        bits = [f"🟢 NEW LEAD · {kind} #{row['id']}"]
        contact = " / ".join(x for x in (row.get("email"), row.get("phone")) if x)
        who = row.get("name") or "—"
        bits.append(f"{who}{(' — ' + contact) if contact else ''}")
        meta = " · ".join(
            str(x) for x in (
                f"qty {row['quantity_interested']}" if row.get("quantity_interested") else None,
                f"lot {row['lot_id']}" if row.get("lot_id") else None,
            ) if x
        )
        if meta:
            bits.append(meta)
        if row.get("message"):
            bits.append(f"“{str(row['message'])[:240]}”")
        bits.append("→ blackwhole.com/admin (Inquiries)")
        await telegram_alerts.send_message("\n".join(bits), topic="leads")
    except Exception:
        pass


_DEPOSIT_ALERT_HEADS = {
    "checkout.session.completed": "💰 DEPOSIT PAID",
    "checkout.session.async_payment_succeeded": "💰 DEPOSIT PAID",
    "checkout.session.async_payment_failed": "✗ ACH FAILED",
    "checkout.session.expired": "✗ CHECKOUT EXPIRED",
    "charge.refunded": "↩ REFUNDED",
}


def _dollars(cents: Any) -> str:
    return f"${(int(cents or 0) / 100):,.2f}"


async def _notify_deposit(row: dict, event_type: str) -> None:
    """Money moved — tell the operator. Best-effort, exactly like the lead ping.

    Only called when `deposits.transition()` reported `changed=True`, so a
    Stripe retry of an already-applied event stays silent.
    """
    try:
        status = (row or {}).get("status") or ""
        head = _DEPOSIT_ALERT_HEADS.get(event_type, "◉ DEPOSIT UPDATE")
        # A completed session that only reached 'processing' is an ACH debit in
        # flight, not money in the bank — say so rather than crying "PAID".
        if status == "processing":
            head = "🏦 ACH INITIATED"
        elif status == "failed":
            head = "✗ ACH FAILED"
        elif status == "refunded":
            head = "↩ REFUNDED"
        elif status == "canceled":
            head = "✗ CHECKOUT EXPIRED"

        kind = (row.get("kind") or "deposit").strip()
        bits = [f"{head} · #{row.get('id')}"]
        bits.append(
            f"{_dollars(row.get('amount_cents'))} "
            f"({'deposit' if kind == 'deposit' else 'paid in full'})"
            + (f" · via {row['payment_method']}" if row.get("payment_method") else "")
        )
        lot_id = row.get("lot_id")
        qty = row.get("quantity")
        lot_line = f"{PUBLIC_BASE_URL}/listings/{lot_id}" if lot_id else "—"
        bits.append(f"lot {lot_id or '—'} × {qty or '—'} — {lot_line}")

        contact = " / ".join(
            x for x in (row.get("buyer_email"), row.get("buyer_phone")) if x
        )
        who = row.get("buyer_name") or "—"
        bits.append(f"{who}{(' — ' + contact) if contact else ''}")

        if row.get("failure_reason"):
            bits.append(f"reason: {row['failure_reason']}")

        if status == "paid":
            bits.append(
                "⚠ inventory NOT auto-decremented — adjust qty on the Inventory tab"
            )
        bits.append(f"→ {PUBLIC_BASE_URL}/admin (Deposits)")
        await telegram_alerts.send_message("\n".join(bits), topic="leads")
    except Exception:
        pass


@app.post("/contact")
async def public_contact(payload: dict):
    payload = payload or {}
    attr = attribution.from_payload(payload)   # pure: no I/O on the loop
    try:
        row = await asyncio.to_thread(lambda: inventory.create_inquiry(
            attribution=attr,
            kind=(payload.get("kind") or "buy").strip(),
            name=(payload.get("name") or "").strip(),
            email=(payload.get("email") or "").strip() or None,
            phone=(payload.get("phone") or "").strip() or None,
            message=(payload.get("message") or "").strip() or None,
            lot_id=(payload.get("lot_id") or "").strip() or None,
            quantity_interested=(
                int(payload["quantity_interested"])
                if payload.get("quantity_interested")
                else None
            ),
        ))
    except ValueError as e:
        raise HTTPException(400, str(e))
    asyncio.create_task(_notify_new_inquiry(row))
    return {"ok": True, "id": row["id"]}


async def _notify_new_subscriber(row: dict) -> None:
    # Fire-and-forget: a lost ping must never surface as a failed signup.
    try:
        bits = [f"◉ NEW ALERTS SIGNUP #{row['id']}"]
        contact = " / ".join(x for x in (row.get("email"), row.get("phone")) if x)
        who = row.get("name") or "—"
        bits.append(f"{who} — {contact}")
        geo = " ".join(x for x in (row.get("city"), row.get("state"), row.get("zip_code")) if x)
        prefs = " · ".join(
            str(x) for x in (
                geo or None,
                f"qty {row['quantity_wanted']}" if row.get("quantity_wanted") else None,
                row.get("use_case"), row.get("chair_type"), row.get("timeline"),
                row.get("budget_per_chair"), row.get("delivery"),
            ) if x
        )
        if prefs:
            bits.append(prefs)
        if row.get("notes"):
            bits.append(f"“{row['notes']}”")
        await telegram_alerts.send_message("\n".join(bits), topic="leads")
    except Exception:
        pass


@app.post("/subscribe")
async def public_subscribe(payload: dict):
    payload = payload or {}
    attr = attribution.from_payload(payload)   # pure: no I/O on the loop
    try:
        row = await asyncio.to_thread(lambda: inventory.create_subscriber(
            attribution=attr,
            name=(payload.get("name") or "").strip() or None,
            email=(payload.get("email") or "").strip() or None,
            phone=(payload.get("phone") or "").strip() or None,
            city=(payload.get("city") or "").strip() or None,
            state=(payload.get("state") or "").strip() or None,
            zip_code=(payload.get("zip_code") or "").strip() or None,
            quantity_wanted=(
                int(payload["quantity_wanted"])
                if payload.get("quantity_wanted")
                else None
            ),
            use_case=(payload.get("use_case") or "").strip() or None,
            chair_type=(payload.get("chair_type") or "").strip() or None,
            timeline=(payload.get("timeline") or "").strip() or None,
            budget_per_chair=(payload.get("budget_per_chair") or "").strip() or None,
            delivery=(payload.get("delivery") or "").strip() or None,
            notes=(payload.get("notes") or "").strip() or None,
            source=(payload.get("source") or "site_listings").strip(),
        ))
    except (ValueError, TypeError) as e:
        raise HTTPException(400, str(e))
    asyncio.create_task(_notify_new_subscriber(row))
    return {"ok": True, "id": row["id"]}


EVENT_PER_IP_LIMIT = 60   # per hour; a real person clicks a phone number once


@app.post("/event")
def public_event(payload: dict, request: Request):
    """Click-only lead events (a `tel:` / `mailto:` link in the footer).

    Those clicks never reach the server on their own, so site.js posts a
    beacon here and it lands in `site_visits` as `/_event/<kind>` with the
    visitor's first-touch attribution. Public path (outside `/api/`), bot
    UAs dropped, per-IP rate-limited; the insert runs on the visits daemon
    thread so this handler is a plain `def` and never touches the DB itself.
    """
    ip = rate_limit.client_ip(request)
    if not rate_limit.allow(f"event:{ip}", limit=EVENT_PER_IP_LIMIT):
        raise HTTPException(429, "rate_limited")
    ok = visits.track_event(request, payload or {})
    if ok is None:
        raise HTTPException(400, "unknown event kind")
    return {"ok": bool(ok)}


# ── Freight estimate (public, self-serve) ────────────────────────────────────
# A buyer gives a ZIP, an email and a phone on a lot page and gets an honest
# RANGE. Public paths, deliberately outside `/api/` (auth.py's
# PROTECTED_PREFIXES), same as /contact and /reserve.
#
# THE HARD RULE: never invent a number. An unquotable lane (international,
# offshore/Alaska, unresolvable ZIP, or a lot whose origin we can't locate)
# returns HTTP 200 with `ok: false`. It does NOT guess, and it does not 500 — a
# lane we can't price is a normal outcome of a public form, not an error.
#
# EVERY REQUEST IS A LEAD. Contact details come first and the request is written
# to `freight_quotes` whether or not it could be priced, so the Sales tab shows
# it and nothing depends on somebody reading a Telegram ping. (Before 2026-10
# the email was an optional second step and 4 of 5 requests were anonymous.)
#
# The number the buyer sees is the in-house estimator's. Real carrier prices
# (automation/warp_rates.py) are fetched afterwards, in the background, for the
# operator only.

FREIGHT_UNQUOTABLE = {
    "ok": False,
    "reason": "unquotable",
    "saved": True,
    "message": (
        "We'll quote this lane by hand and come back to you with a real number "
        "at the email and phone you gave us."
    ),
}

# The same lane, but nothing was written (pre-021 schema, or the insert failed).
# The buyer must NOT be told "got it": the contact form below is the durable
# path (it writes `inquiries`), so send them there.
FREIGHT_UNQUOTABLE_NOT_SAVED = {
    "ok": False,
    "reason": "unquotable",
    "saved": False,
    "message": (
        "We can't price this lane automatically. Send the request with the form "
        "below and we'll come back with a real number."
    ),
}

FREIGHT_NOT_SAVED = {
    "ok": False,
    "reason": "not_saved",
    "message": (
        "We couldn't save your request just now. Please try again in a minute, "
        "or send it with the form below."
    ),
}

# What the estimate is and isn't. Shipped with every quote so the widget can't
# drift from the terms, and so a screenshot of the number carries its caveats.
FREIGHT_FRAMING = {
    "estimate_only": True,
    "residential_liftgate_included": True,
    "chair_price_separate": True,
    "pickup_free": True,
}

# Sanity ceiling on a requested quantity. Bigger than any real lot (the largest
# to date is ~4,900) and small enough that a fat-fingered 9-digit number can't
# turn into a nonsense weight.
FREIGHT_MAX_QTY = 10_000

# Strong refs for fire-and-forget work. The event loop only holds a WEAK
# reference to a task, so a bare `create_task(...)` can be collected mid-flight
# — a real risk for the carrier check, which waits ~20-45 s on Warp.
_FREIGHT_TASKS: set[asyncio.Task] = set()


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _FREIGHT_TASKS.add(task)
    task.add_done_callback(_FREIGHT_TASKS.discard)


# ── carrier-check plumbing ──
# A Warp call blocks a thread for ~20-45 s. On asyncio's DEFAULT executor that
# would compete with every `asyncio.to_thread` DB hop in this file (the pool is
# min(32, cpu+4) threads), so 20 quotes from one caller could stall the whole
# app. Carrier checks get their own two threads instead, a short queue, an
# hourly budget, and a per-lane memo — the worst a flood can do is leave rows
# "not checked yet" (the Sales tab has a button for that).
_CARRIER_POOL = concurrent.futures.ThreadPoolExecutor(
    max_workers=2, thread_name_prefix="carrier-check"
)
CARRIER_QUEUE_MAX = 8          # background checks waiting or running; more are skipped
# Warp allows 60 keyless calls an hour. Stay under it so a burst of quotes can
# never burn the whole hour and leave real leads without prices. Raise it with
# `WARP_RATES_PER_HOUR` once a key (10,000/h) is in place.
CARRIER_CALLS_PER_HOUR = int(os.getenv("WARP_RATES_PER_HOUR", "40"))
CARRIER_LANE_TTL_SEC = 6 * 3600
CARRIER_LANE_CACHE_MAX = 200
_carrier_lock = threading.Lock()
_carrier_pending = 0
_carrier_lane_cache: dict[tuple, tuple[float, dict]] = {}
_carrier_budget = [0.0, 0]     # [window_end_epoch, calls_in_window]


class CarrierBudgetExceeded(Exception):
    """This hour's outbound Warp calls are used up."""


def _carrier_lane_get(key: tuple) -> dict | None:
    with _carrier_lock:
        hit = _carrier_lane_cache.get(key)
        if hit and hit[0] > time.time():
            return dict(hit[1])
        _carrier_lane_cache.pop(key, None)
        return None


def _carrier_lane_put(key: tuple, summary: dict) -> None:
    with _carrier_lock:
        if len(_carrier_lane_cache) >= CARRIER_LANE_CACHE_MAX:
            oldest = min(_carrier_lane_cache, key=lambda k: _carrier_lane_cache[k][0])
            _carrier_lane_cache.pop(oldest, None)
        _carrier_lane_cache[key] = (time.time() + CARRIER_LANE_TTL_SEC, dict(summary))


def _carrier_budget_take() -> bool:
    """Count one outbound Warp call; False when this hour's budget is spent.

    Its own locked counter rather than `rate_limit.allow`: this runs on worker
    threads, and that module's dict is only safe from the event loop.
    """
    now = time.time()
    with _carrier_lock:
        if now >= _carrier_budget[0]:
            _carrier_budget[0] = (now // 3600 + 1) * 3600
            _carrier_budget[1] = 0
        if _carrier_budget[1] >= CARRIER_CALLS_PER_HOUR:
            return False
        _carrier_budget[1] += 1
        return True


def _carrier_reset() -> None:
    """Tests only: forget the lane memo, the queue depth and the hour's count."""
    global _carrier_pending
    with _carrier_lock:
        _carrier_lane_cache.clear()
        _carrier_pending = 0
        _carrier_budget[0], _carrier_budget[1] = 0.0, 0


def _freight_origin_zip(row: dict) -> str | None:
    """Where this lot ships FROM. Server-side only — never client-supplied.

    Origin is the one input a buyer must not control: letting them pass it
    would turn the endpoint into a free general-purpose freight calculator and
    make every logged lane a lie. Falls back to the state capital's ZIP when a
    lot has no ZIP on file (±a state's width, which the range already absorbs);
    no ZIP and no known state means no quote.

    Normalization goes through the estimator's own `_resolve_zip` rather than a
    second hand-rolled `zfill(5)` here — one module decides what a ZIP is.
    """
    zip_code = freight_estimate._resolve_zip(row.get("zip_code"))
    if zip_code:
        return zip_code
    state = (row.get("state") or "").strip().upper()
    return freight_estimate.STATE_CENTER_ZIP.get(state)


def _freight_default_qty(row: dict) -> int:
    """What to quote when the buyer doesn't say — the whole lot, basically."""
    for key in ("quantity_remaining", "quantity_original"):
        try:
            qty = int(row.get(key) or 0)
        except (TypeError, ValueError):
            continue
        if qty > 0:
            return min(qty, FREIGHT_MAX_QTY)
    return 1


def _freight_rate_ok(request: Request) -> None:
    """429 unless this caller (and the site as a whole) is under the hour's cap."""
    ip = rate_limit.client_ip(request)
    if not rate_limit.allow(f"freight:{ip}", limit=rate_limit.FREIGHT_PER_IP_LIMIT):
        log.warning("freight estimate rate-limited (per-ip) ip=%s", ip)
        raise HTTPException(429, "rate_limited")
    if not rate_limit.allow("freight:global", limit=rate_limit.FREIGHT_GLOBAL_LIMIT):
        log.warning("freight estimate rate-limited (global) ip=%s", ip)
        raise HTTPException(429, "rate_limited")


def _freight_range(quote: dict, low_key: str, high_key: str) -> dict | None:
    low, high = quote.get(low_key), quote.get(high_key)
    if low is None or high is None:
        return None
    return {"low": low, "high": high}


def _freight_public_estimate(quote: dict) -> dict:
    """The subset of the estimator's dict a browser may see.

    `raw` (calibration constants, NMFC class) stays server-side: it's the audit
    trail for a quote, not a spec sheet for a competitor, and every one of
    those knobs is tunable-by-us guesswork.
    """
    return {
        "mode": quote.get("mode"),
        "recommended_mode": quote.get("recommended_mode"),
        "ltl": _freight_range(quote, "ltl_low", "ltl_high"),
        "partial": _freight_range(quote, "partial_low", "partial_high"),
        "miles": quote.get("miles"),
        "transit_days": quote.get("transit_days"),
        "valid_until": quote.get("valid_until"),
    }


def _freight_range_str(quote: dict) -> str:
    mode = quote.get("recommended_mode") or quote.get("mode") or "ltl"
    rng = _freight_range(quote, f"{mode}_low", f"{mode}_high") or _freight_range(
        quote, "ltl_low", "ltl_high"
    )
    if not rng:
        return "—"
    return f"${rng['low']:,.0f}–${rng['high']:,.0f} ({mode})"


def _looks_like_email(value: str) -> bool:
    """Cheap plausibility check — the real validation is whether it bounces."""
    if not value or len(value) > 254 or any(c.isspace() for c in value):
        return False
    if any(c in value for c in '?#<>"'):
        # `a@b.co?bcc=x@evil.io` would pre-fill a bcc when the operator clicks
        # the mailto: link on the Sales tab. (An apostrophe stays legal —
        # o'brien@… is a real address.)
        return False
    local, _, domain = value.partition("@")
    return bool(local) and "." in domain and not domain.startswith(".") \
        and not domain.endswith(".")


def _clean_phone(value: Any) -> str:
    """A US phone as 10 bare digits, or ValueError.

    Takes whatever a person types — ``(404) 555-0100``, ``+1 404.555.0100``,
    ``404 555 0100 x12`` is rejected (extension digits make it 12) — strips
    everything that isn't a digit, drops a leading country ``1``, and insists
    on a real NANP shape: ten digits, area code and exchange not starting with
    0 or 1. Same spirit as `_looks_like_email`: plausibility, not proof.
    """
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10 or digits[0] in "01" or digits[3] in "01":
        raise ValueError("invalid phone")
    return digits


def _safe_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _pretty_phone(digits: str | None) -> str:
    d = digits or ""
    return f"({d[:3]}) {d[3:6]}-{d[6:]}" if len(d) == 10 and d.isdigit() else d


_ZIP_PLUS4 = re.compile(r"^(\d{5})(?:[-\s]?\d{4})$")


def _freight_dest_zip(value: Any) -> str:
    """The buyer's destination as typed, with ZIP+4 trimmed to the 5-digit ZIP.

    Anything else is passed through untouched — a Canadian postal code or a typo
    is for the estimator to refuse (and for the row to record), not for this
    function to silently "fix".
    """
    raw = str(value or "").strip()
    m = _ZIP_PLUS4.match(raw)
    return m.group(1) if m else raw[:32]


_ZIP5 = re.compile(r"^\d{5}$")


async def _notify_freight_estimate(
    row: dict,
    quote: dict | None,
    *,
    dest_zip: str,
    quantity: int,
    quote_id: int | None,
    email: str | None = None,
    phone: str | None = None,
    reason: str | None = None,
    available: int | None = None,
) -> None:
    """One ping per request, carrying everything needed to answer it.

    The contact details ride in the message on purpose: if the row could not be
    written (`quote_id` is None) this ping is the only record of the lead.
    Best-effort like `_notify_new_inquiry` — a dead Telegram must never surface
    as a failed estimate — but a failed send is LOGGED, not swallowed.
    """
    try:
        lot_id = row.get("lot_id") or "—"
        head = f"🚚 FREIGHT REQUEST · lot {lot_id}"
        if quote_id:
            head += f" · #{quote_id}"
        bits = [head]
        if quote:
            bits.append(f"{quantity} chairs → {dest_zip} · {_freight_range_str(quote)}")
            bits.append(
                f"~{quote.get('miles')} mi · ~{quote.get('transit_days')} days · "
                f"via {quote.get('provider') or 'estimator'}"
            )
        else:
            bits.append(f"{quantity} chairs → {dest_zip or '—'} · NO PRICE — quote by hand")
            if reason:
                bits.append(f"why: {reason}")
        contact = " · ".join(x for x in (email, _pretty_phone(phone)) if x)
        if contact:
            bits.append(f"📇 {contact}")
        if available is not None:
            bits.append(f"⚠ asked for {quantity}, lot has {available}")
        if not quote_id:
            bits.append("⚠ NOT SAVED to the database — this message is the only record")
        bits.append(f"→ {PUBLIC_BASE_URL}/admin?tab=quotes")
        ok, err = await telegram_alerts.send_message("\n".join(bits), topic="leads")
        if not ok:
            _log_unsent_alert(err, lot_id, quote_id, dest_zip, quantity, email, phone)
    except Exception as e:
        _log_unsent_alert(
            repr(e), row.get("lot_id"), quote_id, dest_zip, quantity, email, phone
        )


def _log_unsent_alert(err, lot_id, quote_id, dest_zip, quantity, email, phone) -> None:
    """The alert did not go out. If the row was not written either, this log
    line is the ONLY place the lead exists — so it carries the contact details.
    With a row on file the Sales tab has them, and they stay out of the log."""
    if quote_id:
        log.warning(
            "freight request alert not sent (%s): lot=%s quote_id=%s", err, lot_id, quote_id
        )
    else:
        log.error(
            "FREIGHT LEAD NOT SAVED AND NOT ALERTED (%s): lot=%s dest=%s qty=%s "
            "email=%s phone=%s", err, lot_id, dest_zip, quantity, email, phone,
        )


async def _notify_freight_email(quote_id: int, email: str) -> None:
    """Legacy second step (see `/freight-estimate/email`)."""
    try:
        ok, err = await telegram_alerts.send_message(
            f"📧 FREIGHT LEAD · quote #{quote_id} → {email}", topic="leads"
        )
        if not ok:
            log.warning("freight email alert not sent (%s): quote_id=%s", err, quote_id)
    except Exception:
        log.warning("freight email alert failed", exc_info=True)


def _run_carrier_check(quote_id: int) -> dict | None:
    """Fetch real carrier prices for one stored request and save the summary.

    Blocking (a DB read, a ~20-45 s HTTP call, a DB write) — callers run it in a
    worker thread. Returns the summary that was stored, or None when there was
    nothing to check (unknown id, or a request with no priced lane).
    """
    if not freight_log.schema_ready():
        # Nowhere to store the answer before migration 021 — don't spend a
        # 20-45 s Warp call (and one of this hour's calls) on a result we drop.
        return None
    quote = freight_log.get_quote(quote_id)
    if not quote or quote.get("unquotable_reason") or not quote.get("origin_zip"):
        return None
    if not _ZIP5.match(str(quote.get("dest_zip") or "")):
        return None
    lot = inventory.get(quote["lot_id"]) if quote.get("lot_id") else None
    cal = freight_estimate.calibration_from_row(lot)
    quantity = int(quote.get("quantity") or 0)
    pallets = warp_rates.pallets_for(quantity, cal.chairs_per_pallet)
    if pallets <= 0:
        return None
    if pallets > warp_rates.MAX_LTL_PALLETS:
        summary = warp_rates.unavailable("too_big")
    else:
        weight = warp_rates.weight_per_pallet(quantity, pallets, cal.lbs_per_chair)
        lane = (quote["origin_zip"], quote["dest_zip"], pallets, weight, cal.pallet_height_in)
        summary = _carrier_lane_get(lane)
        if summary is None:
            # One shared hourly budget for automatic AND manual checks.
            if not _carrier_budget_take():
                raise CarrierBudgetExceeded(
                    f"{CARRIER_CALLS_PER_HOUR} carrier checks used this hour"
                )
            try:
                summary = warp_rates.summarize(warp_rates.market_options(
                    quote["origin_zip"],
                    quote["dest_zip"],
                    pallets=pallets,
                    weight_lbs_per_pallet=weight,
                    height_in=cal.pallet_height_in,
                ))
            except warp_rates.WarpUnavailable as e:
                log.warning("carrier check failed for quote %s: %s", quote_id, e)
                summary = warp_rates.unavailable("error")
            if summary.get("carrier_status") == "ok":
                _carrier_lane_put(lane, summary)   # same lane again ⇒ no second call
    freight_log.set_carrier_result(quote_id, summary)
    readcache.invalidate_all()   # the Sales tab memo may hold the row without prices
    return summary


async def _carrier_check_task(quote_id: int) -> None:
    """Background wrapper: never raises, never blocks the event loop, never
    touches the default executor, and gives up rather than queue without bound
    (the row stays "not checked yet"; the Sales tab can re-run it)."""
    global _carrier_pending
    if not warp_rates.enabled():
        return
    with _carrier_lock:
        if _carrier_pending >= CARRIER_QUEUE_MAX:
            log.warning("carrier check skipped for quote %s: queue full", quote_id)
            return
        _carrier_pending += 1
    try:
        await asyncio.get_running_loop().run_in_executor(
            _CARRIER_POOL, _run_carrier_check, quote_id
        )
    except CarrierBudgetExceeded as e:
        log.warning("carrier check skipped for quote %s: %s", quote_id, e)
    except Exception:
        log.warning("carrier check crashed for quote %s", quote_id, exc_info=True)
    finally:
        with _carrier_lock:
            _carrier_pending -= 1


@app.post("/freight-estimate")
async def public_freight_estimate(payload: dict, request: Request):
    """`{lot_id, dest_zip, quantity?, email, phone}` → a freight range for the lane.

    Email and phone are required: this is a quote request, and a request nobody
    can answer is not worth storing.
    """
    _freight_rate_ok(request)
    payload = payload or {}
    attr = attribution.from_payload(payload)   # pure: no I/O on the loop
    lot_id = str(payload.get("lot_id") or "").strip()
    dest_zip = _freight_dest_zip(payload.get("dest_zip"))

    # Contact details first: pure checks, no I/O. Everything after this point
    # has a way to reach the buyer, so every failure below can still be a lead.
    email = str(payload.get("email") or "").strip()
    try:
        if not _looks_like_email(email):
            raise ValueError("valid email required")
        phone = _clean_phone(payload.get("phone"))
    except ValueError as e:
        # Also what a lot page left open across the deploy gets (its old script
        # posts no contact details): leave a trace of the attempt.
        log.warning(
            "freight request refused (no usable contact): lot=%s dest=%s qty=%s",
            lot_id, dest_zip, payload.get("quantity"),
        )
        raise HTTPException(
            400, "valid email required" if "email" in str(e) else "valid phone required"
        )

    try:
        row = await asyncio.to_thread(inventory.get, lot_id) if lot_id else None
    except Exception:
        # The database did not answer. The buyer's details exist only in this
        # request — page the operator with them rather than answering 500.
        log.warning("freight request: lot lookup failed for %s", lot_id, exc_info=True)
        _spawn(
            _notify_freight_estimate(
                {"lot_id": lot_id}, None, dest_zip=dest_zip,
                quantity=_safe_int(payload.get("quantity")), quote_id=None,
                email=email, phone=phone, reason="database unavailable",
            )
        )
        return dict(FREIGHT_NOT_SAVED)
    if not row or row.get("status") == "hidden" or inventory.is_sold(row):
        # A sold lot has nothing to ship; quoting freight on it would be a
        # promise we can't keep.
        raise HTTPException(404, "listing not found")

    if payload.get("quantity") in (None, ""):
        quantity = _freight_default_qty(row)
    else:
        try:
            quantity = int(payload["quantity"])
        except (TypeError, ValueError):
            raise HTTPException(400, "quantity must be a number")
        quantity = max(1, min(quantity, FREIGHT_MAX_QTY))

    origin_zip = _freight_origin_zip(row)

    quote: dict | None = None
    reason: str | None = None
    if not origin_zip:
        # We don't know where the lot is. Better a hand quote than a lane
        # measured from nowhere.
        reason = "lot has no origin ZIP or state"
    elif not _ZIP5.match(dest_zip):
        # The estimator zero-pads short ZIPs (right for an origin stored as a
        # number, wrong for a buyer's typo): "3003" would be priced as 03003,
        # New Hampshire. Not exactly five digits ⇒ not priced.
        reason = f"destination {dest_zip!r} is not a 5-digit US ZIP"
    else:
        try:
            # Pure arithmetic over a committed lookup table — microseconds, no
            # I/O, so it runs inline rather than paying for a thread hop.
            quote = freight_estimate.get_freight_estimate(
                origin_zip, dest_zip, quantity,
                cal=freight_estimate.calibration_from_row(row),
            )
        except freight_estimate.FreightUnavailable as e:
            reason = str(e)[:200]

    # Asking for more than the lot holds is still a lead — quote what was asked,
    # record the stock at that moment, and tell both sides.
    try:
        remaining = int(row.get("quantity_remaining"))
    except (TypeError, ValueError):
        remaining = None
    available = remaining if (remaining is not None and 0 < remaining < quantity) else None

    quote_id = await asyncio.to_thread(
        freight_log.insert_storefront_quote,
        lot_id=row.get("lot_id") or lot_id,
        origin_zip=origin_zip,
        dest_zip=dest_zip,
        quantity=quantity,
        quote=quote,
        buyer_email=email,
        buyer_phone=phone,
        client_ip=rate_limit.client_ip(request),
        unquotable_reason=reason,
        lot_quantity_remaining=remaining,
        attribution=attr,
    )
    _spawn(
        _notify_freight_estimate(
            row, quote, dest_zip=dest_zip, quantity=quantity, quote_id=quote_id,
            email=email, phone=phone, reason=reason, available=available,
        )
    )
    if quote_id:
        readcache.invalidate_all()   # this path is outside /api/: drop the memo by hand

    if quote is None:
        # "We'll follow up" only when the request is actually on file. If it
        # was not written (pre-021 schema, or the insert failed) the alert is a
        # best-effort ping, not a record — hand the buyer to the contact form.
        return dict(FREIGHT_UNQUOTABLE if quote_id else FREIGHT_UNQUOTABLE_NOT_SAVED)
    if quote_id is None:
        # The row is the product here. No row ⇒ no price on screen, so the buyer
        # retries or uses the form instead of walking away with a number we
        # have no record of having given.
        return dict(FREIGHT_NOT_SAVED)

    _spawn(_carrier_check_task(quote_id))
    return {
        "ok": True,
        "quote_id": quote_id,
        "estimate": _freight_public_estimate(quote),
        "framing": dict(FREIGHT_FRAMING),
        "available": available,
    }


@app.post("/freight-estimate/email")
async def public_freight_estimate_email(payload: dict, request: Request):
    """LEGACY second step: attach an email to a quote already on screen.

    The widget now asks for contact details before the estimate, so nothing we
    serve calls this. It stays for lot pages that were open in a browser across
    the deploy; remove it once those have aged out.
    """
    _freight_rate_ok(request)
    payload = payload or {}
    try:
        quote_id = int(payload.get("quote_id"))
    except (TypeError, ValueError):
        raise HTTPException(400, "quote_id required")

    email = str(payload.get("email") or "").strip()
    if not _looks_like_email(email):
        raise HTTPException(400, "valid email required")

    saved = await asyncio.to_thread(freight_log.set_quote_email, quote_id, email)
    _spawn(_notify_freight_email(quote_id, email))
    if not saved:
        # Don't tell the buyer "sent" when nothing was written.
        raise HTTPException(503, "could not save that email — try again")
    return {"ok": True}


# ── Reserve with deposit (Stripe Checkout) ───────────────────────────────────
# All of these are PUBLIC paths, deliberately outside `/api/`: `auth.py`'s
# PROTECTED_PREFIXES gate `/admin`, `/api/`, `/screenshot/`, so a buyer can
# still reach Checkout with operator auth switched on. Don't move them.
#
# ROUTE ORDER IS LOAD-BEARING: `/reserve/success` must be registered BEFORE
# `/reserve/{lot_id}` or the path param swallows "success" and every buyer
# coming back from Stripe lands on a 404.


def _reserve_lot_or_404(lot_id: str) -> dict:
    """Dark feature, unknown lot and hidden lot all look the same from outside."""
    if not _reserve_enabled():
        raise HTTPException(404, "not found")
    row = inventory.get(lot_id)
    if not row or row.get("status") == "hidden":
        raise HTTPException(404, "listing not found")
    return row


@app.get("/reserve/success", response_class=HTMLResponse)
async def reserve_success(request: Request, session_id: str = Query("")):
    """Where Stripe drops the buyer after Checkout.

    The redirect races the webhook, and on ACH it beats it by days — so this
    page reads our row and says what's actually true: `paid` => confirmed,
    anything else => "we've got your payment initiated". It never asserts a
    payment landed on the strength of the redirect alone.
    """
    if not _reserve_enabled():
        raise HTTPException(404, "not found")
    row = await asyncio.to_thread(deposits.get_by_session, session_id)
    if not row:
        raise HTTPException(404, "reservation not found")
    return templates.TemplateResponse(
        request, "reserve_success.html",
        _public_ctx({
            "deposit": row,
            "is_paid": row.get("status") == "paid",
            "policy": stripe_gateway.REFUND_POLICY_SHORT,
        }),
    )


@app.get("/reserve/{lot_id}", response_class=HTMLResponse)
async def reserve_page(request: Request, lot_id: str):
    row = _reserve_lot_or_404(lot_id)
    if not _reservable(row):
        # Sold out, unpriced, or nothing left — there's no quote to show, so
        # send them back to the lot page instead of a form that can't submit.
        return RedirectResponse(f"/listings/{lot_id}", status_code=303)
    _decorate(row)
    # deposit_rules() reads site_settings (DB) — off the event loop.
    pct, min_cents = await asyncio.to_thread(deposits.deposit_rules, row)
    return templates.TemplateResponse(
        request, "reserve.html",
        _public_ctx({
            "item": row,
            "hero": row.get("hero_src"),
            "pct": pct,
            "min_cents": min_cents,
            "policy": stripe_gateway.REFUND_POLICY_SHORT,
            "canceled": request.query_params.get("canceled") == "1",
        }),
    )


@app.post("/reserve/{lot_id}/checkout")
async def reserve_checkout(lot_id: str, payload: dict):
    """Turn a quantity into a Stripe Checkout URL.

    Everything the client sends is a *request*, not a fact. The quantity is
    re-bounded against `quantity_remaining`, and the amount is re-derived from
    the lot's own price by `deposits.quote_for_lot` — an `amount` field in the
    payload is read by nobody. The row is written BEFORE the session so a
    session we never hear about still has something to reconcile against.
    """
    row = _reserve_lot_or_404(lot_id)
    if not _reservable(row):
        raise HTTPException(400, "lot is not reservable")
    payload = payload or {}
    attr = attribution.from_payload(payload)   # pure: no I/O on the loop

    remaining = int(row.get("quantity_remaining") or 0)
    try:
        quantity = int(payload.get("quantity"))
    except (TypeError, ValueError):
        raise HTTPException(400, "quantity required")
    if quantity < 1 or quantity > remaining:
        raise HTTPException(400, f"quantity must be between 1 and {remaining}")

    kind = (payload.get("kind") or "deposit").strip()
    if kind not in deposits.DEPOSIT_KINDS:
        raise HTTPException(400, "kind must be 'deposit' or 'full'")

    # Same contact rule as the contact form: a name plus at least one way to
    # reach them. A deposit we can't chase to a pickup is worse than no deposit.
    name = (payload.get("name") or "").strip()
    email = (payload.get("email") or "").strip() or None
    phone = (payload.get("phone") or "").strip() or None
    if not name:
        raise HTTPException(400, "name required")
    if not email and not phone:
        raise HTTPException(400, "email or phone required")

    try:
        quote = await asyncio.to_thread(
            deposits.quote_for_lot, row, quantity=quantity, kind=kind
        )
        deposit = await asyncio.to_thread(
            deposits.create_pending,
            lot_id=row.get("lot_id") or lot_id,
            quantity=quantity,
            price_per_chair=row.get("price_per_chair"),
            quote=quote,
            buyer_name=name,
            buyer_email=email,
            buyer_phone=phone,
            attribution=attr,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))

    try:
        session = await asyncio.to_thread(
            stripe_gateway.create_checkout_session, deposit=deposit, lot=row
        )
    except Exception:
        # Close the row out rather than leaving a pending record no webhook can
        # ever resolve (there is no session to expire).
        await asyncio.to_thread(
            deposits.transition, deposit["id"], "canceled",
            failure_reason="session_create_failed",
        )
        raise HTTPException(502, "checkout_unavailable")

    await asyncio.to_thread(deposits.attach_session, deposit["id"], session.id)
    return {"ok": True, "url": session.url, "deposit_id": deposit["id"]}


@app.get("/returns", include_in_schema=False)
def public_returns():
    """Short, shareable link to the returns policy (Merchant Center + DMs)."""
    return RedirectResponse("/terms#returns", status_code=301)


@app.get("/terms", response_class=HTMLResponse)
async def public_terms(request: Request):
    """The deposit policy, on a stable URL.

    Renders even when the feature is dark — the policy is the trust artifact and
    gets linked from DMs and emails whether or not Checkout is live. Only the
    call-to-action copy is gated on `reserve_enabled`.
    """
    return templates.TemplateResponse(
        request, "terms.html",
        _public_ctx({"policy": stripe_gateway.REFUND_POLICY_SHORT}),
    )


@app.get("/privacy", response_class=HTMLResponse)
async def public_privacy(request: Request):
    """The privacy policy, on a stable URL.

    Meta lead forms refuse to publish without a privacy-policy link, and the
    same URL goes in Merchant Center and email footers. Static copy; the only
    context is how to reach us.
    """
    return templates.TemplateResponse(
        request, "privacy.html",
        _public_ctx({
            "postal_address": app_config.ALERTS_POSTAL_ADDRESS,
            "contact_phone": app_config.PUBLIC_CONTACT_PHONE,
        }),
    )


@app.post("/stripe/webhook")
async def stripe_webhook(request: Request):
    """Stripe's side of the ledger.

    The signature is the ONLY authentication this endpoint has, so an
    unverifiable body is a 400 and nothing else happens. Once verified, the
    answer is always 2xx — an unknown event type, a replay, and a row we can't
    match are all *fine*; returning non-2xx just makes Stripe redeliver for
    three days.

    Note for go-live: Cloudflare Bot Fight Mode blocks Stripe's POSTs. A WAF
    skip rule for this path is part of the runbook.
    """
    if not _reserve_enabled() or not app_config.STRIPE_WEBHOOK_SECRET:
        raise HTTPException(404, "not found")

    raw = await request.body()
    signature = request.headers.get("stripe-signature", "")
    try:
        event = await asyncio.to_thread(stripe_gateway.verify_webhook, raw, signature)
    except Exception:
        raise HTTPException(400, "invalid signature")

    row, changed = await asyncio.to_thread(deposits.apply_stripe_event, event)
    # `changed` is what keeps a Stripe retry from re-pinging the operator: the
    # state machine no-ops the second delivery and reports False.
    if changed and row:
        asyncio.create_task(_notify_deposit(row, event.get("type") or ""))
    return {"ok": True}


# ── unsubscribe (public capability URL, BLACKWHOLE-10 / PRD §6) ──────────────
# The token in the email's List-Unsubscribe link / footer. GET is the human
# click; POST is RFC 8058 one-click (Gmail native "Unsubscribe"). Both flip
# status='unsubscribed' and render the SAME page regardless of whether the token
# matched — no oracle, idempotent, honored instantly (CAN-SPAM). Not under a
# protected prefix, so it stays public even with admin auth on.
_UNSUBSCRIBE_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>Unsubscribed — BLACKWHOLE</title>
<style>body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
max-width:34rem;margin:12vh auto;padding:0 1.25rem;color:#1a1a1a;line-height:1.5}
h1{font-size:1.4rem;margin:0 0 .5rem}p{color:#555}a{color:#1a1a1a}</style></head>
<body><h1>You're off the list.</h1>
<p>You won't get any more new-inventory alerts from BLACKWHOLE. No further action
needed — this takes effect immediately.</p>
<p>Changed your mind? <a href="{base}/listings">Browse current listings</a>.</p>
</body></html>"""


async def _do_unsubscribe(token: str) -> HTMLResponse:
    # Best-effort: never leak whether the token existed. Swallow DB errors so the
    # page always renders (an errored unsubscribe must not 500 in the user's face).
    try:
        await asyncio.to_thread(inventory.unsubscribe_by_token, token)
    except Exception:
        pass
    return HTMLResponse(_UNSUBSCRIBE_PAGE.replace("{base}", PUBLIC_BASE_URL))


@app.get("/alerts/unsubscribe", response_class=HTMLResponse)
async def alerts_unsubscribe(token: str = Query("")):
    return await _do_unsubscribe(token)


@app.post("/alerts/unsubscribe", response_class=HTMLResponse)
async def alerts_unsubscribe_one_click(token: str = Query("")):
    # RFC 8058 List-Unsubscribe-Post=One-Click. Token rides in the query string
    # (that's how compose_email builds the URL); the form body is ignored.
    return await _do_unsubscribe(token)


@app.get("/api/runs/state")
async def get_run_state():
    return JSONResponse(state.snapshot())


def _channels_args_from_payload(payload: dict) -> list[str]:
    """Flags for `lot_channels.py add` (the default ▶ mode)."""
    extra: list[str] = []
    if payload.get("price"):
        extra += ["--price", str(payload["price"])]
    for key, flag in (("split", "--split"), ("title", "--title"), ("blurb", "--blurb"),
                      ("chair_type", "--chair-type"), ("fb_city", "--fb-city"),
                      ("fb_state", "--fb-state")):
        val = (payload.get(key) or "").strip() if isinstance(payload.get(key), str) else payload.get(key)
        if val:
            extra += [flag, str(val)]
    if payload.get("quantity"):
        extra += ["--quantity", str(int(payload["quantity"]))]
    channels = payload.get("channels")
    if isinstance(channels, (list, tuple)) and channels:
        extra += ["--channels", ",".join(str(c) for c in channels)]
    elif isinstance(channels, str) and channels.strip():
        extra += ["--channels", channels.strip()]
    if payload.get("skip_fb") or payload.get("no_publish"):
        extra.append("--no-publish")
    return extra


def _extra_args_from_payload(payload: dict) -> list[str]:
    if (payload.get("mode") or DEFAULT_LAUNCH_MODE) == "channels":
        return _channels_args_from_payload(payload)
    extra: list[str] = []
    for flag in ("skip_dewatermark", "skip_fb", "skip_ebay", "force_republish"):
        if payload.get(flag):
            extra.append("--" + flag.replace("_", "-"))
    if payload.get("price"):
        extra += ["--price", str(int(payload["price"]))]
    if payload.get("quantity"):
        extra += ["--quantity", str(int(payload["quantity"]))]
    return extra


def _collect_urls(payload: dict) -> list[str]:
    """Pull one or many URLs out of a request payload.

    Accepts `url: str` (legacy Launcher) or `urls: list[str]` (Auctions tab,
    Queue-all). Validates govdeals.com membership; drops anything else.
    """
    raw: list[str] = []
    single = (payload.get("url") or "").strip()
    if single:
        raw.append(single)
    for u in payload.get("urls") or []:
        if isinstance(u, str) and u.strip():
            raw.append(u.strip())
    return [u for u in raw if "govdeals.com" in u]


@app.post("/api/runs/start")
@app.post("/api/runs/queue")
async def start_run(payload: dict):
    payload = payload or {}
    urls = _collect_urls(payload)
    if not urls:
        raise HTTPException(400, "Provide at least one govdeals.com URL")

    mode = payload.get("mode") or DEFAULT_LAUNCH_MODE
    if mode not in ("channels", "pipeline"):
        raise HTTPException(400, f"mode must be channels or pipeline, got {mode!r}")
    extra = _extra_args_from_payload({**payload, "mode": mode})
    return await _enqueue(urls, extra, mode)


async def _enqueue(targets: list[str], extra: list[str], mode: str) -> dict:
    async with state.lock:
        if state.run_status == "running":
            for u in targets:
                state.pending.append({"url": u, "extra_args": extra, "mode": mode})
            await _broadcast_queue()
            return {"ok": True, "queued": len(targets), "running": state.url,
                    "queue_length": len(state.pending), "mode": mode}

        head, *rest = targets
        for u in rest:
            state.pending.append({"url": u, "extra_args": extra, "mode": mode})
        state.reset()
        state.url = head
        asyncio.create_task(_run_subprocess(head, extra, mode))
        await _broadcast_queue()
        return {"ok": True, "running": head, "queued": len(rest),
                "queue_length": len(state.pending), "mode": mode}


@app.post("/api/lots/{lot_id}/remove")
async def remove_lot_everywhere(lot_id: str, payload: dict | None = None):
    """Take a lot off site + FB Business catalog + Marketplace as "moved"
    (fake sold-out + Mark as sold). Streams through the Launcher console."""
    payload = payload or {}
    if not await asyncio.to_thread(inventory.get, lot_id):
        raise HTTPException(404, f"no lot {lot_id}")
    extra: list[str] = []
    channels = payload.get("channels")
    if channels:
        extra += ["--channels", ",".join(channels) if isinstance(channels, list) else str(channels)]
    return await _enqueue([lot_id], extra, "remove")


@app.get("/api/lots/status")
def lots_channel_status(lot_id: str | None = None):
    """Per-lot channel matrix: site / business feed / Marketplace."""
    return {"items": lot_channels.channel_matrix([lot_id] if lot_id else None)}


@app.post("/api/runs/queue/clear")
async def clear_queue():
    async with state.lock:
        n = len(state.pending)
        state.pending.clear()
        await _broadcast_queue()
    return {"ok": True, "cleared": n}


@app.post("/api/runs/cancel")
async def cancel_run():
    if state.proc and state.proc.returncode is None:
        try:
            state.proc.send_signal(signal.SIGINT)
        except ProcessLookupError:
            pass
        return {"ok": True}
    return {"ok": False, "reason": "no active run"}


@app.post("/api/runs/stdin")
async def write_stdin(payload: dict):
    """Forward a line to the subprocess stdin (used to confirm price prompt)."""
    line = (payload or {}).get("line", "")
    if not state.proc or state.proc.stdin is None or state.proc.returncode is not None:
        raise HTTPException(409, "no active run")
    state.proc.stdin.write((line + "\n").encode())
    await state.proc.stdin.drain()
    return {"ok": True}


@app.get("/api/runs/stream")
async def stream_run(request: Request):
    queue: asyncio.Queue = asyncio.Queue(maxsize=2048)

    async def event_gen():
        # Replay
        for msg in list(state.lines):
            yield {"event": msg["stream"], "data": json.dumps(msg)}
        # Subscribe
        state.subscribers.append(queue)
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield {"event": msg["stream"], "data": json.dumps(msg)}
                except asyncio.TimeoutError:
                    yield {"event": "ping", "data": "{}"}
        finally:
            if queue in state.subscribers:
                state.subscribers.remove(queue)

    return EventSourceResponse(event_gen())


# ───────────────────────────── drafts ─────────────────────────────

IMG_EXTS = {".jpg", ".jpeg", ".png", ".webp"}


def _list_listing_folders() -> list[Path]:
    if not DOWNLOAD_ROOT.exists():
        return []
    folders = []
    for p in DOWNLOAD_ROOT.iterdir():
        if not p.is_dir():
            continue
        # skip non-listing buckets
        if p.name.startswith(".") or p.name in {"General listing", "Lost biddings",
                                                 "Listing Automations", "Listing_automation_html"}:
            continue
        folders.append(p)
    folders.sort(key=lambda x: x.stat().st_mtime, reverse=True)
    return folders


def _folder_images(folder: Path) -> list[str]:
    if not folder.exists():
        return []
    files = []
    for p in folder.iterdir():
        if p.is_file() and p.suffix.lower() in IMG_EXTS:
            files.append(p.name)
    files.sort()
    return files


def _all_compare_rows() -> list[dict]:
    """Single query — callers that iterate over folders should call this once
    and pass the result to ``_latest_compare_for_folder`` to avoid an O(N) DB
    fan-out (24 folders × 200 ms pooler latency = a 5 s page)."""
    return db.fetch_all(
        "SELECT dom_hint, primary_extraction, secondary_extraction "
        "FROM llm_compare_logs ORDER BY ts DESC"
    )


def _latest_compare_for_folder(folder_name: str, rows: list[dict]) -> dict | None:
    """Best-effort match: pick the most recent llm log whose primary title slug
    matches the folder name (folders are slugified titles). ``rows`` is the
    pre-loaded output of :func:`_all_compare_rows`."""
    folder_slug = re.sub(r"[^a-z0-9]", "", folder_name.lower())
    for row in rows:
        primary = row["primary_extraction"] or {}
        title = primary.get("title") or ""
        title_slug = re.sub(r"[^a-z0-9]", "", title.lower())
        if title_slug and (title_slug in folder_slug or folder_slug in title_slug):
            return {
                "dom_hint": row["dom_hint"],
                "primary": row["primary_extraction"],
                "secondary": row["secondary_extraction"],
            }
    return None


@app.get("/api/drafts")
@readcache.cached()
def list_drafts():
    inv_by_folder = {
        r["folder_name"]: r for r in inventory.list_all() if r.get("folder_name")
    }
    compare_rows = _all_compare_rows()
    out = []
    for folder in _list_listing_folders():
        imgs = _folder_images(folder)
        meta = _latest_compare_for_folder(folder.name, compare_rows)
        primary = (meta or {}).get("primary") or {}
        inv = inv_by_folder.get(folder.name) or {}
        out.append({
            "folder": folder.name,
            "path": str(folder),
            "modified": folder.stat().st_mtime,
            "image_count": len(imgs),
            "images": imgs[:24],
            "title": primary.get("title") or inv.get("title"),
            "location": primary.get("location"),
            "quantity": primary.get("quantity") or inv.get("quantity_remaining"),
            "chair_type": primary.get("chair_type") or inv.get("chair_type"),
            "dimensions": primary.get("dimensions"),
            "suggested_price": primary.get("suggested_price_per_chair") or inv.get("price_per_chair"),
            "lot_id": inv.get("lot_id"),
            "facebook_url": inv.get("facebook_url"),
            "ebay_url": inv.get("ebay_url"),
        })
    return {"drafts": out}


@app.get("/image/{folder}/{name}")
async def serve_image(folder: str, name: str):
    folder_path = DOWNLOAD_ROOT / folder
    target = (folder_path / name).resolve()
    if not str(target).startswith(str(folder_path.resolve())):
        raise HTTPException(403, "path traversal")
    if not target.exists():
        raise HTTPException(404, "not found")
    return FileResponse(str(target))


@app.get("/screenshot/{folder}/{name}")
async def serve_screenshot(folder: str, name: str):
    target = (DOWNLOAD_ROOT / folder / "_screenshots" / name).resolve()
    base = (DOWNLOAD_ROOT / folder / "_screenshots").resolve()
    if not str(target).startswith(str(base)):
        raise HTTPException(403, "path traversal")
    if not target.exists():
        raise HTTPException(404, "not found")
    return FileResponse(str(target))


# ───────────────────────────── scraper ─────────────────────────────

# Maps logical source names to the script file inside auction_extractors/.
# Each script is invoked with cwd=auction_extractors/ so its sibling
# imports (`from paths import STATE_DIR`, etc.) resolve.
_SCRAPE_SCRIPTS = {
    "gd": "govdeals_chairs_extraction.py",
    "ps": "public_surplus_automation.py",
    "bs": "bidspotter_automation.py",
}
_SCRAPE_LABELS = {"gd": "GovDeals", "ps": "Public Surplus", "bs": "BidSpotter"}

# Maps the `[n]` / `[nX]` prefixes the scrapers emit to human labels that
# render in the SCRAPE strip. Prefix order mirrors the scrapers' pipeline:
# preflight → scrape → describe → regex refine → llm refine → rank → alert.
_SCRAPE_STAGES = {
    "[0]":  "preflight",
    "[1]":  "scraping listings",
    "[1b]": "fetching descriptions",
    "[1c]": "regex refine",
    "[1d]": "llm refine",
    "[2]":  "ranking",
    "[3a]": "telegram alert",
}
_SCRAPE_STAGE_RE = re.compile(r"^\s*(\[[0-9a-z]+\])")
_PAGE_DETAIL_RE = re.compile(r"\bPage\s+(\d+):")
_DESC_DETAIL_RE = re.compile(r"…\s+(\d+)\s*/\s*(\d+)")
_FILTER_DETAIL_RE = re.compile(r"Filter:\s+'([^']+)'")


def _parse_stage(line: str) -> tuple[str | None, str | None]:
    """Return (stage_label, stage_detail) for a stdout line, or (None, None)."""
    m = _SCRAPE_STAGE_RE.match(line)
    label = _SCRAPE_STAGES.get(m.group(1)) if m else None
    detail = None
    if (mp := _PAGE_DETAIL_RE.search(line)):
        detail = f"page {mp.group(1)}"
    elif (md := _DESC_DETAIL_RE.search(line)):
        detail = f"{md.group(1)}/{md.group(2)} descriptions"
    elif (mf := _FILTER_DETAIL_RE.search(line)):
        detail = f"filter: {mf.group(1)}"
    return label, detail


class ScrapeState:
    """Separate from RunState so scrapes and pipeline runs coexist.

    A single scrape job at a time (same design as pipeline: it's a 5-30min
    Playwright job, no point in running two concurrently against the same
    DB row set).
    """

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.proc: asyncio.subprocess.Process | None = None
        self.source: str | None = None        # "gd" | "ps" | "both"
        self.current_step: str | None = None  # "gd" | "ps" — the running sub-job
        self.current_stage: str | None = None  # human label from _SCRAPE_STAGES
        self.stage_detail: str | None = None   # e.g. "page 3" / "12/40 descriptions"
        self.started_at: float | None = None
        self.finished_at: float | None = None
        self.return_code: int | None = None
        self.test_mode: bool = False
        self.status: str = "idle"             # idle | running | finished | error | cancelled
        self.last_line: str = ""
        self.lines: list[dict] = []
        self.subscribers: list[asyncio.Queue] = []

    def reset(self, source: str, test_mode: bool) -> None:
        self.source = source
        self.test_mode = test_mode
        self.current_step = None
        self.current_stage = None
        self.stage_detail = None
        self.started_at = time.time()
        self.finished_at = None
        self.return_code = None
        self.status = "running"
        self.last_line = ""
        self.lines.clear()

    async def broadcast(self, msg: dict) -> None:
        self.lines.append(msg)
        if len(self.lines) > 2000:
            del self.lines[:500]
        dead = []
        for q in self.subscribers:
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                dead.append(q)
        for q in dead:
            self.subscribers.remove(q)

    def snapshot(self) -> dict:
        return {
            "status": self.status,
            "source": self.source,
            "current_step": self.current_step,
            "current_stage": self.current_stage,
            "stage_detail": self.stage_detail,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "return_code": self.return_code,
            "test_mode": self.test_mode,
            "last_line": self.last_line,
            "line_count": len(self.lines),
        }


scrape_state = ScrapeState()


async def _pump_scrape_stream(stream: asyncio.StreamReader, kind: str) -> None:
    while True:
        raw = await stream.readline()
        if not raw:
            break
        line = raw.decode("utf-8", errors="replace").rstrip("\n")
        scrape_state.last_line = line[-300:]

        if kind == "stdout":
            label, detail = _parse_stage(line)
            changed = False
            if label and label != scrape_state.current_stage:
                scrape_state.current_stage = label
                scrape_state.stage_detail = None  # reset detail when stage flips
                changed = True
            if detail and detail != scrape_state.stage_detail:
                scrape_state.stage_detail = detail
                changed = True
            if changed:
                await scrape_state.broadcast({
                    "t": time.time(), "stream": "event",
                    "data": {
                        "kind": "scrape_stage",
                        "stage": scrape_state.current_stage,
                        "detail": scrape_state.stage_detail,
                        "step": scrape_state.current_step,
                    },
                })

        await scrape_state.broadcast({"t": time.time(), "stream": kind, "data": line})


async def _run_scraper(source: str, test_mode: bool, profile_slug: str | None = None) -> None:
    """Run one or all scrapers sequentially (gd → ps → bs when source='both')."""
    steps: list[str] = ["gd", "ps", "bs"] if source == "both" else [source]
    overall_rc = 0

    for step in steps:
        scrape_state.current_step = step
        # Reset stage between steps so the prior sub-job's final stage doesn't
        # bleed into the next sub-job's "starting" window.
        scrape_state.current_stage = "starting"
        scrape_state.stage_detail = None
        await scrape_state.broadcast({
            "t": time.time(), "stream": "event",
            "data": {
                "kind": "scrape_stage",
                "stage": "starting", "detail": None, "step": step,
            },
        })
        script = AUCTION_EXTRACTORS_DIR / _SCRAPE_SCRIPTS[step]
        cmd = [sys.executable, "-u", str(script)]
        if step in ("ps", "bs") and test_mode:
            cmd.append("--test")

        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        if profile_slug:
            # Research profile → scraper env (auction_extractors reads both;
            # unset = the chair defaults, so a bare launch is unchanged).
            try:
                prof = profiles.resolve(profile_slug)
                if prof.search_terms:
                    env["SCRAPE_SEARCH_TERMS"] = ",".join(prof.search_terms)
                env["SCRAPE_ITEM_NOUN"] = prof.item_noun
                await scrape_state.broadcast({"t": time.time(), "stream": "system",
                    "data": f"[profile {prof.slug}] terms={prof.search_terms} noun={prof.item_noun}"})
            except KeyError:
                await scrape_state.broadcast({"t": time.time(), "stream": "system",
                    "data": f"[profile {profile_slug!r} unknown — using scraper defaults]"})

        await scrape_state.broadcast({
            "t": time.time(), "stream": "system",
            "data": f"$ cd auction_extractors && {' '.join(cmd[-2:])}",
        })

        try:
            scrape_state.proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(AUCTION_EXTRACTORS_DIR),
                env=env,
            )
        except Exception as e:
            await scrape_state.broadcast({
                "t": time.time(), "stream": "system",
                "data": f"[spawn failed for {step}] {e!r}",
            })
            overall_rc = -1
            break

        try:
            await asyncio.gather(
                _pump_scrape_stream(scrape_state.proc.stdout, "stdout"),
                _pump_scrape_stream(scrape_state.proc.stderr, "stderr"),
            )
            rc = await scrape_state.proc.wait()
        except Exception as e:
            await scrape_state.broadcast({
                "t": time.time(), "stream": "system",
                "data": f"[runner error] {e!r}",
            })
            rc = -1

        await scrape_state.broadcast({
            "t": time.time(), "stream": "system",
            "data": f"[{_SCRAPE_LABELS.get(step, step)} exit {rc}]",
        })
        if rc != 0:
            overall_rc = rc
            # Stop the chain — don't run ps if gd failed.
            break

    # The scrapers write the SQLite cache (auction_extractors/state/listings.db),
    # but /api/auctions reads Supabase `auction_listings`. Mirror the fresh rows
    # across now — otherwise the scrape "succeeds" yet the Auctions tab keeps
    # showing the previous sync's data.
    if overall_rc == 0 and scrape_state.status != "cancelled":
        scrape_state.current_stage = "syncing"
        await scrape_state.broadcast({
            "t": time.time(), "stream": "event",
            "data": {"kind": "scrape_stage", "stage": "syncing",
                     "detail": "Supabase", "step": scrape_state.current_step},
        })
        sync_script = PROJECT_ROOT / "scripts" / "transfer_listings_to_supabase.py"
        await scrape_state.broadcast({
            "t": time.time(), "stream": "system",
            "data": f"$ python scripts/{sync_script.name}",
        })
        try:
            sync_env = os.environ.copy()
            sync_env["PYTHONUNBUFFERED"] = "1"
            scrape_state.proc = await asyncio.create_subprocess_exec(
                sys.executable, "-u", str(sync_script),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(PROJECT_ROOT),
                env=sync_env,
            )
            await asyncio.gather(
                _pump_scrape_stream(scrape_state.proc.stdout, "stdout"),
                _pump_scrape_stream(scrape_state.proc.stderr, "stderr"),
            )
            sync_rc = await scrape_state.proc.wait()
            await scrape_state.broadcast({
                "t": time.time(), "stream": "system",
                "data": f"[supabase sync exit {sync_rc}]",
            })
            if sync_rc != 0:
                overall_rc = sync_rc
        except Exception as e:
            await scrape_state.broadcast({
                "t": time.time(), "stream": "system",
                "data": f"[supabase sync failed] {e!r}",
            })
            overall_rc = -1

    scrape_state.proc = None
    scrape_state.return_code = overall_rc
    scrape_state.finished_at = time.time()
    # Treat cancelled run as cancelled, not error.
    if scrape_state.status != "cancelled":
        scrape_state.status = "finished" if overall_rc == 0 else "error"
        readcache.invalidate_all()   # the run/scrape wrote rows the admin memo may hold

    # Fresh rows are in Supabase now — bust the read-side cache so the next
    # /api/auctions call returns the updated set.
    _AUCTIONS_CACHE.clear()
    readcache.invalidate_all()

    await scrape_state.broadcast({
        "t": time.time(), "stream": "event",
        "data": {"kind": "scrape", "status": scrape_state.status,
                 "return_code": overall_rc},
    })


@app.post("/api/scrape/start")
async def scrape_start(payload: dict):
    payload = payload or {}
    source = (payload.get("source") or "gd").strip()
    if source not in ("gd", "ps", "bs", "both"):
        raise HTTPException(400, "source must be 'gd', 'ps', 'bs', or 'both'")
    test_mode = bool(payload.get("test"))
    profile_slug = (payload.get("profile") or "").strip() or None

    async with scrape_state.lock:
        if scrape_state.status == "running":
            raise HTTPException(409, "a scrape is already running")
        scrape_state.reset(source, test_mode)
        asyncio.create_task(_run_scraper(source, test_mode, profile_slug))

    return {"ok": True, "source": source, "test_mode": test_mode, "profile": profile_slug}


@app.post("/api/scrape/cancel")
async def scrape_cancel():
    if scrape_state.proc and scrape_state.proc.returncode is None:
        scrape_state.status = "cancelled"
        try:
            scrape_state.proc.send_signal(signal.SIGINT)
        except ProcessLookupError:
            pass
        return {"ok": True}
    return {"ok": False, "reason": "no active scrape"}


@app.get("/api/scrape/state")
async def scrape_state_snapshot():
    return JSONResponse(scrape_state.snapshot())


@app.get("/api/scrape/stream")
async def scrape_stream(request: Request):
    queue: asyncio.Queue = asyncio.Queue(maxsize=2048)

    async def event_gen():
        for msg in list(scrape_state.lines):
            yield {"event": msg["stream"], "data": json.dumps(msg)}
        scrape_state.subscribers.append(queue)
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield {"event": msg["stream"], "data": json.dumps(msg)}
                except asyncio.TimeoutError:
                    yield {"event": "ping", "data": "{}"}
        finally:
            if queue in scrape_state.subscribers:
                scrape_state.subscribers.remove(queue)

    return EventSourceResponse(event_gen())


# ───────────────────────────── auctions ─────────────────────────────

# Full state name → USPS abbreviation, for parsing the scraper cache's
# pre-formatted "City, State, Country" location strings into something
# alerts.geo.resolve_latlon understands (it wants a 2-letter state).
_STATE_ABBREV = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "district of columbia": "DC", "florida": "FL", "georgia": "GA",
    "hawaii": "HI", "idaho": "ID", "illinois": "IL", "indiana": "IN",
    "iowa": "IA", "kansas": "KS", "kentucky": "KY", "louisiana": "LA",
    "maine": "ME", "maryland": "MD", "massachusetts": "MA", "michigan": "MI",
    "minnesota": "MN", "mississippi": "MS", "missouri": "MO", "montana": "MT",
    "nebraska": "NE", "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ",
    "new mexico": "NM", "new york": "NY", "north carolina": "NC",
    "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR",
    "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
    "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
}


def _auction_state(location: str | None) -> str | None:
    """Best-effort 2-letter state from a "City, State, Country" string."""
    for part in (location or "").split(","):
        part = part.strip()
        if len(part) == 2 and part.upper() in _STATE_ABBREV.values():
            return part.upper()
        abbrev = _STATE_ABBREV.get(part.lower())
        if abbrev:
            return abbrev
    return None


def _annotate_auction_geo(items: list[dict]) -> None:
    """Attach lat/lng/geo_precision to scraper-cache items, in place.

    The cache stores only a location string + pickup_zip, so coordinates are
    resolved zip-first (pgeocode, offline) with a state-centroid fallback —
    same ladder the alert matcher uses. Feeds the Auctions tab map.
    """
    from automation.alerts.geo import resolve_latlon

    for it in items:
        lat, lng, precision = resolve_latlon(
            it.get("pickup_zip"), _auction_state(it.get("location"))
        )
        it["lat"], it["lng"], it["geo_precision"] = lat, lng, precision


# In-memory cache for /api/auctions responses. Condition-scoring LLM calls
# are 3-10s per request; cache by query-string for ~10 min so toggling
# filters in the UI doesn't re-run the LLM on every click.
_AUCTIONS_CACHE: dict[str, tuple[float, list[dict]]] = {}
_AUCTIONS_TTL = 600.0  # seconds


@app.get("/api/auctions")
async def list_auctions(
    source: str = "gd",
    n: int = 15,
    min_qty: int | None = None,
    condition: int = 0,
    active_only: int = 1,
    max_stale_days: int = 2,
    category: str | None = None,
    profile: str | None = None,
):
    """Top cached lots for a research profile (`profile=<slug>`; default =
    the default profile, i.e. chairs). Legacy `category=banquet|medical`
    still works and maps onto a profile."""
    if get_top_lots is None:
        raise HTTPException(503, "auction_extractors package not available")
    if source not in ("gd", "ps", "bs"):
        raise HTTPException(400, "source must be 'gd', 'ps', or 'bs'")
    # legacy sub-tab param → profile slug (banquet == default chairs profile)
    if not profile and category not in (None, "", "all"):
        if category not in ("banquet", "medical"):
            raise HTTPException(400, "category must be banquet|medical (or use profile=)")
        profile = "medical" if category == "medical" else None
    try:
        prof = await asyncio.to_thread(profiles.resolve, profile)
    except KeyError:
        raise HTTPException(404, f"unknown profile {profile!r}")
    n = max(1, min(int(n), 100))
    # min_qty default is the profile's floor (chairs 50, medical 1, …).
    min_qty = prof.min_quantity if min_qty is None else max(1, int(min_qty))
    include_condition = bool(int(condition))
    active_flag = bool(int(active_only))
    stale = max(1, int(max_stale_days))

    key = (
        f"{prof.slug}|{source}|{n}|{min_qty}|{int(include_condition)}|"
        f"{int(active_flag)}|{stale}"
    )
    now = time.time()
    cached = _AUCTIONS_CACHE.get(key)
    if cached and (now - cached[0]) < _AUCTIONS_TTL:
        return {"items": cached[1], "cached": True, "age": int(now - cached[0]),
                "profile": prof.slug}

    try:
        items = await asyncio.to_thread(
            get_top_lots, prof,
            source=source,
            n=n,
            min_quantity=min_qty,
            include_condition=include_condition,
            active_only=active_flag,
            max_stale_days=stale,
        )
    except Exception as e:
        raise HTTPException(500, f"get_top_lots failed: {e!r}")

    try:
        _annotate_auction_geo(items)
    except Exception as e:
        print(f"[auctions] geo annotation failed: {e!r}")

    _AUCTIONS_CACHE[key] = (now, items)
    return {"items": items, "cached": False, "age": 0, "profile": prof.slug}


@app.post("/api/auctions/refresh")
async def refresh_auctions_cache():
    _AUCTIONS_CACHE.clear()
    readcache.invalidate_all()
    return {"ok": True}


# ───────────────────────────── test scrape ─────────────────────────────
# Backing for the "08 Test Scrape" tab: a live keyword search against one
# source's fast path (GovDeals maestro JSON API / Public Surplus server-
# rendered search pages). Read-only relevance probe for new categories —
# nothing is written to listings.db or Supabase, and the LLM never runs.
# Quantity on the returned cards is the title-regex seed only.

_TEST_SCRAPE_FIELDS = (
    "title", "link", "price", "image_url", "location",
    "end_date", "time_left", "quantity",
)


def _test_scrape_sync(source: str, q: str, pages: int) -> list[dict]:
    """Blocking worker for /api/test-scrape; runs under asyncio.to_thread.

    Imports the scraper modules lazily (with the auction_extractors dir on
    sys.path for their flat sibling imports) so the dashboard still boots
    when the package is absent.
    """
    pkg_dir = str(AUCTION_EXTRACTORS_DIR)
    if pkg_dir not in sys.path:
        sys.path.insert(0, pkg_dir)
    if source == "gd":
        import govdeals_chairs_extraction as gd
        term = gd._singularize_term(q)
        cards: list[dict] = []
        for page in range(1, pages + 1):
            assets = gd._search_via_api(term, page)
            if not assets:
                break
            cards.extend(gd._asset_to_card(a) for a in assets)
            if len(assets) < gd.GOVDEALS_API_ROWS:
                break
        cards = gd._dedup_listings(cards)
    elif source == "ps":
        import requests
        import public_surplus_automation as ps
        cards = []
        for page_idx in range(pages):
            resp = requests.get(
                ps._search_url(q, page_idx), headers=ps._HTTP_HEADERS, timeout=30)
            resp.raise_for_status()
            page_cards = ps._parse_search_cards(resp.text)
            if not page_cards:
                break
            cards.extend(page_cards)
            if len(page_cards) < ps.PS_PAGE_SIZE:
                break
        cards = ps._dedup(cards)
    elif source == "bs":
        # Lazy import: the scraper file may not exist yet — importing app.py
        # must never require it at module load.
        import bidspotter_automation as bs
        cards = []
        for page in range(1, pages + 1):
            html = bs._fetch_search_page(q, page)
            page_cards = bs._parse_search_cards(html)
            if not page_cards:
                break
            cards.extend(page_cards)
    else:
        raise ValueError(source)
    return [{k: c.get(k) for k in _TEST_SCRAPE_FIELDS} for c in cards]


@app.get("/api/test-scrape")
async def test_scrape(q: str, source: str = "gd", pages: int = 1):
    q = (q or "").strip()
    if not q:
        raise HTTPException(400, "q (search keyword) is required")
    if source not in ("gd", "ps", "bs"):
        raise HTTPException(400, "source must be 'gd', 'ps', or 'bs'")
    pages = max(1, min(int(pages), 5))
    try:
        items = await asyncio.to_thread(_test_scrape_sync, source, q, pages)
    except Exception as e:
        raise HTTPException(502, f"test scrape failed: {e!r}")
    return {"source": source, "q": q, "count": len(items), "items": items}


# ───────────────────────────── auction favorites ──────────────────────────


def _asset_id_from_link(link: str) -> str:
    """Lift the auction_extractors helper inline to avoid an import cycle.

    GovDeals: ``/asset/<a>/<b>`` → ``"<a>/<b>"``.
    PublicSurplus: ``?auc=<n>`` → ``"ps:<n>"``.
    BidSpotter: ``bidspotter.com/…/lot-<guid>`` → ``"bs:<guid>"``.
    """
    if not link:
        return ""
    m = re.search(r"/asset/(\d+)/(\d+)", link)
    if m:
        return f"{m.group(1)}/{m.group(2)}"
    m = re.search(r"[?&]auc=(\d+)", link)
    if m:
        return f"ps:{m.group(1)}"
    m = re.search(
        r"bidspotter\.com/.*/lot-"
        r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
        link,
    )
    if m:
        return f"bs:{m.group(1)}"
    return ""


@app.get("/api/auctions/favorites")
async def list_favorites():
    """All starred auctions, newest first. Each item carries a derived
    ``seconds_until_end`` and a ``sent_intervals`` list so the UI can render
    a checklist of which alerts have already fired."""
    favs = await asyncio.to_thread(favorites.list_all)
    return {
        "items": [f.to_dict() for f in favs],
        "intervals": [label for label, _ in favorites.ALERT_INTERVALS],
        "telegram_configured": telegram_alerts.is_configured(),
    }


@app.post("/api/auctions/favorites")
def star_favorite(payload: dict, background: BackgroundTasks):
    """Star (or refresh) an auction by URL. Body: ``{link, title?, quantity?,
    end_date?, image_url?, location?, asset_id?}``. ``asset_id`` is derived
    from the link if not provided."""
    payload = payload or {}
    link = (payload.get("link") or "").strip()
    if not link:
        raise HTTPException(400, "link required")
    asset_id = (payload.get("asset_id") or "").strip() or _asset_id_from_link(link)
    if not asset_id:
        raise HTTPException(400, "could not derive asset_id from link")
    fav = favorites.upsert(
        asset_id=asset_id,
        link=link,
        title=payload.get("title"),
        quantity=int(payload["quantity"]) if payload.get("quantity") not in (None, "") else None,
        end_date_raw=payload.get("end_date") or payload.get("end_date_raw"),
        image_url=payload.get("image_url"),
        location=payload.get("location"),
        notes=payload.get("notes"),
    )
    if fav and favorite_images.on_star_enabled():
        # Clean photos for the public map's "incoming" pin. Runs after the
        # response, so the star never waits on dewatermark.ai; budget caps
        # apply, and a failure (migration 010 unapplied, closed lot) surfaces
        # in the server log, never in the star response. `_safely` because a
        # background task that raises tears down the request's task group.
        background.add_task(favorite_images.mirror_favorite_photos_safely, fav.asset_id)
    return fav.to_dict() if fav else {}


@app.delete("/api/auctions/favorites/{asset_id:path}")
def unstar_favorite(asset_id: str):
    ok = favorites.delete(asset_id)
    if not ok:
        raise HTTPException(404, "not favorited")
    return {"ok": True}


@app.post("/api/auctions/favorites/test-telegram")
async def telegram_test():
    """Fire a one-shot Telegram message so the user can verify their bot
    token + chat_id are wired up before counting on countdown alerts."""
    ok, err = await telegram_alerts.send_message(
        "✅ listing_automation: Telegram alerts are wired up. "
        "You'll get pings as your favorite auctions wind down.",
        topic="health",
    )
    if ok:
        return {"ok": True}
    raise HTTPException(500, err or "send failed")


# ─────────── countdown alert scheduler (runs in-process) ───────────

_SCHEDULER_TICK_SEC = 30.0  # tight enough for the 5m alert to be ±30s
_alerts_task: asyncio.Task | None = None


def _format_alert(fav_dict: dict, label: str) -> str:
    """Compose the Telegram body. Plain text — no Markdown, since one bad
    underscore in a title breaks Telegram's parser silently."""
    title = (fav_dict.get("title") or "Untitled lot").strip()
    qty = fav_dict.get("quantity")
    qty_line = f"{qty:,} ×" if qty else ""
    secs = fav_dict.get("seconds_until_end") or 0
    if secs <= 0:
        when = "now"
    elif secs < 3600:
        when = f"{secs // 60} min"
    elif secs < 86400:
        when = f"~{secs // 3600}h {secs % 3600 // 60}m"
    else:
        when = f"~{secs // 86400}d {(secs % 86400) // 3600}h"
    return (
        f"⏰ Auction ending in {label} ({when} left)\n"
        f"{title}\n"
        f"{qty_line}\n"
        f"{fav_dict.get('link') or ''}"
    ).strip()


def _alerts_collect_due() -> list:
    """Blocking half of the scheduler tick: every favorites.* call opens a
    fresh Supabase pooler connection (sync psycopg), so this must run in a
    worker thread, never on the event loop — one stalled handshake would
    freeze every request (including /api/health) for the whole TCP timeout."""
    favs = favorites.list_all()
    if not favs:
        return []

    # Re-sync end_date from auction_extractors cache so we catch relists.
    # Cheap: one indexed lookup per favorite. If listings.db is gone we
    # silently skip the sync — alerts still fire off the snapshot.
    try:
        import sqlite3
        db_path = AUCTION_EXTRACTORS_DIR / "state" / "listings.db"
        if db_path.exists():
            conn = sqlite3.connect(str(db_path))
            conn.row_factory = sqlite3.Row
            try:
                for f in favs:
                    row = conn.execute(
                        "SELECT end_date, time_left, image_url, title, "
                        "quantity, location FROM listings WHERE asset_id = ?",
                        (f.asset_id,),
                    ).fetchone()
                    if row is None:
                        continue
                    # ONLY the absolute end_date — never time_left. A
                    # relative "2 days left" string re-parses to a new
                    # instant every tick, which re-armed alerts endlessly
                    # (the alert flood). No absolute date → keep snapshot.
                    fresh_end = (row["end_date"] or "").strip()
                    if not fresh_end:
                        continue
                    fresh_dt = favorites._parse_end_date(fresh_end)
                    if fresh_dt is None:
                        continue
                    # Compare PARSED times, not raw strings: formatting
                    # drift must not trigger a needless re-sync/re-arm.
                    if f.end_dt and abs((fresh_dt - f.end_dt).total_seconds()) <= 120:
                        continue
                    favorites.upsert(
                        asset_id=f.asset_id,
                        link=f.link,
                        title=row["title"] or f.title,
                        quantity=row["quantity"] or f.quantity,
                        end_date_raw=fresh_end or f.end_date_raw,
                        image_url=row["image_url"] or f.image_url,
                        location=row["location"] or f.location,
                    )
            finally:
                conn.close()
    except Exception as e:
        print(f"[favorites] sync from listings.db failed: {e!r}")

    # Re-read after sync.
    favs = favorites.list_all()
    return favorites.due_alerts(favs)


async def _alerts_tick() -> None:
    """One scheduler pass. Re-syncs end_date from listings.db where possible
    (catches relists with fresh end_date), then ships any due alerts."""
    try:
        due = await asyncio.to_thread(_alerts_collect_due)
        if not due:
            return
        if not telegram_alerts.is_configured():
            # Don't burn entries if we can't actually send. The user will see
            # the favorite still primed once they configure Telegram.
            print(
                f"[favorites] {len(due)} alert(s) due but Telegram not "
                "configured (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID); skipping"
            )
            return
        for fav, label in due:
            text = _format_alert(fav.to_dict(), label)
            ok, err = await telegram_alerts.send_message(text, topic="deals")
            if ok:
                await asyncio.to_thread(favorites.mark_sent, fav.asset_id, label)
                print(f"[favorites] alert sent: {fav.asset_id} {label}")
            else:
                print(f"[favorites] alert FAILED: {fav.asset_id} {label}: {err}")
    except Exception as e:
        # Never let the scheduler die from a single bad tick.
        print(f"[favorites] tick error: {e!r}")


def _tracking_pass() -> dict:
    """Blocking: adopt new favorites, then poll whatever tracked lots are due.
    Runs in a worker thread off the scheduler tick. Cheap when nothing is due
    (two indexed SELECTs); the per-lot schedule in tracked_lots.next_poll_at
    decides how often the bidbox is actually hit."""
    from deals import tracking
    adapter = _govdeals_adapter()
    tracking.adopt_favorites(adapter)
    report = tracking.sync_tracked(adapter, verbose=False)
    report["costs_filled"] = tracking.fill_missing_costs(adapter, limit=5)
    return report


async def _tracking_tick() -> None:
    try:
        rep = await asyncio.to_thread(_tracking_pass)
        if rep.get("recorded") or rep.get("closed") or rep.get("errors"):
            print(f"[tracking] {rep}")
    except Exception as e:
        # Same rule as the favorites tick: one bad pass must not kill the loop.
        print(f"[tracking] tick error: {e!r}")


async def _alerts_loop() -> None:
    while True:
        await _alerts_tick()
        await asyncio.sleep(_SCHEDULER_TICK_SEC)


_tracking_task: asyncio.Task | None = None
_geo_warm_task: asyncio.Task | None = None


async def _tracking_loop() -> None:
    # Its own task, not a step of _alerts_loop: every db call opens a fresh
    # pooler connection (~1s), so a pass over ten due lots can run a minute,
    # and the 5-minute countdown alert must not wait behind it.
    while True:
        await _tracking_tick()
        await asyncio.sleep(_SCHEDULER_TICK_SEC)


_channel_sync_task: asyncio.Task | None = None


def _channel_sync_interval() -> float:
    """Seconds between channel-sync passes. `CHANNEL_SYNC_SEC=0` disables the
    loop (tests, a laptop with no Chrome profile); junk falls back to 300."""
    raw = os.getenv("CHANNEL_SYNC_SEC")
    if raw is None or not raw.strip():
        return 300.0
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 300.0


async def _channel_sync_tick() -> None:
    try:
        rep = await asyncio.to_thread(channel_sync.run_once)
        if rep.get("applied") or rep.get("errors"):
            print(f"[channels] {rep}")
    except Exception as e:
        # Same rule as the other pollers: one bad pass must not kill the loop.
        print(f"[channels] sync error: {e!r}")


async def _channel_sync_loop() -> None:
    # Its own task: a browser channel post can take a minute and must not
    # delay the 5-minute favorites alert or the bid poller.
    while True:
        await _channel_sync_tick()
        await asyncio.sleep(_channel_sync_interval() or 300.0)


# ─────────── auction-expiry sync (runs in-process, sibling of _tracking_loop) ───────────
#
# A lot we're bidding on sits in the ledger as `active_bid` and shows on the
# storefront as incoming stock. When its GovDeals auction closes, nothing told
# the site — so it gets flipped fake-sold-out here, and put back (plus a
# Telegram ping) if the auction relists. Detail: automation/auction_sync.py.
#
# Same placement rule as the tracking poller: in the web process, NOT a Render
# cron — the Render blueprint has not been re-applied since 2026-07-18, so a
# cron declared in render.yaml would simply never run.
_AUCTION_SYNC_INTERVAL_SEC = float(os.getenv("AUCTION_SYNC_INTERVAL_SEC", "1800"))
_AUCTION_SYNC_ENABLED = (os.getenv("AUCTION_SYNC_ENABLED", "1").strip().lower()
                         not in ("0", "false", "no", "off"))
_auction_sync_task: asyncio.Task | None = None


def _auction_sync_pass() -> dict:
    """Blocking: one reconcile pass. Runs in a worker thread off the tick.

    Cheap when nothing changed — one indexed SELECT plus two maestro calls per
    watched lot, and the watched set is a handful of lots, not the whole site.
    """
    from automation import auction_sync
    return auction_sync.sync_once(log=lambda m: None)


async def _auction_sync_tick() -> None:
    try:
        rep = await asyncio.to_thread(_auction_sync_pass)
        if rep.get("expired") or rep.get("relisted") or rep.get("errors") or rep.get("error"):
            print(f"[auction-sync] {rep}")
    except Exception as e:
        # Same rule as the other two ticks: one bad pass must not kill the loop.
        print(f"[auction-sync] tick error: {e!r}")


async def _auction_sync_loop() -> None:
    while True:
        await _auction_sync_tick()
        await asyncio.sleep(_AUCTION_SYNC_INTERVAL_SEC)


@app.on_event("startup")
async def _start_alerts_loop() -> None:
    global _alerts_task, _tracking_task, _geo_warm_task, _channel_sync_task, _auction_sync_task
    # Pre-warm the DB pool: constructing it is non-blocking (psycopg_pool fills
    # min_size in worker threads), so the first admin open after a boot doesn't
    # pay the pooler handshake. Skipped when no DSN is configured (tests, CI).
    if os.getenv("BLACKWHOLE_DB_URL"):
        try:
            await asyncio.to_thread(db.get_pool)
        except Exception as e:  # never block startup on the pool
            print(f"[db] pool pre-warm skipped: {e!r}")
    # Pre-warm pgeocode: constructing Nominatim("us") downloads the GeoNames US
    # dataset to ~/.cache/pgeocode the first time, which is seconds of blocking
    # work no map request should pay for. Fire-and-forget, NEVER awaited: the app
    # serves nothing (not even /api/health) until this hook returns, and pgeocode
    # opens that URL with no timeout — one blackholed connection would hang the
    # boot forever. The task's own failures are logged inside `_pgeocode_us`.
    try:
        from ..alerts import geo as _geo
        # Held in a global: asyncio keeps only a weak reference to a bare task.
        _geo_warm_task = asyncio.create_task(asyncio.to_thread(_geo._pgeocode_us))
    except Exception as e:  # never block startup on the geocoder
        print(f"[geo] pgeocode pre-warm skipped: {e!r}")
    if _tracking_task is None or _tracking_task.done():
        _tracking_task = asyncio.create_task(_tracking_loop())
        print(f"[tracking] bid-history poller started (tick={_SCHEDULER_TICK_SEC:.0f}s)")
    if _alerts_task is None or _alerts_task.done():
        _alerts_task = asyncio.create_task(_alerts_loop())
        print(
            f"[favorites] countdown scheduler started "
            f"(tick={_SCHEDULER_TICK_SEC:.0f}s, intervals="
            f"{[l for l,_ in favorites.ALERT_INTERVALS]})"
        )
    sync_sec = _channel_sync_interval()
    if sync_sec and (_channel_sync_task is None or _channel_sync_task.done()):
        _channel_sync_task = asyncio.create_task(_channel_sync_loop())
        print(f"[channels] sync loop started (every {sync_sec:.0f}s; CHANNEL_SYNC_SEC=0 disables)")
    if _AUCTION_SYNC_ENABLED and (_auction_sync_task is None or _auction_sync_task.done()):
        _auction_sync_task = asyncio.create_task(_auction_sync_loop())
        print(f"[auction-sync] expiry/relist sync started "
              f"(every {_AUCTION_SYNC_INTERVAL_SEC / 60:.0f} min)")
    elif not _AUCTION_SYNC_ENABLED:
        print("[auction-sync] disabled (AUCTION_SYNC_ENABLED=0)")


@app.on_event("shutdown")
async def _stop_alerts_loop() -> None:
    global _alerts_task
    await asyncio.to_thread(db.reset_pool)   # close pooled sockets off-loop
    for task in (_alerts_task, _tracking_task, _channel_sync_task, _auction_sync_task):
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass


@app.get("/api/auctions/cache-stats")
@readcache.cached(ttl=60)
def auctions_cache_stats():
    """Cheap roll-up over the Supabase `auction_listings` table — powers the
    'N lots in cache · newest scraped X days ago' header on the Auctions tab."""
    if _auctions_cache_stats is None:
        return {"total": 0, "newest_seen_at": None, "oldest_seen_at": None, "by_source": {}}
    return _auctions_cache_stats()


@app.get("/api/listings")
async def list_raw_listings(
    source: str = "all",           # 'all' | 'gd' | 'ps' | 'bs'
    q: str = "",                   # text search over title + description
    min_qty: int = 1,
    max_qty: int = 99999,
    status: str = "all",           # 'all' | 'active' | 'expired' | 'unknown'
    seen_within_days: int = 0,     # 0 = any
    sort: str = "qty_desc",        # 'qty_desc' | 'qty_asc' | 'price_low' | 'last_seen_desc' | 'first_seen_desc'
    limit: int = 50,
    offset: int = 0,
):
    """Admin DB browser over Supabase `auction_listings` — the same table the
    Auctions tab reads — as raw rows with filters. No ranking / LLM step.
    (It used to read the laptop's listings.db, which the cloud scrape stopped
    writing in July; see auctions_supabase.browse_listings.)"""
    if browse_listings is None:
        raise HTTPException(503, "auction listings loader not available")
    limit = max(1, min(int(limit), 500))
    offset = max(0, int(offset))
    min_qty = max(0, int(min_qty))
    max_qty = max(min_qty, int(max_qty))
    seen_within_days = max(0, int(seen_within_days))

    total, rows = await asyncio.to_thread(
        browse_listings, source=source, q=q, min_qty=min_qty, max_qty=max_qty,
        status=status, seen_within_days=seen_within_days, sort=sort,
        limit=limit, offset=offset,
    )

    def _source_of(link: str) -> str:
        if "govdeals.com" in link: return "gd"
        if "publicsurplus.com" in link: return "ps"
        if "bidspotter.com" in link: return "bs"
        return "other"

    for r in rows:
        r["source"] = _source_of(r.get("link") or "")
        # Truncate description so the network payload stays small.
        desc = r.get("description") or ""
        if len(desc) > 400:
            r["description"] = desc[:400] + "…"

    return {"items": rows, "total": total, "limit": limit, "offset": offset}


@app.get("/api/health", response_class=PlainTextResponse)
async def health():
    return "ok"


# ───────────────────────────── inventory API ─────────────────────────────

def _inventory_to_public(row: dict) -> dict:
    """Enrich an inventory row with the image URL the UI can render directly.

    Strips `govdeals_password` from the response (the admin only needs to know
    *whether* one is on file). Adds `buyer_cert_url` when an attachment exists.
    """
    out = dict(row)
    # Prefer the durable Supabase Storage URL (BLACKWHOLE-6); only synthesize a
    # local /image/ path when no cloud URL is on file. Don't clobber the cloud URL.
    out["hero_image_url"] = _hero_src(row)
    out["govdeals_password_set"] = bool(out.pop("govdeals_password", None))
    # One editable cell on the Inventory tab: "Baltimore, MD x1200; Atlanta, GA".
    out["locations_text"] = "; ".join(
        loc["city"]
        + (f", {loc['state']}" if loc.get("state") else "")
        + (f" x{loc['quantity']}" if loc.get("quantity") else "")
        for loc in (row.get("locations") or [])
        if isinstance(loc, dict) and loc.get("city")
    )
    if out.get("buyer_cert_path"):
        out["buyer_cert_url"] = f"/api/inventory/{row['lot_id']}/buyer-cert"
    else:
        out["buyer_cert_url"] = None
    return out


@app.get("/api/inventory")
@readcache.cached()
def inv_list(status: str | None = None, with_stats: int = 0):
    """`with_stats=1` returns rows + headline counts from ONE connection —
    the admin tab uses it so a tab open costs one pooler handshake, not two."""
    if with_stats:
        data = inventory.list_with_stats(status)
        return {"items": [_inventory_to_public(r) for r in data["items"]],
                "stats": data["stats"]}
    rows = inventory.list_all(status=status)
    return {"items": [_inventory_to_public(r) for r in rows]}


@app.get("/api/inventory/{lot_id}")
def inv_get(lot_id: str):
    row = inventory.get(lot_id)
    if not row:
        raise HTTPException(404, "not found")
    return _inventory_to_public(row)


@app.patch("/api/inventory/{lot_id}")
def inv_update(lot_id: str, payload: dict):
    payload = dict(payload or {})
    if "locations_text" in payload:
        payload["locations"] = payload.pop("locations_text")
    try:
        row = inventory.set_fields(lot_id, **payload)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        # The chair-data inputs always render; before migration 021 their
        # columns don't exist. Say which command adds them instead of a bare 500.
        if type(e).__name__ == "UndefinedColumn" and (set(payload) & inventory.CHAIR_FIELDS):
            raise HTTPException(409, freight_log.MIGRATION_HINT)
        raise
    if not row:
        raise HTTPException(404, "not found")
    return _inventory_to_public(row)


@app.post("/api/inventory")
def inv_create(payload: dict):
    payload = payload or {}
    try:
        row = inventory.insert_manual(
            lot_id=str(payload["lot_id"]).strip(),
            title=str(payload["title"]).strip(),
            quantity=int(payload["quantity"]),
            subtitle=payload.get("subtitle") or None,
            price_per_chair=(float(payload["price_per_chair"])
                             if payload.get("price_per_chair") else None),
            city=payload.get("city") or None,
            state=payload.get("state") or None,
            zip_code=payload.get("zip_code") or None,
            chair_type=payload.get("chair_type") or None,
            dimensions=payload.get("dimensions") or None,
            description=payload.get("description") or None,
            folder_name=payload.get("folder_name") or None,
            hero_image=payload.get("hero_image") or None,
            locations=payload.get("locations") or payload.get("locations_text") or None,
            status=(payload.get("status") or "draft"),
        )
    except (KeyError, ValueError) as e:
        raise HTTPException(400, str(e))
    return _inventory_to_public(row)


@app.delete("/api/inventory/{lot_id}")
def inv_delete(lot_id: str):
    ok = inventory.delete(lot_id)
    if not ok:
        raise HTTPException(404, "not found")
    return {"ok": True}


_ALLOWED_LINK_PLATFORMS = ("facebook", "ebay", "fb_business", "ad")


@app.post("/api/inventory/{lot_id}/platform")
def inv_set_platform(lot_id: str, payload: dict):
    """Backfill a platform URL for a lot that was listed/posted manually.

    Platforms: facebook, ebay, fb_business (FB page post), ad (paid placement).
    """
    payload = payload or {}
    platform = payload.get("platform")
    url = (payload.get("url") or "").strip() or None
    if platform not in _ALLOWED_LINK_PLATFORMS:
        raise HTTPException(
            400, f"platform must be one of {_ALLOWED_LINK_PLATFORMS}"
        )
    row = inventory.set_platform_url(
        lot_id, platform, url, clear_timestamp=(url is None),
    )
    if not row:
        raise HTTPException(404, "not found")
    return _inventory_to_public(row)


# ───────────────────────────── buyer cert ─────────────────────────────

# 10 MB cap on the attachment — a buyer certificate is a PDF/screenshot, not
# a video. Protects the server from a stray multi-GB upload tying up RAM.
_MAX_CERT_BYTES = 10 * 1024 * 1024


@app.post("/api/inventory/{lot_id}/buyer-cert")
async def inv_attach_buyer_cert(lot_id: str, file: UploadFile = File(...)):
    """Upload a winning-bid certificate (PDF/image) for a lot."""
    data = await file.read()
    if not data:
        raise HTTPException(400, "empty upload")
    if len(data) > _MAX_CERT_BYTES:
        raise HTTPException(413, f"file exceeds {_MAX_CERT_BYTES // (1024*1024)} MB")
    row = await asyncio.to_thread(inventory.attach_buyer_cert, lot_id, file.filename or "buyer_cert", data)
    if not row:
        raise HTTPException(404, "lot not found")
    return _inventory_to_public(row)


@app.get("/api/inventory/{lot_id}/buyer-cert")
def inv_get_buyer_cert(lot_id: str):
    row = inventory.get(lot_id)
    if not row:
        raise HTTPException(404, "lot not found")
    path = inventory.buyer_cert_abs_path(row)
    if not path or not path.exists():
        raise HTTPException(404, "no certificate on file")
    return FileResponse(
        str(path),
        filename=row.get("buyer_cert_filename") or path.name,
    )


@app.delete("/api/inventory/{lot_id}/buyer-cert")
def inv_delete_buyer_cert(lot_id: str):
    row = inventory.delete_buyer_cert(lot_id)
    if not row:
        raise HTTPException(404, "lot not found")
    return _inventory_to_public(row)


@app.get("/api/inventory-stats")
@readcache.cached()
def inv_stats():
    return inventory.stats()


@app.get("/api/site-config")
async def site_config():
    return {"facebook_business_url": FACEBOOK_BUSINESS_URL or None}


@app.post("/api/inventory/seed-snapshot")
def inv_seed_snapshot(payload: dict):
    """Idempotent bulk-upsert for an admin-curated inventory snapshot.

    Body: {"rows": [{"lot_id": "...", "title": "...", "quantity": N,
    "city": "...", "state": "...", "status": "..."}]}.
    Updates existing rows by lot_id; inserts new ones. Returns counts.
    """
    rows = (payload or {}).get("rows") or []
    added = 0
    updated = 0
    for r in rows:
        lot_id = str(r.get("lot_id", "")).strip()
        if not lot_id:
            continue
        existing = inventory.get(lot_id)
        if existing:
            inventory.set_fields(
                lot_id,
                **{k: r[k] for k in (
                    "title", "quantity_remaining", "city", "state", "zip_code",
                    "status", "chair_type", "price_per_chair",
                ) if k in r and r[k] is not None},
            )
            updated += 1
        else:
            try:
                inventory.insert_manual(
                    lot_id=lot_id,
                    title=r.get("title") or lot_id,
                    quantity=int(r.get("quantity") or r.get("quantity_remaining") or 0),
                    price_per_chair=r.get("price_per_chair"),
                    city=r.get("city"),
                    state=r.get("state"),
                    zip_code=r.get("zip_code"),
                    chair_type=r.get("chair_type"),
                )
                if r.get("status"):
                    inventory.set_fields(lot_id, status=r["status"])
                added += 1
            except ValueError:
                pass
    return {"added": added, "updated": updated, "total": len(rows)}


@app.post("/api/inventory/backfill")
def inv_backfill():
    """Walk DOWNLOAD_ROOT, add a draft row for any folder not in the table.

    Best-effort: pulls title/qty/city/chair_type from the same
    llm_compare_logs lookup the Drafts tab uses. FB/eBay URLs stay NULL;
    admin must paste them manually for pre-tracking listings.
    """
    added: list[str] = []
    updated: list[str] = []
    skipped: list[str] = []
    compare_rows = _all_compare_rows()
    for folder in _list_listing_folders():
        meta = _latest_compare_for_folder(folder.name, compare_rows) or {}
        primary = meta.get("primary") or {}
        dom_hint = meta.get("dom_hint") or {}
        # Can we derive a stable lot_id? The folder name doesn't carry one, but
        # if the llm log does, use it. Otherwise synthesize "folder:<slug>" so
        # at least the admin can see + edit the row. Real lot_id can be pasted
        # in later.
        lot_id = (primary.get("lot_id") or dom_hint.get("lot_id")
                  or f"folder:{folder.name}")
        existing = inventory.get(lot_id)
        imgs = _folder_images(folder)
        hero = imgs[0] if imgs else None
        title = primary.get("title") or dom_hint.get("title") or folder.name
        city = primary.get("city") or dom_hint.get("city")
        state = primary.get("state") or dom_hint.get("state")
        zip_code = primary.get("zip_code") or dom_hint.get("zip_code")
        qty_raw = primary.get("quantity") or dom_hint.get("quantity")
        try:
            qty_int = int(qty_raw) if qty_raw and str(qty_raw).isdigit() else None
        except Exception:
            qty_int = None
        if existing is None:
            try:
                inventory.insert_manual(
                    lot_id=lot_id,
                    title=title,
                    quantity=qty_int or 0,
                    city=city, state=state, zip_code=zip_code,
                    chair_type=primary.get("chair_type"),
                    dimensions=primary.get("dimensions"),
                    price_per_chair=(
                        float(primary["suggested_price_per_chair"])
                        if primary.get("suggested_price_per_chair") else None
                    ),
                    folder_name=folder.name,
                    hero_image=hero,
                )
                added.append(lot_id)
            except Exception:
                skipped.append(folder.name)
        else:
            # Keep user edits, just refresh folder binding + hero if missing.
            patch = {}
            if not existing.get("hero_image") and hero:
                patch["hero_image"] = hero
            if patch:
                inventory.set_fields(lot_id, **patch)
                updated.append(lot_id)
            else:
                skipped.append(lot_id)
    return {"added": added, "updated": updated, "skipped": skipped,
            "counts": {"added": len(added), "updated": len(updated),
                       "skipped": len(skipped)}}


# ───────────────────────────── inquiries API ─────────────────────────────

@app.get("/api/inquiries")
@readcache.cached()
def inq_list(status: str | None = None):
    # sync handler → FastAPI threadpool; memoised (readcache.py)
    return {"items": inventory.list_inquiries(status)}


@app.patch("/api/inquiries/{inquiry_id}")
def inq_update(inquiry_id: int, payload: dict):
    payload = payload or {}
    row: dict | None = None
    if "status" in payload:
        try:
            row = inventory.set_inquiry_status(inquiry_id, payload["status"])
        except ValueError as e:
            raise HTTPException(400, str(e))
    if "lot_id" in payload:
        row = inventory.link_inquiry(inquiry_id, payload["lot_id"] or None)
    if row is None:
        row = inventory.get_inquiry(inquiry_id)
    if row is None:
        raise HTTPException(404, "not found")
    return row


@app.delete("/api/inquiries/{inquiry_id}")
def inq_delete(inquiry_id: int):
    ok = inventory.delete_inquiry(inquiry_id)
    if not ok:
        raise HTTPException(404, "not found")
    return {"ok": True}


# ─────────────────────────── freight quotes API ───────────────────────────
# The Sales tab's Quotes view: every storefront (and CRM) freight request with a
# follow-up status and, where one was fetched, the cheapest real carrier price
# beside the range we showed. Admin-only by construction — everything here is
# under /api/. `raw_response` (calibration internals + the caller's IP) is never
# selected by `freight_log`, so it cannot leak through these routes.

def _freight_quote_view(row: dict) -> dict:
    """A stored request plus the two things the tab derives from it."""
    out = dict(row)
    mode = "partial" if out.get("mode") == "partial" else "ltl"
    if out.get("mode") == "both" and None not in (
        out.get("ltl_low"), out.get("ltl_high"), out.get("partial_low"), out.get("partial_high")
    ):
        # `recommended_mode` is not stored; re-derive it the way the estimator
        # does — the range with the cheaper midpoint is the one shown first.
        ltl_mid = (out["ltl_low"] + out["ltl_high"]) / 2
        partial_mid = (out["partial_low"] + out["partial_high"]) / 2
        mode = "ltl" if ltl_mid <= partial_mid else "partial"
    low, high = out.get(f"{mode}_low"), out.get(f"{mode}_high")
    if low is None or high is None:
        low, high = out.get("ltl_low"), out.get("ltl_high")
    out["shown_low"], out["shown_high"] = low, high
    out["price_check"] = warp_rates.price_check(low, high, out.get("carrier_low"))
    # Stock to compare the ask against: what the lot held when the buyer asked
    # (recorded since migration 021), else what it holds now — an older row has
    # no snapshot, and "asked 160, lot has 100" is still worth seeing.
    at_request = out.get("lot_quantity_remaining")
    stock = at_request if at_request is not None else out.get("lot_quantity_now")
    out["stock_compared"] = stock
    out["stock_is_current"] = at_request is None and stock is not None
    out["over_stock"] = bool(
        stock is not None and out.get("quantity") and stock > 0
        and out["quantity"] > stock
    )
    return out


@app.get("/api/freight-quotes")
@readcache.cached()
def freight_quotes_list(status: str | None = None):
    # sync handler → FastAPI threadpool; memoised (readcache.py)
    try:
        items = freight_log.list_quotes(status or None)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {
        "items": [_freight_quote_view(r) for r in items],
        "schema_ready": freight_log.schema_ready(),
        "migration_hint": freight_log.MIGRATION_HINT,
        "statuses": list(freight_log.QUOTE_STATUSES),
    }


@app.patch("/api/freight-quotes/{quote_id}")
def freight_quote_update(quote_id: int, payload: dict):
    payload = payload or {}
    if "status" not in payload and "note" not in payload:
        raise HTTPException(400, "nothing to update")
    try:
        row = freight_log.set_quote_status(
            quote_id,
            status=payload.get("status") if "status" in payload else None,
            note=("" if payload.get("note") is None else str(payload["note"]))
            if "note" in payload else None,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    except freight_log.SchemaNotReady as e:
        raise HTTPException(409, str(e))
    if row is None:
        raise HTTPException(404, "not found")
    return _freight_quote_view(row)


@app.post("/api/freight-quotes/{quote_id}/carrier-check")
def freight_quote_carrier_check(quote_id: int):
    """Ask Warp again for this lane (~20-45 s). Sync handler → threadpool."""
    if not freight_log.schema_ready():
        raise HTTPException(409, freight_log.MIGRATION_HINT)
    if not warp_rates.enabled():
        raise HTTPException(409, "carrier checks are switched off (WARP_RATES_ENABLED=0)")
    if freight_log.get_quote(quote_id) is None:
        raise HTTPException(404, "not found")
    try:
        summary = _run_carrier_check(quote_id)
    except CarrierBudgetExceeded as e:
        raise HTTPException(429, f"{e} — try again next hour")
    if summary is None:
        raise HTTPException(409, "this request has no priced lane to check")
    return _freight_quote_view(freight_log.get_quote(quote_id) or {})


@app.get("/api/sales/counts")
@readcache.cached()
def sales_counts():
    """What is waiting on the operator — the badge on the Sales rail tab."""
    return {
        "quotes_new": freight_log.count_new(),
        "inquiries_new": len(inventory.list_inquiries("new")),
    }


# ───────────────────────────── deposits API ─────────────────────────────
# The money ledger behind the storefront's Reserve button (B4). Admin-only by
# construction: everything here is under /api/, which the auth middleware gates.
#
# Two deliberate non-features:
#   1. **No refunds from here.** A refund is executed in the Stripe dashboard
#      and arrives back as a `charge.refunded` webhook, which flips the row.
#      A "refund" button here would be a second source of truth for money.
#   2. **No inventory side-effects.** A paid deposit does NOT decrement
#      `quantity_remaining` (v1 decision) — the operator adjusts by hand once
#      the pickup/freight is actually arranged.
#
# `set_admin_fields` is the manual override for when reality and Stripe
# disagree, so it is NOT state-machine gated. The UI only offers `→ canceled`
# on pending/processing rows; the API stays permissive on purpose.

@app.get("/api/deposits")
async def dep_list(status: str | None = None):
    if status and status not in deposits.DEPOSIT_STATUSES:
        raise HTTPException(400, f"invalid status: {status}")
    items = await asyncio.to_thread(deposits.list_deposits, status=status or None)
    return {"items": items}


@app.patch("/api/deposits/{deposit_id}")
async def dep_update(deposit_id: int, payload: dict):
    payload = payload or {}
    status = payload.get("status")
    if status is not None and status not in deposits.DEPOSIT_STATUSES:
        raise HTTPException(400, f"invalid status: {status}")
    try:
        row = await asyncio.to_thread(
            deposits.set_admin_fields,
            deposit_id,
            status=status,
            admin_note=payload.get("admin_note"),
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    if row is None:
        raise HTTPException(404, "not found")
    return row


@app.delete("/api/deposits/{deposit_id}")
async def dep_delete(deposit_id: int):
    ok = await asyncio.to_thread(deposits.delete_deposit, deposit_id)
    if not ok:
        raise HTTPException(404, "not found")
    return {"ok": True}


# ───────────────────────────── site settings API ─────────────────────────────
# The live deposit rule (pct + floor), editable from the Deposits tab without a
# redeploy. Distinct from `/api/site-config` — that one reports deployment
# config and is load-bearing for the auth tests; do not merge them.

@app.get("/api/settings")
async def settings_get():
    return await asyncio.to_thread(site_settings.get_all)


@app.patch("/api/settings")
async def settings_update(payload: dict):
    try:
        return await asyncio.to_thread(site_settings.set_many, payload or {})
    except ValueError as e:
        raise HTTPException(400, str(e))


# ───────────────────────────── channels API ─────────────────────────────
# Multichannel Phase 1.5. `inventory` stays the master; `listing_channels` holds
# one row per lot × channel and the Channels tab reads/edits it here. Admin-only
# by construction (under /api/). Every write drops the readcache memo through
# the middleware. Three rules the routes enforce, not the UI:
#   1. The switches PATCH only touches `channel*` / `browser_channel*` keys —
#      the deposit rule is not editable from here.
#   2. Approving a queued post on a DISABLED channel is a 409: the approval
#      queue is never a back door past the switch. **FB Marketplace ships OFF
#      and nothing in code flips it.**
#   3. Reject always works — taking something out of the queue needs no switch.

_SWITCH_PREFIXES = ("channel", "browser_channel")


def _channel_switches(values: dict) -> dict:
    return {k: v for k, v in values.items() if k.startswith(_SWITCH_PREFIXES)}


@app.get("/api/channels")
@readcache.cached()
def channels_get():
    # sync handler → FastAPI threadpool; memoised (readcache.py)
    return {
        "switches": _channel_switches(site_settings.get_all()),
        "matrix": channel_store.matrix(),
        "queue": channel_store.queue(),
        "channels": {
            "all": list(channels_pkg.CHANNELS),
            "feed": list(channels_pkg.FEED_CHANNELS),
            "push": list(channels_pkg.PUSH_CHANNELS),
            "browser": sorted(channels_pkg.BROWSER_CHANNELS),
            "approval": sorted(channels_pkg.APPROVAL_CHANNELS),
        },
    }


@app.patch("/api/channels/switches")
def channels_switches_update(payload: dict):
    payload = payload or {}
    if not payload:
        raise HTTPException(400, "no switches given")
    bad = [k for k in payload if not str(k).startswith(_SWITCH_PREFIXES)]
    if bad:
        raise HTTPException(400, f"not a channel switch: {', '.join(map(str, bad))}")
    try:
        return _channel_switches(site_settings.set_many(payload))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/channels/queue/{row_id}/approve")
def channels_queue_approve(row_id: int):
    row = channel_store.get_by_id(row_id)
    if row is None:
        raise HTTPException(404, "not found")
    if not site_settings.channel_enabled(row["channel"]):
        raise HTTPException(409, {"reason": "channel disabled"})
    out = channel_store.approve(row_id)
    if out is None:
        raise HTTPException(409, {"reason": "not pending approval"})
    return out


@app.post("/api/channels/queue/{row_id}/reject")
def channels_queue_reject(row_id: int):
    if channel_store.get_by_id(row_id) is None:
        raise HTTPException(404, "not found")
    out = channel_store.reject(row_id)
    if out is None:
        raise HTTPException(409, {"reason": "not pending approval"})
    return out


@app.post("/api/channels/sync")
async def channels_sync_now():
    """Manual 'Sync now'. Same pass the background loop runs."""
    try:
        return await asyncio.to_thread(channel_sync.run_once)
    except Exception as e:  # noqa: BLE001 — surface, don't 500
        raise HTTPException(502, f"channel sync failed: {e!r}")


# ───────────────────────────── subscribers API ─────────────────────────────
# Alert-signup rows captured by POST /subscribe (BLACKWHOLE-10). Status-only
# updates — subscribers aren't lot-scoped, so there is no link step.

@app.get("/api/subscribers")
@readcache.cached()
def sub_list(status: str | None = None):
    # sync handler → FastAPI threadpool; memoised (readcache.py)
    return {"items": inventory.list_subscribers(status)}


@app.patch("/api/subscribers/{subscriber_id}")
def sub_update(subscriber_id: int, payload: dict):
    payload = payload or {}
    row: dict | None = None
    if "status" in payload:
        try:
            row = inventory.set_subscriber_status(subscriber_id, payload["status"])
        except ValueError as e:
            raise HTTPException(400, str(e))
    if row is None:
        row = inventory.get_subscriber(subscriber_id)
    if row is None:
        raise HTTPException(404, "not found")
    return row


@app.delete("/api/subscribers/{subscriber_id}")
def sub_delete(subscriber_id: int):
    ok = inventory.delete_subscriber(subscriber_id)
    if not ok:
        raise HTTPException(404, "not found")
    return {"ok": True}


# ─────────────────────── alert blast (BLACKWHOLE-10) ───────────────────────
# Admin-only (gated by the /api/* session auth middleware). The matcher +
# provider-agnostic sender live in automation.alerts. SEND IS OFF BY DEFAULT:
# the blast endpoint runs dry-run unless config.ALERTS_SEND_ENABLED is set AND a
# provider is registered — so hitting it with the default config emails nothing
# and writes no alert_sends rows. Run in a threadpool: the job is sync DB work.

@app.post("/api/alerts/blast/{lot_id}/preview")
async def alerts_blast_preview(lot_id: str):
    """Match subscribers to a lot and return recipients + reasons. Sends nothing."""
    report = await asyncio.to_thread(alerts_blast.preview_blast, lot_id)
    if any("not found" in n for n in report.notes):
        raise HTTPException(404, f"lot {lot_id} not found")
    return report.as_dict()


@app.post("/api/alerts/blast/{lot_id}")
async def alerts_blast_run(lot_id: str):
    """Run the blast for a lot. Dry-run unless send is enabled in config."""
    report = await asyncio.to_thread(alerts_blast.run_blast, lot_id)
    if any("not found" in n for n in report.notes):
        raise HTTPException(404, f"lot {lot_id} not found")
    return report.as_dict()


# ───────────────────────────── entrypoint ─────────────────────────────

def main() -> None:
    import uvicorn
    host = os.getenv("LISTING_WEB_HOST", "127.0.0.1")
    port = int(os.getenv("LISTING_WEB_PORT", "8765"))
    reload = os.getenv("LISTING_WEB_RELOAD", "1") not in ("0", "false", "False", "")
    reload_dirs = [str(Path(__file__).resolve().parent.parent)] if reload else None
    uvicorn.run(
        "automation.web.app:app",
        host=host, port=port,
        reload=reload, reload_dirs=reload_dirs,
        log_level="info",
    )


if __name__ == "__main__":
    main()
