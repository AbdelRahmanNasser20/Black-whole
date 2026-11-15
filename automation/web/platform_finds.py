"""Finds: a liquidator's saved searches, each with its newest lots and an alert switch.

One entity, one table: a saved search IS a `platform_alerts` row (migration 037 adds
the extra filters). `active` is the alert switch, so a search can exist with no email.
The filter names are the public /deals ones (q, category, state, site, max_bids,
ending_within, min_price, max_price) and the reader is `public_deals.fetch_page`, so a
search shows exactly the public lot set, from every source, policy applied.

A visitor with no account keeps searches in the browser (static/site/platform/finds.js)
and reads the public /deals/api/lots. These endpoints are the signed-in half only:
per-user, so `Cache-Control: private, no-store` and no memo of any kind.

Before migration 037 the code stores the four old columns and needs a keyword.
"""
from __future__ import annotations

import importlib
import os

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator

from automation import db
from automation.web import platform_auth, public_deals

router = APIRouter()

_PRIVATE = {"Cache-Control": "private, no-store"}
FILTERS = ("q", "category", "state", "site", "max_bids", "ending_within", "min_price", "max_price")
LOTS_PER_FIND = 50          # one of the feed's own page sizes
IMPORT_MAX = 200

_columns_ready = False


def has_finds_columns() -> bool:
    """True once migration 037 is applied. Only a yes is cached: a pooler hiccup
    must not pin "no" for the life of the process."""
    global _columns_ready
    if _columns_ready:
        return True
    try:
        row = db.fetch_one(
            "SELECT 1 FROM information_schema.columns WHERE table_schema = 'public' "
            "AND table_name = 'platform_alerts' AND column_name = 'last_viewed_at' LIMIT 1")
    except Exception:
        return False
    if row is not None:
        _columns_ready = True
    return _columns_ready


def onboarded_user(user: dict = Depends(platform_auth.get_current_user)) -> dict:
    return platform_auth.require_onboarded(user)


def _max_finds() -> int:
    try:
        return int(os.getenv("PLATFORM_MAX_ALERTS_PER_USER", "10"))
    except ValueError:
        return 10


def _private(payload) -> JSONResponse:
    return JSONResponse(jsonable_encoder(payload), headers=_PRIVATE)


class FindBody(BaseModel):
    name: str | None = Field(None, max_length=80)
    q: str | None = Field(None, max_length=80)
    category: str | None = Field(None, pattern=r"^[a-z0-9_]{1,40}$")
    state: str | None = Field(None, pattern=r"^[A-Za-z]{2}$")
    site: str | None = Field(None, pattern=r"^[A-Za-z0-9_]{1,40}$")
    max_bids: int | None = Field(None, ge=0, le=1000)
    ending_within: int | None = Field(None, gt=0, le=720)          # hours
    min_price: float | None = Field(None, ge=0, le=100_000_000)
    max_price: float | None = Field(None, gt=0, le=100_000_000)
    alert: bool = False

    @field_validator("name", "q", mode="before")
    @classmethod
    def _blank_is_none(cls, v):
        v = v.strip() if isinstance(v, str) else v
        return v or None

    @field_validator("q")
    @classmethod
    def _keyword_len(cls, v):
        if v is not None and len(v) < 2:
            raise ValueError("keyword must be 2-80 characters")
        return v

    @model_validator(mode="after")
    def _needs_a_filter(self):
        if all(getattr(self, k) is None for k in FILTERS):
            raise ValueError("a saved search needs at least one filter")
        return self


class FindPatch(BaseModel):
    alert: bool | None = None
    name: str | None = Field(None, max_length=80)


class ImportLot(BaseModel):
    source: str = Field(min_length=1, max_length=40)
    lot_key: str = Field(min_length=1, max_length=200)


class ImportBody(BaseModel):
    lots: list[ImportLot] = Field(max_length=IMPORT_MAX)


def as_find(r: dict) -> dict:
    """DB row -> the public shape (the /deals filter names)."""
    return {
        "id": r["id"], "name": r.get("name"), "q": r.get("keyword"), "category": r.get("category"),
        "state": r.get("state_filter"), "site": r.get("source_filter"), "max_bids": r.get("max_bids"),
        "ending_within": r.get("ending_within"), "min_price": r.get("min_price"),
        "max_price": r.get("max_price"), "alert": bool(r.get("active")),
        "last_viewed_at": r.get("last_viewed_at"), "last_fired_at": r.get("last_fired_at"),
        "created_at": r.get("created_at"),
    }


def filters_of(find: dict) -> dict:
    return {k: find.get(k) for k in FILTERS}


def _mine(find_id: int, user: dict) -> dict:
    row = db.fetch_one("SELECT * FROM platform_alerts WHERE id = %s AND user_id = %s",
                       (find_id, user["user_id"]))
    if row is None:  # missing and not-yours look identical
        raise HTTPException(status_code=404, detail="not found")
    return row


@router.get("/platform/finds", response_class=HTMLResponse)
def platform_finds_page(request: Request):
    """Renders for everyone. No account = the browser keeps the searches."""
    try:
        user = platform_auth.maybe_user(request)
    except Exception:  # a DB hiccup must never 500 this page
        user = None
    signed_in = bool(user and user.get("onboarded_at"))
    webapp = importlib.import_module("automation.web.app")  # lazy: app imports this module
    resp = webapp.templates.TemplateResponse(request, "platform_finds.html", webapp._public_ctx({
        "signed_in": signed_in, "user_email": user["email"] if signed_in else "",
        "robots_noindex": True}))
    if signed_in:
        resp.headers["Cache-Control"] = _PRIVATE["Cache-Control"]
    return resp


@router.get("/platform/api/finds")
def list_finds(user: dict = Depends(onboarded_user)):
    rows = db.fetch_all("SELECT * FROM platform_alerts WHERE user_id = %s ORDER BY id", (user["user_id"],))
    return _private({"finds": [as_find(r) for r in rows], "max": _max_finds()})


@router.post("/platform/api/finds")
def create_find(body: FindBody, user: dict = Depends(onboarded_user)):
    state = body.state.upper() if body.state else None
    site = body.site.lower() if body.site else None
    cols = ["user_id", "keyword", "state_filter", "source_filter", "max_price", "active"]
    vals = [user["user_id"], body.q, state, site, body.max_price, body.alert]
    full = has_finds_columns()
    if full:
        cols += ["name", "category", "min_price", "max_bids", "ending_within"]
        vals += [body.name, body.category, body.min_price, body.max_bids, body.ending_within]
    elif body.q is None:
        raise HTTPException(status_code=409, detail={"error": "keyword_required"})
    # Cap shared with alerts (same table). Best-effort under concurrency, like create_alert.
    row = db.fetch_one(
        f"INSERT INTO platform_alerts ({', '.join(cols)}) "
        f"SELECT {', '.join(['%s'] * len(cols))} "
        "WHERE (SELECT count(*) FROM platform_alerts WHERE user_id = %s) < %s RETURNING *",
        (*vals, user["user_id"], _max_finds()))
    if row is None:
        raise HTTPException(status_code=409, detail={"error": "alert_limit", "max": _max_finds()})
    out = as_find(row)
    if not full:
        out["degraded"] = True   # only keyword, state, site and max price were stored
    return _private(out)


@router.patch("/platform/api/finds/{find_id}")
def patch_find(find_id: int, body: FindPatch, user: dict = Depends(onboarded_user)):
    if has_finds_columns():
        row = db.fetch_one(
            "UPDATE platform_alerts SET active = COALESCE(%s, active), name = COALESCE(%s, name) "
            "WHERE id = %s AND user_id = %s RETURNING *",
            (body.alert, (body.name or "").strip() or None, find_id, user["user_id"]))
    else:
        row = db.fetch_one(
            "UPDATE platform_alerts SET active = COALESCE(%s, active) "
            "WHERE id = %s AND user_id = %s RETURNING *",
            (body.alert, find_id, user["user_id"]))
    if row is None:
        raise HTTPException(status_code=404, detail="not found")
    return _private(as_find(row))


@router.delete("/platform/api/finds/{find_id}")
def delete_find(find_id: int, user: dict = Depends(onboarded_user)):
    n = db.execute("DELETE FROM platform_alerts WHERE id = %s AND user_id = %s", (find_id, user["user_id"]))
    if not n:
        raise HTTPException(status_code=404, detail="not found")
    return _private({"ok": True})


@router.get("/platform/api/finds/{find_id}/lots")
def find_lots(find_id: int, user: dict = Depends(onboarded_user)):
    """The search's lots, newest first, straight from the public reader (policy applied there)."""
    row = _mine(find_id, user)
    try:
        out = public_deals.fetch_page(**filters_of(as_find(row)), status="active",
                                      sort="newest", page=1, per_page=LOTS_PER_FIND)
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"lots query failed: {e!r}")
    return _private({"rows": out["rows"], "total": out["total"],
                     "last_viewed_at": row.get("last_viewed_at")})


@router.post("/platform/api/finds/{find_id}/seen")
def find_seen(find_id: int, user: dict = Depends(onboarded_user)):
    if has_finds_columns():
        row = db.fetch_one(
            "UPDATE platform_alerts SET last_viewed_at = now() WHERE id = %s AND user_id = %s RETURNING id",
            (find_id, user["user_id"]))
        if row is None:
            raise HTTPException(status_code=404, detail="not found")
    return _private({"ok": True})


@router.post("/platform/api/saved/import")
def import_saved(body: ImportBody, user: dict = Depends(onboarded_user)):
    """Browser stars -> the account, after sign-in. One statement, at most IMPORT_MAX lots."""
    if body.lots:
        db.execute(
            "INSERT INTO platform_saved_lots (user_id, source, lot_key) "
            "SELECT %s, s, k FROM unnest(%s::text[], %s::text[]) AS t(s, k) "
            "ON CONFLICT (user_id, source, lot_key) DO NOTHING",
            (user["user_id"], [l.source for l in body.lots], [l.lot_key for l in body.lots]))
    return _private({"ok": True, "received": len(body.lots)})
