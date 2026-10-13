"""eBay Marketplace Account Deletion endpoint — required before eBay data can live in the CRM.

eBay pushes one notice to this URL every time ANY eBay user closes their account
(high volume, mostly strangers). Contract (developer.ebay.com → Marketplace Account Deletion):

- GET  ?challenge_code=X → {"challengeResponse": sha256(X + token + endpoint)} as JSON.
- POST JSON notice → ack fast with 204. eBay retries anything else and disables the
  keyset if the endpoint keeps failing.

Rules:
- Public path, deliberately NOT under /api/ (that prefix is auth-walled).
- A notice only deletes after its `X-EBAY-SIGNATURE` verifies against eBay's public key
  (ECDSA/SHA1 over the raw body; key fetched by `kid`, cached). Unverified → 412, a failed
  delete → 500; both make eBay retry, so a deletion is never silently dropped.
- Deleting = the CRM contact row (`platform='ebay'`, `buyer_handle` = username or userId);
  `messages` / `drafts` go with it via ON DELETE CASCADE. Nothing about the user is logged.

Env: EBAY_VERIFICATION_TOKEN (32–80 chars, [A-Za-z0-9_-]), EBAY_DELETION_ENDPOINT (the
exact https URL registered on the developer portal), EBAY_APP_ID / EBAY_CERT_ID (for the
public-key lookup).
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import threading

import httpx
from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from automation import db

log = logging.getLogger(__name__)
router = APIRouter()

PATH = "/ebay/account-deletion"
EBAY_API = "https://api.ebay.com"

_keys: dict[str, str] = {}
_lock = threading.Lock()


def challenge_response(code: str, token: str, endpoint: str) -> str:
    return hashlib.sha256((code + token + endpoint).encode()).hexdigest()


@router.get(PATH)
def ebay_challenge(challenge_code: str = "") -> Response:
    token = os.environ.get("EBAY_VERIFICATION_TOKEN", "")
    endpoint = os.environ.get("EBAY_DELETION_ENDPOINT", "")
    if not (challenge_code and token and endpoint):
        return Response(status_code=404)
    return JSONResponse({"challengeResponse": challenge_response(challenge_code, token, endpoint)})


def _app_token() -> str:
    basic = base64.b64encode(
        f"{os.environ['EBAY_APP_ID']}:{os.environ['EBAY_CERT_ID']}".encode()).decode()
    r = httpx.post(f"{EBAY_API}/identity/v1/oauth2/token", timeout=15,
                   headers={"Authorization": f"Basic {basic}"},
                   data={"grant_type": "client_credentials",
                         "scope": "https://api.ebay.com/oauth/api_scope"})
    r.raise_for_status()
    return r.json()["access_token"]


def public_key(kid: str) -> str:
    """PEM for `kid` from eBay's Notification API; cached for the process lifetime."""
    with _lock:
        if kid in _keys:
            return _keys[kid]
    r = httpx.get(f"{EBAY_API}/commerce/notification/v1/public_key/{kid}", timeout=15,
                  headers={"Authorization": f"Bearer {_app_token()}"})
    r.raise_for_status()
    key = r.json()["key"]
    if "BEGIN PUBLIC KEY" in key and "\n" not in key:
        # eBay returns the PEM on one line; re-wrap the armour so the parser accepts it.
        key = key.replace("-----BEGIN PUBLIC KEY-----", "-----BEGIN PUBLIC KEY-----\n") \
                 .replace("-----END PUBLIC KEY-----", "\n-----END PUBLIC KEY-----")
    with _lock:
        _keys[kid] = key
    return key


def verify(body: bytes, header: str | None, get_key=None) -> bool:
    if not header:
        return False
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        sig = json.loads(base64.b64decode(header))
        pem = (get_key or public_key)(sig["kid"])
        key = serialization.load_pem_public_key(pem.encode())
        key.verify(base64.b64decode(sig["signature"]), body, ec.ECDSA(hashes.SHA1()))
        return True
    except Exception as exc:  # noqa: BLE001 — any failure = unverified, never a 500
        log.warning("ebay deletion: signature not verified (%s)", type(exc).__name__)
        return False


def delete_user(username: str | None, user_id: str | None) -> int:
    handles = [h for h in (username, user_id) if h]
    if not handles:
        return 0
    return db.execute(
        "DELETE FROM contacts WHERE platform = 'ebay' AND buyer_handle = ANY(%s)", (handles,)) or 0


@router.post(PATH)
async def ebay_deletion_notice(request: Request) -> Response:
    body = await request.body()
    try:
        data = (json.loads(body).get("notification") or {}).get("data") or {}
    except ValueError:
        return Response(status_code=204)
    if not await asyncio.to_thread(verify, body, request.headers.get("x-ebay-signature")):
        return Response(status_code=412)  # eBay's documented "signature failed" answer
    try:
        n = await asyncio.to_thread(delete_user, data.get("username"), data.get("userId"))
    except Exception as exc:  # noqa: BLE001 — non-2xx so eBay retries; never drop a deletion
        log.error("ebay deletion: delete failed (%s)", type(exc).__name__)
        return Response(status_code=500)
    if n:
        log.info("ebay deletion: removed %d CRM contact(s)", n)
    return Response(status_code=204)
