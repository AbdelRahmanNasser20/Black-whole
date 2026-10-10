"""Price anchors: other sellers' banquet-chair listings priced ABOVE ours.

A daily job (``scripts/price_anchors.py run``, Render cron ``price-anchors``) asks
Claude Haiku — with Anthropic's server-side web search + fetch — for live used
hotel/banquet chair listings asking >= ``MIN_PRICE`` per chair, plus new-price
references for the same hotel brands. Rows land in ``public.price_anchors``
(migration 026), one per listing URL; the photo is copied to the PRIVATE R2
bucket because it is someone else's photo.

Rules:
- These are anchors ("others ask $35-$55"), never our stock. Nothing here writes
  to ``inventory`` or any public surface.
- Our own listings (black-whole.com, our eBay seller) are dropped.
- A row the search stops finding is marked ``gone`` after ``GONE_AFTER_DAYS`` —
  search results vary day to day, so one missed day is not proof it sold.
- ``status='hidden'`` is an operator decision; a re-run never un-hides a row.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass, field
from urllib.parse import urlparse

from automation import db

MODEL = os.getenv("PRICE_ANCHORS_MODEL", "claude-haiku-5-5")
MIN_PRICE = float(os.getenv("PRICE_ANCHORS_MIN_PRICE", "28"))
MAX_SEARCHES = int(os.getenv("PRICE_ANCHORS_MAX_SEARCHES", "12"))
MAX_FETCHES = int(os.getenv("PRICE_ANCHORS_MAX_FETCHES", "10"))
MAX_CONTINUATIONS = 4
GONE_AFTER_DAYS = 21
IMAGE_MAX_BYTES = 4_000_000
R2_PREFIX = "price-anchors/"

OWN_DOMAINS = ("black-whole.com",)
OWN_MARKERS = ("abdel.-3241",)  # our eBay seller id

CONDITIONS = {"used", "refurb", "new"}
TIERS = {"hotel", "event", "new"}

# (column, max length) — mirrors the CHECKs in 026_price_anchors.sql.
_TEXT_LIMITS = {
    "title": 200, "seller": 120, "brand": 80, "source": 40,
    "location": 120, "listed_on": 40, "note": 300,
}

PROMPT = """You research the US used banquet-chair market for a seller of used hotel banquet chairs at $25 per chair.

Find LIVE listings that ask MORE than ${min_price} per chair. Priority order:
1. Used hotel-grade banquet chairs in bulk (100+, ideally 500-1,000+): MTS Seating, Shelby Williams, Gasser, Falcon, Mity-Lite, Chiavari, Holsag. Sources: eBay, Craigslist (big US cities), hotel liquidators, used-furniture dealers, UsedPews, rental-fleet sales.
2. Other used banquet/event chairs at ${min_price}+ (any quantity).
3. NEW prices for the same hotel brands (dealer list or street price) - at most 4 of these.

Skip: anything on black-whole.com, eBay seller "abdel.-3241", office/task chairs, pews, folding plastic chairs, tables.
Already known (do not repeat unless the price changed): {known}

Use web search, and fetch a listing page when you need its price, quantity or photo. For each listing give the direct URL of its main photo (an i.ebayimg.com / images.craigslist.org / og:image .jpg/.png/.webp URL) when you can find it; otherwise null. Never invent a URL or a number - leave unknown fields null.

Reply with ONLY one JSON object, no prose, in this shape:
{{"listings": [{{"listing_url": str, "title": str, "seller": str|null, "brand": str|null,
  "condition": "used"|"refurb"|"new", "tier": "hotel"|"event"|"new",
  "source": "ebay"|"craigslist"|"dealer"|"usedpews"|"retail"|"other",
  "qty": int|null, "price_per_chair": number, "location": str|null,
  "listed_on": str|null, "image_url": str|null, "note": str|null}}]}}
tier = "hotel" for the hotel brands above when used, "event" for other used chairs, "new" for new prices."""


@dataclass
class RunResult:
    listings: list[dict] = field(default_factory=list)
    rejected: list[tuple[str, str]] = field(default_factory=list)
    searches: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    stop_reason: str | None = None


# ── parsing / validation (pure) ──────────────────────────────────────────────

def extract_json(text: str) -> dict:
    """The JSON object in the model's final text (tolerates ``` fences / prose)."""
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    raw = m.group(1) if m else text[text.find("{"): text.rfind("}") + 1]
    if not raw:
        raise ValueError("no JSON object in model reply")
    return json.loads(raw)


def _is_http(url) -> bool:
    if not isinstance(url, str):
        return False
    p = urlparse(url.strip())
    return p.scheme in ("http", "https") and bool(p.netloc)


def is_own(item: dict) -> bool:
    blob = " ".join(str(item.get(k) or "") for k in ("listing_url", "seller", "title")).lower()
    host = urlparse(str(item.get("listing_url") or "")).netloc.lower()
    return any(host.endswith(d) for d in OWN_DOMAINS) or any(m in blob for m in OWN_MARKERS)


def normalize(item: dict, *, min_price: float = MIN_PRICE) -> tuple[dict | None, str | None]:
    """A DB-ready row, or (None, reason). Never invents a value."""
    if not isinstance(item, dict):
        return None, "not an object"
    url = (item.get("listing_url") or "").strip()
    if not _is_http(url):
        return None, "bad listing_url"
    if is_own(item):
        return None, "our own listing"
    try:
        price = round(float(item.get("price_per_chair")), 2)
    except (TypeError, ValueError):
        return None, "no price"
    if price < min_price:
        return None, f"price {price} below {min_price}"
    if price > 2000:
        return None, f"price {price} not per-chair"
    cond = str(item.get("condition") or "").lower()
    tier = str(item.get("tier") or "").lower()
    if cond not in CONDITIONS:
        return None, f"bad condition {cond!r}"
    if tier not in TIERS:
        return None, f"bad tier {tier!r}"
    if (cond == "new") != (tier == "new"):
        tier = "new" if cond == "new" else ("event" if tier == "new" else tier)
    qty = item.get("qty")
    try:
        qty = int(qty) if qty not in (None, "") else None
    except (TypeError, ValueError):
        qty = None
    if qty is not None and qty <= 0:
        qty = None
    row = {
        "listing_url": url[:600],
        "condition": cond,
        "tier": tier,
        "qty": qty,
        "price_per_chair": price,
        "image_source_url": item["image_url"].strip()[:600] if _is_http(item.get("image_url")) else None,
    }
    for col, limit in _TEXT_LIMITS.items():
        v = item.get(col)
        row[col] = str(v).strip()[:limit] if v not in (None, "") else None
    if not row["title"]:
        return None, "no title"
    row["source"] = row["source"] or (urlparse(url).netloc.removeprefix("www.")[:40])
    return row, None


# ── model call ───────────────────────────────────────────────────────────────

def _client():
    import anthropic  # lazy: only the run path needs the SDK

    return anthropic.Anthropic()


def search(*, known_urls: list[str] | None = None, client=None,
           max_searches: int = MAX_SEARCHES, min_price: float = MIN_PRICE) -> RunResult:
    """One Haiku research turn with server-side web search/fetch → validated rows."""
    client = client or _client()
    known = ", ".join(known_urls[:60]) if known_urls else "none"
    messages = [{"role": "user", "content": PROMPT.format(min_price=f"{min_price:g}", known=known)}]
    tools = [
        {"type": "web_search_20250305", "name": "web_search", "max_uses": max_searches,
         "user_location": {"type": "approximate", "country": "US"}},
        {"type": "web_fetch_20250910", "name": "web_fetch", "max_uses": MAX_FETCHES},
    ]
    res = RunResult()
    for _ in range(MAX_CONTINUATIONS + 1):
        resp = client.messages.create(
            model=MODEL, max_tokens=16000, tools=tools, messages=messages,
            output_config={"effort": "low"},
        )
        u = resp.usage
        res.input_tokens += getattr(u, "input_tokens", 0) or 0
        res.output_tokens += getattr(u, "output_tokens", 0) or 0
        stu = getattr(u, "server_tool_use", None)
        res.searches += (getattr(stu, "web_search_requests", 0) or 0) if stu else 0
        res.stop_reason = resp.stop_reason
        if resp.stop_reason != "pause_turn":
            break
        messages = [messages[0], {"role": "assistant", "content": resp.content}]
    if res.stop_reason == "refusal":
        raise RuntimeError("model refused the research request")
    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    payload = extract_json(text)
    seen: set[str] = set()
    for item in payload.get("listings") or []:
        row, why = normalize(item, min_price=min_price)
        if row is None:
            res.rejected.append((str((item or {}).get("listing_url") if isinstance(item, dict) else item)[:120], why))
        elif row["listing_url"] not in seen:
            seen.add(row["listing_url"])
            res.listings.append(row)
    return res


# ── storage ──────────────────────────────────────────────────────────────────

_COLS = ("listing_url", "title", "seller", "brand", "condition", "tier", "source", "qty",
         "price_per_chair", "location", "listed_on", "image_source_url", "note")

_UPSERT = f"""
INSERT INTO public.price_anchors ({", ".join(_COLS)})
VALUES ({", ".join(["%s"] * len(_COLS))})
ON CONFLICT (listing_url) DO UPDATE SET
  title = EXCLUDED.title,
  seller = COALESCE(EXCLUDED.seller, price_anchors.seller),
  brand = COALESCE(EXCLUDED.brand, price_anchors.brand),
  condition = EXCLUDED.condition,
  tier = EXCLUDED.tier,
  source = EXCLUDED.source,
  qty = COALESCE(EXCLUDED.qty, price_anchors.qty),
  price_per_chair = EXCLUDED.price_per_chair,
  location = COALESCE(EXCLUDED.location, price_anchors.location),
  listed_on = COALESCE(EXCLUDED.listed_on, price_anchors.listed_on),
  image_source_url = COALESCE(EXCLUDED.image_source_url, price_anchors.image_source_url),
  note = COALESCE(EXCLUDED.note, price_anchors.note),
  status = CASE WHEN price_anchors.status = 'hidden' THEN 'hidden' ELSE 'active' END,
  last_seen_at = now(),
  seen_count = price_anchors.seen_count + 1
RETURNING id, (xmax = 0) AS inserted, image_r2_key, image_source_url
"""


def upsert(rows: list[dict]) -> list[dict]:
    """Insert or refresh each row. Returns id / inserted / image fields per row."""
    out = []
    for r in rows:
        got = db.fetch_one(_UPSERT, [r.get(c) for c in _COLS])
        if got:
            out.append(got)
    return out


def known_urls(limit: int = 60) -> list[str]:
    rows = db.fetch_all(
        "SELECT listing_url FROM public.price_anchors WHERE status = 'active' "
        "ORDER BY last_seen_at DESC LIMIT %s", [limit])
    return [r["listing_url"] for r in rows]


def mark_gone(days: int = GONE_AFTER_DAYS) -> int:
    return db.execute(
        "UPDATE public.price_anchors SET status = 'gone' WHERE status = 'active' "
        "AND last_seen_at < now() - make_interval(days => %s)", [days])


def r2_key_for(image_url: str, content_type: str) -> str:
    ext = {"image/png": "png", "image/webp": "webp"}.get(content_type, "jpg")
    return f"{R2_PREFIX}{hashlib.sha1(image_url.encode()).hexdigest()[:20]}.{ext}"


def archive_image(row_id: int, image_url: str, *, s3=None, bucket: str | None = None) -> str | None:
    """Copy one listing photo to the PRIVATE bucket; returns the key or None.

    Raises PrivateBucketNotConfigured when the private bucket is unset — the
    caller decides whether that stops the run. Never writes to the public bucket.
    """
    import httpx

    from automation import r2_images

    bucket = bucket or r2_images.private_bucket()
    s3 = s3 or r2_images.client()
    try:
        resp = httpx.get(image_url, timeout=30, follow_redirects=True,
                         headers={"User-Agent": "Mozilla/5.0"})
    except httpx.HTTPError as e:
        print(f"[price_anchors] photo fetch failed {image_url[:80]}: {e}", file=sys.stderr)
        return None
    ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
    if resp.status_code != 200 or not ctype.startswith("image/") or not resp.content:
        print(f"[price_anchors] not an image ({resp.status_code} {ctype}) {image_url[:80]}", file=sys.stderr)
        return None
    if len(resp.content) > IMAGE_MAX_BYTES:
        print(f"[price_anchors] photo too large ({len(resp.content)} B) {image_url[:80]}", file=sys.stderr)
        return None
    key = r2_key_for(image_url, ctype)
    if not r2_images.put_private_object(s3, bucket=bucket, path=key, data=resp.content, content_type=ctype):
        return None
    db.execute("UPDATE public.price_anchors SET image_r2_key = %s WHERE id = %s", [key, row_id])
    return key


def archive_missing_images(limit: int = 50) -> tuple[int, int]:
    """Photos for rows that have a source URL but no private copy yet → (saved, failed)."""
    from automation import r2_images

    rows = db.fetch_all(
        "SELECT id, image_source_url FROM public.price_anchors WHERE image_r2_key IS NULL "
        "AND image_source_url IS NOT NULL AND status <> 'hidden' ORDER BY first_seen_at DESC LIMIT %s",
        [limit])
    if not rows:
        return 0, 0
    bucket = r2_images.private_bucket()  # raises when unset — loud, by design
    s3 = r2_images.client()
    saved = sum(1 for r in rows if archive_image(r["id"], r["image_source_url"], s3=s3, bucket=bucket))
    return saved, len(rows) - saved


def list_active(tier: str | None = None) -> list[dict]:
    sql = ("SELECT id, title, brand, seller, qty, price_per_chair, location, tier, condition, "
           "listing_url, image_r2_key, last_seen_at::date AS last_seen FROM public.price_anchors "
           "WHERE status = 'active'")
    params: list = []
    if tier:
        sql += " AND tier = %s"
        params.append(tier)
    return db.fetch_all(sql + " ORDER BY tier, price_per_chair DESC", params)


def cost_usd(res: RunResult) -> float:
    """Haiku 5.5 tokens ($0.10 / $0.50 per M) + web search ($10 per 1,000)."""
    return res.input_tokens * 0.10e-6 + res.output_tokens * 0.50e-6 + res.searches * 0.01
