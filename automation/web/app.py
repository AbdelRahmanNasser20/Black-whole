"""FastAPI dashboard.

Routes:
  GET  /                  → single page with all three tabs
  GET  /api/runs/state    → snapshot of current/last run
  POST /api/runs/start    → kick off run.py with a GovDeals URL
  GET  /api/runs/stream   → SSE: progress + raw stdout lines
  GET  /api/drafts        → JSON list of listing folders + metadata
  GET  /api/compare       → JSON list of llm_compare_logs rows (Supabase)
  POST /api/compare/{ts}/rate → save a star rating (matched / wrong)
  GET  /image/{folder}/{name} → serve image from a listing folder
  GET  /screenshot/{folder}/{name} → serve a Playwright screenshot
  POST /subscribe             → public alerts signup → subscribers table
  GET/PATCH/DELETE /api/subscribers[/{id}] → admin Subscribers tab

Streams stdout from run.py as Server-Sent Events. Parses
`<<<EVENT>>>{json}` lines emitted by automation.progress.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import sys
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
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
    PUBLIC_BASE_URL,
)
from ..progress import EVENT_PREFIX, parse as parse_event
from .. import config as app_config
from .. import db
from .. import catalog_feed
from .. import inventory
from .. import lot_images
from .. import favorites
from .. import telegram_alerts
from .. import deposits
from .. import site_settings
from .. import stripe_gateway
from .. import freight_estimate
from .. import freight_log
from ..alerts import blast as alerts_blast
from . import deals_query
from . import rate_limit
from . import auth as auth_svc
from deals.fees import fee_model_from_env
from deals.geo import distance_from_home

try:
    # Auctions tab reads the shared Supabase `auction_listings` table. The
    # loader reuses the upstream ranking/condition helpers, so card output is
    # identical to the old SQLite path — only the data source changed.
    from ..auctions_supabase import get_top_chairs, cache_stats as _auctions_cache_stats
except Exception:  # pragma: no cover
    get_top_chairs = None  # unavailable; /api/auctions will 503
    _auctions_cache_stats = None

PKG_DIR = Path(__file__).parent
TEMPLATE_DIR = PKG_DIR / "templates"
STATIC_DIR = PKG_DIR / "static"
PROJECT_ROOT = PKG_DIR.parents[1]
AUCTION_EXTRACTORS_DIR = PROJECT_ROOT / "auction_extractors"

PHASES = ["scrape", "llm", "download", "dewatermark", "facebook", "ebay"]

app = FastAPI(title="listing_automation dashboard")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))

# "auth once" (BLACKWHOLE-14): gate /admin + /api/* behind a 365-day signed
# session cookie once ADMIN_PASSWORD is set (no-op otherwise). The public
# storefront stays open. Cookie/signing mechanics + the env-var contract live
# in automation/web/auth.py.
app.middleware("http")(auth_svc.session_auth_middleware)


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
    elif kind == "price":
        state.suggested_price = ev.get("suggested")
        state.confirmed_price = ev.get("confirmed")


async def _run_subprocess(url: str, extra_args: list[str]) -> None:
    """Spawn `python run.py <url>` and pump its output into the event stream."""
    project_root = Path(__file__).resolve().parents[2]
    run_py = project_root / "run.py"
    cmd = [sys.executable, "-u", str(run_py), url] + extra_args
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
    asyncio.create_task(_run_subprocess(item["url"], item.get("extra_args") or []))
    await _broadcast_queue()


# ───────────────────────────── HTTP routes ─────────────────────────────

@app.get("/admin", response_class=HTMLResponse)
async def admin(request: Request):
    return templates.TemplateResponse(
        request,
        "index.html",
        {"phases": PHASES, "now": int(time.time())},
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
        "now": int(time.time()),
        "facebook_business_url": FACEBOOK_BUSINESS_URL or None,
        "base_url": PUBLIC_BASE_URL,
        "google_site_verification": GOOGLE_SITE_VERIFICATION or None,
        "reserve_enabled": _reserve_enabled(),
        **extra,
    }


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

    seo_title = f"{qty}× {title}" if qty else title
    if loc:
        seo_title += f" — {loc}"
    seo_title += " | Black Whole Liquidation"

    desc_bits = []
    if qty:
        desc_bits.append(f"{qty} sold" if sold else f"{qty} available")
    if row.get("price_per_chair"):
        desc_bits.append(f"${row['price_per_chair']:.0f}/chair")
    if loc:
        desc_bits.append(("sourced from " if sold else "pickup in ") + loc)
    if sold:
        lead = f"{title} — this lot has sold" + (
            f" ({' · '.join(desc_bits)})." if desc_bits else "."
        ) + " We buy sets like it every week; ask us about the next one."
    else:
        lead = f"{title} for sale in bulk" + (
            f" — {' · '.join(desc_bits)}." if desc_bits else "."
        )
    body = (row.get("description") or "").strip()
    if body:
        lead += " " + (body[:150] + "…" if len(body) > 150 else body)

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
        "sku": row.get("lot_id"),
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
        # </ escaped so a scraped description can't close the <script> tag
        "product_jsonld": json.dumps(product, ensure_ascii=False).replace("</", "<\\/"),
    }


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


@app.get("/", response_class=HTMLResponse)
async def public_landing(request: Request):
    try:
        counts = inventory.stats()
        # Featured carousel: Idaho lots lead (the Boise nationwide-ships
        # campaign), then the rest in ledger order.
        def _idaho_first(r: dict) -> int:
            state = (r.get("state") or "").strip().upper()
            city = (r.get("city") or "").strip().lower()
            return 0 if state in ("ID", "IDAHO") or "boise" in city else 1
        featured = sorted(inventory.list_public(), key=_idaho_first)[:12]
        for r in featured:
            r["hero_src"] = _hero_src(r)
    except Exception:
        counts = {"lots": 0, "chairs": 0, "cities": 0, "moved": 0}
        featured = []
    return templates.TemplateResponse(
        request, "landing.html",
        _public_ctx({"stats": counts, "featured": featured}),
    )


def _decorate(row: dict) -> dict:
    """Attach the derived fields every public card/page needs."""
    row["hero_src"] = _hero_src(row)
    row["location_labels"] = inventory.location_labels(row)
    row["is_sold"] = inventory.is_sold(row)
    return row


@app.get("/listings", response_class=HTMLResponse)
async def public_listings(request: Request):
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
                     "cities": cities, "chair_types": chair_types}),
    )


@app.get("/listings/{lot_id}", response_class=HTMLResponse)
async def public_listing_detail(request: Request, lot_id: str):
    row = inventory.get(lot_id)
    if not row or row.get("status") in ("hidden",):
        raise HTTPException(404, "listing not found")
    _decorate(row)
    hero = _hero_src(row)
    images = _gallery_srcs(row)
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
            **_detail_seo(row, hero, images),
        }),
    )


@app.get("/deals/{asset_id}/{account_id}/{auction_id}", response_class=HTMLResponse)
async def deal_listing(request: Request, asset_id: int, account_id: int, auction_id: int):
    row = db.fetch_one("""SELECT * FROM deal_lots
        WHERE asset_id=%s AND account_id=%s AND auction_id=%s""",
        (asset_id, account_id, auction_id))
    if not row:
        raise HTTPException(status_code=404, detail="lot not archived")
    return templates.TemplateResponse(request, "deal_listing.html", {"lot": row})


# ── Deals dashboard API (BLACKWHOLE-12) ─────────────────────────────────────

_DEALS_COLS = (
    "asset_id, account_id, auction_id, title, canonical_category, city, state, "
    "bid_count, current_bid, currency_code, end_utc, outcome, final_bid, "
    "outcome_complete, first_seen_at, hero_image_url, archived_hero_url, "
    "lat, lng"
)

_DEALS_ACTIVE = "outcome_complete IS NOT TRUE AND end_utc > now()"

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
    list_id: int | None = None,
    tag: str | None = None,
    max_distance: float | None = None,
):
    """Search/filter/sort deal_lots for the admin Deals tab.

    Facets reflect the full active set (not the filtered subset) — v1 keeps
    the SQL simple; counts guide, not gate.
    """
    if status not in ("active", "closed", "all"):
        raise HTTPException(400, "status must be active|closed|all")
    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset))
    where, args = deals_query.build_where(
        q=q, category=category, native=native, state=state, max_bids=max_bids,
        ending_within=ending_within, status=status,
        min_margin=min_margin, list_id=list_id, tag=tag,
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


@app.get("/api/deals/tree")
async def deals_tree(status: str = "active"):
    """Category tree for the Deals tab explorer: canonical bucket (branch) →
    native GovDeals category (twig), each with lot / zero-bid / ending-24h
    counts. Same status semantics as /api/deals."""
    if status not in ("active", "closed", "all"):
        raise HTTPException(400, "status must be active|closed|all")
    where, args = deals_query.build_where(status=status)

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
async def deals_lists():
    return db.fetch_all(
        "SELECT dl.id, dl.name, count(li.list_id) AS count "
        "FROM deal_lists dl LEFT JOIN deal_list_items li ON li.list_id = dl.id "
        "GROUP BY dl.id, dl.name ORDER BY dl.name"
    )


@app.post("/api/deals/lists")
async def deals_list_create(payload: dict):
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
async def deals_list_delete(list_id: int):
    n = db.execute("DELETE FROM deal_lists WHERE id=%s", (list_id,))
    if not n:
        raise HTTPException(404, "list not found")
    return {"ok": True}


@app.put("/api/deals/lists/{list_id}/items/{asset_id}/{account_id}/{auction_id}")
async def deals_list_item_add(list_id: int, asset_id: int, account_id: int,
                              auction_id: int):
    db.execute(
        "INSERT INTO deal_list_items (list_id, asset_id, account_id, auction_id) "
        "VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
        (list_id, asset_id, account_id, auction_id),
    )
    return {"ok": True}


@app.delete("/api/deals/lists/{list_id}/items/{asset_id}/{account_id}/{auction_id}")
async def deals_list_item_remove(list_id: int, asset_id: int, account_id: int,
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
async def deals_tags():
    return db.fetch_all(
        "SELECT tag, count(*) AS count FROM deal_lot_tags "
        "GROUP BY tag ORDER BY count DESC, tag"
    )


@app.put("/api/deals/tags/{asset_id}/{account_id}/{auction_id}/{tag}")
async def deals_tag_add(asset_id: int, account_id: int, auction_id: int, tag: str):
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
async def deals_tag_remove(asset_id: int, account_id: int, auction_id: int, tag: str):
    n = db.execute(
        "DELETE FROM deal_lot_tags WHERE asset_id=%s AND account_id=%s "
        "AND auction_id=%s AND tag=%s",
        (asset_id, account_id, auction_id, tag),
    )
    if not n:
        raise HTTPException(404, "tag not on lot")
    return {"ok": True}


@app.get("/api/deals/searches")
async def deals_searches():
    return db.fetch_all(
        "SELECT id, name, params, alert, created_at, last_run_at "
        "FROM saved_searches ORDER BY name"
    )


@app.post("/api/deals/searches")
async def deals_search_create(payload: dict):
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
async def deals_search_delete(search_id: int):
    n = db.execute("DELETE FROM saved_searches WHERE id=%s", (search_id,))
    if not n:
        raise HTTPException(404, "search not found")
    return {"ok": True}
def _deal_images(row: dict) -> list[str]:
    """Ordered image list for a lot: archived copies when we have them
    (durable Supabase URLs), else the GovDeals CDN hero."""
    imgs = [row.get("archived_hero_url") or row.get("hero_image_url")]
    imgs += row.get("gallery_urls") or []
    return [u for u in imgs if u]


@app.get("/api/deals/{asset_id}/{account_id}/{auction_id}")
async def deal_lot_json(asset_id: int, account_id: int, auction_id: int):
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


@app.get("/sell", response_class=HTMLResponse)
async def public_sell(request: Request):
    return templates.TemplateResponse(
        request, "sell.html", _public_ctx({}),
    )


@app.get("/robots.txt", response_class=PlainTextResponse)
async def robots_txt():
    return (
        "User-agent: *\n"
        "Disallow: /admin\n"
        "Disallow: /api/\n"
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
async def sitemap_xml():
    body = '<?xml version="1.0" encoding="UTF-8"?>\n'
    body += '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
    for path in ("/", "/listings", "/sell"):
        body += _sitemap_entry(f"{PUBLIC_BASE_URL}{path}")
    # Sold lots are indexable too (BLACKWHOLE-29): "500 banquet chairs Atlanta"
    # should land on our archive page and convert into a next-lot inquiry.
    for row in [*inventory.list_public(), *inventory.list_sold_showcase()]:
        updated = row.get("updated_at")
        lastmod = None
        if updated is not None:
            # timestamptz comes back as datetime from Postgres; sitemap wants a date
            lastmod = updated.date().isoformat() if hasattr(updated, "date") else str(updated)[:10]
        body += _sitemap_entry(f"{PUBLIC_BASE_URL}/listings/{row['lot_id']}", lastmod)
    body += "</urlset>\n"
    return Response(content=body, media_type="application/xml")


@app.get("/catalog/facebook.csv")
async def facebook_catalog_feed():
    """Facebook Business product-catalog feed (BLACKWHOLE-7).

    Public, read-only, no secrets — Commerce Manager pulls this URL on a
    schedule. One row per sellable lot (see `inventory.list_catalog_feed` for
    the status/quantity filter; `catalog_feed` drops rows FB would reject).
    Every product's `link` points back to `/listings/{lot_id}`. Operator setup
    is in docs/fb_catalog_feed_runbook.md.
    """
    body = catalog_feed.rows_to_csv(inventory.list_catalog_feed())
    return PlainTextResponse(body, media_type="text/csv; charset=utf-8")


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
    try:
        row = inventory.create_inquiry(
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
        )
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
    try:
        row = inventory.create_subscriber(
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
        )
    except (ValueError, TypeError) as e:
        raise HTTPException(400, str(e))
    asyncio.create_task(_notify_new_subscriber(row))
    return {"ok": True, "id": row["id"]}


# ── Freight estimate (public, self-serve) ────────────────────────────────────
# A buyer types their ZIP on a lot page and gets an honest RANGE. Public paths,
# deliberately outside `/api/` (auth.py's PROTECTED_PREFIXES), same as /contact
# and /reserve.
#
# THE HARD RULE: never invent a number. An unquotable lane (international,
# offshore/Alaska, unresolvable ZIP, or a lot whose origin we can't locate)
# returns HTTP 200 with `ok: false` and hands the buyer to the contact form. It
# does NOT guess, and it does not 500 — a lane we can't price is a normal
# outcome of a public form, not an error.

FREIGHT_UNQUOTABLE = {
    "ok": False,
    "reason": "unquotable",
    "message": (
        "We'll quote this lane by hand — send the request below and we'll come "
        "back with a real number."
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
        raise HTTPException(429, "rate_limited")
    if not rate_limit.allow("freight:global", limit=rate_limit.FREIGHT_GLOBAL_LIMIT):
        raise HTTPException(429, "rate_limited")


def _freight_range(quote: dict, low_key: str, high_key: str) -> dict | None:
    low, high = quote.get(low_key), quote.get(high_key)
    if low is None or high is None:
        return None
    return {"low": low, "high": high}


def _freight_public_estimate(quote: dict) -> dict:
    """The subset of the estimator's dict a browser may see.

    `raw` (calibration constants, NMFC class, the carrier's own response) stays
    server-side: it's the audit trail for a quote, not a spec sheet for a
    competitor, and every one of those knobs is tunable-by-us guesswork.
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


async def _notify_freight_estimate(
    row: dict, quote: dict, *, dest_zip: str, quantity: int, quote_id: int | None
) -> None:
    """Someone priced a real lane — that's a warm lead even without an email.

    Best-effort, exactly like `_notify_new_inquiry`: a dead Telegram must never
    surface as a failed estimate.
    """
    try:
        lot_id = row.get("lot_id") or "—"
        bits = [f"🚚 FREIGHT ESTIMATE · lot {lot_id}"]
        bits.append(
            f"{quantity} chairs → {dest_zip} · {_freight_range_str(quote)}"
        )
        bits.append(
            f"~{quote.get('miles')} mi · ~{quote.get('transit_days')} days · "
            f"via {quote.get('provider') or 'estimator'}"
            + (f" · quote #{quote_id}" if quote_id else "")
        )
        bits.append(f"→ {PUBLIC_BASE_URL}/listings/{lot_id}")
        await telegram_alerts.send_message("\n".join(bits), topic="leads")
    except Exception:
        pass


async def _notify_freight_email(quote_id: int, email: str) -> None:
    """The buyer traded their email for the estimate — that's the hot signal."""
    try:
        await telegram_alerts.send_message(
            f"📧 FREIGHT LEAD · quote #{quote_id} → {email}", topic="leads"
        )
    except Exception:
        pass


@app.post("/freight-estimate")
async def public_freight_estimate(payload: dict, request: Request):
    """`{lot_id, dest_zip, quantity?}` → a freight cost range for that lane."""
    _freight_rate_ok(request)
    payload = payload or {}

    lot_id = str(payload.get("lot_id") or "").strip()
    row = await asyncio.to_thread(inventory.get, lot_id) if lot_id else None
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

    dest_zip = str(payload.get("dest_zip") or "").strip()
    origin_zip = _freight_origin_zip(row)
    if not origin_zip:
        # We don't know where the lot is. Better a hand quote than a lane
        # measured from nowhere.
        return dict(FREIGHT_UNQUOTABLE)

    try:
        # Pure arithmetic over a committed lookup table — microseconds, no I/O,
        # so it runs inline rather than paying for a thread hop. (A configured
        # WarpProvider would add a network call; it falls back to the estimator
        # on failure and is not wired on the storefront today.)
        quote = freight_estimate.get_freight_estimate(origin_zip, dest_zip, quantity)
    except freight_estimate.FreightUnavailable:
        return dict(FREIGHT_UNQUOTABLE)

    quote_id = await asyncio.to_thread(
        freight_log.insert_storefront_quote,
        lot_id=row.get("lot_id") or lot_id,
        origin_zip=origin_zip,
        dest_zip=dest_zip,
        quantity=quantity,
        quote=quote,
        client_ip=rate_limit.client_ip(request),
    )
    asyncio.create_task(
        _notify_freight_estimate(
            row, quote, dest_zip=dest_zip, quantity=quantity, quote_id=quote_id
        )
    )
    return {
        "ok": True,
        "quote_id": quote_id,
        "estimate": _freight_public_estimate(quote),
        "framing": dict(FREIGHT_FRAMING),
    }


def _looks_like_email(value: str) -> bool:
    """Cheap plausibility check — the real validation is whether it bounces."""
    if not value or len(value) > 254 or any(c.isspace() for c in value):
        return False
    local, _, domain = value.partition("@")
    return bool(local) and "." in domain and not domain.startswith(".") \
        and not domain.endswith(".")


@app.post("/freight-estimate/email")
async def public_freight_estimate_email(payload: dict, request: Request):
    """Step two: attach an email to a quote the buyer already has on screen.

    Split from the estimate itself on purpose — asking for an email before
    showing a number costs more quotes than the addresses are worth.
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

    await asyncio.to_thread(freight_log.set_quote_email, quote_id, email)
    asyncio.create_task(_notify_freight_email(quote_id, email))
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


def _extra_args_from_payload(payload: dict) -> list[str]:
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

    extra = _extra_args_from_payload(payload)

    async with state.lock:
        if state.run_status == "running":
            for u in urls:
                state.pending.append({"url": u, "extra_args": extra})
            await _broadcast_queue()
            return {"ok": True, "queued": len(urls), "running": state.url,
                    "queue_length": len(state.pending)}

        head, *rest = urls
        for u in rest:
            state.pending.append({"url": u, "extra_args": extra})
        state.reset()
        state.url = head
        asyncio.create_task(_run_subprocess(head, extra))
        await _broadcast_queue()
        return {"ok": True, "running": head, "queued": len(rest),
                "queue_length": len(state.pending)}


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
async def list_drafts():
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


# ───────────────────────────── compare ─────────────────────────────

@app.get("/api/compare")
async def list_compare():
    rows = db.fetch_all(
        "SELECT id, ts, dom_hint, primary_extraction, secondary_extraction, rating "
        "FROM llm_compare_logs ORDER BY ts DESC"
    )
    out = []
    for row in rows:
        ts_epoch = int(row["ts"].timestamp())
        out.append({
            "id": str(row["id"]),
            "filename": f"llm_compare_{row['id']}.json",
            "timestamp": row["id"],
            "modified": ts_epoch,
            "dom_hint": row["dom_hint"],
            "primary": row["primary_extraction"],
            "secondary": row["secondary_extraction"],
            "rating": row["rating"],
        })
    return {"entries": out}


@app.post("/api/compare/{cid}/rate")
async def rate_compare(cid: str, payload: dict):
    rating = (payload or {}).get("rating")
    if rating not in (None, "", "match", "wrong"):
        raise HTTPException(400, "rating must be 'match', 'wrong', or null")
    try:
        cid_int = int(cid)
    except ValueError:
        raise HTTPException(400, "id must be an integer")
    if rating in (None, ""):
        db.execute(
            "UPDATE llm_compare_logs SET rating = NULL, rated_at = NULL WHERE id = %s",
            (cid_int,),
        )
    else:
        db.execute(
            "UPDATE llm_compare_logs SET rating = %s, rated_at = now() WHERE id = %s",
            (rating, cid_int),
        )
    rows = db.fetch_all(
        "SELECT id, rating FROM llm_compare_logs WHERE rating IS NOT NULL"
    )
    ratings = {str(r["id"]): r["rating"] for r in rows}
    return {"ok": True, "ratings": ratings}


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


async def _run_scraper(source: str, test_mode: bool) -> None:
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

    # Fresh rows are in Supabase now — bust the read-side cache so the next
    # /api/auctions call returns the updated set.
    _AUCTIONS_CACHE.clear()

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

    async with scrape_state.lock:
        if scrape_state.status == "running":
            raise HTTPException(409, "a scrape is already running")
        scrape_state.reset(source, test_mode)
        asyncio.create_task(_run_scraper(source, test_mode))

    return {"ok": True, "source": source, "test_mode": test_mode}


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
):
    if get_top_chairs is None:
        raise HTTPException(503, "auction_extractors package not available")
    if source not in ("gd", "ps", "bs"):
        raise HTTPException(400, "source must be 'gd', 'ps', or 'bs'")
    _CATS = {"banquet", "medical", "other"}
    if category in ("", "all"):
        category = None
    if category is not None and category not in _CATS:
        raise HTTPException(400, f"category must be one of {sorted(_CATS)}")
    n = max(1, min(int(n), 100))
    # min_qty default depends on category: medical lots sell as singles.
    if min_qty is None:
        min_qty = 1 if category == "medical" else 50
    min_qty = max(1, int(min_qty))
    include_condition = bool(int(condition))
    active_flag = bool(int(active_only))
    stale = max(1, int(max_stale_days))

    key = (
        f"{source}|{n}|{min_qty}|{int(include_condition)}|"
        f"{int(active_flag)}|{stale}|{category or ''}"
    )
    now = time.time()
    cached = _AUCTIONS_CACHE.get(key)
    if cached and (now - cached[0]) < _AUCTIONS_TTL:
        return {"items": cached[1], "cached": True, "age": int(now - cached[0])}

    try:
        items = await asyncio.to_thread(
            get_top_chairs,
            source=source,
            n=n,
            min_quantity=min_qty,
            include_condition=include_condition,
            active_only=active_flag,
            max_stale_days=stale,
            category=category,
        )
    except Exception as e:
        raise HTTPException(500, f"get_top_chairs failed: {e!r}")

    _AUCTIONS_CACHE[key] = (now, items)
    return {"items": items, "cached": False, "age": 0}


@app.post("/api/auctions/refresh")
async def refresh_auctions_cache():
    _AUCTIONS_CACHE.clear()
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
    favs = favorites.list_all()
    return {
        "items": [f.to_dict() for f in favs],
        "intervals": [label for label, _ in favorites.ALERT_INTERVALS],
        "telegram_configured": telegram_alerts.is_configured(),
    }


@app.post("/api/auctions/favorites")
async def star_favorite(payload: dict):
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
    return fav.to_dict() if fav else {}


@app.delete("/api/auctions/favorites/{asset_id:path}")
async def unstar_favorite(asset_id: str):
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


async def _alerts_tick() -> None:
    """One scheduler pass. Re-syncs end_date from listings.db where possible
    (catches relists with fresh end_date), then ships any due alerts."""
    try:
        favs = favorites.list_all()
        if not favs:
            return

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
        due = favorites.due_alerts(favs)
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
                favorites.mark_sent(fav.asset_id, label)
                print(f"[favorites] alert sent: {fav.asset_id} {label}")
            else:
                print(f"[favorites] alert FAILED: {fav.asset_id} {label}: {err}")
    except Exception as e:
        # Never let the scheduler die from a single bad tick.
        print(f"[favorites] tick error: {e!r}")


async def _alerts_loop() -> None:
    while True:
        await _alerts_tick()
        await asyncio.sleep(_SCHEDULER_TICK_SEC)


@app.on_event("startup")
async def _start_alerts_loop() -> None:
    global _alerts_task
    if _alerts_task is None or _alerts_task.done():
        _alerts_task = asyncio.create_task(_alerts_loop())
        print(
            f"[favorites] countdown scheduler started "
            f"(tick={_SCHEDULER_TICK_SEC:.0f}s, intervals="
            f"{[l for l,_ in favorites.ALERT_INTERVALS]})"
        )


@app.on_event("shutdown")
async def _stop_alerts_loop() -> None:
    global _alerts_task
    if _alerts_task and not _alerts_task.done():
        _alerts_task.cancel()
        try:
            await _alerts_task
        except (asyncio.CancelledError, Exception):
            pass


@app.get("/api/auctions/cache-stats")
async def auctions_cache_stats():
    """Cheap roll-up over the Supabase `auction_listings` table — powers the
    'N lots in cache · newest scraped X days ago' header on the Auctions tab."""
    if _auctions_cache_stats is None:
        return {"total": 0, "newest_seen_at": None, "oldest_seen_at": None, "by_source": {}}
    return await asyncio.to_thread(_auctions_cache_stats)


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
    """Admin DB browser over auction_extractors/state/listings.db — raw rows
    with filters. Unlike /api/auctions this has no ranking / LLM step; it's
    a straight SQL query for admins."""
    import sqlite3

    db_path = AUCTION_EXTRACTORS_DIR / "state" / "listings.db"
    if not db_path.exists():
        return {"items": [], "total": 0, "limit": limit, "offset": offset}

    limit = max(1, min(int(limit), 500))
    offset = max(0, int(offset))
    min_qty = max(0, int(min_qty))
    max_qty = max(min_qty, int(max_qty))
    seen_within_days = max(0, int(seen_within_days))

    where = []
    params: list = []
    if source == "gd":
        where.append("link LIKE '%govdeals.com%'")
    elif source == "ps":
        where.append("link LIKE '%publicsurplus.com%'")
    elif source == "bs":
        where.append("link LIKE '%bidspotter.com%'")
    if q.strip():
        where.append("(title LIKE ? OR description LIKE ?)")
        like = f"%{q.strip()}%"
        params.extend([like, like])
    where.append("COALESCE(quantity, 0) BETWEEN ? AND ?")
    params.extend([min_qty, max_qty])

    if seen_within_days > 0:
        from datetime import datetime, timedelta, timezone
        cutoff = (datetime.now(timezone.utc) - timedelta(days=seen_within_days)).isoformat()
        where.append("last_seen_at >= ?")
        params.append(cutoff)

    if status == "active":
        # end_date populated and parseable → GovDeals rows; time_left present → Public Surplus.
        where.append("((end_date IS NOT NULL AND end_date != '' AND end_date >= datetime('now')) "
                     "OR (time_left IS NOT NULL AND time_left != ''))")
    elif status == "expired":
        where.append("(end_date IS NOT NULL AND end_date != '' AND end_date < datetime('now'))")
    elif status == "unknown":
        where.append("((end_date IS NULL OR end_date = '') AND (time_left IS NULL OR time_left = ''))")

    order = {
        "qty_desc":         "COALESCE(quantity, 0) DESC, last_seen_at DESC",
        "qty_asc":          "COALESCE(quantity, 0) ASC, last_seen_at DESC",
        "last_seen_desc":   "last_seen_at DESC",
        "first_seen_desc":  "first_seen_at DESC",
        "price_low":        "CAST(REPLACE(REPLACE(REPLACE(price,'USD',''),'$',''),',','') AS REAL) ASC, last_seen_at DESC",
    }.get(sort, "COALESCE(quantity, 0) DESC, last_seen_at DESC")

    where_sql = (" WHERE " + " AND ".join(where)) if where else ""

    def _query():
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:
            total = conn.execute(f"SELECT COUNT(*) FROM listings{where_sql}", params).fetchone()[0]
            rows = conn.execute(
                f"SELECT asset_id, link, title, description, quantity, quantity_source, "
                f"quantity_confidence, price, location, lot_number, end_date, time_left, "
                f"description_fetched_at, first_seen_at, last_seen_at, image_url "
                f"FROM listings{where_sql} ORDER BY {order} LIMIT ? OFFSET ?",
                params + [limit, offset],
            ).fetchall()
            return total, [dict(r) for r in rows]
        finally:
            conn.close()

    total, rows = await asyncio.to_thread(_query)

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
async def inv_list(status: str | None = None):
    rows = inventory.list_all(status=status)
    return {"items": [_inventory_to_public(r) for r in rows]}


@app.get("/api/inventory/{lot_id}")
async def inv_get(lot_id: str):
    row = inventory.get(lot_id)
    if not row:
        raise HTTPException(404, "not found")
    return _inventory_to_public(row)


@app.patch("/api/inventory/{lot_id}")
async def inv_update(lot_id: str, payload: dict):
    payload = dict(payload or {})
    if "locations_text" in payload:
        payload["locations"] = payload.pop("locations_text")
    try:
        row = inventory.set_fields(lot_id, **payload)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not row:
        raise HTTPException(404, "not found")
    return _inventory_to_public(row)


@app.post("/api/inventory")
async def inv_create(payload: dict):
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
async def inv_delete(lot_id: str):
    ok = inventory.delete(lot_id)
    if not ok:
        raise HTTPException(404, "not found")
    return {"ok": True}


_ALLOWED_LINK_PLATFORMS = ("facebook", "ebay", "fb_business", "ad")


@app.post("/api/inventory/{lot_id}/platform")
async def inv_set_platform(lot_id: str, payload: dict):
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
    row = inventory.attach_buyer_cert(lot_id, file.filename or "buyer_cert", data)
    if not row:
        raise HTTPException(404, "lot not found")
    return _inventory_to_public(row)


@app.get("/api/inventory/{lot_id}/buyer-cert")
async def inv_get_buyer_cert(lot_id: str):
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
async def inv_delete_buyer_cert(lot_id: str):
    row = inventory.delete_buyer_cert(lot_id)
    if not row:
        raise HTTPException(404, "lot not found")
    return _inventory_to_public(row)


@app.get("/api/inventory-stats")
async def inv_stats():
    return inventory.stats()


@app.get("/api/site-config")
async def site_config():
    return {"facebook_business_url": FACEBOOK_BUSINESS_URL or None}


@app.post("/api/inventory/seed-snapshot")
async def inv_seed_snapshot(payload: dict):
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
async def inv_backfill():
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
async def inq_list(status: str | None = None):
    return {"items": inventory.list_inquiries(status=status)}


@app.patch("/api/inquiries/{inquiry_id}")
async def inq_update(inquiry_id: int, payload: dict):
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
async def inq_delete(inquiry_id: int):
    ok = inventory.delete_inquiry(inquiry_id)
    if not ok:
        raise HTTPException(404, "not found")
    return {"ok": True}


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


# ───────────────────────────── subscribers API ─────────────────────────────
# Alert-signup rows captured by POST /subscribe (BLACKWHOLE-10). Status-only
# updates — subscribers aren't lot-scoped, so there is no link step.

@app.get("/api/subscribers")
async def sub_list(status: str | None = None):
    return {"items": inventory.list_subscribers(status=status)}


@app.patch("/api/subscribers/{subscriber_id}")
async def sub_update(subscriber_id: int, payload: dict):
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
async def sub_delete(subscriber_id: int):
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
