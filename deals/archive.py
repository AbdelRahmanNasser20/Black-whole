import os, re, hashlib, httpx
from deals.models import Lot
from automation.downloader import DOWNLOAD_HEADERS

BUCKET = "listing-images"

_EXT_RE = re.compile(r'\.(jpe?g|png|webp)(?:\?|$)', re.I)
_CONTENT_TYPES = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp"}

def _content_type(path: str) -> str:
    return _CONTENT_TYPES.get(path.rsplit(".", 1)[-1].lower(), "image/jpeg")

def _storage_path(lot: Lot, idx: int, url: str) -> str:
    # idx retained for call-site signature compatibility; the path is
    # content-addressed by the URL hash so the same image always resolves to the
    # same object key regardless of scrape ordering (idempotent re-archiving).
    m = _EXT_RE.search(url)
    ext = ".jpg"
    if m:
        e = m.group(1).lower()
        ext = ".jpg" if e == "jpeg" else "." + e
    h = hashlib.sha256(url.encode()).hexdigest()[:10]
    return f"govdeals/{lot.asset_id}_{lot.account_id}_{lot.auction_id}/{h}{ext}"

def _download(url: str) -> bytes | None:
    try:
        r = httpx.get(url, headers=DOWNLOAD_HEADERS, timeout=30, follow_redirects=True)
        r.raise_for_status()
        return r.content
    except Exception:
        return None

def _upload(path: str, data: bytes) -> str:
    """Upload to Supabase storage; return public URL. Uses SUPABASE_STORAGE_* env."""
    base = os.environ["SUPABASE_STORAGE_URL"].rstrip("/")
    key = os.environ["SUPABASE_STORAGE_KEY"]
    httpx.post(f"{base}/object/{BUCKET}/{path}", content=data,
               headers={"Authorization": f"Bearer {key}", "content-type": _content_type(path),
                        "x-upsert": "true"}, timeout=60).raise_for_status()
    return f"{base}/object/public/{BUCKET}/{path}"

def archive_lot_images(lot: Lot, gallery: list[str]) -> list[str]:
    urls = list(dict.fromkeys([u for u in ([lot.hero_image_url] + gallery) if u]))
    stored = []
    for idx, url in enumerate(urls):
        data = _download(url)
        if data:
            stored.append(_upload(_storage_path(lot, idx, url), data))
    return stored
