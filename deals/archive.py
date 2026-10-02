import os, re, sys, time, hashlib, httpx
from deals.models import Lot
from automation.downloader import DOWNLOAD_HEADERS
from automation import r2_images


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
    # object-key namespace = lot.site (govdeals for every existing object/_RowLot)
    return f"{getattr(lot, 'site', 'govdeals')}/{lot.asset_id}_{lot.account_id}_{lot.auction_id}/{h}{ext}"


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


# Scraped auction photos are the SELLER's images, undisguised — the moat, and
# not ours to republish. They live in the PRIVATE bucket and are served to the
# operator only through the session-walled proxy below (`/api/` is gated by
# auth.PROTECTED_PREFIXES). The stored URL is that relative proxy path, so the
# admin deals tab, DealCard and the operator view of /deals/{…} keep working.
PHOTO_PROXY_PREFIX = "/api/deal-photos/"

# Exactly the shape `_storage_path` mints. The proxy refuses anything else, so
# it can never be used to read other private objects (raw archive, lot archive).
# Foreign sites' synthesized ids are negative (models.synth_ids: account_id =
# -ordinal), so each id segment allows one leading minus. No dots or slashes
# beyond the two separators, so traversal can't match.
PHOTO_KEY_RE = re.compile(r"^[a-z0-9_-]{1,32}/-?\d+_-?\d+_-?\d+/[0-9a-f]{10}\.(?:webp|jpg|png)$")


def photo_proxy_url(path: str, version: str | None = None) -> str:
    url = PHOTO_PROXY_PREFIX + path
    return f"{url}?v={version}" if version else url


def fetch_private_photo(path: str) -> tuple[bytes, str] | None:
    """(bytes, content type) of one archived photo from the private bucket, or
    None when the key is malformed or absent. Raises when R2 / the private
    bucket is not configured (the route turns that into a 503)."""
    if not PHOTO_KEY_RE.match(path or ""):
        return None
    r2 = _r2()
    if not r2:
        raise RuntimeError("R2 is not configured")
    s3, _ = r2
    bucket = r2_images.private_bucket()
    try:
        obj = s3.get_object(Bucket=bucket, Key=path)
    except Exception as e:  # noqa: BLE001 - NoSuchKey and friends
        code = getattr(e, "response", {}).get("Error", {}).get("Code")
        if code in ("NoSuchKey", "404", "NotFound"):
            return None
        raise
    return obj["Body"].read(), _content_type(path)


_R2: dict = {}


def _r2():
    """(s3, cfg) when R2 is usable, else None. Built once — a sweep uploads
    hundreds of images and each boto3 client costs a fresh session. Only a
    working client is cached: a failed init is retried on the next call, so a
    long-lived web process isn't stuck without R2 after one transient error."""
    if "s3" not in _R2:
        cfg = r2_images.env_config()
        if cfg:
            try:
                _R2["s3"], _R2["cfg"] = r2_images.client(cfg), cfg
            except Exception as e:  # noqa: BLE001 - caller decides; never crash a sweep
                print(f"[deals.archive] R2 unavailable: {e}", file=sys.stderr)
    return (_R2["s3"], _R2["cfg"]) if "s3" in _R2 else None


def _upload(path: str, data: bytes) -> str:
    """Upload one image; return its durable admin-proxy URL.

    PRIVATE R2 bucket only (`r2_images.private_bucket()` raises when unset —
    never the public image bucket). With R2 unconfigured this raises: the old
    Supabase fallback minted a PUBLIC URL for the seller's photos, and that
    bucket 402s anyway. Supabase Storage is 402-restricted on the shared free project —
    every URL it ever minted returns Payment Required, not an image — so it
    survives only as a fallback for installs with no R2 credentials. See
    CLAUDE.md "Lot photos": never write a new Supabase Storage URL.
    """
    r2 = _r2()
    if r2:
        s3, _cfg = r2
        bucket = r2_images.private_bucket()
        if not r2_images.put_private_object(s3, bucket=bucket, path=path,
                                            data=data, content_type=_content_type(path)):
            # Deliberately do NOT fall back here. The 402 is on *serving*, so a
            # Supabase write can still succeed — and then set_archived_images()
            # stamps images_archived=true over a URL that will never load, and
            # unarchived_active() (WHERE images_archived IS NOT TRUE) never
            # retries the lot. Raising instead lets the per-lot guards in
            # archive_lot_images()/run_discovery() count an error and retry next
            # run. Keys are content-addressed, so a partial gallery re-uploads
            # idempotently.
            raise RuntimeError(
                f"R2 upload failed for {path!r}; refusing Supabase fallback")
        return photo_proxy_url(path, r2_images.content_version(data))
    raise RuntimeError(
        "R2 is not configured — scraped photos need the private R2 bucket "
        "(no Supabase / public fallback)")


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
    """Minimal Lot stand-in for archive paths (key + hero + site namespace)."""
    def __init__(self, asset_id, account_id, auction_id, hero_image_url, site="govdeals"):
        self.asset_id, self.account_id, self.auction_id = asset_id, account_id, auction_id
        self.hero_image_url = hero_image_url
        self.site = site or "govdeals"


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
        lot = _RowLot(*key, row["hero_image_url"], row.get("site"))
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
