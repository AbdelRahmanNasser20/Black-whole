"""eBay Promoted Listings: read the campaign + suggested rates, and (on demand) write ad rates.

Plan `docs/superpowers/plans/2026-10-13-ebay-api-channel.md`, Phase 4. Used by
`scripts/ebay_ads.py` (CLI) and the read-only eBay section of the admin Channels tab
(`GET /api/channels/ebay`).

Rules:
- Rate rule = eBay's suggested rate for the listing, never above the cap
  (`EBAY_AD_RATE_CAP`, default 15). Listings with no suggestion get the cap.
- Nothing is written except through `apply()`, and `apply()` refuses unless the
  campaign state is `running` or `paused`.
- eBay answers 409 / errorId 35078 ("you must be in good standing with recent sales
  activity") when the account is not allowed Promoted Listings. That is a state
  (`not_eligible`) with eBay's own message, never a crash.
- `snapshot()` caches the whole read for `EBAY_ADS_CACHE_SEC` (default 900 s) so an
  admin page load never calls eBay. It holds no tokens and no raw responses.

Env (.env, gitignored): EBAY_APP_ID, EBAY_CERT_ID, EBAY_REFRESH_TOKEN (seller OAuth
refresh token). Sandbox: EBAY_SANDBOX_APP_ID / _CERT_ID / _REFRESH_TOKEN.
"""
from __future__ import annotations

import base64
import os
import re
import threading
import time
import xml.etree.ElementTree as ET
from typing import Any

import httpx

NS = "urn:ebay:apis:eBLBaseComponents"
SCOPES = " ".join([
    "https://api.ebay.com/oauth/api_scope",
    "https://api.ebay.com/oauth/api_scope/sell.marketing",
    "https://api.ebay.com/oauth/api_scope/sell.inventory",
])
# eBay error ids that mean "this seller may not use Promoted Listings right now".
NOT_ELIGIBLE_ERROR_IDS = frozenset({35078})

STATE_RUNNING = "running"
STATE_PAUSED = "paused"
STATE_NOT_ELIGIBLE = "not_eligible"
STATE_NO_CAMPAIGN = "no_campaign"
STATE_NOT_CONFIGURED = "not_configured"
STATE_ERROR = "error"
WRITABLE_STATES = (STATE_RUNNING, STATE_PAUSED)


class EbayNotConfigured(RuntimeError):
    """App keys or seller refresh token missing from the environment."""


class EbayError(RuntimeError):
    """An eBay API call failed. `error_id` / `ebay_message` come from eBay's error body."""

    def __init__(self, status: int, path: str, error_id: int | None, ebay_message: str, raw: str = ""):
        self.status, self.path = status, path
        self.error_id, self.ebay_message = error_id, ebay_message
        super().__init__(f"{path} → {status}"
                         + (f" (error {error_id})" if error_id else "")
                         + (f": {ebay_message}" if ebay_message else f": {raw[:300]}"))

    @property
    def not_eligible(self) -> bool:
        return self.error_id in NOT_ELIGIBLE_ERROR_IDS


def _error_from(r: httpx.Response, path: str) -> EbayError:
    error_id, msg = None, ""
    try:
        errs = (r.json() or {}).get("errors") or []
        if errs:
            error_id = int(errs[0].get("errorId")) if errs[0].get("errorId") is not None else None
            msg = str(errs[0].get("message") or "")
    except Exception:  # noqa: BLE001 — non-JSON error body
        pass
    return EbayError(r.status_code, path, error_id, msg, r.text)


def default_cap() -> float:
    try:
        return float(os.environ.get("EBAY_AD_RATE_CAP", "15"))
    except ValueError:
        return 15.0


def item_id_from_url(url: str | None) -> str | None:
    """`https://www.ebay.com/itm/123456789012?...` → `123456789012`. Anything else → None."""
    if not url:
        return None
    m = re.search(r"/itm/(?:[^/?#]+/)?(\d{9,15})", url)
    return m.group(1) if m else None


class EbayClient:
    def __init__(self, sandbox: bool = False, http: httpx.Client | None = None):
        p = "EBAY_SANDBOX_" if sandbox else "EBAY_"
        self.host = "https://api.sandbox.ebay.com" if sandbox else "https://api.ebay.com"
        self.app, self.cert = os.environ.get(p + "APP_ID"), os.environ.get(p + "CERT_ID")
        self.refresh = os.environ.get(p + "REFRESH_TOKEN")
        if not (self.app and self.cert):
            raise EbayNotConfigured(f"missing {p}APP_ID / {p}CERT_ID in .env")
        self.http = http or httpx.Client(timeout=30)
        self._token: str | None = None

    def _mint(self, body: dict) -> str:
        basic = base64.b64encode(f"{self.app}:{self.cert}".encode()).decode()
        path = "/identity/v1/oauth2/token"
        r = self.http.post(self.host + path, data=body, headers={"Authorization": f"Basic {basic}"})
        if r.status_code != 200:
            raise _error_from(r, path)
        return r.json()["access_token"]

    def app_token(self) -> str:
        return self._mint({"grant_type": "client_credentials",
                           "scope": "https://api.ebay.com/oauth/api_scope"})

    def token(self) -> str:
        if not self.refresh:
            raise EbayNotConfigured(
                "missing EBAY_REFRESH_TOKEN — developer.ebay.com → User Tokens → OAuth → "
                "sign in as the seller → copy the refresh token into .env")
        if not self._token:
            self._token = self._mint({"grant_type": "refresh_token",
                                      "refresh_token": self.refresh, "scope": SCOPES})
        return self._token

    def rest(self, method: str, path: str, **kw) -> dict:
        r = self.http.request(method, self.host + path, headers={
            "Authorization": f"Bearer {self.token()}",
            "X-EBAY-C-MARKETPLACE-ID": "EBAY_US", "Content-Type": "application/json"}, **kw)
        if r.status_code >= 400:
            raise _error_from(r, path)
        return r.json() if r.content else {}

    def active_listings(self) -> list[dict]:
        """Trading GetMyeBaySelling — the seller's active listings (id, title, price, qty)."""
        out, page = [], 1
        while True:
            body = (f'<?xml version="1.0" encoding="utf-8"?><GetMyeBaySellingRequest xmlns="{NS}">'
                    f"<ActiveList><Include>true</Include><Pagination><EntriesPerPage>200</EntriesPerPage>"
                    f"<PageNumber>{page}</PageNumber></Pagination></ActiveList></GetMyeBaySellingRequest>")
            r = self.http.post(self.host + "/ws/api.dll", content=body, headers={
                "X-EBAY-API-CALL-NAME": "GetMyeBaySelling", "X-EBAY-API-SITEID": "0",
                "X-EBAY-API-COMPATIBILITY-LEVEL": "1415", "X-EBAY-API-IAF-TOKEN": self.token(),
                "Content-Type": "text/xml"})
            root = ET.fromstring(r.content)
            if root.findtext(f"{{{NS}}}Ack") not in ("Success", "Warning"):
                raise EbayError(r.status_code, "GetMyeBaySelling", None,
                                root.findtext(f".//{{{NS}}}LongMessage") or "", r.text)
            for it in root.iter(f"{{{NS}}}Item"):
                out.append({"id": it.findtext(f"{{{NS}}}ItemID"),
                            "title": it.findtext(f"{{{NS}}}Title"),
                            "price": it.findtext(f".//{{{NS}}}CurrentPrice"),
                            "qty": it.findtext(f"{{{NS}}}QuantityAvailable")})
            pages = int(root.findtext(f".//{{{NS}}}ActiveList/{{{NS}}}PaginationResult/"
                                      f"{{{NS}}}TotalNumberOfPages") or 1)
            if page >= pages:
                return out
            page += 1

    def campaigns(self) -> list[dict]:
        return self.rest("GET", "/sell/marketing/v1/ad_campaign?limit=100").get("campaigns", [])

    def suggested_rates(self, ids: list[str]) -> dict[str, float]:
        """Recommendation API: eBay's suggested ad rate per listing (max 500 ids/call)."""
        rates: dict[str, float] = {}
        for i in range(0, len(ids), 500):
            res = self.rest("POST", "/sell/recommendation/v1/find?filter=recommendationTypes:%7BAD%7D",
                            json={"listingIds": ids[i:i + 500]})
            for rec in res.get("listingRecommendations", []):
                bids = (rec.get("marketing") or {}).get("ad", {}).get("bidPercentages") or []
                vals = [float(b["value"]) for b in bids if b.get("value")]
                if vals:
                    rates[rec["listingId"]] = max(vals)
        return rates

    def ads(self, campaign_id: str) -> dict[str, float]:
        res = self.rest("GET", f"/sell/marketing/v1/ad_campaign/{campaign_id}/ad?limit=500")
        return {a["listingId"]: float(a.get("bidPercentage") or 0) for a in res.get("ads", [])}


def _campaign_summary(c: dict) -> dict:
    fs = c.get("fundingStrategy") or {}
    rate = fs.get("bidPercentage")
    return {"id": c.get("campaignId"), "name": c.get("campaignName"),
            "status": c.get("campaignStatus"), "model": fs.get("fundingModel"),
            "rate": float(rate) if rate not in (None, "") else None}


def plan(client: EbayClient, cap: float | None = None) -> dict:
    """Read-only. Returns {state, reason, cap, campaign, rows, campaigns}.

    rows = one per active listing: {id, title, price, qty, now, suggested, want}.
    A 409 "not eligible" from eBay → state `not_eligible`, reason = eBay's message;
    the listing rows are still returned (current/suggested may be None).
    """
    cap = default_cap() if cap is None else float(cap)
    listings = client.active_listings()
    all_camps = client.campaigns()
    camps = [c for c in all_camps if (c.get("fundingStrategy") or {}).get("fundingModel")
             == "COST_PER_SALE" and c.get("campaignStatus") != "ENDED"]
    camp = camps[0] if camps else None
    state, reason = STATE_NO_CAMPAIGN, "No Promoted Listings General campaign — create one in Seller Hub."
    current: dict[str, float] = {}
    if camp:
        state = STATE_RUNNING if camp.get("campaignStatus") == "RUNNING" else STATE_PAUSED
        reason = None if state == STATE_RUNNING else f"eBay reports the campaign as {camp.get('campaignStatus')}."
        try:
            current = client.ads(camp["campaignId"])
        except EbayError as e:
            if not e.not_eligible:
                raise
            state, reason = STATE_NOT_ELIGIBLE, e.ebay_message or str(e)
    sugg: dict[str, float] = {}
    try:
        sugg = client.suggested_rates([l["id"] for l in listings if l.get("id")])
    except EbayError as e:
        if not e.not_eligible:
            raise
        state, reason = STATE_NOT_ELIGIBLE, e.ebay_message or str(e)
    rows = []
    for l in listings:
        s = sugg.get(l["id"])
        want = min(s if s is not None else cap, cap)
        rows.append({**l, "now": current.get(l["id"]), "suggested": s, "want": round(want, 1)})
    return {"state": state, "reason": reason, "cap": cap,
            "campaign": _campaign_summary(camp) if camp else None,
            "campaigns": [_campaign_summary(c) for c in all_camps], "rows": rows}


def apply(client: EbayClient, p: dict) -> dict:
    """Write the planned rates. Refuses unless the campaign is RUNNING/PAUSED (never on 409)."""
    if p["state"] not in WRITABLE_STATES or not p.get("campaign"):
        raise RuntimeError(f"refusing to write ad rates: state={p['state']} ({p.get('reason')})")
    cid = p["campaign"]["id"]
    rows = p["rows"]
    upd = [{"listingId": r["id"], "bidPercentage": str(r["want"])}
           for r in rows if r["now"] is not None and abs(r["now"] - r["want"]) >= 0.1]
    new = [{"listingId": r["id"], "bidPercentage": str(r["want"])} for r in rows if r["now"] is None]
    if upd:
        client.rest("POST", f"/sell/marketing/v1/ad_campaign/{cid}/bulk_update_ads_bid_by_listing_id",
                    json={"requests": upd})
    if new:
        client.rest("POST", f"/sell/marketing/v1/ad_campaign/{cid}/bulk_create_ads_by_listing_id",
                    json={"requests": new})
    return {"campaign_id": cid, "updated": len(upd), "added": len(new)}


# ───────────── cached snapshot for the admin (never call eBay per page load) ─────────────

CACHE_SEC = float(os.getenv("EBAY_ADS_CACHE_SEC", "900"))
_cache_lock = threading.Lock()
_cache: dict[str, Any] = {}


def reset_cache() -> None:
    with _cache_lock:
        _cache.clear()


def _fresh_snapshot(cap: float) -> dict:
    try:
        p = plan(EbayClient(), cap)
        p["error"] = None
        return p
    except EbayNotConfigured as e:
        return {"state": STATE_NOT_CONFIGURED, "reason": str(e), "cap": cap,
                "campaign": None, "campaigns": [], "rows": [], "error": None}
    except (EbayError, httpx.HTTPError, ET.ParseError) as e:
        return {"state": STATE_ERROR, "reason": f"eBay read failed: {e}", "cap": cap,
                "campaign": None, "campaigns": [], "rows": [], "error": str(e)}


def snapshot(force: bool = False, ttl: float | None = None) -> dict:
    """The plan, cached for `ttl` seconds (default EBAY_ADS_CACHE_SEC). Blocking — call it
    from a threadpool. Errors are cached too (so a dead eBay is not hammered), with a
    shorter TTL of a minute. Adds `fetched_at` (epoch s)."""
    ttl_s = CACHE_SEC if ttl is None else float(ttl)
    now = time.time()
    with _cache_lock:
        hit = _cache.get("snap")
        if hit and not force:
            age_limit = ttl_s if hit["state"] != STATE_ERROR else min(ttl_s, 60.0)
            if now - hit["fetched_at"] < age_limit:
                return hit
    snap = _fresh_snapshot(default_cap())
    snap["fetched_at"] = time.time()
    with _cache_lock:
        _cache["snap"] = snap
    return snap
