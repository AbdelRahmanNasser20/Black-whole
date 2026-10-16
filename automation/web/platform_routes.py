"""Liquidator platform: signup page, onboarding page + API.

Auth is Supabase (see platform_auth). DB handlers are plain ``def`` so FastAPI
runs them in its threadpool, never on the event loop.
"""
from __future__ import annotations

import importlib
import os
import re

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field, field_validator

from automation import db
from automation.web import platform_auth

router = APIRouter()

_PHONE_STRIP = re.compile(r"[+\-().\s]")


def normalize_phone(raw: str) -> str:
    """Digits only (E.164 without the plus); 10-15 digits or ValueError."""
    digits = _PHONE_STRIP.sub("", raw or "")
    if not digits.isdigit() or not 10 <= len(digits) <= 15:
        raise ValueError("phone must be 10-15 digits")
    return digits


class OnboardingBody(BaseModel):
    phone: str = Field(max_length=40)
    sms_consent: bool = False
    company: str = Field("", max_length=200)
    state: str = Field("", max_length=40)
    flips_what: str = Field("", max_length=500)
    biggest_pain: str = Field("", max_length=1000)


def _render(request: Request, name: str, extra: dict) -> HTMLResponse:
    webapp = importlib.import_module("automation.web.app")  # lazy: app imports this module
    return webapp.templates.TemplateResponse(request, name, webapp._public_ctx(extra))


@router.get("/platform/signup", response_class=HTMLResponse)
def platform_signup(request: Request):
    return _render(request, "platform_signup.html", {
        "supabase_url": os.getenv("SUPABASE_URL", ""),
        "supabase_key": os.getenv("SUPABASE_PUBLISHABLE_KEY", ""),
        "robots_noindex": True,
    })


@router.get("/platform/onboarding", response_class=HTMLResponse)
def platform_onboarding(request: Request):
    user = platform_auth.maybe_user(request)
    if user is None:
        return RedirectResponse("/platform/signup", status_code=302)
    return _render(request, "platform_onboarding.html", {"user_email": user["email"], "robots_noindex": True})


@router.post("/platform/api/onboarding")
def platform_onboarding_submit(body: OnboardingBody, user: dict = Depends(platform_auth.get_current_user)):
    try:
        phone = normalize_phone(body.phone)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    db.execute(
        "UPDATE platform_users SET phone = %s, sms_consent = %s, company = %s, state = %s, "
        "flips_what = %s, biggest_pain = %s, onboarded_at = now() WHERE user_id = %s",
        (phone, body.sms_consent, body.company.strip(), body.state.strip(),
         body.flips_what.strip(), body.biggest_pain.strip(), user["user_id"]),
    )
    return {"ok": True, "next": "/platform/alerts"}


# ---- alerts + saved lots ---------------------------------------------------

_ALERT_COLS = "id, keyword, state_filter, source_filter, max_price, active, last_fired_at, created_at"


class AlertBody(BaseModel):
    keyword: str
    state_filter: str | None = Field(None, pattern=r"^[A-Za-z]{2}$")
    source_filter: str | None = Field(None, max_length=40)
    max_price: float | None = Field(None, gt=0, le=100_000_000)

    @field_validator("keyword")
    @classmethod
    def _keyword_len(cls, v: str) -> str:
        v = v.strip()  # strip first so whitespace can't satisfy the minimum
        if not 2 <= len(v) <= 80:
            raise ValueError("keyword must be 2-80 characters")
        return v


class SavedBody(BaseModel):
    source: str = Field(min_length=1, max_length=40)
    lot_key: str = Field(min_length=1, max_length=200)


def onboarded_user(user: dict = Depends(platform_auth.get_current_user)) -> dict:
    return platform_auth.require_onboarded(user)


def _max_alerts() -> int:
    try:
        return int(os.getenv("PLATFORM_MAX_ALERTS_PER_USER", "10"))
    except ValueError:
        return 10


@router.get("/platform/alerts", response_class=HTMLResponse)
def platform_alerts_page(request: Request):
    user = platform_auth.maybe_user(request)
    if user is None:
        return RedirectResponse("/platform/signup", status_code=302)
    if not user.get("onboarded_at"):
        return RedirectResponse("/platform/onboarding", status_code=302)
    return _render(request, "platform_alerts.html", {"user_email": user["email"], "robots_noindex": True})


@router.get("/platform/api/alerts")
def list_alerts(user: dict = Depends(onboarded_user)):
    rows = db.fetch_all(
        f"SELECT {_ALERT_COLS} FROM platform_alerts WHERE user_id = %s ORDER BY id", (user["user_id"],))
    return {"alerts": rows}


@router.post("/platform/api/alerts")
def create_alert(body: AlertBody, user: dict = Depends(onboarded_user)):
    state = body.state_filter.upper() if body.state_filter else None
    source = (body.source_filter or "").strip().lower() or None
    # Cap is best-effort under concurrency (READ COMMITTED): worst case it overshoots
    # by the number of concurrent requests.
    row = db.fetch_one(
        "INSERT INTO platform_alerts (user_id, keyword, state_filter, source_filter, max_price) "
        "SELECT %s, %s, %s, %s, %s WHERE (SELECT count(*) FROM platform_alerts WHERE user_id = %s) < %s "
        f"RETURNING {_ALERT_COLS}",
        (user["user_id"], body.keyword, state, source, body.max_price, user["user_id"], _max_alerts()),
    )
    if row is None:
        raise HTTPException(status_code=409, detail={"error": "alert_limit", "max": _max_alerts()})
    return row


@router.delete("/platform/api/alerts/{alert_id}")
def delete_alert(alert_id: int, user: dict = Depends(onboarded_user)):
    n = db.execute("DELETE FROM platform_alerts WHERE id = %s AND user_id = %s", (alert_id, user["user_id"]))
    if not n:  # missing and not-yours look identical
        raise HTTPException(status_code=404, detail="not found")
    return {"ok": True}


@router.get("/platform/api/saved")
def list_saved(user: dict = Depends(onboarded_user)):
    rows = db.fetch_all(
        "SELECT source, lot_key FROM platform_saved_lots WHERE user_id = %s ORDER BY created_at DESC",
        (user["user_id"],))
    return {"saved": rows}


@router.post("/platform/api/saved")
def save_lot(body: SavedBody, user: dict = Depends(onboarded_user)):
    db.execute(
        "INSERT INTO platform_saved_lots (user_id, source, lot_key) VALUES (%s, %s, %s) "
        "ON CONFLICT (user_id, source, lot_key) DO NOTHING",
        (user["user_id"], body.source, body.lot_key))
    return {"ok": True}


@router.delete("/platform/api/saved/{source}/{lot_key:path}")
def unsave_lot(source: str, lot_key: str, user: dict = Depends(onboarded_user)):
    db.execute("DELETE FROM platform_saved_lots WHERE user_id = %s AND source = %s AND lot_key = %s",
               (user["user_id"], source, lot_key))
    return {"ok": True}


# ---- unsubscribe (public, token-authenticated) -----------------------------

@router.get("/platform/unsubscribe", response_class=HTMLResponse)
def platform_unsubscribe(token: str = ""):
    from automation import platform_email  # lazy: keeps requests/itsdangerous off import path
    user_id = platform_email.verify_unsub_token(token)
    if not user_id:
        raise HTTPException(status_code=400, detail="invalid or expired link")
    db.execute("UPDATE platform_alerts SET active = false WHERE user_id = %s", (user_id,))
    return HTMLResponse("<!doctype html><meta charset=utf-8><title>Unsubscribed</title>"
                        "<p>You are unsubscribed. All your lot alerts are off.</p>")
