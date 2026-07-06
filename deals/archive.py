import os, re, sys, time, hashlib, httpx
from deals.models import Lot
from automation.downloader import DOWNLOAD_HEADERS

BUCKET = "listing-images"

_EXT_RE = re.compile(r'\.(jpe?g|png|webp)(?:\?|$)', re.I)
_CONTENT_TYPES = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp"}


def _resize_height() -> int:
    """Archive images downsized via the CDN's own resize params to control
    storage spend (Supabase free tier is 1 GB). 0 disables (full-res)."""
    try:
        return int(os.environ.get("DEALS_ARCHIVE_IMG_HEIGHT", "800"))
    except ValueError:
        return 800


def _canon(url: str) -> str:
    """Cache-buster-free identity of an image. The sweep and the detail endpoint
    stamp different ?cb= values on the same photo; dedupe/hash on the bare URL."""
    return url.split("?")[0]


def _content_type(path: str) -> str:
    return _CONTENT_TYPES.get(path.rsplit(".", 1)[-1].lower(), "image/jpeg")


def _storage_path(lot, idx: int, url: str) -> str:
    # idx retained for call-site signature compatibility; the path is
    # content-addressed by the canonical URL hash so the same image always
    # resolves to the same object key regardless of scrape ordering or ?cb=
    # churn (idempotent re-archiving).
    canon = _canon(url)
    m = _EXT_RE.search(canon)
    ext = ".jpg"
    if m:
        e = m.group(1).lower()
        ext = ".jpg" if e == "jpeg" else "." + e
    if _resize_height():
        ext = ".webp"   # CDN resize responses are webp
    h = hashlib.sha256(canon.encode()).hexdigest()[:10]
    return f"govdeals/{lot.asset_id}_{lot.account_id}_{lot.auction_id}/{h}{ext}"


def _download(url: str) -> bytes | None:
    h = _resize_height()
    if h:
        url = f"{url}{'&' if '?' in url else '?'}h={h}&webp=true"
    try:
        r = httpx.get(url, headers=DOWNLOAD_HEADERS, timeout=30, follow_redirects=True)
        r.raise_for_status()
        return r.content
    except Exception:
        return None


def _upload(path: str, data: bytes) -> str:
    """Upload to Supabase storage; return public URL. Uses SUPABASE_STORAGE_* env."""
    # SUPABASE_STORAGE_URL is the project base (no /storage/v1) — same contract
    # as automation/listing_images.py.
    base = os.environ["SUPABASE_STORAGE_URL"].rstrip("/")
    key = os.environ["SUPABASE_STORAGE_KEY"]
    httpx.post(f"{base}/storage/v1/object/{BUCKET}/{path}", content=data,
               headers={"Authorization": f"Bearer {key}", "content-type": _content_type(path),
                        "x-upsert": "true"}, timeout=60).raise_for_status()
    return f"{base}/storage/v1/object/public/{BUCKET}/{path}"


def archive_lot_images(lot: Lot, gallery: list[str], meter: dict | None = None) -> list[str]:
    seen, urls = set(), []
    for u in [lot.hero_image_url] + list(gallery):
        if u and _canon(u) not in seen:
            seen.add(_canon(u))
            urls.append(u)
    stored = []
    for idx, url in enumerate(urls):
        data = _download(url)
        if data:
            stored.append(_upload(_storage_path(lot, idx, url), data))
            if meter is not None:
                meter["bytes"] = meter.get("bytes", 0) + len(data)
                meter["images"] = meter.get("images", 0) + 1
    return stored


class _RowLot:
    """Minimal Lot stand-in for archive paths (only the key + hero are used)."""
    def __init__(self, asset_id, account_id, auction_id, hero_image_url):
        self.asset_id, self.account_id, self.auction_id = asset_id, account_id, auction_id
        self.hero_image_url = hero_image_url


def archive_active(adapter, *, limit: int = 100, max_mb: float = 200.0,
                   zero_bid_only: bool = False, sleep_s: float = 0.4) -> dict:
    """Backfill archiver: store images for active, not-yet-archived lots before
    their listings expire. Zero-bid lots (the actual buy candidates) first,
    soonest-ending first. Stops at `limit` lots or `max_mb` uploaded bytes."""
    from deals.store import unarchived_active, set_archived_images
    meter = {"bytes": 0, "images": 0, "lots": 0, "empty": 0, "errors": 0}
    for row in unarchived_active(limit=limit, zero_bid_only=zero_bid_only):
        if meter["bytes"] >= max_mb * 1024 * 1024:
            print(f"[archive] stopping: max_mb={max_mb} reached", file=sys.stderr)
            break
        key = (row["asset_id"], row["account_id"], row["auction_id"])
        lot = _RowLot(*key, row["hero_image_url"])
        try:
            gallery = adapter.fetch_gallery(row["asset_id"], row["account_id"])
            stored = archive_lot_images(lot, gallery, meter)
            if stored:
                set_archived_images(key, stored[0], stored[1:])
                meter["lots"] += 1
            else:
                meter["empty"] += 1
        except Exception as e:
            meter["errors"] += 1
            print(f"[archive] error on {key}: {e}", file=sys.stderr)
        if meter["lots"] % 10 == 0 and meter["lots"]:
            print(f"[archive] {meter['lots']} lots, {meter['images']} images, "
                  f"{meter['bytes']/1e6:.1f} MB", file=sys.stderr)
        time.sleep(sleep_s)
    return meter
