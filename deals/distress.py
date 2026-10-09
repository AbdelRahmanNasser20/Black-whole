"""Distress cases — bankruptcies + WARN closures of chair-heavy businesses.

Why: a hotel, resort, caterer, venue, party-rental house or church in Chapter 7
(or shutting down under a WARN notice) sells its banquet chairs and FF&E
*before* the lot ever reaches GovDeals. This module builds the lead list the
operator calls — trustees / counsel / the closing business — and the "Sales"
calendar (dockets that already carry a 363 sale / auction notice).

Two sources, one table (`distress_cases`, migration `scripts/sql/024_distress_cases.sql`):

* **CourtListener v4 RECAP search** (`type=r`): bankruptcy dockets matching
  `QUERY`. Anonymous works (5 req/min, then HTTP 429 + `Retry-After`); a
  `COURTLISTENER_TOKEN` lifts the limit and unlocks `/docket-entries/`
  (anonymous answers 401, recorded 2026-10-09). We honour `Retry-After`,
  space calls, and never retry past `MAX_RETRIES` — a failed fetch raises
  `DistressUnavailable`; it is never reported as "no new cases".
* **laborcurrent.com WARN feed**: free tier = 25 calls/day, 7-day delay.
  `WarnBudget` (file counter, same idea as `dewatermark_cache.RunBudget`)
  stops the run before the 26th call.

Pure helpers (`is_org`, `industry_tag`, `naics_from_text`, `hit_to_row`,
`warn_to_row`, `sale_notice_from_docs`) are unit-tested on recorded responses in
`tests/deals/fixtures/distress/`. Everything that touches the network or the DB
takes an injectable client / goes through `automation.db`.

`raw` is BOUNDED by `compact_raw()` (no document snippets, ≤ 5 docs, ≤ 8 KB)
so the 500 MB rule holds; if it ever needs more, it goes R2-cold like
`deal_lots.raw` (`deals/raw_archive.py`) — never grow the column first.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

from automation import db

# ── constants ────────────────────────────────────────────────────────────────

CL_BASE = "https://www.courtlistener.com/api/rest/v4"
CL_STORAGE = "https://storage.courtlistener.com/"
CL_SITE = "https://www.courtlistener.com"
USER_AGENT = "blackwhole-distress/0.1 (+https://black-whole.com; abdel.nasser@black-whole.com)"

QUERY = ('(chapter:11 OR chapter:7) AND (hotel OR resort OR lodging OR hospitality '
         'OR banquet OR catering OR "event venue" OR "party rental" OR church)')
# Second, anonymous-safe pass: dockets whose RECAP documents already talk about
# a sale. The matching documents come back inline, so no /docket-entries/ call.
SALE_TERMS = '("notice of sale" OR "sale motion" OR "bid procedures" OR "363 sale" OR auction)'
SALE_QUERY = f"{QUERY} AND {SALE_TERMS}"

SALE_RE = re.compile(r"notice of sale|\b363\b|auction|bid procedures|sale motion|motion to sell|"
                     r"sell (?:substantially all|the debtor|property|assets)", re.I)

DEFAULT_RETRY_AFTER = 3.0
MAX_RETRIES = 4
ANON_SPACING_S = 12.5         # anonymous search = 5 req/min (429 body, 2026-10-09) — stay under it
TOKEN_SPACING_S = 0.5
PETITION_MAX_BYTES = 4_000_000
CACHE_TTL_S = 24 * 3600

WARN_BASE = "https://laborcurrent.com/api"
WARN_INDUSTRIES = ("Accommodation and Food Services", "Arts, Entertainment, and Recreation")
WARN_MAX_PER_DAY = int(os.getenv("DISTRESS_WARN_MAX_PER_DAY", "25"))
WARN_PAGE = 100

INDUSTRY_TAGS = ("hotel", "resort", "catering", "event_venue", "party_rental", "church",
                 "restaurant", "other")


def cache_dir() -> Path:
    return Path(os.getenv("DISTRESS_CACHE_DIR") or (Path.home() / ".listing_automation" / "distress_cache"))


def cache_enabled() -> bool:
    return os.getenv("DISTRESS_CACHE", "0").strip().lower() in ("1", "true", "yes", "on")


class DistressUnavailable(RuntimeError):
    """The source could not be read (rate limit exhausted, HTTP error, budget).
    A failure is never an answer — callers report it, never an empty sync."""


# ── org vs person ────────────────────────────────────────────────────────────

_TAG_RE = re.compile(r"<[^>]+>")
_JOINT_ADMIN_RE = re.compile(r"\s*\(?Jointly Administered\b.*$", re.I)
_STRONG_ORG_RE = re.compile(
    r"\b(LLC|L\.L\.C\.|Inc|Incorporated|LP|L\.P\.|LLP|PLLC|Corp|Corporation|Co\.|Company|Companies|"
    r"Ltd|Limited|REIT|Holdings|Group|Associates|Partners|Partnership|Enterprises|Properties|"
    r"Ventures|Trust|Foundation|Fund|Services|Hotel|Hotels|Motel|Resort|Resorts|Lodging|Hospitality|"
    r"Inn|Suites|Lodge|Casino|Banquet|Banquets|Catering|Caterers|Restaurant|Restaurants|Grill|Cafe|"
    r"Kitchen|Entertainment|Events|Rentals|Rental|Venue|Ministries|Ministry|Tabernacle|Cathedral|"
    r"Fellowship|Chapel|Temple|Parish|Diocese|Club|Center|Centre)\b", re.I)
# Tokens that are also common surnames — need a second signal.
_WEAK_ORG_RE = re.compile(r"\b(Church|Hall|Bar|House)\b", re.I)
_ORG_WORDS_RE = re.compile(r"\b(of|the|and|&|for|at|on|in|d/b/a|dba)\b|\d|'s\b", re.I)
_PERSON_DOC_RE = re.compile(r"for Individuals|Individual Debtor|Social Security Number|"
                            r"Statement of Your Current Monthly Income|Means Test|Chapter 13 Plan", re.I)
_ORG_DOC_RE = re.compile(r"Non-Individual|\(Corp\)|Corporate (?:Ownership|Vote|Resolution)", re.I)
_JOINT_RE = re.compile(r"\b(and|&)\b", re.I)


def clean_name(name: str | None) -> str:
    """CourtListener wraps "Jointly Administered" flags in HTML — strip it."""
    text = re.sub(r"\s+", " ", _TAG_RE.sub(" ", name or ""))
    return _JOINT_ADMIN_RE.sub("", text).strip(" ,")


def _doc_texts(hit: dict) -> str:
    parts = []
    for rd in hit.get("recap_documents") or []:
        parts.append(rd.get("short_description") or "")
        parts.append(rd.get("description") or "")
    return " | ".join(parts)


def debtor_type(hit: dict) -> str:
    """'org' or 'person'. Heuristic, documented in deals.md — a lead list, not
    a legal determination. Order: strong org token > individual-only forms >
    org-only forms > weak token with a second signal > person."""
    name = clean_name(hit.get("caseName"))
    docs = _doc_texts(hit)
    if _STRONG_ORG_RE.search(name):
        return "org"
    if _PERSON_DOC_RE.search(docs):
        return "person"
    if _ORG_DOC_RE.search(docs):
        return "org"
    if _WEAK_ORG_RE.search(name):
        # "Rising Hope Church" (ch 11) vs "Karen Renee Church" (ch 7, 3 words):
        # individuals almost never file Chapter 11; a phrase shape (of/the/&,
        # digits, 4+ words) is not a personal name.
        if str(hit.get("chapter") or "") == "11" or _ORG_WORDS_RE.search(name) or len(name.split()) >= 4:
            return "org"
        return "person"
    return "person"


def is_org(hit: dict) -> bool:
    return debtor_type(hit) == "org"


# ── industry tag ─────────────────────────────────────────────────────────────

# Order matters: the first match wins. Parties are included so "ABC Holdings
# LLC" with party "Hilton Garden Inn Tucson" still tags as hotel.
_INDUSTRY_KEYWORDS: tuple[tuple[str, re.Pattern], ...] = (
    ("party_rental", re.compile(r"party rental|event rental|tent rental|\brentals?\b", re.I)),
    ("catering", re.compile(r"cater|banquet", re.I)),
    ("church", re.compile(r"\bchurch|ministr|cathedral|chapel|tabernacle|fellowship|temple|parish|"
                          r"diocese|congregation|worship|\bmosque|synagogue", re.I)),
    ("resort", re.compile(r"\bresort|\bspa\b|golf|casino|country club", re.I)),
    ("hotel", re.compile(r"\bhotel|\bmotel|\binn\b|\bsuites|lodging|hospitality|\blodge\b|hostel", re.I)),
    ("event_venue", re.compile(r"\bevent|\bvenue|ballroom|convention|conference|entertainment|"
                               r"amphitheat|theat|arena|\bhall\b|\bclub\b|wedding", re.I)),
    ("restaurant", re.compile(r"restaurant|\bgrill|\bcafe|\bbbq\b|barbecue|\bkitchen|\bbistro|\bdiner|"
                              r"\bpizz|\btavern|\bpub\b|\bbakery|\bfood\b", re.I)),
)

# NAICS → tag. 4-digit industry groups; 6-digit codes map through their prefix.
NAICS_TAGS = {
    "7211": "hotel", "7212": "resort", "7213": "hotel",
    "7223": "catering", "7224": "restaurant", "7225": "restaurant",
    "7131": "event_venue", "7132": "resort", "7139": "event_venue",
    "7111": "event_venue", "7113": "event_venue", "7115": "event_venue",
    "8131": "church", "5321": "party_rental", "5322": "party_rental",
    "5619": "event_venue",
}


def naics_tag(naics: str | None) -> str | None:
    if not naics:
        return None
    digits = re.sub(r"\D", "", str(naics))
    for n in (6, 5, 4):
        if len(digits) >= n and digits[:n] in NAICS_TAGS:
            return NAICS_TAGS[digits[:n]]
    if len(digits) >= 4 and digits[:4] in NAICS_TAGS:
        return NAICS_TAGS[digits[:4]]
    return None


def industry_tag(name: str | None, parties: Iterable[str] | None = None,
                 naics: str | None = None) -> str:
    """Keyword map on the case name + parties; a specific (4+ digit) NAICS
    wins over keywords. Never a doc description ("Lodging Proposed Order" is a
    court form, not a hotel)."""
    tagged = naics_tag(naics)
    if tagged:
        return tagged
    text = " ".join([clean_name(name), *(parties or [])])
    for tag, rx in _INDUSTRY_KEYWORDS:
        if rx.search(text):
            return tag
    return "other"


# ── NAICS from petition text ─────────────────────────────────────────────────

_NAICS_RE = re.compile(r"NAICS[^0-9]{0,120}?(\d{4,6})\b", re.I | re.S)


def naics_from_text(text: str | None) -> str | None:
    """First 4–6 digit code after the word NAICS (Form 201 § 7 / Form 101).
    None when the text has none — never a default."""
    if not text:
        return None
    m = _NAICS_RE.search(text)
    return m.group(1) if m else None


_ZIP_RE = re.compile(r"ZIP(?:\s*Code)?[^0-9]{0,40}?(\d{5})(?:-\d{4})?\b", re.I | re.S)


def zip_from_text(text: str | None) -> str | None:
    if not text:
        return None
    m = _ZIP_RE.search(text)
    return m.group(1) if m else None


def have_pypdf() -> bool:
    """pypdf is NOT a declared dependency (2026-10-09). Without it the sync
    never downloads a petition — NAICS/ZIP come from the search snippet only."""
    try:
        import pypdf  # noqa: F401
    except ImportError:
        return False
    return True


def petition_text_from_pdf(data: bytes, max_pages: int = 4) -> str:
    """pypdf text of the first pages; '' when pypdf is missing or the PDF is a
    scan (most RECAP petitions are — the snippet path covers those)."""
    try:
        from io import BytesIO
        from pypdf import PdfReader
    except ImportError:
        return ""
    try:
        reader = PdfReader(BytesIO(data))
        return "\n".join((p.extract_text() or "") for p in reader.pages[:max_pages])
    except Exception:  # noqa: BLE001 — a bad PDF yields no NAICS, not a crash
        return ""


# ── hit → row ────────────────────────────────────────────────────────────────

_PETITION_RE = re.compile(r"voluntary petition", re.I)


def state_from_court(court_id: str | None) -> str | None:
    """Federal bankruptcy court ids start with the state code: txnb → TX,
    ganb → GA, mab → MA, prb → PR."""
    cid = (court_id or "").strip().lower()
    return cid[:2].upper() if len(cid) >= 3 else None


def petition_doc(hit: dict) -> dict | None:
    for rd in hit.get("recap_documents") or []:
        text = f"{rd.get('short_description') or ''} {rd.get('description') or ''}"
        if _PETITION_RE.search(text) and rd.get("filepath_local"):
            return rd
    return None


def petition_url(hit: dict) -> str | None:
    rd = petition_doc(hit)
    if rd:
        return CL_STORAGE + rd["filepath_local"].lstrip("/")
    path = hit.get("docket_absolute_url")
    return CL_SITE + path if path else None


def docket_url(hit: dict) -> str | None:
    path = hit.get("docket_absolute_url")
    return CL_SITE + path if path else None


def sale_notice_from_docs(docs: Iterable[dict], base_url: str | None = None) -> tuple[date | None, str | None]:
    """(date, url) of the earliest document whose description reads like a
    sale / auction / bid-procedures filing; (None, None) when none does."""
    best: tuple[date, str | None] | None = None
    for rd in docs or []:
        text = f"{rd.get('short_description') or ''} {rd.get('description') or ''}"
        if not SALE_RE.search(text):
            continue
        raw = rd.get("entry_date_filed") or rd.get("date_filed")
        try:
            d = date.fromisoformat(str(raw)[:10]) if raw else None
        except ValueError:
            d = None
        url = rd.get("absolute_url")
        url = CL_SITE + url if url and url.startswith("/") else (url or base_url)
        if d is None:
            d = date.min
        if best is None or d < best[0]:
            best = (d, url)
    if best is None:
        return None, None
    return (None if best[0] == date.min else best[0]), best[1]


def compact_raw(hit: dict) -> dict:
    """The bounded subset of a search hit we keep (no snippets, ≤ 5 docs)."""
    keep = ("docket_id", "caseName", "case_name_full", "court_id", "court", "docketNumber",
            "dateFiled", "dateTerminated", "chapter", "trustee_str", "assignedTo", "party",
            "attorney", "firm", "pacer_case_id", "docket_absolute_url", "jurisdictionType")
    out = {k: hit.get(k) for k in keep if hit.get(k) not in (None, "", [])}
    docs = []
    for rd in (hit.get("recap_documents") or [])[:5]:
        docs.append({k: rd.get(k) for k in ("entry_number", "short_description", "description",
                                             "entry_date_filed", "filepath_local", "absolute_url")
                     if rd.get(k) not in (None, "")})
    if docs:
        out["recap_documents"] = docs
    blob = json.dumps(out, default=str)
    if len(blob) > 8000:   # a 60-doc mega-case: drop the docs, keep the header
        out.pop("recap_documents", None)
    return out


def _parse_date(raw: Any) -> date | None:
    if not raw:
        return None
    try:
        return date.fromisoformat(str(raw)[:10])
    except ValueError:
        return None


def hit_to_row(hit: dict, *, naics: str | None = None, zip_code: str | None = None) -> dict:
    """One CourtListener search hit → a `distress_cases` row dict (column names)."""
    name = clean_name(hit.get("caseName"))
    parties = [clean_name(p) for p in (hit.get("party") or []) if p]
    attorneys = [{"name": a} for a in (hit.get("attorney") or []) if a]
    firms = [f for f in (hit.get("firm") or []) if f]
    for i, f in enumerate(firms[:len(attorneys)]):
        attorneys[i]["firm"] = f
    sale_at, sale_url = sale_notice_from_docs(hit.get("recap_documents") or [], docket_url(hit))
    lat = lng = None
    if zip_code:
        lat, lng = zip_latlng(zip_code)
    return {
        "source": "courtlistener",
        "source_key": str(hit.get("docket_id")),
        "docket_id": hit.get("docket_id"),
        "case_name": name,
        "court_id": hit.get("court_id"),
        "docket_number": hit.get("docketNumber"),
        "date_filed": _parse_date(hit.get("dateFiled")),
        "chapter": (str(hit.get("chapter")) if hit.get("chapter") else None),
        "trustee": (hit.get("trustee_str") or "").strip() or None,
        "debtor_type": debtor_type(hit),
        "naics": naics,
        "industry_tag": industry_tag(name, parties, naics),
        "city": None,                      # search hits carry no address; ZIP comes from the petition
        "state": state_from_court(hit.get("court_id")),
        "zip_code": zip_code,
        "lat": lat, "lng": lng,
        "parties": parties,
        "attorneys": attorneys,
        "petition_url": petition_url(hit),
        "sale_noticed_at": sale_at,
        "sale_url": sale_url,
        "employees_affected": None,
        "effective_date": None,
        "raw": compact_raw(hit),
    }


def zip_latlng(zip_code: str | None) -> tuple[float | None, float | None]:
    """3-digit-prefix centroid (automation/zip_centroids.py): ±30 mi, no network."""
    try:
        from automation.zip_centroids import PREFIX_CENTROIDS
    except ImportError:
        return None, None
    z = re.sub(r"\D", "", str(zip_code or ""))[:5]
    if len(z) != 5:
        return None, None
    hit = PREFIX_CENTROIDS.get(z[:3])
    return (hit[0], hit[1]) if hit else (None, None)


# ── WARN rows ────────────────────────────────────────────────────────────────

def warn_to_row(rec: dict) -> dict:
    name = clean_name(rec.get("company_name") or rec.get("display_name"))
    naics = str(rec.get("naics_code") or "") or None
    lat, lng = rec.get("latitude"), rec.get("longitude")
    return {
        "source": "warn",
        "source_key": str(rec.get("id")),
        "docket_id": None,
        "case_name": name,
        "court_id": None,
        "docket_number": None,
        "date_filed": _parse_date(rec.get("notice_date")),
        "chapter": None,
        "trustee": None,
        "debtor_type": "org",
        "naics": naics,
        "industry_tag": industry_tag(name, None, naics),
        "city": rec.get("city"),
        "state": (rec.get("state") or "").upper() or None,
        "zip_code": None,
        "lat": float(lat) if lat is not None else None,
        "lng": float(lng) if lng is not None else None,
        "parties": [name] if name else [],
        "attorneys": [],
        "petition_url": rec.get("source_url"),
        "sale_noticed_at": None,
        "sale_url": None,
        "employees_affected": rec.get("employees_affected"),
        "effective_date": _parse_date(rec.get("effective_date")),
        "raw": {k: rec.get(k) for k in ("id", "company_name", "city", "county", "state",
                                        "employees_affected", "notice_date", "effective_date",
                                        "layoff_type", "source_url", "naics_code", "industry",
                                        "source_agency") if rec.get(k) not in (None, "")},
    }


# ── HTTP client (rate-limit aware, dev cache) ───────────────────────────────

class CourtListenerClient:
    """Thin GET wrapper. `http` is any object with `.get(url, params=, headers=,
    timeout=)` returning `.status_code`, `.headers`, `.json()`, `.content`
    (requests / httpx); tests inject a fake. `sleep` is injectable too."""

    def __init__(self, token: str | None = None, *, http=None, sleep: Callable[[float], None] = time.sleep,
                 cache: bool | None = None, cache_root: Path | None = None, spacing_s: float | None = None):
        self.token = token or os.getenv("COURTLISTENER_TOKEN") or None
        if http is None:
            import requests
            http = requests
        self.http = http
        self.sleep = sleep
        self.cache = cache_enabled() if cache is None else cache
        self.cache_root = cache_root or cache_dir()
        self.spacing_s = spacing_s if spacing_s is not None else (TOKEN_SPACING_S if self.token else ANON_SPACING_S)
        self.calls = 0
        self._last_call = 0.0

    @property
    def headers(self) -> dict:
        h = {"Accept": "application/json", "User-Agent": USER_AGENT}
        if self.token:
            h["Authorization"] = f"Token {self.token}"
        return h

    def _cache_path(self, url: str, params: dict | None) -> Path:
        key = hashlib.sha1(json.dumps([url, params or {}], sort_keys=True).encode()).hexdigest()
        return self.cache_root / f"{key}.json"

    def get_json(self, url: str, params: dict | None = None) -> dict:
        if self.cache:
            p = self._cache_path(url, params)
            try:
                if p.exists() and time.time() - p.stat().st_mtime < CACHE_TTL_S:
                    return json.loads(p.read_text())
            except (OSError, ValueError):
                pass
        data = self._get_live(url, params)
        if self.cache:
            try:
                self.cache_root.mkdir(parents=True, exist_ok=True)
                self._cache_path(url, params).write_text(json.dumps(data))
            except OSError:
                pass
        return data

    def _pace(self) -> None:
        wait = self.spacing_s - (time.monotonic() - self._last_call)
        if self._last_call and wait > 0:
            self.sleep(wait)

    def _get_live(self, url: str, params: dict | None) -> dict:
        last = None
        for attempt in range(MAX_RETRIES + 1):
            self._pace()
            try:
                r = self.http.get(url, params=params, headers=self.headers, timeout=60)
            except (OSError, IOError) as e:  # requests' Timeout/ConnectionError subclass IOError
                self._last_call = time.monotonic()
                self.calls += 1
                if attempt < MAX_RETRIES:
                    self.sleep(DEFAULT_RETRY_AFTER * (attempt + 1) * 2)
                    last = None
                    continue
                raise DistressUnavailable(f"courtlistener transport error for {url}: {e!r}") from e
            self._last_call = time.monotonic()
            self.calls += 1
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429 and attempt < MAX_RETRIES:
                try:
                    wait = float(r.headers.get("Retry-After") or DEFAULT_RETRY_AFTER)
                except (TypeError, ValueError):
                    wait = DEFAULT_RETRY_AFTER
                # back off a little more each time; never hammer
                self.sleep(max(wait, DEFAULT_RETRY_AFTER) * (attempt + 1))
                last = r
                continue
            if r.status_code in (401, 403):
                raise DistressUnavailable(f"courtlistener {r.status_code} for {url} — needs COURTLISTENER_TOKEN")
            raise DistressUnavailable(f"courtlistener http_{r.status_code} for {url}")
        raise DistressUnavailable(f"courtlistener rate-limited after {MAX_RETRIES} retries ({last.status_code if last else '?'})")

    def search(self, query: str, *, filed_after: date | str | None = None, max_pages: int = 5) -> Iterator[dict]:
        params: dict[str, Any] = {"type": "r", "q": query, "order_by": "dateFiled desc"}
        if filed_after:
            params["filed_after"] = str(filed_after)
        url: str | None = f"{CL_BASE}/search/"
        pages = 0
        while url and pages < max_pages:
            data = self.get_json(url, params)
            for hit in data.get("results") or []:
                yield hit
            url = data.get("next")
            params = None   # the cursor URL carries everything
            pages += 1

    def docket_entries(self, docket_id: int, *, max_pages: int = 2) -> list[dict]:
        """Needs a token (anonymous = 401). Returns the entries, newest first."""
        if not self.token:
            raise DistressUnavailable("docket-entries needs COURTLISTENER_TOKEN (anonymous answers 401)")
        url: str | None = f"{CL_BASE}/docket-entries/"
        params: dict[str, Any] | None = {"docket": docket_id, "order_by": "-date_filed"}
        out: list[dict] = []
        pages = 0
        while url and pages < max_pages:
            data = self.get_json(url, params)
            out.extend(data.get("results") or [])
            url = data.get("next"); params = None; pages += 1
        return out

    def fetch_petition(self, url: str) -> bytes | None:
        """One size-capped GET on the static storage host (no API quota)."""
        try:
            r = self.http.get(url, headers={"User-Agent": USER_AGENT}, timeout=60, stream=True)
        except Exception as e:  # noqa: BLE001
            print(f"[distress] petition fetch failed: {e}", file=sys.stderr)
            return None
        if getattr(r, "status_code", 0) != 200:
            return None
        try:
            length = int(r.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        if length > PETITION_MAX_BYTES:
            return None
        data = r.content
        return data if len(data) <= PETITION_MAX_BYTES else None


# ── WARN budget + client ─────────────────────────────────────────────────────

class WarnBudget:
    """Daily call counter for laborcurrent's free tier (25/day). One JSON file
    `{"day": "YYYY-MM-DD", "calls": n}` in the distress cache dir — resets on a
    new UTC day. `check()` is False once the day's cap is reached."""

    def __init__(self, path: Path | None = None, max_per_day: int = WARN_MAX_PER_DAY):
        self.path = path or (cache_dir() / "warn_budget.json")
        self.max_per_day = max_per_day

    def _read(self) -> dict:
        try:
            d = json.loads(self.path.read_text())
        except (OSError, ValueError):
            d = {}
        today = datetime.now(timezone.utc).date().isoformat()
        if d.get("day") != today:
            d = {"day": today, "calls": 0}
        return d

    def calls_today(self) -> int:
        return int(self._read().get("calls") or 0)

    def check(self) -> bool:
        return self.calls_today() < self.max_per_day

    def record_call(self) -> None:
        d = self._read()
        d["calls"] = int(d.get("calls") or 0) + 1
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(d))
        except OSError:
            pass


def fetch_warn(since: date | str, *, http=None, budget: WarnBudget | None = None,
               industries: Iterable[str] = WARN_INDUSTRIES, state: str | None = None,
               max_calls: int = 6) -> list[dict]:
    """Records from the two chair-heavy NAICS groups, newest first. Stops at
    `max_calls` for this run or when the daily budget is spent (raises so the
    run can say "WARN skipped: budget", never "no closures")."""
    if http is None:
        import requests
        http = requests
    budget = budget or WarnBudget()
    out: list[dict] = []
    calls = 0
    for industry in industries:
        offset = 0
        while True:
            if calls >= max_calls:
                return out
            if not budget.check():
                raise DistressUnavailable(f"laborcurrent daily budget spent ({budget.max_per_day}/day)")
            params = {"industry": industry, "date_from": str(since), "limit": WARN_PAGE, "offset": offset}
            if state:
                params["state"] = state
            r = http.get(f"{WARN_BASE}/records", params=params,
                         headers={"User-Agent": USER_AGENT, "Accept": "application/json"}, timeout=30)
            budget.record_call(); calls += 1
            if r.status_code == 429:
                raise DistressUnavailable("laborcurrent 429 — free tier exhausted for today")
            if r.status_code != 200:
                raise DistressUnavailable(f"laborcurrent http_{r.status_code}")
            data = r.json()
            recs = data.get("records") or []
            out.extend(recs)
            total = int(data.get("total") or 0)
            offset += len(recs)
            if not recs or offset >= total:
                break
    return out


# ── store ────────────────────────────────────────────────────────────────────

COLUMNS = ["source", "source_key", "docket_id", "case_name", "court_id", "docket_number",
           "date_filed", "chapter", "trustee", "debtor_type", "naics", "industry_tag", "city",
           "state", "zip_code", "lat", "lng", "parties", "attorneys", "petition_url",
           "sale_noticed_at", "sale_url", "employees_affected", "effective_date", "raw"]
_JSON_COLS = {"parties", "attorneys", "raw"}
# Columns a re-sync refreshes. first_seen_at is never touched; sale_noticed_at /
# naics / zip / lat / lng only move NULL → value (COALESCE) so a later, thinner
# search hit can't erase what a petition download or docket scan found.
_SET_ONLY_IF_NULL = {"naics", "zip_code", "lat", "lng", "sale_noticed_at", "sale_url", "city"}
_IMMUTABLE = {"source", "source_key"}


def upsert_sql() -> str:
    cols = ",".join(COLUMNS)
    ph = ",".join("%s" for _ in COLUMNS)
    sets = []
    for c in COLUMNS:
        if c in _IMMUTABLE:
            continue
        if c in _SET_ONLY_IF_NULL:
            sets.append(f"{c}=COALESCE(distress_cases.{c}, EXCLUDED.{c})")
        else:
            sets.append(f"{c}=EXCLUDED.{c}")
    sets.append("last_seen_at=now()")
    return (f"INSERT INTO distress_cases ({cols}) VALUES ({ph}) "
            f"ON CONFLICT (source, source_key) DO UPDATE SET {', '.join(sets)} "
            f"RETURNING id, (xmax = 0) AS inserted, sale_noticed_at")


def row_values(row: dict) -> tuple:
    return tuple(json.dumps(row.get(c), default=str) if c in _JSON_COLS else row.get(c) for c in COLUMNS)


@dataclass
class SyncReport:
    fetched: int = 0
    kept: int = 0
    inserted: int = 0
    updated: int = 0
    warn_fetched: int = 0
    warn_kept: int = 0
    sale_gained: list[dict] = field(default_factory=list)
    new_ch7_trustee: list[dict] = field(default_factory=list)
    skipped_persons: int = 0
    notes: list[str] = field(default_factory=list)
    api_calls: int = 0
    dry_run: bool = False

    def __str__(self) -> str:
        head = ("[dry-run] " if self.dry_run else "") + (
            f"courtlistener {self.fetched} hits → {self.kept} org rows ({self.skipped_persons} persons dropped); "
            f"warn {self.warn_fetched} → {self.warn_kept}; inserted {self.inserted}, updated {self.updated}; "
            f"{len(self.new_ch7_trustee)} new ch7+trustee, {len(self.sale_gained)} gained a sale notice; "
            f"{self.api_calls} API call(s)")
        return "\n".join([head, *(f"  note: {n}" for n in self.notes)])


def upsert_rows(rows: list[dict]) -> tuple[int, int, list[dict], list[dict]]:
    """One transaction. Returns (inserted, updated, new_ch7_trustee_rows, sale_gained_rows)."""
    if not rows:
        return 0, 0, [], []
    keys = [(r["source"], r["source_key"]) for r in rows]
    prev: dict[tuple, Any] = {}
    inserted = updated = 0
    new_ch7: list[dict] = []
    gained: list[dict] = []
    sql = upsert_sql()
    with db.connect() as conn:
        for (src, key) in keys:
            got = conn.execute("SELECT sale_noticed_at FROM distress_cases WHERE source=%s AND source_key=%s",
                               (src, key)).fetchone()
            if got:
                prev[(src, key)] = got["sale_noticed_at"]
        for r in rows:
            res = conn.execute(sql, row_values(r)).fetchone()
            k = (r["source"], r["source_key"])
            if res and res["inserted"]:
                inserted += 1
                if r.get("chapter") == "7" and r.get("trustee") and r.get("debtor_type") == "org":
                    new_ch7.append({**r, "id": res["id"]})
            else:
                updated += 1
            if res and res["sale_noticed_at"] and k in prev and prev[k] is None:
                gained.append({**r, "id": res["id"], "sale_noticed_at": res["sale_noticed_at"]})
            elif res and res["sale_noticed_at"] and k not in prev and r.get("sale_noticed_at"):
                gained.append({**r, "id": res["id"]})
    return inserted, updated, new_ch7, gained


def record_sync_state(source: str, since: date | str | None, note: str | None = None) -> None:
    db.execute("""INSERT INTO distress_sync_state (source, last_run_at, last_since, note)
        VALUES (%s, now(), %s, %s)
        ON CONFLICT (source) DO UPDATE SET last_run_at=now(), last_since=EXCLUDED.last_since, note=EXCLUDED.note""",
               (source, since, note))


def last_run(source: str) -> datetime | None:
    try:
        r = db.fetch_one("SELECT last_run_at FROM distress_sync_state WHERE source=%s", (source,))
    except Exception:  # noqa: BLE001 — the table is a PENDING migration
        return None
    return r["last_run_at"] if r else None


# ── alerts ───────────────────────────────────────────────────────────────────

def alerts_enabled() -> bool:
    return os.getenv("DISTRESS_ALERTS", "0").strip().lower() in ("1", "true", "yes", "on")


def _row_url(r: dict) -> str:
    return r.get("sale_url") or r.get("petition_url") or ""


def format_alert(new_ch7: list[dict], gained: list[dict]) -> str:
    lines = ["🏨 distress cases"]
    if new_ch7:
        lines.append(f"{len(new_ch7)} new Chapter 7 org(s) with a trustee:")
        for r in new_ch7[:10]:
            lines.append(f"• {r['case_name'][:60]} — {r.get('state') or '?'} · {r.get('industry_tag')} · "
                         f"trustee {r.get('trustee')} — {_row_url(r)}")
        if len(new_ch7) > 10:
            lines.append(f"…+{len(new_ch7) - 10} more")
    if gained:
        lines.append(f"{len(gained)} docket(s) now carry a sale / auction notice:")
        for r in gained[:10]:
            lines.append(f"• {r['case_name'][:60]} — ch {r.get('chapter') or '?'} · {r.get('state') or '?'} "
                         f"· {r.get('sale_noticed_at') or ''} — {_row_url(r)}")
        if len(gained) > 10:
            lines.append(f"…+{len(gained) - 10} more")
    return "\n".join(lines)


def send_alert(new_ch7: list[dict], gained: list[dict]) -> tuple[bool, str | None]:
    if not (new_ch7 or gained):
        return False, "nothing_to_alert"
    if not alerts_enabled():
        return False, "DISTRESS_ALERTS off"
    from automation.telegram_alerts import send_message_sync
    return send_message_sync(format_alert(new_ch7, gained), topic="deals")


# ── the sync ─────────────────────────────────────────────────────────────────

def default_since(days: int = 30) -> date:
    return date.today() - timedelta(days=days)


def enrich_from_petition(client: CourtListenerClient, hit: dict, row: dict) -> None:
    """Best effort: NAICS + ZIP from the petition's snippet (free) and, when
    the PDF is linked, from its first pages. Sets nothing when nothing parses."""
    rd = petition_doc(hit)
    text = (rd or {}).get("snippet") or ""
    naics = naics_from_text(text)
    zip_code = zip_from_text(text)
    if (naics is None or zip_code is None) and rd and rd.get("filepath_local") and have_pypdf():
        data = client.fetch_petition(CL_STORAGE + rd["filepath_local"].lstrip("/"))
        if data:
            pdf_text = petition_text_from_pdf(data)
            naics = naics or naics_from_text(pdf_text)
            zip_code = zip_code or zip_from_text(pdf_text)
    if naics:
        row["naics"] = naics
        row["industry_tag"] = industry_tag(row["case_name"], row["parties"], naics)
    if zip_code:
        row["zip_code"] = zip_code
        row["lat"], row["lng"] = zip_latlng(zip_code)


def run_sync(*, since: date | str | None = None, dry_run: bool = False, warn: bool = True,
             max_docket_entries: int = 0, max_pages: int = 5, petitions: bool = True,
             max_petitions: int = 10, client: CourtListenerClient | None = None,
             warn_http=None, warn_budget: WarnBudget | None = None,
             out=print) -> SyncReport:
    """The `distress-sync` command. Dry-run prints every row it would upsert and
    never touches the DB or Telegram."""
    rep = SyncReport(dry_run=dry_run)
    since = since or default_since()
    client = client or CourtListenerClient()
    rows: list[dict] = []
    seen: set[str] = set()
    petitions_done = 0

    hits = list(client.search(QUERY, filed_after=since, max_pages=max_pages))
    rep.fetched = len(hits)
    for hit in hits:
        if not is_org(hit):
            rep.skipped_persons += 1
            continue
        key = str(hit.get("docket_id"))
        if key in seen:
            continue
        seen.add(key)
        row = hit_to_row(hit)
        if petitions and petitions_done < max_petitions and (row["naics"] is None or row["zip_code"] is None):
            petitions_done += 1
            enrich_from_petition(client, hit, row)
        rows.append(row)
    rep.kept = len(rows)

    # sale pass: dockets already talking about a sale — matching docs come inline
    try:
        for hit in client.search(SALE_QUERY, filed_after=since, max_pages=max(1, max_pages // 2)):
            key = str(hit.get("docket_id"))
            sale_at, sale_url = sale_notice_from_docs(hit.get("recap_documents") or [], docket_url(hit))
            if not (sale_at or sale_url):
                continue
            target = next((r for r in rows if r["source_key"] == key), None)
            if target is None:
                if not is_org(hit):
                    continue
                target = hit_to_row(hit)
                rows.append(target); seen.add(key); rep.kept += 1
            if target["sale_noticed_at"] is None:
                target["sale_noticed_at"], target["sale_url"] = sale_at, sale_url
    except DistressUnavailable as e:
        rep.notes.append(f"sale pass skipped: {e}")

    # token-only: scan docket entries of the likeliest sellers (ch 7 / trustee set)
    if max_docket_entries > 0:
        if not client.token:
            rep.notes.append("docket-entries scan skipped: needs COURTLISTENER_TOKEN (anonymous = 401)")
        else:
            n = 0
            for r in rows:
                if n >= max_docket_entries:
                    break
                if r["sale_noticed_at"] or not (r["chapter"] == "7" or r["trustee"]):
                    continue
                n += 1
                try:
                    entries = client.docket_entries(int(r["source_key"]))
                except DistressUnavailable as e:
                    rep.notes.append(f"docket-entries {r['source_key']}: {e}")
                    break
                sale_at, sale_url = sale_notice_from_docs(entries, r["petition_url"])
                if sale_at or sale_url:
                    r["sale_noticed_at"], r["sale_url"] = sale_at, sale_url

    if warn:
        try:
            recs = fetch_warn(since, http=warn_http, budget=warn_budget)
            rep.warn_fetched = len(recs)
            for rec in recs:
                wr = warn_to_row(rec)
                if wr["source_key"] in seen:
                    continue
                seen.add(wr["source_key"]); rows.append(wr); rep.warn_kept += 1
        except DistressUnavailable as e:
            rep.notes.append(f"WARN skipped: {e}")
    rep.api_calls = client.calls

    if dry_run:
        for r in rows:
            out(f"{r['source']:<13} {str(r['date_filed'] or ''):<10} ch{r['chapter'] or '--':<3} "
                f"{r['state'] or '??'} {r['industry_tag']:<12} {r['case_name'][:55]:<55} "
                f"trustee={r['trustee'] or '-'} sale={r['sale_noticed_at'] or '-'} naics={r['naics'] or '-'}")
        rep.new_ch7_trustee = [r for r in rows if r["chapter"] == "7" and r["trustee"] and r["debtor_type"] == "org"]
        rep.sale_gained = [r for r in rows if r["sale_noticed_at"]]
        return rep

    rep.inserted, rep.updated, rep.new_ch7_trustee, rep.sale_gained = upsert_rows(rows)
    try:
        record_sync_state("courtlistener", since, f"{rep.kept} rows")
        if warn:
            record_sync_state("warn", since, f"{rep.warn_kept} rows")
    except Exception as e:  # noqa: BLE001 — state is bookkeeping, never the run's result
        rep.notes.append(f"sync state not recorded: {e}")
    ok, err = send_alert(rep.new_ch7_trustee, rep.sale_gained)
    if not ok and err not in ("nothing_to_alert",):
        rep.notes.append(f"alert: {err}")
    return rep
