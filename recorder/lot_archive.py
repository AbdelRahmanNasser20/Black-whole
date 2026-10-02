"""Permanent, private archive of every closed GovDeals lot (the data moat).

Why: a closed GovDeals page cannot be re-scraped for long. The maestro detail
(116 keys incl. the description), the photo gallery and the bidbox final keep
answering for a few weeks after close (verified 2026-09-29: 5282/3780/2 and
28/16539/1, 26 days after close), then they are purged. `listing_snapshots`
keeps only prices. This module keeps the whole listing — once, at close — so
the "what did this kind of lot really sell for, and what did it look like"
question can be answered forever from our own storage.

Layout (one object set per lot; `slug` = `{asset}_{account}_{auction}`):

    archive/lots/govdeals/{slug}.json.gz        the document (done marker)
    archive/lots/govdeals/{slug}/{i}.jpg        up to N photos, ≤1024 px, q75
    archive/lots/govdeals/_meta/{slug}.json     small list-page summary
    archive/lots/govdeals/_analysis/{slug}.json cached LLM analysis (lot_analysis.py)

**Private.** Nothing here is disguised (image_disguise.py) because nothing here
is ever served publicly: the admin routes read objects through the R2 API and
stream them behind the session cookie. Never hand an archive key to a public
template, the storefront, the catalog feeds or `/deals`.

Rules carried over from `deals/raw_archive.py`:
- R2 unconfigured is a hard error for a write, never a silent skip. The local
  store (`LOT_ARCHIVE_STORE=local`) exists for development and must be asked
  for by name.
- Every object is read back and checked before the document (the done marker)
  is written; the document itself is read back and parsed.
- Idempotent: a lot whose document exists is skipped without a single request.

Photos first, then `_meta`, then the document — so a document never points at
a photo that is not there, and a crash mid-lot is simply redone next run.
"""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

SOURCE = "govdeals"
PREFIX = "archive/lots/govdeals"
# Maestro-family sources the archive keeps, each under its own prefix
# (archive/lots/{source}/…). Anything else is refused, never guessed.
SOURCES = ("govdeals", "allsurplus")


def prefix(source: str = SOURCE) -> str:
    if source not in SOURCES:
        raise ValueError(f"lot archive: unknown source {source!r}")
    return f"archive/lots/{source}"
SCHEMA_VERSION = 1

PHOTOS_DEFAULT = 6
PHOTO_MAX_PX = 1024
PHOTO_QUALITY = 75

# A lot is archived once its close has settled. The detail endpoint answered a
# single key for a lot that had closed hours earlier (2026-09-29), so a thin
# detail is "not ready, retry", not "archive what we got" — until the lot is
# old enough that waiting cannot help any more.
MIN_AGE = timedelta(hours=1)
PARTIAL_AFTER = timedelta(days=3)
MIN_DETAIL_KEYS = 20
NON_SALE_OUTCOMES = frozenset({"reserve_not_met", "no_bid", "cancelled"})


def env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name) or default)
    except ValueError:
        return default


def photos_per_lot() -> int:
    return max(0, env_int("LOT_ARCHIVE_PHOTOS", PHOTOS_DEFAULT))


# ─────────────────────────────── keys ───────────────────────────────

def slug(key: tuple[int, int, int] | str) -> str:
    if isinstance(key, str):
        key = tuple(int(p) for p in key.split("/"))  # type: ignore[assignment]
    a, b, c = key  # type: ignore[misc]
    return f"{int(a)}_{int(b)}_{int(c)}"


def parse_slug(s: str) -> tuple[int, int, int] | None:
    parts = s.split("_")
    if len(parts) != 3:
        return None
    try:
        return int(parts[0]), int(parts[1]), int(parts[2])
    except ValueError:
        return None


def doc_key(key, source: str = SOURCE) -> str:
    return f"{prefix(source)}/{slug(key)}.json.gz"


def photo_key(key, i: int, source: str = SOURCE) -> str:
    return f"{prefix(source)}/{slug(key)}/{i}.jpg"


def meta_key(key, source: str = SOURCE) -> str:
    return f"{prefix(source)}/_meta/{slug(key)}.json"


def analysis_key(key, source: str = SOURCE) -> str:
    return f"{prefix(source)}/_analysis/{slug(key)}.json"


# ─────────────────────────────── stores ───────────────────────────────

class StoreNotConfigured(RuntimeError):
    pass


class PrivateBucketNotConfigured(StoreNotConfigured):
    """LOT_ARCHIVE_R2_BUCKET is unset, or names the public R2_BUCKET.

    Local guard until Track A's `automation.r2_images.private_bucket()` lands;
    then R2Store switches to it. Same rule: NEVER fall back to the public
    bucket — its r2.dev base serves any key to anyone who guesses it."""


def private_bucket(cfg: dict) -> str:
    bucket = (os.getenv("LOT_ARCHIVE_R2_BUCKET") or "").strip()
    if not bucket:
        raise PrivateBucketNotConfigured(
            "LOT_ARCHIVE_R2_BUCKET is not set — the lot archive only writes to a private "
            "bucket (blackwhole-archive), never to the public R2_BUCKET")
    if bucket == (cfg.get("bucket") or "").strip():
        raise PrivateBucketNotConfigured(
            f"LOT_ARCHIVE_R2_BUCKET={bucket!r} is the public R2_BUCKET — set it to the "
            "private archive bucket (blackwhole-archive)")
    return bucket


class R2Store:
    """The canonical backend. Objects are read back through the S3 API — the
    archive is never linked by its public r2.dev URL."""

    kind = "r2"

    def __init__(self, cfg: dict | None = None, s3=None):
        from automation import r2_images
        self.cfg = cfg or r2_images.env_config()
        if not self.cfg:
            raise StoreNotConfigured("R2 is not configured (R2_ACCOUNT_ID/ACCESS_KEY_ID/"
                                     "SECRET_ACCESS_KEY/BUCKET/PUBLIC_BASE)")
        # A dedicated bucket with no public access is the stronger form of
        # "private": the shared bucket's r2.dev base serves ANY key to anyone
        # who guesses it, and these keys spell the GovDeals ids.
        self.bucket = private_bucket(self.cfg)
        self._s3 = s3 or r2_images.client(self.cfg)

    def put(self, key: str, data: bytes, content_type: str) -> None:
        # Private, content-stable objects: no public Cache-Control.
        self._s3.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=content_type)

    def get(self, key: str) -> bytes | None:
        try:
            return self._s3.get_object(Bucket=self.bucket, Key=key)["Body"].read()
        except Exception as e:  # noqa: BLE001
            if _is_missing(e):
                return None
            raise

    def exists(self, key: str) -> bool:
        try:
            self._s3.head_object(Bucket=self.bucket, Key=key)
            return True
        except Exception as e:  # noqa: BLE001
            if _is_missing(e):
                return False
            raise

    def list(self, prefix: str) -> list[str]:
        """Keys directly under `prefix` (no recursion into photo folders)."""
        out: list[str] = []
        token = None
        while True:
            kw = {"Bucket": self.bucket, "Prefix": prefix, "Delimiter": "/", "MaxKeys": 1000}
            if token:
                kw["ContinuationToken"] = token
            r = self._s3.list_objects_v2(**kw)
            out.extend(o["Key"] for o in r.get("Contents", []))
            if not r.get("IsTruncated"):
                return out
            token = r.get("NextContinuationToken")


def _is_missing(e: Exception) -> bool:
    code = str(getattr(e, "response", {}).get("Error", {}).get("Code", "")) if hasattr(e, "response") else ""
    return code in ("404", "NoSuchKey", "NotFound")


class LocalStore:
    """Same key layout on local disk. Development only — ask for it with
    `LOT_ARCHIVE_STORE=local` (+ optional `LOT_ARCHIVE_LOCAL_DIR`)."""

    kind = "local"

    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser()

    def _p(self, key: str) -> Path:
        p = (self.root / key).resolve()
        if self.root.resolve() not in p.parents:
            raise ValueError(f"key escapes the store: {key!r}")
        return p

    def put(self, key: str, data: bytes, content_type: str) -> None:
        p = self._p(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(p)

    def get(self, key: str) -> bytes | None:
        p = self._p(key)
        return p.read_bytes() if p.is_file() else None

    def exists(self, key: str) -> bool:
        return self._p(key).is_file()

    def list(self, prefix: str) -> list[str]:
        d = self._p(prefix.rstrip("/") + "/x").parent
        if not d.is_dir():
            return []
        base = prefix.rstrip("/")
        return sorted(f"{base}/{f.name}" for f in d.iterdir()
                      if f.is_file() and not f.name.endswith(".tmp"))


DEFAULT_LOCAL_DIR = "~/.listing_automation/lot_archive"

_last_store_error: str | None = None


def last_store_error() -> str | None:
    """Why the last `store_from_env()` returned None (for the run's NOTE)."""
    return _last_store_error


def store_from_env():
    """The configured store, or None. `LOT_ARCHIVE_STORE=local` → LocalStore;
    otherwise R2 when configured; otherwise None (callers decide: a writer
    raises, a reader shows "not configured")."""
    global _last_store_error
    _last_store_error = None
    kind = (os.getenv("LOT_ARCHIVE_STORE") or "r2").strip().lower()
    if kind == "local":
        return LocalStore(os.getenv("LOT_ARCHIVE_LOCAL_DIR") or DEFAULT_LOCAL_DIR)
    try:
        return R2Store()
    except StoreNotConfigured as e:
        _last_store_error = str(e)
        return None


def require_store(store=None):
    store = store or store_from_env()
    if store is None:
        raise StoreNotConfigured(
            "lot archive: R2 is not configured — refusing to archive without a destination. "
            "Set R2_* + LOT_ARCHIVE_R2_BUCKET (production) or LOT_ARCHIVE_STORE=local for a "
            "local dev store." + (f" ({_last_store_error})" if _last_store_error else ""))
    return store


# ─────────────────────────────── pure helpers ───────────────────────────────

def serialize(doc: dict) -> bytes:
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz:
        gz.write(json.dumps(doc, default=str, sort_keys=True).encode("utf-8"))
    return buf.getvalue()


def parse(blob: bytes) -> dict:
    with gzip.GzipFile(fileobj=io.BytesIO(blob), mode="rb") as gz:
        return json.loads(gz.read())


def resize_jpeg(data: bytes, max_px: int = PHOTO_MAX_PX, quality: int = PHOTO_QUALITY) -> tuple[bytes, int, int] | None:
    """Any image → metadata-free JPEG, longest side ≤ max_px. None if unreadable."""
    from PIL import Image, ImageOps
    try:
        im = Image.open(io.BytesIO(data))
        im = ImageOps.exif_transpose(im)
        im = im.convert("RGB")
    except Exception:  # noqa: BLE001
        return None
    im.thumbnail((max_px, max_px))
    out = io.BytesIO()
    im.save(out, format="JPEG", quality=quality, optimize=True, progressive=True)
    return out.getvalue(), im.width, im.height


def _str(*vals) -> str | None:
    """First non-empty value as a stripped string (maestro sends some fields,
    e.g. zipCode, as numbers on some sellers). Pure."""
    for v in vals:
        if v is None:
            continue
        t = str(v).strip()
        if t:
            return t
    return None


def _money(v) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _iso_utc(s: Any) -> str | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        return None
    return dt.astimezone(timezone.utc).isoformat()


def html_to_text(s: str | None) -> str:
    """GovDeals descriptions are HTML fragments (`<br />`, `<p><b>`…). Plain
    text for the page; the untouched HTML stays in `detail`. Pure."""
    import html as _html
    import re as _re
    if not s:
        return ""
    t = _re.sub(r"(?i)<\s*br\s*/?\s*>", "\n", s)
    t = _re.sub(r"(?i)</?\s*(p|div|li|tr|h[1-6])\b[^>]*>", "\n", t)
    t = _re.sub(r"<[^>]+>", "", t)
    t = _html.unescape(t).replace("\xa0", " ")
    t = _re.sub(r"[ \t]+\n", "\n", t)
    t = _re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def summarize(detail: dict | None, bidbox: dict | None, search_raw: dict | None,
              timeline: list[dict] | None = None) -> dict:
    """The fields the rebuilt page and the list need, from whatever we have.
    Pure. Precedence: bidbox (final numbers, true UTC clock) > detail (the
    listing) > search raw (what the sweep saw)."""
    from recorder.sources.govdeals import close_outcome
    from deals.categories import canonical_category

    d, b, s = detail or {}, bidbox or {}, search_raw or {}
    code = b.get("assetStatusCd") or d.get("assetStatusCd") or s.get("assetStatusCd")
    bids = b.get("bidCount") if b.get("bidCount") is not None else s.get("bidCount")
    try:
        bids = None if bids is None else int(bids)
    except (TypeError, ValueError):
        bids = None
    final = _money(b.get("currentBid"))
    if final is None and timeline:
        priced = [t for t in timeline if t.get("current_bid") is not None]
        final = _money(priced[-1]["current_bid"]) if priced else None
    cat_code = str(d.get("assetCategory") or s.get("assetCategory") or "")
    outcome = close_outcome(code, bids) if b else None
    return {
        "title": _str(d.get("assetShortDesc"), s.get("assetShortDescription")) or "",
        "description": html_to_text(_str(d.get("assetLongDesc"), s.get("assetLongDescription")) or ""),
        "category_code": cat_code or None,
        "category_name": _str(d.get("catDesc"), s.get("categoryDescription")),
        "parent_category": _str(d.get("parentCatDesc")),
        "canonical_category": canonical_category(cat_code) if cat_code else None,
        "seller": _str(d.get("companyName"), s.get("companyName")),
        "department": _str(d.get("department")) if str(d.get("department") or "").strip() not in ("", "0") else None,
        "city": _str(d.get("city"), b.get("city"), s.get("locationCity")),
        "state": _str(d.get("state"), b.get("state"), s.get("locationState")),
        "zip": _str(d.get("zipCode"), b.get("zipCode"), s.get("locationZip")),
        "lat": d.get("latitude") or b.get("latitude") or s.get("latitude"),
        "lng": d.get("longitude") or b.get("longitude") or s.get("longitude"),
        "opened_at": _iso_utc(b.get("assetAuctionStartDateUTC")) or _iso_utc(s.get("assetAuctionStartDateUtc")),
        "closed_at": _iso_utc(b.get("assetAuctionEndDateUTC")) or _iso_utc(s.get("assetAuctionEndDateUtc")),
        "final_price": final,
        "bid_count": bids,
        "opening_bid": _money(b.get("assetBidPrice")) if b else _money(s.get("assetBidPrice")),
        "status_code": code,
        "outcome": outcome,
        "currency": _str(d.get("currencyCode"), s.get("currencyCode")) or "USD",
        "premium_pct": _money(b.get("premiumPercent")),
        "grand_total": _money(b.get("grandTotalAmount")),
        "winner_handle": b.get("highBidderUsername"),
        "visitors": b.get("visitors"),
        "hits": b.get("hits"),
        "condition_code": d.get("assetConditionCd"),
        "quantity_text": d.get("assetQtyText") or None,
        "quantity": d.get("assetQty"),
        "has_reserve": b.get("hasReservePrice"),
        "reserve_not_met": b.get("isReserveNotMet"),
        "auto_extensions": b.get("autoExtensionCount"),
    }


def build_document(key: tuple[int, int, int], *, detail: dict | None, gallery: list[str],
                   bidbox: dict | None, bidbox_result: str, search_raw: dict | None,
                   timeline: list[dict], photos: list[dict], completeness: str,
                   archived_at: datetime | None = None, source: str = SOURCE) -> dict:
    return {
        "schema": SCHEMA_VERSION,
        "source": source,
        "lot_key": f"{key[0]}/{key[1]}/{key[2]}",
        "archived_at": (archived_at or datetime.now(timezone.utc)).isoformat(),
        "completeness": completeness,
        "summary": summarize(detail, bidbox, search_raw, timeline),
        "detail": detail,
        "gallery_urls": gallery,
        "bidbox": bidbox,
        "bidbox_result": bidbox_result,
        "search_raw": search_raw,
        "timeline": timeline,
        "photos": photos,
    }


META_FIELDS = ("title", "canonical_category", "category_name", "city", "state", "seller",
               "closed_at", "final_price", "bid_count", "outcome", "status_code")


def meta_of(doc: dict) -> dict:
    s = doc.get("summary") or {}
    m = {k: s.get(k) for k in META_FIELDS}
    m.update({"lot_key": doc["lot_key"], "archived_at": doc.get("archived_at"),
              "source": doc.get("source") or SOURCE,
              "currency": s.get("currency"),
              "photo_count": len(doc.get("photos") or []),
              "completeness": doc.get("completeness")})
    return m


# ─────────────────────────────── the archiver ───────────────────────────────

@dataclass
class ArchiveResult:
    lot_key: str
    result: str                     # archived | skipped_exists | not_ready | error
    reason: str = ""
    doc_bytes: int = 0
    photo_bytes: int = 0
    photos: int = 0
    outcome: str | None = None
    final_price: float | None = None
    requests: int = 0

    @property
    def total_bytes(self) -> int:
        return self.doc_bytes + self.photo_bytes


def _verify_put(store, key: str, data: bytes, content_type: str) -> None:
    store.put(key, data, content_type)
    got = store.get(key)
    if got is None or hashlib.sha256(got).digest() != hashlib.sha256(data).digest():
        raise RuntimeError(f"readback mismatch for {key!r}")


def _download(url: str, http_get: Callable) -> bytes | None:
    try:
        r = http_get(url, timeout=30)
    except Exception as e:  # noqa: BLE001 - a photo is never worth the lot
        print(f"[lot_archive] photo fetch failed {url}: {e}", file=sys.stderr)
        return None
    if getattr(r, "status_code", 0) != 200 or not r.content:
        print(f"[lot_archive] photo fetch {url}: HTTP {getattr(r, 'status_code', '?')}", file=sys.stderr)
        return None
    return r.content


def archive_lot(key: tuple[int, int, int], *, store, adapter, http_get: Callable,
                timeline_fn: Callable[[str], tuple[list[dict], dict | None]],
                end_date: datetime | None = None, bidbox: dict | None = None,
                max_photos: int | None = None, now: datetime | None = None,
                index_fn: Callable[[dict], None] | None = None, force: bool = False,
                source: str = SOURCE) -> ArchiveResult:
    """Archive one closed lot. Never raises for a lot-level problem: returns an
    ArchiveResult whose `result` says what happened."""
    from recorder.sources import govdeals as gd

    now = now or datetime.now(timezone.utc)
    lk = f"{key[0]}/{key[1]}/{key[2]}"
    res = ArchiveResult(lk, "error")
    max_photos = photos_per_lot() if max_photos is None else max_photos

    prefix(source)   # unknown source → ValueError before any request
    if not force and store.exists(doc_key(key, source)):
        res.result = "skipped_exists"
        return res
    if end_date is not None and now - end_date < MIN_AGE:
        res.result, res.reason = "not_ready", "closed < 1h ago"
        return res

    # 1. bidbox — the final numbers (reuse the recorder's stored payload if any)
    bidbox_result = "stored" if bidbox else "none"
    if not bidbox:
        result, raw = gd.fetch_bidbox(adapter, key)
        res.requests += 1
        if result == "error":
            res.result, res.reason = "not_ready", "bidbox read failed"
            return res
        if result == "ok":
            verdict = gd.classify_bidbox(raw, now)
            if verdict != "final":
                res.result, res.reason = "not_ready", f"bidbox says {verdict}"
                return res
            bidbox, bidbox_result = raw, "ok"
        else:
            bidbox_result = "purged"

    if end_date is None and bidbox:
        end_date = gd._bidbox_end(bidbox)   # --lot rows: the bidbox knows the true close

    # 2. detail + gallery
    gd._bidbox_throttle()  # same ≤1 req/s budget as every other maestro call
    res.requests += 1
    try:
        detail = adapter.fetch_detail(key[0], key[1])
    except Exception as e:  # noqa: BLE001 - JSONDecodeError (204) included
        detail = None
        print(f"[lot_archive] detail read failed for {lk}: {e}", file=sys.stderr)
    too_thin = not detail or len(detail) < MIN_DETAIL_KEYS or not detail.get("assetId")
    age = (now - end_date) if end_date else None
    # A lot that did not sell loses its detail at close (204 on all 9 reserve-
    # not-met / no-bid lots sampled 2026-09-29, hours to days after close) —
    # waiting cannot bring it back, so keep what we have now.
    non_sale = bool(bidbox) and gd.close_outcome(
        bidbox.get("assetStatusCd"), gd._bidbox_bid_count(bidbox)) in NON_SALE_OUTCOMES
    if too_thin and not non_sale and (age is None or age < PARTIAL_AFTER):
        res.result, res.reason = "not_ready", f"detail has {len(detail or {})} key(s)"
        return res
    completeness = "partial" if too_thin or bidbox_result == "purged" else "full"
    if too_thin:
        detail = detail or None

    from deals.mapping import _hero_url, photo_paths_to_urls
    gallery = photo_paths_to_urls((detail or {}).get("assetPhotos") or [])

    # 3. our own history of the lot (DB, read-only)
    timeline, search_raw = timeline_fn(lk, source)
    if not gallery and (search_raw or {}).get("photo"):
        # purged detail: the sweep's cover photo is often still on the CDN
        gallery = [_hero_url(key[1], search_raw["photo"])]

    # 4. photos first (so the document never points at a missing one)
    photos: list[dict] = []
    for url in gallery:
        if len(photos) >= max_photos:
            break
        raw_img = _download(url, http_get)
        res.requests += 1
        if raw_img is None:
            continue
        out = resize_jpeg(raw_img)
        if out is None:
            continue
        data, w, h = out
        i = len(photos)
        k = photo_key(key, i, source)
        _verify_put(store, k, data, "image/jpeg")
        photos.append({"i": i, "key": k, "bytes": len(data), "w": w, "h": h, "src": url})
        res.photo_bytes += len(data)

    doc = build_document(key, detail=detail, gallery=gallery, bidbox=bidbox,
                         bidbox_result=bidbox_result, search_raw=search_raw,
                         timeline=timeline, photos=photos, completeness=completeness,
                         archived_at=now, source=source)
    meta = meta_of(doc)
    _verify_put(store, meta_key(key, source), json.dumps(meta, default=str).encode(), "application/json")

    blob = serialize(doc)
    store.put(doc_key(key, source), blob, "application/gzip")
    got = store.get(doc_key(key, source))
    if got is None or parse(got).get("lot_key") != lk or len(parse(got).get("photos") or []) != len(photos):
        raise RuntimeError(f"readback mismatch for {doc_key(key, source)!r}")

    if index_fn is not None:
        try:
            index_fn(meta)
        except Exception as e:  # noqa: BLE001 - the index is bookkeeping; R2 is the record
            print(f"[lot_archive] index write failed for {lk}: {e}", file=sys.stderr)

    res.result = "archived"
    res.doc_bytes = len(blob)
    res.photos = len(photos)
    res.outcome = doc["summary"].get("outcome")
    res.final_price = doc["summary"].get("final_price")
    return res


def load(store, key, source: str = SOURCE) -> dict | None:
    """The stored document with its `summary` re-derived from the untouched
    payloads — like `sold_comps`, a fix to the derivation reaches every lot
    already archived without re-fetching anything."""
    blob = store.get(doc_key(key, source))
    if not blob:
        return None
    doc = parse(blob)
    try:
        doc["summary"] = summarize(doc.get("detail"), doc.get("bidbox"), doc.get("search_raw"),
                                   doc.get("timeline"))
    except Exception as e:  # noqa: BLE001 - fall back to what was stored
        print(f"[lot_archive] summary re-derive failed for {doc.get('lot_key')}: {e}", file=sys.stderr)
    return doc


def archived_slugs(store, source: str = SOURCE) -> set[str]:
    """Every lot with a document. One LIST call per 1,000 lots."""
    out = set()
    for k in store.list(prefix(source) + "/"):
        name = k.rsplit("/", 1)[-1]
        if name.endswith(".json.gz"):
            out.add(name[: -len(".json.gz")])
    return out


def run_archive(candidates: Iterable[dict], *, store, adapter, http_get: Callable,
                timeline_fn, limit: int, apply: bool, time_budget_s: float | None = None,
                already: set[str] | None = None, index_fn=None, force: bool = False,
                log: Callable[[str], None] = print, source: str = SOURCE) -> dict:
    """Archive up to `limit` candidates (rows with `source_lot_id`, `end_date`,
    optional `bidbox`). Dry-run (`apply=False`) makes no request at all."""
    from recorder.sources.govdeals import _parse_lot_key

    t0 = time.monotonic()
    already = already if already is not None else archived_slugs(store, source)
    meter = {"considered": 0, "archived": 0, "skipped_exists": 0, "not_ready": 0,
             "error": 0, "would_archive": 0, "doc_bytes": 0, "photo_bytes": 0, "photos": 0,
             "requests": 0, "outcomes": {}, "results": []}
    for row in candidates:
        if meter["archived"] + meter["would_archive"] >= limit:
            break
        if time_budget_s is not None and time.monotonic() - t0 > time_budget_s:
            log(f"[lot_archive] time budget {time_budget_s:.0f}s spent — the rest go next run")
            break
        parsed = _parse_lot_key(str(row["source_lot_id"]))
        if parsed is None:
            continue
        meter["considered"] += 1
        if slug(parsed) in already and not force:
            meter["skipped_exists"] += 1
            continue
        if not apply:
            meter["would_archive"] += 1
            continue
        try:
            r = archive_lot(parsed, store=store, adapter=adapter, http_get=http_get,
                            timeline_fn=timeline_fn, end_date=row.get("end_date"),
                            bidbox=row.get("bidbox"), index_fn=index_fn, force=force,
                            source=source)
        except Exception as e:  # noqa: BLE001 - one lot never kills the pass
            log(f"[lot_archive] RECORDER ERROR archive failed for {row['source_lot_id']}: {e!r}")
            meter["error"] += 1
            continue
        meter[r.result] = meter.get(r.result, 0) + 1
        meter["requests"] += r.requests
        if r.result == "archived":
            meter["doc_bytes"] += r.doc_bytes
            meter["photo_bytes"] += r.photo_bytes
            meter["photos"] += r.photos
            meter["outcomes"][r.outcome or "unknown"] = meter["outcomes"].get(r.outcome or "unknown", 0) + 1
            already.add(slug(parsed))
        meter["results"].append(r)
    return meter
