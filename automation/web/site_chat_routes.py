"""Storefront chat endpoints, and the admin read side for Sales › Chats.

Public (outside `/api/`, which is auth-walled):
  POST /chat/start    open or resume a chat           -> {sid, history, ...prompt}
  POST /chat/answer   one buyer answer                -> {error?, ...prompt}

Admin (under `/api/`, behind the session wall):
  GET /api/chats?view=answered|completed|open|all
  GET /api/chats/stats?days=30

Every handler is a plain `def`: FastAPI runs it on the threadpool, so the DB
calls never sit on the event loop. The script itself is `automation/site_chat.py`
(pure). A finished chat writes ONE `inquiries` row through
`inventory.create_inquiry`, which also puts the buyer on the alert list, and
sends one Telegram `leads` alert.
"""
from __future__ import annotations

import logging
import threading
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from automation import attribution as attribution_mod
from automation import inventory, lot_channels, lot_images, lot_urls
from automation import site_chat, site_chat_store, telegram_alerts
from automation.web import rate_limit, readcache

log = logging.getLogger(__name__)
router = APIRouter()

START_PER_IP_LIMIT = 30      # per hour: a page reload resumes, so a real buyer needs few
ANSWER_PER_IP_LIMIT = 240    # per hour: one chat is about 11 answers
GLOBAL_LIMIT = 3000          # per hour, all callers: the circuit breaker

_LOTS_TTL_S = 60.0
_lots_memo: tuple[float, list[dict]] = (0.0, [])


# Pages on _public_base.html that are NOT for chair buyers: no chat there.
_NO_WIDGET_PREFIXES = ("/platform", "/liquidators", "/sell", "/reserve")


def widget_on(request: Request) -> bool:
    """Template global `site_chat_on(request)`: render the chat on this page?
    Reads the cached schema answer only, never the DB (it runs inside a render)."""
    if not site_chat_store.ready_cached():
        return False
    return not request.url.path.startswith(_NO_WIDGET_PREFIXES)


def _sellable_rows() -> list[dict]:
    """`inventory.list_catalog_feed()` (the one sellable-lots query), memoised for a minute."""
    global _lots_memo
    now = time.monotonic()
    if _lots_memo[1] and now - _lots_memo[0] < _LOTS_TTL_S:
        return _lots_memo[1]
    rows = inventory.list_catalog_feed()
    _lots_memo = (now, rows)
    return rows


def _offer(zip_code: str | None) -> dict:
    from automation.alerts import geo  # lazy: pgeocode is heavy and pre-warmed at startup
    return site_chat.lot_cards(
        _sellable_rows(), zip_code,
        resolve=geo.resolve_place, distance=geo.haversine_miles,
        hero=lot_images.hero_src, url=lot_urls.public_path, unit=lot_channels.unit_word)


def _guard(request: Request, bucket: str, limit: int) -> None:
    if not site_chat_store.enabled() or not site_chat_store.schema_ready():
        raise HTTPException(503, "chat_unavailable")
    ip = rate_limit.client_ip(request)
    if not (rate_limit.allow(f"{bucket}:{ip}", limit=limit)
            and rate_limit.allow("chat:global", limit=GLOBAL_LIMIT)):
        raise HTTPException(429, "rate_limited")


def _reply(body: dict) -> JSONResponse:
    return JSONResponse(body, headers={"Cache-Control": "no-store"})


def _prompt(state: dict) -> dict:
    offer = _offer(state["answers"].get("zip")) if state["step"] == "lots" else None
    return site_chat.prompt(state, offer)


@router.post("/chat/start")
def chat_start(payload: dict, request: Request):
    """Open a chat, or resume the one the browser remembers."""
    _guard(request, "chat-start", START_PER_IP_LIMIT)
    payload = payload or {}
    sid = payload.get("sid")
    row = site_chat_store.get(sid) if sid else None
    if row is not None:
        state = row["state"]
        return _reply({"sid": str(row["id"]), "history": state.get("log") or [], **_prompt(state)})
    state = site_chat.new_state()
    facts = site_chat_store.visitor_facts(
        payload, user_agent=request.headers.get("user-agent"),
        country=request.headers.get("cf-ipcountry"),
        attribution=attribution_mod.from_payload(payload))
    sid = site_chat_store.create(state, facts)
    return _reply({"sid": sid, "history": [], **_prompt(state)})


@router.post("/chat/answer")
def chat_answer(payload: dict, request: Request):
    """Apply one answer. A bad answer comes back as `error` with the same question."""
    _guard(request, "chat", ANSWER_PER_IP_LIMIT)
    payload = payload or {}
    row = site_chat_store.get(payload.get("sid"))
    if row is None:
        raise HTTPException(404, "no_such_chat")
    state = row["state"]
    if state.get("step") == "done":
        return _reply(_prompt(state))
    offer = _offer(state["answers"].get("zip")) if state["step"] == "lots" else None
    error = site_chat.advance(state, payload.get("value"), offer)
    if error:
        return _reply({"error": error, **site_chat.prompt(state, offer)})
    inquiry_id = None
    if state["step"] == "done":
        inquiry_id = _file_lead(str(row["id"]), state, attribution_mod.from_payload(payload))
    site_chat_store.save(str(row["id"]), state,
                         trail=site_chat_store.clean_trail(payload.get("trail")) or None,
                         inquiry_id=inquiry_id)
    return _reply(_prompt(state))


def _file_lead(session_id: str, state: dict, attr: dict) -> int | None:
    """A finished chat becomes an inquiry and one Telegram alert. Never raises:
    the chat row is the record of last resort and is saved by the caller."""
    inquiry = None
    try:
        inquiry = inventory.create_inquiry(**site_chat.lead(state), attribution=attr)
    except Exception as e:  # noqa: BLE001
        log.warning("site chat %s: inquiry not saved: %r", session_id, e)
    threading.Thread(target=_after_lead, args=(state, inquiry, attr), daemon=True).start()
    return inquiry.get("id") if inquiry else None


def _after_lead(state: dict, inquiry: dict | None, attr: dict) -> None:
    try:
        if inquiry:
            inventory.subscribe_from_inquiry(inquiry, attr)
    except Exception:  # noqa: BLE001
        pass
    try:
        a = state["answers"]
        contact = a.get("phone") or a.get("email") or ""
        head = f"💬 CHAT LEAD{(' #' + str(inquiry['id'])) if inquiry else ' (inquiry NOT saved)'}"
        meta = " · ".join(x for x in (
            f"ZIP {a['zip']}" if a.get("zip") else None,
            f"qty {a['qty']}" if a.get("qty") else None,
            f"from {attr.get('source') or attr.get('referrer') or 'direct'}") if x)
        lines = [head, f"{a.get('name') or 'No name'} {contact}".strip(), meta,
                 site_chat.summary(state)[:500], "→ black-whole.com/admin?tab=chats"]
        telegram_alerts.send_message_sync("\n".join(lines), topic="leads")
    except Exception:  # noqa: BLE001
        pass


# ───────────────────────────── admin ─────────────────────────────

@router.get("/api/chats")
@readcache.cached()
def chats_list(view: str = "answered"):
    if view not in site_chat_store.VIEWS:
        raise HTTPException(400, "unknown view")
    if not site_chat_store.schema_ready():
        raise HTTPException(409, "Apply scripts/sql/038_chat_sessions.sql, then restart the web process.")
    return {"items": site_chat_store.list_sessions(view)}


@router.get("/api/chats/stats")
@readcache.cached()
def chats_stats(days: int = 30):
    if not site_chat_store.schema_ready():
        raise HTTPException(409, "Apply scripts/sql/038_chat_sessions.sql, then restart the web process.")
    return site_chat_store.stats(days)
