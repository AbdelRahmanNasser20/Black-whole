"""Price anchors: other sellers' banquet-chair listings priced ABOVE ours.

A daily job (``scripts/price_anchors.py run``, Render cron ``price-anchors``) asks
Groq's free ``openai/gpt-oss-120b`` — with Groq's built-in server-side
``browser_search`` tool — for live used hotel/banquet chair listings asking
>= ``MIN_PRICE`` per chair, plus new-price references for the same hotel brands.
One chat request per research topic (``TOPICS``), capped per run by
``MAX_REQUESTS`` (the free tier allows ~1,000 requests/day, but each browsing
request burns 10-30k tokens, so the real ceiling is the daily token budget).
Rows land in ``public.price_anchors`` (migration 026), one per listing URL; the
photo is copied to the PRIVATE R2 bucket because it is someone else's photo.

Rules:
- These are anchors ("others ask $35-$55"), never our stock. Nothing here writes
  to ``inventory`` or any public surface.
- Our own listings (black-whole.com, our eBay seller) are dropped.
- **Grounding.** gpt-oss invents plausible eBay item ids, Craigslist post ids and
  prices (seen live 2026-10-13). A row is kept only when its URL appeared in the
  tool's own search results / opened pages, and its per-chair price (or the lot
  total) appears in that source's text. Everything else is rejected, not stored.
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
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse

from automation import db

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
MODEL = os.getenv("PRICE_ANCHORS_MODEL", "openai/gpt-oss-120b")
MIN_PRICE = float(os.getenv("PRICE_ANCHORS_MIN_PRICE", "28"))
# One request = one topic. Hard ceiling keeps a typo far under the 1,000/day cap.
MAX_REQUESTS = int(os.getenv("PRICE_ANCHORS_MAX_REQUESTS", "5"))
REQUEST_CEILING = 50
MAX_RETRIES = 2            # per request, on 429 / 5xx, honouring Retry-After
MAX_RETRY_WAIT = 90        # seconds; a longer Retry-After = daily budget gone, stop
REPLY_TOKENS = 6000        # gpt-oss reasoning is billed against this; 4k truncated
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

# Research topics, highest value first. A run takes the first MAX_REQUESTS.
TOPICS = (
    "Used MTS Seating, Shelby Williams or Gasser hotel banquet chairs for sale in bulk "
    "(100+) on Craigslist in big US cities.",
    "Hotel liquidators and used commercial-furniture dealers selling used hotel banquet "
    "chairs (MTS, Shelby Williams, Gasser, Falcon, Mity-Lite) in bulk.",
    "eBay listings of used hotel banquet chairs (MTS, Shelby Williams, Gasser) sold as lots.",
    "Other used banquet / event / stacking chairs for sale (Chiavari, Holsag, church "
    "chairs, UsedPews.org, rental-fleet sales).",
    "NEW dealer or street prices for MTS Seating, Shelby Williams and Gasser banquet "
    "chairs (at most 4 references; condition=new, tier=new).",
)

PROMPT = """You research the US used banquet-chair market for a seller of used hotel banquet chairs at $25 per chair.

Topic for this search: {topic}

Find LIVE listings that ask MORE than ${min_price} per chair. Use browser_search, and open a listing page when you need its price, quantity or photo.
Skip: anything on black-whole.com, eBay seller "abdel.-3241", office/task chairs, pews, folding plastic chairs, tables.
Already known (do not repeat unless the price changed): {known}

Only report a listing whose URL you actually saw in a search result or opened page - copy it exactly. Never invent a URL, an item id or a number; leave unknown fields null. If you found nothing, reply {{"listings": []}}.
For each listing give the direct URL of its main photo (an i.ebayimg.com / images.craigslist.org / og:image .jpg/.png/.webp URL) when you saw it; otherwise null.

Reply with ONLY one JSON object, no table, no prose, in this shape:
{{"listings": [{{"listing_url": str, "title": str, "seller": str|null, "brand": str|null,
  "condition": "used"|"refurb"|"new", "tier": "hotel"|"event"|"new",
  "source": "ebay"|"craigslist"|"dealer"|"usedpews"|"retail"|"other",
  "qty": int|null, "price_per_chair": number, "location": str|null,
  "listed_on": str|null, "image_url": str|null, "note": str|null}}]}}
tier = "hotel" for used MTS/Shelby Williams/Gasser/Falcon/Mity-Lite, "event" for other used chairs, "new" for new prices."""


class GroqUnavailable(RuntimeError):
    """No key, auth failure, or the daily budget is spent — no result, not an empty one."""


@dataclass
class RunResult:
    listings: list[dict] = field(default_factory=list)
    rejected: list[tuple[str, str]] = field(default_factory=list)
    requests: int = 0          # chat requests sent to Groq (the free-tier unit)
    searches: int = 0          # browser_search calls the model made inside them
    tokens: int = 0
    failed_topics: list[tuple[str, str]] = field(default_factory=list)
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


# ── grounding (pure) ─────────────────────────────────────────────────────────

def _url_key(url: str) -> str:
    """Comparable form of a URL: lower host without www, path without trailing /, no query/fragment."""
    p = urlparse(str(url or "").strip())
    return f"{p.netloc.lower().removeprefix('www.')}{p.path.rstrip('/')}"


def evidence_from(executed_tools) -> dict[str, str]:
    """{url_key: text the tool actually returned for it} from Groq's ``executed_tools``."""
    ev: dict[str, str] = {}
    for t in executed_tools or []:
        results = ((t or {}).get("search_results") or {}).get("results") or []
        for r in results:
            url = (r or {}).get("url")
            if not _is_http(url):
                continue
            k = _url_key(url)
            ev[k] = ev.get(k, "") + "\n" + f"{r.get('title') or ''}\n{r.get('content') or ''}"
    return ev


def _numbers(text: str) -> set[float]:
    return {round(float(n.replace(",", "")), 2)
            for n in re.findall(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?", text or "")}


def grounded(row: dict, evidence: dict[str, str]) -> str | None:
    """None when the row is backed by what the tool saw, else the rejection reason."""
    text = evidence.get(_url_key(row["listing_url"]))
    if text is None:
        return "url not in search results"
    nums = _numbers(text)
    price = row["price_per_chair"]
    if price in nums:
        return None
    if row.get("qty") and round(price * row["qty"], 2) in nums:
        return None   # source shows the lot total; the model divided it
    return f"price {price:g} not in source text"


# ── model call ───────────────────────────────────────────────────────────────

def _api_key() -> str:
    key = os.getenv("GROQ_API_KEY")
    if not key:
        raise GroqUnavailable("GROQ_API_KEY is not set")
    return key


def _post(body: dict, *, http=None, sleep=time.sleep) -> dict:
    """One Groq chat request with bounded Retry-After retries."""
    import httpx

    http = http or httpx
    for attempt in range(MAX_RETRIES + 1):
        resp = http.post(GROQ_URL, json=body, timeout=240,
                         headers={"Authorization": f"Bearer {_api_key()}"})
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code in (401, 403):
            raise GroqUnavailable(f"Groq auth failed ({resp.status_code})")
        retryable = resp.status_code == 429 or resp.status_code >= 500
        try:
            wait = float(resp.headers.get("retry-after") or 20)
        except ValueError:
            wait = 20.0
        if not retryable or attempt == MAX_RETRIES:
            raise RuntimeError(f"Groq HTTP {resp.status_code}: {resp.text[:300]}")
        if wait > MAX_RETRY_WAIT:
            raise GroqUnavailable(f"Groq rate limit: retry in {wait:.0f}s (daily budget spent?)")
        sleep(wait)
    raise AssertionError("unreachable")


def search(*, known_urls: list[str] | None = None, http=None, sleep=time.sleep,
           max_requests: int = MAX_REQUESTS, min_price: float = MIN_PRICE) -> RunResult:
    """One Groq browser_search request per topic → grounded, validated, deduped rows."""
    known = ", ".join(known_urls[:40]) if known_urls else "none"
    n = max(0, min(max_requests, REQUEST_CEILING, len(TOPICS)))
    res = RunResult()
    seen: set[str] = set()
    for topic in TOPICS[:n]:
        body = {
            "model": MODEL,
            "messages": [{"role": "user", "content": PROMPT.format(
                topic=topic, min_price=f"{min_price:g}", known=known)}],
            "tools": [{"type": "browser_search"}],
            "tool_choice": "required",
            "reasoning_effort": os.getenv("PRICE_ANCHORS_REASONING_EFFORT", "low"),
            "max_completion_tokens": REPLY_TOKENS,
            "temperature": 0,
        }
        res.requests += 1
        try:
            data = _post(body, http=http, sleep=sleep)
        except GroqUnavailable:
            if not res.listings and not res.failed_topics:
                raise          # nothing worked at all — loud
            res.failed_topics.append((topic[:60], "budget/auth"))
            break
        except Exception as e:  # noqa: BLE001 - one bad topic never kills the run
            res.failed_topics.append((topic[:60], f"{type(e).__name__}: {e}"[:200]))
            continue
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        res.stop_reason = choice.get("finish_reason")
        res.tokens += (data.get("usage") or {}).get("total_tokens", 0) or 0
        tools = msg.get("executed_tools") or []
        res.searches += sum(1 for t in tools if (t or {}).get("type") == "browser_search")
        try:
            payload = extract_json(msg.get("content") or "")
        except ValueError as e:
            res.failed_topics.append((topic[:60], f"unparseable reply ({res.stop_reason}): {e}"[:200]))
            continue
        evidence = evidence_from(tools)
        for item in payload.get("listings") or []:
            row, why = normalize(item, min_price=min_price)
            if row is not None:
                why = grounded(row, evidence)
            if why:
                label = item.get("listing_url") if isinstance(item, dict) else item
                res.rejected.append((str(label)[:120], why))
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

