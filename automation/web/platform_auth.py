"""Supabase auth for the liquidator platform: JWT verification + current user.

Token lookup: ``Authorization: Bearer <jwt>``, else the ``sb-access-token``
cookie (aud ``authenticated``). The token's own header picks the check:
ES256/RS256 = the project's public signing keys from ``SUPABASE_URL`` (JWKS,
no secret needed; issuer must be that project), HS256 = ``SUPABASE_JWT_SECRET``.
Each algorithm is bound to its own key type, so one can never stand in for the other.
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
    "INSERT INTO platform_users (user_id, email, last_seen_at) VALUES (%s, %s, now()) "
    "ON CONFLICT (user_id) DO UPDATE SET email = EXCLUDED.email, last_seen_at = now() "
    "RETURNING *"
)


def _token_from(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    if header[:7].lower() == "bearer " and header[7:].strip():
        return header[7:].strip()
    return request.cookies.get(COOKIE_NAME) or None


_JWKS_CLIENTS: dict[str, jwt.PyJWKClient] = {}
_NOT_CONFIGURED = HTTPException(status_code=401, detail="platform auth not configured")


def _jwks(url: str) -> jwt.PyJWKClient:
    """One cached key-set client per project (keys are re-read hourly)."""
    if url not in _JWKS_CLIENTS:
        _JWKS_CLIENTS[url] = jwt.PyJWKClient(
            f"{url}/auth/v1/.well-known/jwks.json", lifespan=3600, timeout=5)
    return _JWKS_CLIENTS[url]


def _claims(token: str) -> dict:
    secret = os.environ.get("SUPABASE_JWT_SECRET")
    url = (os.environ.get("SUPABASE_URL") or "").rstrip("/")
    alg = jwt.get_unverified_header(token).get("alg")
    extra = {}
    if alg == "HS256":
        if not secret:
            raise _NOT_CONFIGURED
        key = secret
    elif alg in ("ES256", "RS256"):
        if not url:
            raise _NOT_CONFIGURED
        key = _jwks(url).get_signing_key_from_jwt(token).key
        extra["issuer"] = f"{url}/auth/v1"
    else:
        raise jwt.InvalidAlgorithmError(alg)
    return jwt.decode(token, key, algorithms=[alg], audience="authenticated",
                      options={"require": ["exp", "sub"]}, **extra)


def get_current_user(request: Request) -> dict:
    """The ``platform_users`` row for the caller; 401 if unauthenticated."""
    if not os.environ.get("SUPABASE_JWT_SECRET") and not os.environ.get("SUPABASE_URL"):
        raise _NOT_CONFIGURED
    token = _token_from(request)
    if not token:
        raise HTTPException(status_code=401, detail="missing token")
    try:
        claims = _claims(token)
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
