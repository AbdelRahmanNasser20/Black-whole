"""SEO Pulse — the BrightEdge loop (audit → keywords → recommendations → trend)
sized for one operator and one site.

Design: docs/superpowers/specs/2026-11-05-seo-pulse-design.md
CLI:    scripts/seo_pulse.py run|audit|keywords

Four parts, each importable and testable without a network (every fetch goes
through an injected `http` object with `.get(url)`):

  crawl()        ContentIQ-lite: every sitemap URL → PageAudit (status, title,
                 meta, H1, canonical, noindex, JSON-LD, word count, alt text,
                 internal links) + site-wide issues (orphans, duplicates).
  load_gsc()     Search Console rows from a UI export (zip/folder of CSVs) or
                 the API (service account). Query × page rows when the API is
                 used; query-only and page-only tables from the export.
  keyword_ideas() Data-Cube-lite: seeds × cities → Google autocomplete.
  recommend()    the ranked list the operator acts on: striking-distance
                 queries, content gaps, CTR gaps, page fixes, orphans.

Nothing here writes to the site, the DB, or any channel. Snapshots are JSON
files under `SEO_PULSE_DIR`; the report is Markdown, summary first.
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
import time
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urljoin, urlsplit, urlunsplit, quote

__all__ = [
    "PageAudit", "SiteAudit", "GscData", "Recommendation", "crawl", "audit_html",
    "load_gsc", "load_gsc_export", "fetch_gsc_api", "keyword_ideas", "recommend",
    "draft_copy", "save_snapshot", "load_previous_snapshot", "diff_snapshots",
    "render_report", "telegram_lines", "run",
]

USER_AGENT = "BLACKWHOLE-SEO-Pulse/1.0 (+https://black-whole.com/about)"
SITEMAP_TIMING: dict[str, int] = {"ms": 0}   # last fetch_sitemap() wall time; >5 s is a site issue
CRAWL_GAP_SEC = 0.2
RETRY_GAP_SEC = 2.0
TITLE_MIN, TITLE_MAX = 30, 65
DESC_MIN, DESC_MAX = 50, 160
THIN_WORDS = 300

# Recommendation thresholds (design doc "Decided by the builder").
STRIKING_POS = (4.0, 20.0)
STRIKING_MIN_IMPR = 3
GAP_MIN_IMPR = 3
CTR_GAP_MIN_IMPR = 20
CTR_GAP_MAX_POS = 20.0
CTR_GAP_MAX_CTR = 0.02

DEFAULT_SEEDS = (
    "bulk banquet chairs", "used banquet chairs", "wholesale banquet chairs",
    "bulk seating", "church chairs for sale", "stackable chairs bulk",
    "banquet chairs for sale",
)
STOPWORDS = {
    "a", "an", "the", "for", "in", "of", "to", "and", "or", "on", "at", "with",
    "near", "me", "buy", "sale", "sell", "my", "is", "are", "it", "by", "from",
}
# Query tokens that say *where* or *how* rather than *what* — they never
# decide whether a page "covers" a query.
SOFT_TOKENS = {"used", "cheap", "new", "best", "online", "wholesale", "bulk"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


# ── crawl / audit ────────────────────────────────────────────────────────────

@dataclass
class PageAudit:
    url: str
    status: int = 0
    final_url: str = ""
    elapsed_ms: int = 0
    title: str = ""
    description: str = ""
    h1s: list[str] = field(default_factory=list)
    canonical: str = ""
    noindex: bool = False
    jsonld_types: list[str] = field(default_factory=list)
    jsonld_errors: int = 0
    word_count: int = 0
    images: int = 0
    images_no_alt: int = 0
    internal_links: list[str] = field(default_factory=list)
    inbound: int = 0
    issues: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def path(self) -> str:
        return urlsplit(self.url).path or "/"


@dataclass
class SiteAudit:
    base: str
    pages: list[PageAudit]
    site_issues: list[str] = field(default_factory=list)

    def by_path(self) -> dict[str, PageAudit]:
        return {p.path: p for p in self.pages}


def _norm_url(url: str, base: str) -> str:
    """Absolute URL, no fragment, no trailing slash except the root."""
    u = urlsplit(urljoin(base, url.strip()))
    path = u.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path[:-1]
    return urlunsplit((u.scheme, u.netloc.lower(), path, u.query, ""))


def _same_site(url: str, base: str) -> bool:
    return urlsplit(url).netloc.lower() == urlsplit(base).netloc.lower()


def fetch_sitemap(base: str, http) -> list[str]:
    """Every <loc> in /sitemap.xml (and one level of sitemap index)."""
    t0 = time.monotonic()
    resp = http.get(urljoin(base, "/sitemap.xml"))
    if getattr(resp, "status_code", 0) != 200:
        raise RuntimeError(f"sitemap.xml returned {getattr(resp, 'status_code', '?')}")
    SITEMAP_TIMING["ms"] = int((time.monotonic() - t0) * 1000)
    locs = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", resp.text)
    out: list[str] = []
    for loc in locs:
        if loc.endswith(".xml"):                       # sitemap index entry
            sub = http.get(loc)
            if getattr(sub, "status_code", 0) == 200:
                out.extend(re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", sub.text))
        else:
            out.append(loc)
    seen: set[str] = set()
    uniq = []
    for u in out:
        n = _norm_url(u, base)
        if n not in seen:
            seen.add(n)
            uniq.append(n)
    return uniq


def audit_html(url: str, html: str, *, base: str, status: int = 200,
               final_url: str | None = None, elapsed_ms: int = 0) -> PageAudit:
    """Pure function: one page's HTML → its PageAudit (issues included)."""
    from bs4 import BeautifulSoup

    a = PageAudit(url=url, status=status, final_url=final_url or url, elapsed_ms=elapsed_ms)
    soup = BeautifulSoup(html or "", "html.parser")

    t = soup.find("title")
    a.title = re.sub(r"\s+", " ", t.get_text(" ", strip=True)) if t else ""
    d = soup.find("meta", attrs={"name": re.compile(r"^description$", re.I)})
    a.description = (d.get("content") or "").strip() if d else ""
    a.h1s = [t for t in (re.sub(r"\s+", " ", h.get_text(" ", strip=True)) for h in soup.find_all("h1")) if t]
    c = soup.find("link", attrs={"rel": lambda v: v and "canonical" in ([v] if isinstance(v, str) else v)})
    a.canonical = _norm_url(c.get("href"), base) if c and c.get("href") else ""
    r = soup.find("meta", attrs={"name": re.compile(r"^robots$", re.I)})
    a.noindex = bool(r and "noindex" in (r.get("content") or "").lower())

    for s in soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.I)}):
        try:
            data = json.loads(s.string or s.get_text() or "")
        except (ValueError, TypeError):
            a.jsonld_errors += 1
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if isinstance(item, dict):
                typ = item.get("@type")
                if isinstance(typ, list):
                    a.jsonld_types.extend(str(x) for x in typ)
                elif typ:
                    a.jsonld_types.append(str(typ))

    for tag in soup.find_all(["script", "style", "noscript", "nav", "footer", "header"]):
        tag.decompose()
    body = soup.find("main") or soup.find("body") or soup
    text = body.get_text(" ", strip=True)
    a.word_count = len(re.findall(r"[A-Za-z0-9$][A-Za-z0-9$'’.-]*", text))

    imgs = soup.find_all("img")
    a.images = len(imgs)
    a.images_no_alt = sum(1 for i in imgs if not (i.get("alt") or "").strip())

    links: set[str] = set()
    for link in soup.find_all("a", href=True):
        href = link["href"].strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        n = _norm_url(href, base)
        if _same_site(n, base) and n != _norm_url(url, base):
            links.add(n)
    a.internal_links = sorted(links)

    # ── issues (one page) ──
    iss = a.issues
    if status != 200:
        iss.append(f"HTTP {status}")
    if final_url and _norm_url(final_url, base) != _norm_url(url, base):
        iss.append("sitemap URL redirects (list the final URL instead)")
    if a.noindex:
        iss.append("noindex but in sitemap")
    if not a.title:
        iss.append("missing <title>")
    elif len(a.title) < TITLE_MIN:
        iss.append(f"title short ({len(a.title)} chars)")
    elif len(a.title) > TITLE_MAX:
        iss.append(f"title long ({len(a.title)} chars, cap {TITLE_MAX})")
    if not a.description:
        iss.append("missing meta description")
    elif len(a.description) < DESC_MIN:
        iss.append(f"description short ({len(a.description)} chars)")
    elif len(a.description) > DESC_MAX:
        iss.append(f"description long ({len(a.description)} chars, cap {DESC_MAX})")
    if len(a.h1s) == 0:
        iss.append("no H1")
    elif len(a.h1s) > 1:
        iss.append(f"{len(a.h1s)} H1s")
    if not a.canonical:
        iss.append("no canonical")
    elif a.canonical != _norm_url(final_url or url, base):
        iss.append(f"canonical points elsewhere ({a.canonical})")
    if a.jsonld_errors:
        iss.append(f"{a.jsonld_errors} broken JSON-LD block(s)")
    if status == 200 and a.word_count < THIN_WORDS:
        iss.append(f"thin ({a.word_count} words)")
    if a.images_no_alt:
        iss.append(f"{a.images_no_alt}/{a.images} images without alt")
    if elapsed_ms > 2500:
        iss.append(f"slow ({elapsed_ms} ms)")
    return a


def crawl(base: str, http, *, urls: Iterable[str] | None = None, limit: int | None = None,
          sleep: Callable[[float], None] = time.sleep, log: Callable[[str], None] | None = None) -> SiteAudit:
    """Audit every sitemap URL (or `urls`) politely, then the site-wide issues."""
    base = _norm_url(base, base)
    targets = list(urls) if urls is not None else fetch_sitemap(base, http)
    if limit:
        targets = targets[:limit]
    pages: list[PageAudit] = []
    for i, url in enumerate(targets):
        t0 = time.monotonic()
        try:
            resp = http.get(url)
            flaky = ""
            if resp.status_code >= 500:
                # Render answers 502 when a slow page overruns the proxy; one retry
                # after a pause separates "intermittent" (an SEO finding in itself)
                # from "dead".
                sleep(RETRY_GAP_SEC)
                first = resp.status_code
                t0 = time.monotonic()
                resp = http.get(url)
                if resp.status_code == 200:
                    flaky = f"intermittent HTTP {first} (second try 200)"
            elapsed = int((time.monotonic() - t0) * 1000)
            final = str(getattr(resp, "url", url) or url)
            a = audit_html(url, resp.text, base=base, status=resp.status_code,
                           final_url=final, elapsed_ms=elapsed)
            if flaky:
                a.issues.insert(0, flaky)
            pages.append(a)
        except Exception as exc:  # noqa: BLE001 — one dead page must not kill the run
            a = PageAudit(url=url, error=f"{type(exc).__name__}: {exc}"[:200])
            a.issues.append(f"fetch failed: {a.error}")
            pages.append(a)
        if log:
            log(f"[{i + 1}/{len(targets)}] {pages[-1].status or 'ERR'} {urlsplit(url).path}")
        if i + 1 < len(targets):
            sleep(CRAWL_GAP_SEC)
    site = SiteAudit(base=base, pages=pages)
    _site_issues(site)
    return site


def _site_issues(site: SiteAudit) -> None:
    in_sitemap = {p.url for p in site.pages}
    inbound: Counter[str] = Counter()
    for p in site.pages:
        for l in p.internal_links:
            if l in in_sitemap:
                inbound[l] += 1
    for p in site.pages:
        p.inbound = inbound.get(p.url, 0)
        if p.status == 200 and p.inbound == 0 and p.path != "/":
            p.issues.append("orphan (no internal link from any sitemap page)")
    titles = Counter(p.title for p in site.pages if p.title)
    for t, n in titles.items():
        if n > 1:
            site.site_issues.append(f"{n} pages share the title “{t[:70]}”")
    descs = Counter(p.description for p in site.pages if p.description)
    for d, n in descs.items():
        if n > 1:
            site.site_issues.append(f"{n} pages share the same meta description ({d[:60]}…)")
    bad = [p for p in site.pages if p.status and p.status != 200]
    if bad:
        site.site_issues.append(f"{len(bad)} sitemap URLs do not answer 200")
    if SITEMAP_TIMING["ms"] > 5000:
        site.site_issues.append(f"sitemap.xml took {SITEMAP_TIMING['ms'] / 1000:.0f} s (Googlebot gives up around 30 s)")


# ── Search Console ───────────────────────────────────────────────────────────

@dataclass
class GscData:
    source: str = ""
    days: int = 0
    queries: list[dict] = field(default_factory=list)      # query, clicks, impressions, ctr, position
    pages: list[dict] = field(default_factory=list)        # page, clicks, impressions, ctr, position
    query_pages: list[dict] = field(default_factory=list)  # query, page, … (API only)

    @property
    def totals(self) -> dict:
        clicks = sum(r["clicks"] for r in self.pages) or sum(r["clicks"] for r in self.queries)
        impr = sum(r["impressions"] for r in self.pages) or sum(r["impressions"] for r in self.queries)
        return {"clicks": clicks, "impressions": impr, "ctr": (clicks / impr) if impr else 0.0}


def _num(v: str) -> float:
    v = (v or "").strip().replace(",", "")
    if v.endswith("%"):
        return float(v[:-1] or 0) / 100.0
    try:
        return float(v or 0)
    except ValueError:
        return 0.0


def _rows_from_csv(text: str, key: str) -> list[dict]:
    rdr = csv.reader(io.StringIO(text.lstrip("﻿")))
    header = next(rdr, None)
    if not header:
        return []
    cols = [h.strip().lower() for h in header]
    out = []
    for row in rdr:
        if not row or not row[0].strip():
            continue
        rec = dict(zip(cols, row))
        out.append({
            key: row[0].strip(),
            "clicks": int(_num(rec.get("clicks", "0"))),
            "impressions": int(_num(rec.get("impressions", "0"))),
            "ctr": _num(rec.get("ctr", "0")),
            "position": _num(rec.get("position", "0")),
        })
    return out


def load_gsc_export(path: str | Path) -> GscData:
    """A Search Console UI export: the zip Google downloads, or its unzipped
    folder. Only `Queries.csv` and `Pages.csv` are read."""
    p = Path(path).expanduser()
    files: dict[str, str] = {}
    if p.is_file() and p.suffix.lower() == ".zip":
        with zipfile.ZipFile(p) as z:
            for name in z.namelist():
                files[Path(name).name.lower()] = z.read(name).decode("utf-8-sig", "replace")
    elif p.is_dir():
        for f in p.iterdir():
            if f.suffix.lower() == ".csv":
                files[f.name.lower()] = f.read_text("utf-8-sig", "replace")
    else:
        raise FileNotFoundError(f"{p} is not a GSC export zip or folder")
    data = GscData(source=f"export:{p.name}")
    if "queries.csv" in files:
        data.queries = _rows_from_csv(files["queries.csv"], "query")
    if "pages.csv" in files:
        data.pages = _rows_from_csv(files["pages.csv"], "page")
    if not data.queries and not data.pages:
        raise ValueError(f"{p}: no Queries.csv / Pages.csv inside")
    return data


def _service_account_session(sa: str):
    """google-auth session from a path or inline JSON (scope: webmasters.readonly)."""
    from google.oauth2 import service_account
    from google.auth.transport.requests import AuthorizedSession

    scopes = ["https://www.googleapis.com/auth/webmasters.readonly"]
    sa = sa.strip()
    if sa.startswith("{"):
        creds = service_account.Credentials.from_service_account_info(json.loads(sa), scopes=scopes)
    else:
        creds = service_account.Credentials.from_service_account_file(os.path.expanduser(sa), scopes=scopes)
    return AuthorizedSession(creds)


def fetch_gsc_api(site_url: str, *, days: int = 90, sa_json: str | None = None,
                  session=None, today: date | None = None) -> GscData:
    """Search Analytics via the API. `session` is injectable for tests; otherwise
    built from `sa_json` (or env GSC_SERVICE_ACCOUNT_JSON)."""
    from datetime import timedelta

    sa_json = sa_json or os.getenv("GSC_SERVICE_ACCOUNT_JSON", "")
    if session is None:
        if not sa_json:
            raise RuntimeError("GSC_SERVICE_ACCOUNT_JSON is not set")
        session = _service_account_session(sa_json)
    today = today or date.today()
    end = today - timedelta(days=3)          # GSC lags ~3 days
    start = end - timedelta(days=days)
    endpoint = f"https://www.googleapis.com/webmasters/v3/sites/{quote(site_url, safe='')}/searchAnalytics/query"

    def q(dims: list[str]) -> list[dict]:
        body = {"startDate": start.isoformat(), "endDate": end.isoformat(),
                "dimensions": dims, "rowLimit": 5000, "dataState": "final"}
        resp = session.post(endpoint, json=body, timeout=60)
        if resp.status_code != 200:
            raise RuntimeError(f"GSC API {resp.status_code}: {resp.text[:300]}")
        rows = resp.json().get("rows") or []
        out = []
        for r in rows:
            rec = {"clicks": int(r.get("clicks", 0)), "impressions": int(r.get("impressions", 0)),
                   "ctr": float(r.get("ctr", 0.0)), "position": float(r.get("position", 0.0))}
            for d, k in zip(dims, r.get("keys") or []):
                rec[d] = k
            out.append(rec)
        return out

    data = GscData(source="api", days=days)
    data.queries = q(["query"])
    data.pages = q(["page"])
    data.query_pages = q(["query", "page"])
    return data


def load_gsc(spec: str | None, *, site_url: str = "", days: int = 90, session=None) -> GscData | None:
    """`spec` = path to an export zip/folder, 'api', or None/'' (skip)."""
    if not spec:
        return None
    if spec.lower() == "api":
        return fetch_gsc_api(site_url, days=days, session=session)
    return load_gsc_export(spec)


# ── keyword ideas (Google autocomplete) ──────────────────────────────────────

AUTOCOMPLETE_URL = "https://suggestqueries.google.com/complete/search?client=firefox&hl=en&gl=us&q="


def autocomplete(term: str, http) -> list[str]:
    try:
        resp = http.get(AUTOCOMPLETE_URL + quote(term))
        if resp.status_code != 200:
            return []
        data = json.loads(resp.text)
        return [s for s in (data[1] if len(data) > 1 else []) if isinstance(s, str)]
    except Exception:  # noqa: BLE001 — suggestions are a bonus, never a failure
        return []


def keyword_ideas(http, *, seeds: Iterable[str] = DEFAULT_SEEDS, cities: Iterable[str] = (),
                  known: Iterable[str] = (), max_requests: int = 40,
                  sleep: Callable[[float], None] = time.sleep) -> list[dict]:
    """Data-Cube-lite: seeds, then `<seed> <city>` for the cities we stock in.
    Returns [{term, from, new}] — `new` = not already a query GSC shows us for."""
    known_set = {k.lower().strip() for k in known}
    seen: set[str] = set()
    out: list[dict] = []
    probes = list(dict.fromkeys(list(seeds) + [f"{s} {c}" for c in cities for s in list(seeds)[:3]]))
    for i, probe in enumerate(probes[:max_requests]):
        for s in autocomplete(probe, http):
            key = s.lower().strip()
            if key in seen or key == probe.lower():
                continue
            seen.add(key)
            out.append({"term": s, "from": probe, "new": key not in known_set})
        if i + 1 < min(len(probes), max_requests):
            sleep(CRAWL_GAP_SEC)
    return out


# ── recommendations ──────────────────────────────────────────────────────────

@dataclass
class Recommendation:
    kind: str          # striking_distance | content_gap | ctr_gap | page_fix | orphan | site
    priority: float    # higher first
    target: str        # page path or query
    why: str
    action: str
    query: str = ""
    page: str = ""
    draft: dict | None = None


def _tokens(text: str) -> set[str]:
    toks = set()
    for w in re.findall(r"[a-z0-9]+", (text or "").lower()):
        if w in STOPWORDS:
            continue
        if len(w) > 3 and w.endswith("s"):
            w = w[:-1]
        toks.add(w)
    return toks


def _query_core(query: str) -> set[str]:
    return {t for t in _tokens(query) if t not in SOFT_TOKENS}


def _covering_page(query: str, pages: list[PageAudit]) -> PageAudit | None:
    core = _query_core(query)
    if not core:
        return None
    best, best_score = None, 0.0
    for p in pages:
        if p.status != 200 or p.noindex:
            continue
        have = _tokens(p.title) | set().union(*(_tokens(h) for h in p.h1s)) if p.h1s else _tokens(p.title)
        hit = len(core & have) / len(core)
        if hit > best_score:
            best, best_score = p, hit
    return best if best_score >= 0.999 else None


def _best_page_for(query: str, gsc: GscData, site: SiteAudit) -> str:
    for r in gsc.query_pages:
        if r.get("query") == query:
            return urlsplit(r["page"]).path or "/"
    p = _covering_page(query, site.pages)
    return p.path if p else ""


def recommend(site: SiteAudit, gsc: GscData | None, ideas: list[dict] | None = None) -> list[Recommendation]:
    recs: list[Recommendation] = []
    by_path = site.by_path()
    impr_by_path: dict[str, int] = {}
    if gsc:
        for r in gsc.pages:
            impr_by_path[urlsplit(r["page"]).path or "/"] = r["impressions"]

    if gsc:
        for r in gsc.queries:
            q, impr, pos = r["query"], r["impressions"], r["position"]
            if impr < STRIKING_MIN_IMPR:
                continue
            page = _best_page_for(q, gsc, site)
            if STRIKING_POS[0] <= pos <= STRIKING_POS[1] and page:
                a = by_path.get(page)
                in_title = a is not None and _query_core(q) <= _tokens(a.title)
                recs.append(Recommendation(
                    kind="striking_distance", priority=impr * (1.5 if not in_title else 1.0) + (21 - pos),
                    target=page, query=q, page=page,
                    why=f"“{q}” sits at position {pos:.0f} with {impr} impressions, {r['clicks']} clicks",
                    action=("put the exact phrase in the title and H1, answer it above the fold"
                            if not in_title else "add a section + FAQ that answers this query; link it from the home page"),
                ))
            elif not page:
                recs.append(Recommendation(
                    kind="content_gap", priority=impr * 1.2, target=q, query=q,
                    why=f"Google shows us for “{q}” ({impr} impressions, position {pos:.0f}) but no page targets it",
                    action="create or retitle a page whose title + H1 say exactly this; link it from the home page and footer",
                ))
        for r in gsc.pages:
            path = urlsplit(r["page"]).path or "/"
            if r["impressions"] >= CTR_GAP_MIN_IMPR and r["position"] <= CTR_GAP_MAX_POS and r["ctr"] < CTR_GAP_MAX_CTR:
                recs.append(Recommendation(
                    kind="ctr_gap", priority=r["impressions"] * 0.8, target=path, page=path,
                    why=f"{r['impressions']} impressions at position {r['position']:.0f} but CTR {r['ctr'] * 100:.1f}%",
                    action="rewrite title (≤60) + description (≤155) as the answer a buyer wants: qty, $/chair, city, pickup/freight",
                ))

    for p in site.pages:
        fixable = [i for i in p.issues if not i.startswith("orphan")]
        if fixable:
            weight = impr_by_path.get(p.path, 0)
            recs.append(Recommendation(
                kind="page_fix", priority=weight * 0.5 + len(fixable) + (50 if p.status != 200 else 0)
                + (20 if any(i.startswith("intermittent") for i in fixable) else 0),
                target=p.path, page=p.path, why="; ".join(fixable), action="fix the listed on-page issues",
            ))
        if any(i.startswith("orphan") for i in p.issues):
            recs.append(Recommendation(
                kind="orphan", priority=impr_by_path.get(p.path, 0) * 0.3 + 2, target=p.path, page=p.path,
                why="no sitemap page links to it", action="link it from a city page, the footer, or a related lot",
            ))
    for s in site.site_issues:
        recs.append(Recommendation(kind="site", priority=3, target="site", why=s, action="dedupe"))

    for idea in (ideas or []):
        if idea.get("new") and not _covering_page(idea["term"], site.pages):
            recs.append(Recommendation(
                kind="keyword_idea", priority=0.5, target=idea["term"], query=idea["term"],
                why=f"Google suggests “{idea['term']}” (from “{idea['from']}”); no page covers it",
                action="candidate for a city page section or FAQ — only if we stock what it asks for",
            ))

    recs.sort(key=lambda r: -r.priority)
    return recs


# ── optional Groq drafts (Copilot-lite) ─────────────────────────────────────

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = os.getenv("SEO_PULSE_MODEL", "openai/gpt-oss-20b")
DRAFT_PROMPT = """You write on-page SEO copy for black-whole.com, a one-person liquidator selling used banquet/event chairs BY THE LOT (hundreds at a time) to churches, venues, hotels, schools. Local pickup or nationwide freight. No hype, no exclamation marks, plain words, honest.

Target search query: "{query}"
Page: {page}
Current title: {title}
Current H1: {h1}
Current description: {description}

Return ONLY a JSON object with keys: title (<=60 chars, contains the query naturally), description (<=155 chars, says qty/price-per-chair/pickup-or-freight if known), h1 (<=70 chars), faqs (list of 3 objects {{q, a}}, each answer <=40 words, factual, no invented numbers — say "varies by lot" when unknown)."""


def _groq_chat(messages: list[dict], *, http, key: str) -> str:
    body = {"model": GROQ_MODEL, "messages": messages, "temperature": 0.2,
            "max_completion_tokens": 1500, "response_format": {"type": "json_object"}}
    resp = http.post(GROQ_URL, json=body, timeout=120, headers={"Authorization": f"Bearer {key}"})
    if resp.status_code != 200:
        raise RuntimeError(f"Groq HTTP {resp.status_code}: {resp.text[:200]}")
    return resp.json()["choices"][0]["message"]["content"]


def draft_copy(recs: list[Recommendation], site: SiteAudit, *, http, max_drafts: int | None = None,
               key: str | None = None) -> int:
    """Fill `rec.draft` for the top query-bearing recommendations. Returns the
    number drafted. No key → 0, silently (drafts are optional)."""
    if key is None:
        key = os.getenv("GROQ_API_KEY", "")
    if not key:
        return 0
    max_drafts = max_drafts if max_drafts is not None else _env_int("SEO_PULSE_MAX_DRAFTS", 5)
    by_path = site.by_path()
    n = 0
    for rec in recs:
        if n >= max_drafts:
            break
        if rec.kind not in ("striking_distance", "content_gap", "ctr_gap"):
            continue
        a = by_path.get(rec.page) if rec.page else None
        query = rec.query or (a.title if a else rec.target)
        prompt = DRAFT_PROMPT.format(
            query=query, page=rec.page or "(new page)", title=a.title if a else "(none)",
            h1=(a.h1s[0] if a and a.h1s else "(none)"), description=a.description if a else "(none)")
        try:
            raw = _groq_chat([{"role": "user", "content": prompt}], http=http, key=key)
            d = json.loads(raw)
            if isinstance(d, dict) and d.get("title"):
                rec.draft = {k: d.get(k) for k in ("title", "description", "h1", "faqs")}
                n += 1
        except Exception as exc:  # noqa: BLE001 — a failed draft is just no draft
            rec.draft = {"error": f"{type(exc).__name__}: {exc}"[:160]}
    return n


# ── snapshots + trend (share-of-voice-lite) ──────────────────────────────────

def snapshot_dir() -> Path:
    return Path(os.getenv("SEO_PULSE_DIR") or "~/.listing_automation/seo_pulse").expanduser()


def build_snapshot(site: SiteAudit, gsc: GscData | None, *, today: date | None = None) -> dict:
    today = today or date.today()
    snap: dict[str, Any] = {
        "date": today.isoformat(), "base": site.base,
        "pages": {p.path: {"status": p.status, "issues": len(p.issues), "title": p.title,
                           "words": p.word_count, "inbound": p.inbound} for p in site.pages},
    }
    if gsc:
        snap["gsc"] = {
            "source": gsc.source, "totals": gsc.totals,
            "queries": {r["query"]: {"impressions": r["impressions"], "clicks": r["clicks"],
                                     "position": r["position"]} for r in gsc.queries},
            "pages": {urlsplit(r["page"]).path or "/": {"impressions": r["impressions"], "clicks": r["clicks"],
                                                        "position": r["position"]} for r in gsc.pages},
        }
    return snap


def save_snapshot(snap: dict, directory: Path | None = None) -> Path:
    d = directory or snapshot_dir()
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"snapshot-{snap['date']}.json"
    path.write_text(json.dumps(snap, indent=1, ensure_ascii=False))
    return path


def load_previous_snapshot(before: str, directory: Path | None = None) -> dict | None:
    d = directory or snapshot_dir()
    if not d.exists():
        return None
    older = sorted(p for p in d.glob("snapshot-*.json") if p.stem[len("snapshot-"):] < before)
    if not older:
        return None
    try:
        return json.loads(older[-1].read_text())
    except (ValueError, OSError):
        return None


def diff_snapshots(prev: dict | None, cur: dict) -> dict:
    """Movers since the previous run: pages gained/lost/fixed, query position deltas."""
    out: dict[str, Any] = {"since": prev.get("date") if prev else None, "pages_new": [], "pages_gone": [],
                           "issues_delta": 0, "movers": [], "totals_delta": None}
    if not prev:
        return out
    pp, cp = prev.get("pages", {}), cur.get("pages", {})
    out["pages_new"] = sorted(set(cp) - set(pp))
    out["pages_gone"] = sorted(set(pp) - set(cp))
    out["issues_delta"] = sum(v["issues"] for v in cp.values()) - sum(v["issues"] for v in pp.values())
    pg, cg = prev.get("gsc") or {}, cur.get("gsc") or {}
    if pg and cg:
        pt, ct = pg.get("totals", {}), cg.get("totals", {})
        out["totals_delta"] = {k: ct.get(k, 0) - pt.get(k, 0) for k in ("clicks", "impressions")}
        for q, c in cg.get("queries", {}).items():
            p = pg.get("queries", {}).get(q)
            if not p or c["impressions"] < STRIKING_MIN_IMPR:
                continue
            delta = p["position"] - c["position"]      # positive = moved up
            if abs(delta) >= 3:
                out["movers"].append({"query": q, "from": p["position"], "to": c["position"],
                                      "delta": round(delta, 1), "impressions": c["impressions"]})
        out["movers"].sort(key=lambda m: -abs(m["delta"]))
    return out


# ── report ───────────────────────────────────────────────────────────────────

def render_report(site: SiteAudit, gsc: GscData | None, recs: list[Recommendation],
                  ideas: list[dict] | None, trend: dict | None, *, today: date | None = None,
                  max_recs: int = 25) -> str:
    today = today or date.today()
    ok = [p for p in site.pages if p.status == 200]
    with_issues = [p for p in site.pages if p.issues]
    top = [r for r in recs if r.kind in ("striking_distance", "content_gap", "ctr_gap")][:3]

    L: list[str] = [f"# SEO Pulse — {site.base} — {today.isoformat()}", ""]
    # ── summary (3 lines) ──
    if gsc:
        t = gsc.totals
        L.append(f"- **Search:** {t['clicks']} clicks / {t['impressions']} impressions "
                 f"(CTR {t['ctr'] * 100:.1f}%) · {len(gsc.queries)} queries · source {gsc.source}")
    else:
        L.append("- **Search:** no Search Console data this run (pass `--gsc <export.zip>` or `--gsc api`)")
    L.append(f"- **Site:** {len(ok)}/{len(site.pages)} sitemap pages answer 200 · "
             f"{len(with_issues)} with issues · {len(site.site_issues)} site-wide")
    if top:
        L.append("- **Do first:** " + " · ".join(f"{r.target} ← “{r.query}”" if r.query else r.target for r in top))
    else:
        L.append("- **Do first:** fix the page issues below")
    if trend and trend.get("since"):
        td = trend.get("totals_delta")
        bits = [f"vs {trend['since']}"]
        if td:
            bits.append(f"clicks {td['clicks']:+d}, impressions {td['impressions']:+d}")
        bits.append(f"issues {trend['issues_delta']:+d}")
        if trend["pages_new"]:
            bits.append(f"{len(trend['pages_new'])} new pages")
        L.append("- **Trend:** " + " · ".join(bits))
    L.append("")

    # ── recommendations ──
    L.append("## Recommendations (ranked)")
    L.append("")
    for i, r in enumerate(recs[:max_recs], 1):
        head = f"{i}. **{r.kind.replace('_', ' ')}** — `{r.target}`"
        L.append(head)
        L.append(f"    - why: {r.why}")
        L.append(f"    - do: {r.action}")
        if r.draft:
            if "error" in r.draft:
                L.append(f"    - draft: failed ({r.draft['error']})")
            else:
                L.append(f"    - draft title: {r.draft.get('title')}")
                L.append(f"    - draft description: {r.draft.get('description')}")
                L.append(f"    - draft H1: {r.draft.get('h1')}")
                for f in (r.draft.get("faqs") or [])[:3]:
                    if isinstance(f, dict):
                        L.append(f"    - FAQ: **{f.get('q')}** — {f.get('a')}")
    if len(recs) > max_recs:
        L.append(f"    - … {len(recs) - max_recs} more in the snapshot")
    L.append("")

    # ── trend detail ──
    if trend and trend.get("movers"):
        L.append("## Movers (position, since last run)")
        L.append("")
        L.append("| query | from | to | Δ | impressions |")
        L.append("|---|---|---|---|---|")
        for m in trend["movers"][:15]:
            L.append(f"| {m['query']} | {m['from']:.0f} | {m['to']:.0f} | {m['delta']:+.0f} | {m['impressions']} |")
        L.append("")

    # ── GSC tables ──
    if gsc:
        L.append("## Search Console — top queries")
        L.append("")
        L.append("| query | clicks | impr | CTR | pos | covered by |")
        L.append("|---|---|---|---|---|---|")
        for r in sorted(gsc.queries, key=lambda x: (-x["impressions"], x["position"]))[:25]:
            cov = _best_page_for(r["query"], gsc, site) or "—"
            L.append(f"| {r['query']} | {r['clicks']} | {r['impressions']} | {r['ctr'] * 100:.1f}% | {r['position']:.0f} | {cov} |")
        L.append("")
        if gsc.pages:
            L.append("## Search Console — top pages")
            L.append("")
            L.append("| page | clicks | impr | CTR | pos |")
            L.append("|---|---|---|---|---|")
            for r in sorted(gsc.pages, key=lambda x: -x["impressions"])[:20]:
                L.append(f"| {urlsplit(r['page']).path or '/'} | {r['clicks']} | {r['impressions']} | {r['ctr'] * 100:.1f}% | {r['position']:.0f} |")
            L.append("")

    # ── audit table ──
    L.append("## Site audit")
    L.append("")
    L.append("| page | status | title | words | in-links | issues |")
    L.append("|---|---|---|---|---|---|")
    for p in sorted(site.pages, key=lambda x: (-len(x.issues), x.path)):
        L.append(f"| {p.path} | {p.status or 'ERR'} | {len(p.title)} | {p.word_count} | {p.inbound} | "
                 f"{'; '.join(p.issues) or '—'} |")
    L.append("")
    if site.site_issues:
        L.append("Site-wide: " + " · ".join(site.site_issues))
        L.append("")

    # ── keyword ideas ──
    if ideas:
        new = [i for i in ideas if i["new"]]
        L.append(f"## Keyword ideas (Google autocomplete) — {len(new)} not yet in Search Console")
        L.append("")
        for i in new[:40]:
            L.append(f"- {i['term']}  ← {i['from']}")
        L.append("")
    return "\n".join(L)


def telegram_lines(site: SiteAudit, gsc: GscData | None, recs: list[Recommendation], trend: dict | None) -> str:
    ok = sum(1 for p in site.pages if p.status == 200)
    parts = ["🔎 SEO Pulse"]
    if gsc:
        t = gsc.totals
        parts.append(f"{t['clicks']} clicks / {t['impressions']} impr · {len(gsc.queries)} queries")
    parts.append(f"{ok}/{len(site.pages)} pages OK · {sum(1 for p in site.pages if p.issues)} with issues")
    if trend and trend.get("totals_delta"):
        td = trend["totals_delta"]
        parts.append(f"Δ clicks {td['clicks']:+d} impr {td['impressions']:+d} since {trend['since']}")
    for r in [r for r in recs if r.kind in ("striking_distance", "content_gap", "ctr_gap")][:3]:
        parts.append(f"→ {r.target}: {r.why}")
    return "\n".join(parts)


# ── one-shot run ─────────────────────────────────────────────────────────────

def run(*, base: str, http, gsc_spec: str | None = None, site_url: str = "", days: int = 90,
        keywords: bool = True, draft: bool = False, limit: int | None = None,
        directory: Path | None = None, today: date | None = None, log: Callable[[str], None] | None = None,
        gsc_session=None, sleep: Callable[[float], None] = time.sleep) -> dict:
    """audit → gsc → keywords → recommend → (draft) → snapshot → report."""
    today = today or date.today()
    site = crawl(base, http, limit=limit, sleep=sleep, log=log)
    gsc: GscData | None = None
    gsc_error = ""
    if gsc_spec:
        try:
            gsc = load_gsc(gsc_spec, site_url=site_url or (base.rstrip("/") + "/"), days=days, session=gsc_session)
        except Exception as exc:  # noqa: BLE001 — the audit half still runs
            gsc_error = f"{type(exc).__name__}: {exc}"[:200]
            if log:
                log(f"GSC unavailable: {gsc_error}")
    ideas: list[dict] = []
    if keywords:
        cities = []
        for p in site.pages:
            m = re.match(r"^/chairs/([a-z0-9-]+)-([a-z]{2})$", p.path)
            if m:
                cities.append(m.group(1).replace("-", " "))
        ideas = keyword_ideas(http, cities=cities[:8], known=[r["query"] for r in (gsc.queries if gsc else [])],
                              sleep=sleep)
    recs = recommend(site, gsc, ideas)
    drafted = draft_copy(recs, site, http=http) if draft else 0
    snap = build_snapshot(site, gsc, today=today)
    prev = load_previous_snapshot(snap["date"], directory)
    trend = diff_snapshots(prev, snap)
    snap_path = save_snapshot(snap, directory)
    report = render_report(site, gsc, recs, ideas, trend, today=today)
    if gsc_error:
        report = report.replace("- **Search:** no Search Console data this run",
                                f"- **Search:** Search Console failed ({gsc_error})", 1)
    d = directory or snapshot_dir()
    report_path = d / f"report-{snap['date']}.md"
    report_path.write_text(report)
    return {"site": site, "gsc": gsc, "ideas": ideas, "recs": recs, "trend": trend, "drafted": drafted,
            "report": report, "report_path": report_path, "snapshot_path": snap_path, "gsc_error": gsc_error}
