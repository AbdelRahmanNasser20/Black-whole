"""Minnesota MNBid (`mnbid.mn.gov`) source adapter — AuctionSoftware.com React
SPA whose API host is plain nginx/Express JSON.

LIVE RECON (verified 2026-10-09):

- Frontend `https://mnbid.mn.gov` (Radware bot-manager cookies; robots.txt
  allows everything). API `https://minnbidapi-prod.ecommerce.auction`
  (`REACT_APP_DOMAIN` in the bundle), no robots.txt (404), no auth for the
  public endpoints — BUT it checks the browser `Origin`: without
  `Origin: https://mnbid.mn.gov` every call answers
  `{"status":"error","data":{"responseType":203,"message":"API disabled by
  admin","additionalMessage":"Front Domain Mismatch"}}` (HTTP 200!). So the
  adapter sends `Origin`/`Referer` for the frontend on every request and
  treats a `status != "success"` body as a fetch failure.
- The brief's `POST /api/front/product/search` is 404; the SPA's request
  builder (`k_("post","search",payload,"","front/product",true)`) prefixes
  `/api/custom/` when that last flag is set, so the real paths are:
    `POST /api/custom/front/product/search`         (listing + sold search)
    `POST /api/custom/front/product/singleProduct`  (`{"id": <int>}`)
    `POST /api/product/bidhistory`                  (`{"id": <int>, "limit", "page",
        "filters": {"declined": {"value": "0", "type": "in", "field": "b.declined"}}}`)
  Search payload (copied from the bundle's `/search` + `/sold_items` pages):
  `{"limit": N, "page": 1, "orderby": "p.id, desc", "order": "",
    "market_status": ["open"] | ["sold"], "filters": {...}}` — `market_status`
  is a TOP-LEVEL list, not a filters entry (a `filters.market_status` guess
  returned 0 rows). `filters.searchbar` = `{"value": "chair", "type": "like",
  "field": "p.title,p.desc_proc,p.id,it.manufacturer,it.model_no"}` is the
  site search box; we leave filters empty and match `FURNITURE_TERMS` on
  `title + desc_proc` ourselves (the site search matches "wheelchair" for
  "chair"). Response: `data.data.responseData.records[]` + `totalRecords`.
  163 open lots site-wide at probe time; sold search sorted
  `"p.date_closed, desc"` returns the most recent closes first.
- Record fields used: `id` (int, `source_lot_id` as string), `title`,
  `desc_proc` (HTML), `wprice` (current/winning price — `current_bid` on
  search rows equals it), `market_status` (`open` | `sold` | `closed`),
  `date_closed` (ISO-8601 **UTC** with `Z` — no tz conversion needed),
  `date_added`, `category` (int id, no name endpoint found — stored as a
  string), `region` (int id), `conditionTypeId`, `avatarorg` (full image
  URL), `avatar`, `auctiontype`, `qty`, `sold`, `reserve_met`. No bid count
  on a product record — `bidhistory.totalRecords` is the bid count (one
  extra POST per furniture lot; failure → `bid_count=None`).
- Lot page: `https://mnbid.mn.gov/productView/<id>` (SPA route).
- `singleProduct` for an unknown id returns `records: []` with
  `status: success` → treated as not-found (gone once past `end_date`).
- Currency USD. Timezone: n/a (UTC in the API).

raw keys (stable, read by the web layer): `title`, `city` (None — the API
gives region ids only), `state` ("MN"), `category` (id as string),
`image_url`, `url`, `currency`, plus the untouched product record under
`mnbid` and `bid_history_total` when fetched.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

import requests

from recorder.models import Observation
from recorder.sources.base import FURNITURE_TERMS, PollBudget, SourceFetchFailed, polite_post

SOURCE = "mnbid"

FRONTEND_URL = "https://mnbid.mn.gov"
API_URL = "https://minnbidapi-prod.ecommerce.auction"
SEARCH_URL = f"{API_URL}/api/custom/front/product/search"
SINGLE_URL = f"{API_URL}/api/custom/front/product/singleProduct"
BIDHISTORY_URL = f"{API_URL}/api/product/bidhistory"
HEADERS = {"Origin": FRONTEND_URL, "Referer": f"{FRONTEND_URL}/", "Content-Type": "application/json"}
PAGE_SIZE = 100
MAX_PAGES = 20
SOLD_SWEEP_PAGES = 2
SEARCH_FIELDS = "p.title,p.desc_proc,p.id,it.manufacturer,it.model_no"

_TAG_RE = re.compile(r"<[^>]+>")


def _money(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def _parse_iso(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _is_furniture(rec: dict) -> bool:
    text = f"{rec.get('title') or ''} {_TAG_RE.sub(' ', rec.get('desc_proc') or '')}".lower()
    return any(term in text for term in FURNITURE_TERMS)


def _status(rec: dict) -> str:
    ms = str(rec.get("market_status") or "").lower()
    if ms in ("sold", "closed", "unsold"):
        return "closed"
    return "active"


def search_payload(market_status: str, page: int, *, orderby: str = "p.id, desc",
                   limit: int = PAGE_SIZE, searchbar: str | None = None) -> dict:
    filters: dict = {}
    if searchbar:
        filters["searchbar"] = {"value": searchbar, "type": "like", "field": SEARCH_FIELDS}
    return {"limit": limit, "page": page, "orderby": orderby, "order": "",
            "market_status": [market_status], "filters": filters}


def _post(url: str, payload: dict) -> dict | None:
    """The API's `responseData` dict, or None on any failure (transport,
    non-200, non-JSON, `status != success` — e.g. the Origin check)."""
    try:
        resp = polite_post(url, json=payload, headers=HEADERS)
    except requests.exceptions.RequestException as e:
        print(f"[{SOURCE}] RECORDER ERROR: request failed: {e}")
        return None
    if resp.status_code != 200:
        print(f"[{SOURCE}] RECORDER ERROR: HTTP {resp.status_code} on {url}")
        return None
    try:
        body = resp.json()
    except ValueError:
        print(f"[{SOURCE}] RECORDER ERROR: non-JSON response from {url}")
        return None
    if not isinstance(body, dict) or body.get("status") != "success":
        msg = (body.get("data") or {}).get("message") if isinstance(body, dict) else None
        print(f"[{SOURCE}] RECORDER ERROR: API status {body.get('status') if isinstance(body, dict) else '?'}"
              f" on {url}: {msg}")
        return None
    data = (body.get("data") or {}).get("responseData")
    if not isinstance(data, dict) or not isinstance(data.get("records"), list):
        print(f"[{SOURCE}] RECORDER ERROR: unexpected response shape from {url}")
        return None
    return data


def _search_all(market_status: str, *, orderby: str, max_pages: int) -> list[dict] | None:
    """Every record across pages; None if page 1 failed, partial (with a
    warning) if a later page failed."""
    records: list[dict] = []
    page = 1
    total: int | None = None
    while page <= max_pages:
        data = _post(SEARCH_URL, search_payload(market_status, page, orderby=orderby))
        if data is None:
            if page == 1:
                return None
            print(f"[{SOURCE}] WARNING: search page {page} failed — returning {len(records)} partial records")
            break
        page_records = data["records"]
        records.extend(r for r in page_records if isinstance(r, dict))
        try:
            total = int(data.get("totalRecords"))
        except (TypeError, ValueError):
            total = None
        if not page_records or (total is not None and len(records) >= total):
            break
        page += 1
    return records


def _bid_count(product_id: int) -> int | None:
    data = _post(BIDHISTORY_URL, {
        "limit": 1, "page": 1, "id": product_id,
        "filters": {"declined": {"value": "0", "type": "in", "field": "b.declined"}},
    })
    if data is None:
        return None
    try:
        return int(data.get("totalRecords"))
    except (TypeError, ValueError):
        return None


def _to_observation(rec: dict, bid_count: int | None, *, status: str | None = None) -> Observation:
    raw = {
        "title": rec.get("title"),
        "city": None,
        "state": "MN",
        "category": str(rec["category"]) if rec.get("category") is not None else None,
        "image_url": rec.get("avatarorg"),
        "url": f"{FRONTEND_URL}/productView/{rec.get('id')}",
        "currency": "USD",
        "mnbid": rec,
    }
    if bid_count is not None:
        raw["bid_history_total"] = bid_count
    return Observation(
        source=SOURCE,
        source_lot_id=str(rec["id"]),
        status=status or _status(rec),
        raw=raw,
        current_bid=_money(rec.get("wprice")),
        bid_count=bid_count,
        end_date=_parse_iso(rec.get("date_closed")),
    )


class MNBidSource:
    SOURCE = SOURCE

    def discover(self) -> list[Observation]:
        records = _search_all("open", orderby="p.id, desc", max_pages=MAX_PAGES)
        if records is None:
            raise SourceFetchFailed("discover() aborted — product search failed", url=SEARCH_URL)
        matched = [r for r in records if r.get("id") is not None and _is_furniture(r)]
        out = [_to_observation(r, _bid_count(int(r["id"]))) for r in matched]
        if not out:
            print(f"[{SOURCE}] WARNING: discover() found 0 furniture-matched open lots out of "
                  f"{len(records)} fetched — check FURNITURE_TERMS for drift")
        return out

    def poll(self, lots: list[dict]) -> list[Observation]:
        if not lots:
            return []
        now = datetime.now(timezone.utc)
        budget = PollBudget()
        observations: list[Observation] = []
        for lot in lots:
            if budget.exhausted:
                print(f"[{SOURCE}] RECORDER ERROR: poll() aborted after "
                      f"{budget.consecutive} consecutive failures — {budget.stats(len(lots))}")
                break
            lot_id = str(lot["source_lot_id"])
            try:
                product_id = int(lot_id)
            except ValueError:
                print(f"[{SOURCE}] RECORDER ERROR: non-numeric source_lot_id {lot_id!r}")
                budget.record(False)
                continue
            data = _post(SINGLE_URL, {"id": product_id})
            if data is None:
                budget.record(False)
                continue
            budget.record(True)
            rec = next((r for r in data["records"] if isinstance(r, dict) and str(r.get("id")) == lot_id), None)
            if rec is None:
                end_date = lot.get("end_date")
                if end_date is not None and end_date <= now:
                    observations.append(Observation(
                        source=SOURCE, source_lot_id=lot_id, status="gone",
                        raw={"recorder_probe": {"result": "not_found", "http_status": 200, "url": SINGLE_URL}},
                    ))
                continue
            observations.append(_to_observation(rec, _bid_count(product_id)))
        return observations

    def sold_sweep(self) -> list[Observation]:
        records = _search_all("sold", orderby="p.date_closed, desc", max_pages=SOLD_SWEEP_PAGES)
        if records is None:
            print(f"[{SOURCE}] RECORDER ERROR: sold_sweep() — sold search failed, 0 observations")
            return []
        matched = [r for r in records if r.get("id") is not None and _is_furniture(r)]
        return [_to_observation(r, _bid_count(int(r["id"])), status="closed") for r in matched]
