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
from pydantic import BaseModel, Field

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
