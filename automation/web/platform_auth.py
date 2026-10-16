"""Supabase auth for the liquidator platform: JWT verification + current user.

Token lookup: ``Authorization: Bearer <jwt>``, else the ``sb-access-token``
cookie. Verified HS256 against ``SUPABASE_JWT_SECRET`` (aud ``authenticated``).
No route definitions here; routes import these dependencies.
"""
from __future__ import annotations

import os
import uuid

import jwt
from fastapi import HTTPException, Request

from automation import db

COOKIE_NAME = "sb-access-token"

_UPSERT_SQL = (
    "INSERT INTO platform_users (user_id, email) VALUES (%s, %s) "
    "ON CONFLICT (user_id) DO UPDATE SET email = EXCLUDED.email, last_seen_at = now() "
    "RETURNING *"
)


def _token_from(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    if header[:7].lower() == "bearer " and header[7:].strip():
        return header[7:].strip()
    return request.cookies.get(COOKIE_NAME) or None


def get_current_user(request: Request) -> dict:
    """The ``platform_users`` row for the caller; 401 if unauthenticated."""
    secret = os.environ.get("SUPABASE_JWT_SECRET")
    if not secret:
        raise HTTPException(status_code=401, detail="platform auth not configured")
    token = _token_from(request)
    if not token:
        raise HTTPException(status_code=401, detail="missing token")
    try:
        claims = jwt.decode(token, secret, algorithms=["HS256"], audience="authenticated",
            options={"require": ["exp", "sub"]},
        )
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="invalid token")
    uid, email = claims.get("sub"), claims.get("email")
    if not uid or not email:
        raise HTTPException(status_code=401, detail="invalid token")
    try:
        uuid.UUID(str(uid))
    except ValueError:
        raise HTTPException(status_code=401, detail="invalid token")
    return db.fetch_one(_UPSERT_SQL, (uid, email))


def maybe_user(request: Request) -> dict | None:
    """Like ``get_current_user`` but None instead of 401."""
    try:
        return get_current_user(request)
    except HTTPException:
        return None


def require_onboarded(user: dict) -> dict:
    if not user.get("onboarded_at"):
        raise HTTPException(status_code=403, detail={"error": "onboarding_required"})
    return user
