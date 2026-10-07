"""First-touch lead attribution: which channel brought the buyer who just filed a lead.

The storefront's `site.js` stores, on a visitor's FIRST page view, where they
came from (`utm_*` tags, the referrer host, the landing path) and sends that
object as ``attribution`` with every lead POST (`/contact`, `/subscribe`,
`/freight-estimate`, `/reserve/{lot}/checkout`, `/event`). This module is the
server half: clip it, store it next to the lead, and classify it into a channel
for the weekly funnel report (`scripts/lead_funnel_report.py`).

Rules:
- **Raw in, bucketed out.** The columns hold what the browser saw
  (`attr_source='google'`, `attr_referrer='m.facebook.com'`); `channel()` turns
  that into `google_feed` / `facebook` / `direct` at report time, so a
  classification fix never needs a backfill.
- **Bounded.** Every value is clipped to MAX_LEN (200) before it reaches SQL —
  the 500 MB rule: no unbounded blobs.
- **Never raises on a lead path.** A malformed payload degrades to "no
  attribution"; a missing column (migration 022 not applied) degrades to the
  pre-022 INSERT. `columns_ready()` caches only a positive answer, like
  `freight_log.schema_ready()`.
"""
from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import urlsplit

from . import db

log = logging.getLogger(__name__)

MAX_LEN = 200
# Payload key → column suffix. Order is the column order in migration 022.
FIELDS = ("source", "medium", "campaign", "referrer", "landing")
COLUMNS = tuple(f"attr_{f}" for f in FIELDS)
# The LAST column 022 adds per table — see freight_log.schema_ready for why.
_PROBE_COLUMN = "attr_landing"
TABLES = ("inquiries", "subscribers", "freight_quotes", "deposits")

MIGRATION_HINT = (
    "migration 022 is not applied — run "
    ".venv/bin/python scripts/apply_sql.py scripts/sql/022_lead_attribution.sql"
)

# Hosts that are us: a referrer from one of these is an internal hop, not a channel.
OWN_HOSTS = (
    "black-whole.com", "bwliquidation.com", "blackwholeliquidation.com",
    "blackwholesupply.com", "blackwholeservices.com", "localhost", "127.0.0.1",
)

# Referrer host → channel. First match wins.
_REFERRER_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("google_organic", re.compile(r"(^|\.)google\.[a-z.]+$")),
    ("bing", re.compile(r"(^|\.)bing\.com$")),
    ("duckduckgo", re.compile(r"(^|\.)duckduckgo\.com$")),
    ("facebook", re.compile(r"(^|\.)(facebook\.com|fb\.com|fb\.me|messenger\.com|instagram\.com)$")),
    ("craigslist", re.compile(r"(^|\.)craigslist\.org$")),
    ("ebay", re.compile(r"(^|\.)ebay\.(com|ca|co\.uk)$")),
    ("ai_assistant", re.compile(
        r"(^|\.)(chatgpt\.com|openai\.com|perplexity\.ai|claude\.ai|gemini\.google\.com)$")),
)

_SAFE_LABEL_RE = re.compile(r"[^a-z0-9_.-]+")

# utm_source → channel (when the link was tagged we trust the tag).
_UTM_RULES = {
    "facebook": "facebook", "fb": "facebook", "instagram": "facebook", "meta": "facebook",
    "craigslist": "craigslist", "ebay": "ebay", "apollo": "email", "email": "email",
    "smartlead": "email", "newsletter": "email", "offerup": "offerup",
    "nextdoor": "nextdoor", "phone": "phone", "print": "print", "sms": "sms",
    "bing": "bing", "chatgpt.com": "ai_assistant",
}


def clip(value: Any, n: int = MAX_LEN) -> str | None:
    """Trim to a bounded, non-empty string or None."""
    if value is None:
        return None
    s = str(value).strip()
    return s[:n] if s else None


def referrer_host(value: Any) -> str | None:
    """`https://m.facebook.com/x?y` → `m.facebook.com`; a bare host passes through."""
    s = clip(value)
    if not s:
        return None
    if "://" in s:
        try:
            s = urlsplit(s).netloc
        except ValueError:
            return None
    s = s.lower().split("@")[-1].split(":")[0].strip("/")
    return clip(s, 120) or None


def from_payload(payload: dict | None) -> dict[str, str | None]:
    """Pull the browser's attribution object out of a lead POST. Pure, never raises.

    Accepts ``{"attribution": {...}}`` (what site.js sends) or flat ``utm_*`` /
    ``referrer`` / ``landing_path`` keys. Always returns every FIELDS key.
    """
    out: dict[str, str | None] = {f: None for f in FIELDS}
    if not isinstance(payload, dict):
        return out
    raw = payload.get("attribution")
    if not isinstance(raw, dict):
        raw = {
            "source": payload.get("utm_source"), "medium": payload.get("utm_medium"),
            "campaign": payload.get("utm_campaign"), "referrer": payload.get("referrer"),
            "landing": payload.get("landing_path"),
        }
    src = clip(raw.get("source") or raw.get("utm_source"))
    out["source"] = src.lower() if src else None
    med = clip(raw.get("medium") or raw.get("utm_medium"))
    out["medium"] = med.lower() if med else None
    out["campaign"] = clip(raw.get("campaign") or raw.get("utm_campaign"))
    out["referrer"] = referrer_host(raw.get("referrer") or raw.get("ref"))
    landing = clip(raw.get("landing") or raw.get("landing_path") or raw.get("lp"))
    out["landing"] = landing if (landing and landing.startswith("/")) else None
    return out


def is_empty(attr: dict | None) -> bool:
    return not attr or not any(attr.get(f) for f in FIELDS)


def channel(source: str | None, medium: str | None = None, referrer: str | None = None) -> str:
    """Bucket raw attribution into the channel the report groups on.

    utm tag first (a tagged link says what it is), then the referrer host,
    then `direct`. Google is split: the Merchant Center feed tags
    `utm_source=google&utm_medium=feed`, an organic click arrives untagged
    with a google.* referrer.
    """
    s = (source or "").strip().lower()
    m = (medium or "").strip().lower()
    if s:
        if s in ("google", "google.com", "www.google.com"):
            if m in ("feed", "shopping", "merchant", "merchant_center", "product"):
                return "google_feed"
            if m in ("cpc", "ppc", "paid", "ads"):
                return "google_ads"
            return "google_organic"
        if s in _UTM_RULES:
            return _UTM_RULES[s]
        if m == "email":
            return "email"
        # Unknown tag: keep it as its own bucket, but only safe characters —
        # utm_source is attacker-controlled (the log already holds a
        # `'><script>` probe) and the label ends up in reports.
        safe = _SAFE_LABEL_RE.sub("", s)[:40]
        return f"utm:{safe}" if safe else "utm:other"
    host = referrer_host(referrer) or ""
    if not host:
        return "direct"
    if any(host == own or host.endswith("." + own) for own in OWN_HOSTS):
        return "direct"
    for name, rx in _REFERRER_RULES:
        if rx.search(host):
            return name
    return "referral"


# ───────────────────────────── schema detection ─────────────────────────────

_ready: dict[str, bool] = {}


def columns_ready(table: str) -> bool:
    """True once migration 022 added the attr_* columns to `table`.

    Only a positive answer is cached (same reasoning as
    `freight_log.schema_ready`): a DB blip must not pin a process to the old
    INSERT forever, and the operator may apply the migration without a deploy.
    """
    if _ready.get(table):
        return True
    try:
        row = db.fetch_one(
            "SELECT 1 FROM information_schema.columns WHERE table_schema = 'public' "
            "AND table_name = %s AND column_name = %s LIMIT 1",
            (table, _PROBE_COLUMN),
        )
    except Exception:  # noqa: BLE001 — no DB ⇒ behave like the old schema
        return False
    if row is None:
        return False
    _ready[table] = True
    return True


def reset_schema_cache() -> None:
    """Tests only."""
    _ready.clear()


def insert_columns(table: str, attr: dict | None) -> tuple[list[str], list[Any]]:
    """Extra `(columns, values)` for an INSERT into `table`, or `([], [])`.

    Empty when there is nothing to store or the columns are not there yet, so
    a caller can always do ``cols += c; vals += v``. The column names come
    from COLUMNS, never from the payload.
    """
    if is_empty(attr):
        return [], []
    if not columns_ready(table):
        log.info("lead attribution dropped for %s (%s)", table, MIGRATION_HINT)
        return [], []
    return list(COLUMNS), [clip(attr.get(f)) for f in FIELDS]
