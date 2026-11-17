"""`chat_sessions`: the only module that reads or writes the storefront chat table.

One row per chat a visitor opened. The row holds the script state
(`automation/site_chat.py`), a few flat columns for the admin list, and what the
browser told us about the visitor (first-touch attribution, the pages they
looked at, a random per-browser id). No IP address and no user agent is stored.

Migration `scripts/sql/038_chat_sessions.sql`. Until it is applied
`schema_ready()` is False, the widget does not render, and `/chat/*` answers 503.
"""
from __future__ import annotations

import logging
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from psycopg.types.json import Jsonb

from . import db
from . import site_chat

log = logging.getLogger(__name__)

MAX_TRAIL = 30
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_VISITOR_RE = re.compile(r"^[A-Za-z0-9-]{8,40}$")

# ───────────────────────────── schema detection ─────────────────────────────

_schema_ready = False


def enabled() -> bool:
    """`SITE_CHAT_ENABLED=0` turns the chat off without a deploy of new code."""
    return os.getenv("SITE_CHAT_ENABLED", "1").strip() != "0"


def schema_ready() -> bool:
    """True once migration 038 is applied. Reads the DB until the answer is yes.

    Keyed on `inventory.pickup_address`, the LAST object the file creates:
    `apply_sql.py` runs statement by statement, so a half-applied file must not
    read as ready. Only a positive answer is cached.
    """
    global _schema_ready
    if _schema_ready:
        return True
    try:
        row = db.fetch_one(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'inventory' "
            "AND column_name = 'pickup_address' "
            "AND to_regclass('public.chat_sessions') IS NOT NULL LIMIT 1"
        )
    except Exception:  # noqa: BLE001 — no DB means not ready
        return False
    _schema_ready = row is not None
    return _schema_ready


def ready_cached() -> bool:
    """The last known answer, with NO database call. Safe inside a template render
    on the event loop. The startup hook and every chat request refresh it."""
    return _schema_ready and enabled()


def reset_schema_cache() -> None:
    """Tests only."""
    global _schema_ready
    _schema_ready = False


# ───────────────────────────── visitor facts ─────────────────────────────

def _clip(value: Any, limit: int) -> str | None:
    s = " ".join(str(value if value is not None else "").split())[:limit]
    return s or None


def clean_trail(raw: Any) -> list[dict]:
    """The browser's page trail, cut to `MAX_TRAIL` entries of {p: path, t: epoch seconds}."""
    out: list[dict] = []
    if not isinstance(raw, list):
        return out
    for item in raw[-MAX_TRAIL:]:
        if not isinstance(item, dict):
            continue
        path = _clip(item.get("p"), 200)
        try:
            ts = int(item.get("t"))
        except (TypeError, ValueError):
            continue
        if path and path.startswith("/") and 0 < ts < 10**11:
            out.append({"p": path, "t": ts})
    return out


def device_of(user_agent: str | None) -> str:
    ua = (user_agent or "").lower()
    if "ipad" in ua or "tablet" in ua:
        return "tablet"
    if "mobi" in ua or "iphone" in ua or "android" in ua:
        return "phone"
    return "computer" if ua else "unknown"


def visitor_facts(payload: dict, *, user_agent: str | None, country: str | None,
                  attribution: dict | None) -> dict:
    """Everything stored about the visitor, from one `/chat/start` body. Pure."""
    v = payload.get("visitor") if isinstance(payload.get("visitor"), dict) else {}
    vid = str(v.get("id") or "")
    first_seen = None
    try:
        ms = int(v.get("first"))
        if 10**12 < ms < 10**14:
            first_seen = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    except (TypeError, ValueError):
        pass
    try:
        days = max(1, min(int(v.get("days")), 100000))
    except (TypeError, ValueError):
        days = None
    page = _clip(payload.get("page"), 200)
    cc = (country or "").strip().upper()
    attr = attribution or {}
    return {
        "visitor_id": vid if _VISITOR_RE.match(vid) else None,
        "first_seen": first_seen,
        "visit_days": days,
        "page": page if page and page.startswith("/") else None,
        "trail": clean_trail(payload.get("trail")),
        "device": device_of(user_agent),
        "country": cc if re.fullmatch(r"[A-Z]{2}", cc) else None,
        "attr_source": attr.get("source"), "attr_medium": attr.get("medium"),
        "attr_campaign": attr.get("campaign"), "attr_referrer": attr.get("referrer"),
        "attr_landing": attr.get("landing"),
    }


# ───────────────────────────── writes ─────────────────────────────

def valid_id(session_id: Any) -> bool:
    return isinstance(session_id, str) and bool(_UUID_RE.match(session_id))


def create(state: dict, facts: dict) -> str:
    session_id = str(uuid.uuid4())
    db.execute(
        """
        INSERT INTO chat_sessions (id, step, state, visitor_id, first_seen, visit_days, page, trail,
                                   device, country, attr_source, attr_medium, attr_campaign,
                                   attr_referrer, attr_landing)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (session_id, state["step"], Jsonb(state), facts.get("visitor_id"), facts.get("first_seen"),
         facts.get("visit_days"), facts.get("page"), Jsonb(facts.get("trail") or []),
         facts.get("device"), facts.get("country"), facts.get("attr_source"), facts.get("attr_medium"),
         facts.get("attr_campaign"), facts.get("attr_referrer"), facts.get("attr_landing")),
    )
    return session_id


def get(session_id: str) -> dict | None:
    if not valid_id(session_id):
        return None
    return db.fetch_one("SELECT id, status, step, state, inquiry_id FROM chat_sessions WHERE id = %s",
                        (session_id,))


def save(session_id: str, state: dict, *, trail: list[dict] | None = None,
         inquiry_id: int | None = None) -> None:
    """Store the script state after one answer, and the flat columns the admin list reads."""
    a = state.get("answers") or {}
    done = state.get("step") == "done"
    db.execute(
        """
        UPDATE chat_sessions SET
            updated_at = now(), step = %s, state = %s, answered = %s,
            status = %s, completed_at = CASE WHEN %s AND completed_at IS NULL THEN now() ELSE completed_at END,
            name = %s, phone = %s, email = %s, zip_code = %s, quantity = %s, lot_ids = %s,
            inquiry_id = COALESCE(%s, inquiry_id),
            trail = COALESCE(%s, trail)
        WHERE id = %s
        """,
        (state.get("step"), Jsonb(state), len(state.get("log") or []),
         "completed" if done else "open", done,
         a.get("name"), a.get("phone"), a.get("email"), a.get("zip"), site_chat.quantity(state),
         [p["lot_id"] for p in state.get("picked") or []] or None,
         inquiry_id, Jsonb(trail) if trail else None, session_id),
    )


# ───────────────────────────── admin reads ─────────────────────────────

VIEWS = ("answered", "completed", "open", "all")
_VIEW_SQL = {
    "answered": "answered > 0",
    "completed": "status = 'completed'",
    "open": "status = 'open' AND answered > 0",
    "all": "TRUE",
}
_LIST_COLS = (
    "id, created_at, updated_at, completed_at, status, step, answered, state, name, phone, email, "
    "zip_code, quantity, lot_ids, inquiry_id, visitor_id, first_seen, visit_days, page, trail, device, "
    "country, attr_source, attr_medium, attr_campaign, attr_referrer, attr_landing"
)


def list_sessions(view: str = "answered", limit: int = 100) -> list[dict]:
    where = _VIEW_SQL.get(view, _VIEW_SQL["answered"])
    rows = db.fetch_all(
        f"SELECT {_LIST_COLS}, "
        "(SELECT count(*) FROM chat_sessions o WHERE o.visitor_id = c.visitor_id) AS visitor_chats "
        f"FROM chat_sessions c WHERE {where} ORDER BY created_at DESC LIMIT %s",
        (max(1, min(int(limit), 500)),),
    )
    out = []
    for r in rows:
        r = dict(r)
        state = r.pop("state") or {}
        r["id"] = str(r["id"])
        r["log"] = state.get("log") or []
        r["picked"] = [{"lot_id": p.get("lot_id"), "title": p.get("title"), "url": p.get("url")}
                       for p in state.get("picked") or []]
        r["step_label"] = site_chat.STEP_LABELS.get(r.get("step") or "", r.get("step"))
        started, ended = r.get("created_at"), r.get("completed_at") or r.get("updated_at")
        r["seconds"] = int((ended - started).total_seconds()) if started and ended else None
        out.append(r)
    return out


def stats(days: int = 30) -> dict:
    """Funnel numbers for the admin header: how many chats, how far they got, where from."""
    days = max(1, min(int(days), 365))
    since = (f"{days} days",)
    totals = db.fetch_one(
        """
        SELECT count(*) AS opened,
               count(*) FILTER (WHERE answered > 0) AS answered,
               count(*) FILTER (WHERE status = 'completed') AS completed,
               count(*) FILTER (WHERE device = 'phone') AS on_phone,
               (percentile_cont(0.5) WITHIN GROUP (ORDER BY extract(epoch FROM completed_at - created_at))
                    FILTER (WHERE status = 'completed'))::int AS median_seconds
        FROM chat_sessions WHERE created_at > now() - %s::interval
        """, since) or {}
    stopped = db.fetch_all(
        """
        SELECT step, count(*) AS n FROM chat_sessions
        WHERE created_at > now() - %s::interval AND status = 'open' AND answered > 0
        GROUP BY step ORDER BY n DESC
        """, since)
    sources = db.fetch_all(
        """
        SELECT COALESCE(NULLIF(attr_source, ''), NULLIF(attr_referrer, ''), 'direct') AS source,
               count(*) AS chats, count(*) FILTER (WHERE status = 'completed') AS completed
        FROM chat_sessions WHERE created_at > now() - %s::interval AND answered > 0
        GROUP BY 1 ORDER BY chats DESC LIMIT 6
        """, since)
    pages = db.fetch_all(
        """
        SELECT COALESCE(page, '?') AS page, count(*) AS chats FROM chat_sessions
        WHERE created_at > now() - %s::interval AND answered > 0
        GROUP BY 1 ORDER BY chats DESC LIMIT 6
        """, since)
    return {
        "days": days,
        "opened": totals.get("opened") or 0,
        "answered": totals.get("answered") or 0,
        "completed": totals.get("completed") or 0,
        "on_phone": totals.get("on_phone") or 0,
        "median_seconds": totals.get("median_seconds"),
        "stopped_at": [{"step": r["step"], "label": site_chat.STEP_LABELS.get(r["step"], r["step"]),
                        "n": r["n"]} for r in stopped],
        "sources": [dict(r) for r in sources],
        "pages": [dict(r) for r in pages],
    }
