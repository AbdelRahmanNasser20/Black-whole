import os, hashlib, httpx
from deals.models import Lot
from automation.downloader import DOWNLOAD_HEADERS

BUCKET = "listing-images"

def _storage_path(lot: Lot, idx: int, url: str) -> str:
    ext = ".jpg"
    for cand in (".jpg", ".jpeg", ".png", ".webp"):
        if cand in url.lower():
            ext = ".jpg" if cand == ".jpeg" else cand
            break
    h = hashlib.sha256(url.encode()).hexdigest()[:10]
    return f"govdeals/{lot.asset_id}_{lot.account_id}_{lot.auction_id}/{idx:02d}_{h}{ext}"

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
               headers={"Authorization": f"Bearer {key}", "content-type": "image/jpeg",
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
