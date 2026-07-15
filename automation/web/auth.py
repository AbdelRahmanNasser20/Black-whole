"""Signed-cookie "auth once" for the dashboard (BLACKWHOLE-14).

Single-operator auth for the black-whole.com admin surface. Mirrors the CRM's
BWCRM-37 pattern (facebook-scraper-crm PR #72) so the operator learns ONE
system — same env-var names, same cookie mechanics, same "log in at most once
per device per year" behaviour.

Three env vars drive everything (read from the process env / ``.env`` via
``automation.config``'s ``load_dotenv``):

- ``ADMIN_PASSWORD`` — the first factor. **Unset = auth disabled entirely**
  (the admin surface stays open exactly as before this feature — Cloudflare
  Access is still in front of it in prod), so this deploys safely as a no-op
  until the operator sets a password.
- ``SESSION_SECRET`` — itsdangerous signing key for both cookies. Unset = a
  random per-boot key with a loud warning (dev still works, but every restart
  logs everyone out — set it in prod for durable 365-day sessions).
- ``TOTP_SECRET`` — optional pyotp base32 secret. Set = the first login from a
  device additionally requires a 6-digit code; success plants a long-lived
  signed "device trusted" cookie so TOTP is never asked again on that device.
  Unset = TOTP skipped entirely.

Both cookies are itsdangerous-signed (tamper-proof), httponly, secure,
samesite=lax, valid 365 days. There is no server-side session store — the
signature + timestamp are the whole session, which suits the single-operator
deployment (nothing to invalidate but the cookie, nothing to persist).

Route protection (see ``session_auth_middleware``): every ``/admin`` page and
every ``/api/*`` route requires the session cookie once ``ADMIN_PASSWORD`` is
set. The public storefront (``/``, ``/listings``, ``/sell``, ``/contact``,
``/subscribe``, ``/image/*``, ``/static/*`` ...) is never gated. This mirrors
the Cloudflare Access route rule from the deploy design, moved into the app so
the operator can extend the session to a full year (Cloudflare Access caps
sessions at ~1 month).
"""
from __future__ import annotations

import logging
import os
import secrets
from functools import lru_cache

from itsdangerous import BadSignature, URLSafeTimedSerializer
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response

logger = logging.getLogger("automation.web.auth")

SESSION_COOKIE = "bw_session"
DEVICE_COOKIE = "bw_device"
SESSION_MAX_AGE = 365 * 24 * 3600  # seconds — "auth once (a year)"
DEVICE_MAX_AGE = 365 * 24 * 3600

_SESSION_SALT = "bw-session-v1"
_DEVICE_SALT = "bw-device-v1"

# The login page + status/login/logout endpoints have to be reachable without a
# session (the login form must load from somewhere, and the SPA-less admin
# posts the password to /api/auth/login). /api/health stays open for Render's
# health check.
LOGIN_PATH = "/admin/login"
EXEMPT_PATHS = ("/api/health", LOGIN_PATH)
EXEMPT_PREFIXES = ("/api/auth/",)

# Prefixes that REQUIRE a session once auth is enabled. Everything else (the
# public storefront, /image/*, /static/*, /robots.txt, /sitemap.xml, /contact,
# /subscribe) stays open. /screenshot/* is admin-only (Playwright captures).
PROTECTED_PREFIXES = ("/admin", "/api/", "/screenshot/")


# ─────────────────────────────── env / config ───────────────────────────────

def _env(name: str) -> str:
    return (os.getenv(name) or "").strip()


def admin_password() -> str:
    return _env("ADMIN_PASSWORD")


def auth_enabled() -> bool:
    """Auth is a no-op until the operator sets ADMIN_PASSWORD."""
    return bool(admin_password())


def totp_secret() -> str:
    return _env("TOTP_SECRET")


def totp_enabled() -> bool:
    return bool(totp_secret())


@lru_cache
def _signing_key() -> str:
    key = _env("SESSION_SECRET")
    if key:
        return key
    logger.warning(
        "SESSION_SECRET is not set — using a random per-boot signing key. "
        "Sessions will NOT survive a restart. Set SESSION_SECRET in the "
        "environment for durable 365-day sessions."
    )
    return secrets.token_urlsafe(32)


def _serializer(salt: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(_signing_key(), salt=salt)


def reset_caches() -> None:
    """Drop the memoized signing key (tests flip env vars between cases)."""
    _signing_key.cache_clear()


# ──────────────────────────────── token layer ───────────────────────────────

def issue_session_token() -> str:
    return _serializer(_SESSION_SALT).dumps({"sub": "operator"})


def verify_session_token(token: str, *, max_age: int = SESSION_MAX_AGE) -> bool:
    try:
        data = _serializer(_SESSION_SALT).loads(token, max_age=max_age)
    except BadSignature:  # covers SignatureExpired too
        return False
    return isinstance(data, dict) and data.get("sub") == "operator"


def issue_device_token() -> str:
    return _serializer(_DEVICE_SALT).dumps({"dev": "trusted"})


def verify_device_token(token: str, *, max_age: int = DEVICE_MAX_AGE) -> bool:
    try:
        data = _serializer(_DEVICE_SALT).loads(token, max_age=max_age)
    except BadSignature:
        return False
    return isinstance(data, dict) and data.get("dev") == "trusted"


def request_has_session(request: Request) -> bool:
    token = request.cookies.get(SESSION_COOKIE)
    return bool(token) and verify_session_token(token)


def request_has_trusted_device(request: Request) -> bool:
    token = request.cookies.get(DEVICE_COOKIE)
    return bool(token) and verify_device_token(token)


# ───────────────────────────────── cookies ──────────────────────────────────

def set_session_cookie(response: Response) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        issue_session_token(),
        max_age=SESSION_MAX_AGE,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )


def set_device_cookie(response: Response) -> None:
    response.set_cookie(
        DEVICE_COOKIE,
        issue_device_token(),
        max_age=DEVICE_MAX_AGE,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    # The trusted-device cookie survives logout — TOTP stays "once per device",
    # not "once per session".
    response.delete_cookie(SESSION_COOKIE, path="/")


# ────────────────────────────────── totp ────────────────────────────────────

def verify_totp_code(code: str) -> bool:
    secret = totp_secret()
    if not secret:
        return False
    import pyotp

    try:
        return pyotp.TOTP(secret).verify((code or "").strip(), valid_window=1)
    except Exception:
        return False


def totp_provisioning_uri(account: str = "operator",
                          issuer: str = "Black Whole") -> str:
    secret = totp_secret()
    if not secret:
        raise ValueError("TOTP_SECRET is not set")
    import pyotp

    return pyotp.TOTP(secret).provisioning_uri(name=account, issuer_name=issuer)


# ─────────────────────────────── path helpers ───────────────────────────────

def path_requires_auth(path: str) -> bool:
    """True when ``path`` is an admin surface that needs a session.

    Independent of whether auth is currently enabled — the middleware ANDs this
    with :func:`auth_enabled`. Exemptions win over the protected prefixes so the
    login page and the auth endpoints stay reachable.
    """
    if path in EXEMPT_PATHS or path.startswith(EXEMPT_PREFIXES):
        return False
    return path.startswith(PROTECTED_PREFIXES)


def _wants_html(request: Request) -> bool:
    """Browser navigation (Accept: text/html) → redirect to the login page;
    an XHR / fetch to /api/* → a 401 the JS can handle."""
    return "text/html" in request.headers.get("accept", "")


# ─────────────────────────────── middleware ─────────────────────────────────

async def session_auth_middleware(request: Request, call_next):
    """Gate every admin/API route behind the signed session cookie.

    No-op when ADMIN_PASSWORD is unset. Unauthenticated browser hits to /admin
    redirect to the login page; unauthenticated /api hits get a 401.
    """
    path = request.url.path
    if not auth_enabled() or not path_requires_auth(path):
        return await call_next(request)
    if request_has_session(request):
        return await call_next(request)
    if _wants_html(request):
        return RedirectResponse(LOGIN_PATH, status_code=303)
    return JSONResponse({"detail": "not_authenticated"}, status_code=401)
