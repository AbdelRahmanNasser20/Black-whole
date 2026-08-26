"""Put one GovDeals lot on every sales channel — or take it off — in one call.

Three channels, one ledger:

    site      https://black-whole.com/listings/<lot_id>   (reads `inventory`)
    business  FB Business Page catalog, /catalog/facebook.csv (reads `inventory`)
    fb        FB Marketplace on the family account (browser: post_fb_listing.py)

`site` and `business` are *derived* from the inventory row — status-gated by
`inventory.PUBLIC_STATUSES` / `CATALOG_FEED_STATUSES`. So "add" means: write a
correct row (status `active_bid`, photos on R2, price, copy) and both surfaces
pick it up on their own. "remove" means `fake_sold_out = true` + a sold status,
which moves the lot to the ALREADY MOVED strip and drops it from the feed. Only
Marketplace needs a browser, and that goes through the already-proven relist
scripts (`post_fb_listing.py` / `fb_my_listings.py` / `fb_mark_sold.py`) with
the plan JSON as the copy of record, so what ships is what was verified.

Lot ids: `gd-<asset>-<account>` (URL order, `/en/asset/<asset>/<account>`),
plus `-<part>` when a mixed lot is split (`gd-53677-357-chairs`,
`gd-53677-357-tables`). Older rows keyed on the bare account id (`27562`) are
found through `govdeals_url` and reused — the bare-id scheme collided (`357`
was Kennesaw; the Augusta lot is also account 357), which is why it stops here.

Pure helpers (`parse_govdeals_url`, `parse_split`, `plan_entry`, …) have no I/O
and are unit-tested; the orchestration functions take a `log` callable and emit
`<<<EVENT>>>` progress lines so the admin Launcher can stream them.
"""
from __future__ import annotations

import csv
import html
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import config  # noqa: F401  (loads .env)
from . import db, inventory, listing_images, lot_images, progress
from .catalog_feed import FEED_COLUMNS, build_feed_rows, state_code

ROOT = Path(__file__).resolve().parents[1]
PLAN_PATH = ROOT / "scripts" / "fb_relist_plan_2026-08-24.json"
CATALOG_CSV = ROOT / "catalog" / "fb_catalog_products.csv"
SCRIPTS = ROOT / "scripts"
SITE_BASE = os.getenv("SITE_BASE_URL", "https://black-whole.com").rstrip("/")

# The Marketplace account. Abdel's own profile is Marketplace-restricted
# (2026-08-06); the family account in chrome_profile_dad is the one that lists.
FB_PROFILE = Path(os.getenv("LISTING_FB_PROFILE")
                  or (Path.home() / ".listing_automation" / "chrome_profile_dad"))

CHANNELS = ("site", "fb", "business")
GOVDEALS_URL_RE = re.compile(r"govdeals\.com/(?:[a-z]{2}/)?asset/(\d+)/(\d+)", re.I)

# The CDN 403s without a browser-shaped request (same as automation/downloader.py).
DOWNLOAD_HEADERS = {
    "Referer": "https://www.govdeals.com/",
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"),
}

Log = Callable[[str], None]


def _print(msg: str) -> None:
    print(msg, flush=True)


# ───────────────────────────── pure helpers ─────────────────────────────

def parse_govdeals_url(url: str) -> tuple[int, int]:
    """`https://www.govdeals.com/en/asset/111/27562` -> (111, 27562) = (asset, account)."""
    m = GOVDEALS_URL_RE.search(url or "")
    if not m:
        raise ValueError(f"not a GovDeals asset URL: {url!r}")
    return int(m.group(1)), int(m.group(2))


def govdeals_url(asset: int, account: int) -> str:
    return f"https://www.govdeals.com/en/asset/{asset}/{account}"


@dataclass
class Part:
    """One sellable piece of a GovDeals lot. A plain lot is a single unnamed part."""
    kind: str = ""            # "" | "chairs" | "tables" | any word
    quantity: int | None = None
    price: float | None = None
    photos: list[int] | None = None   # indexes into the GovDeals gallery; None = all

    @property
    def suffix(self) -> str:
        return f"-{self.kind}" if self.kind else ""


def _parse_photo_idx(spec: str) -> list[int]:
    """'0-1' -> [0, 1]; '2,5' -> [2, 5]; '3' -> [3]."""
    out: list[int] = []
    for chunk in spec.split("+"):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" in chunk:
            a, b = chunk.split("-", 1)
            out.extend(range(int(a), int(b) + 1))
        else:
            out.append(int(chunk))
    return out


def parse_split(spec: str | None) -> list[Part]:
    """`chairs:60:20:2-3,tables:14:65:0-1` -> two Parts.

    Fields per part: kind:quantity:price[:photo-indexes]. Photo indexes are
    `a-b` ranges or `a+b+c` lists into the seller's gallery order. Without a
    spec you get one anonymous Part (quantity/price filled in later).
    """
    if not spec or not spec.strip():
        return [Part()]
    parts: list[Part] = []
    for raw in spec.split(","):
        raw = raw.strip()
        if not raw:
            continue
        bits = raw.split(":")
        if len(bits) < 3:
            raise ValueError(f"split part needs kind:quantity:price — got {raw!r}")
        kind = re.sub(r"[^a-z0-9]", "", bits[0].lower())
        if not kind:
            raise ValueError(f"split part has no kind: {raw!r}")
        photos = _parse_photo_idx(bits[3]) if len(bits) > 3 and bits[3].strip() else None
        parts.append(Part(kind=kind, quantity=int(bits[1]), price=float(bits[2]), photos=photos))
    kinds = [p.kind for p in parts]
    if len(set(kinds)) != len(kinds):
        raise ValueError(f"duplicate part kinds in split: {kinds}")
    return parts


def new_lot_id(asset: int, account: int, part: Part | None = None) -> str:
    return f"gd-{asset}-{account}{(part or Part()).suffix}"


def clean_html(text: str | None) -> str:
    """GovDeals descriptions are HTML fragments; the ledger wants prose."""
    s = re.sub(r"<\s*/(p|div)\s*>", "\n\n", text or "", flags=re.I)
    s = re.sub(r"<\s*(br|/li)\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r" *\n *", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def kind_label(part: Part, fallback: str = "Chairs") -> str:
    """'tables' -> 'Tables'; '' -> fallback."""
    return part.kind.capitalize() if part.kind else fallback


def is_table_kind(kind: str) -> bool:
    return "table" in (kind or "").lower()


def unit_word(row_or_kind) -> str:
    """'chair' or 'table' — what one unit of the lot is called in copy."""
    if isinstance(row_or_kind, dict):
        text = f"{row_or_kind.get('chair_type') or ''} {row_or_kind.get('title') or ''}"
    else:
        text = str(row_or_kind or "")
    return "table" if "table" in text.lower() else "chair"


def default_title(quantity: int | None, part: Part, chair_type: str | None,
                  city: str | None, state: str | None) -> str:
    what = (chair_type or kind_label(part)).strip()
    qty = f"{quantity:,} " if quantity else ""
    where = f" ({city}, {state_code(state)})" if city and state else ""
    return f"{qty}{what}{where}"


def fb_description(*, blurb: str, city: str, state: str, quantity: int | None,
                   lot_id: str, unit: str, profile_id: str | None) -> str:
    """Same shape as every hand-written entry in the relist plan, so buyers'
    messages (which quote the description) and the verifier see one format."""
    st = state_code(state)
    lines = [blurb.strip(), ""]
    lines.append(f"📍 Location: {city}, {st} (local pickup; delivery quotes on request)")
    if quantity:
        lines.append(f"📦 Quantity available: {quantity:,}")
    lines += [
        "",
        "Ideal for churches, banquet halls, community centers, schools, and event venues.",
        "",
        "To get a quote, please reply with:",
        "  1. Quantity needed",
        "  2. Pickup or delivery",
        "  3. Your city / ZIP",
        "",
        f"More photos and full details: {SITE_BASE}/listings/{lot_id}",
    ]
    if profile_id:
        noun = "lots" if unit == "table" else "chair lots"
        lines.append(f"All our {noun}: https://www.facebook.com/marketplace/profile/{profile_id}/")
    lines += ["", f"SKU {lot_id}"]
    return "\n".join(lines)


def plan_entry(*, lot_id: str, title: str, price: float, city: str, state: str,
               zip_code: str | None, quantity: int | None, blurb: str,
               photo_urls: list[str], unit: str, profile_id: str | None,
               notes: str = "", fb_city: str | None = None, fb_state: str | None = None) -> dict:
    """One `listings[]` element for scripts/fb_relist_plan_*.json."""
    is_table = unit == "table"
    entry = {
        "sku": lot_id,
        "kind": "specific",
        "title": title,
        "price": int(price) if float(price).is_integer() else price,
        "category": "Home & Garden > Furniture > " + ("Tables" if is_table else "Chairs"),
        "condition": "Used - Good",
        "city": city,
        "state": state_code(state),
        "zip": zip_code or "",
        "quantity_wording": f"{quantity:,} available" if quantity else "",
        "description": fb_description(blurb=blurb, city=city, state=state, quantity=quantity,
                                      lot_id=lot_id, unit=unit, profile_id=profile_id),
        "site_link": f"{SITE_BASE}/listings/{lot_id}",
        "cover_url": photo_urls[0] if photo_urls else "",
        "photo_urls": list(photo_urls),
        "tags": (["banquet tables", "round tables", "folding tables", "event tables", "bulk", "wholesale"]
                 if is_table else
                 ["banquet chairs", "stacking chairs", "event chairs", "wedding chairs",
                  "church chairs", "bulk", "wholesale"]),
        "fb_listing_url": None,
        "notes": notes,
        "copy_source": "lot_channels.add",
    }
    if fb_city:
        entry["fb_city"] = fb_city
    if fb_state:
        entry["fb_state"] = fb_state
    return entry


# ───────────────────────────── plan JSON ─────────────────────────────

def load_plan(path: Path = PLAN_PATH) -> dict:
    if not path.exists():
        return {"campaign": "lot_channels", "account": {}, "listings": []}
    return json.loads(path.read_text())


def save_plan(plan: dict, path: Path = PLAN_PATH) -> None:
    path.write_text(json.dumps(plan, indent=2, ensure_ascii=False) + "\n")


def upsert_plan_entry(entry: dict, path: Path = PLAN_PATH) -> dict:
    """Insert or refresh the entry for entry['sku']; a live fb_listing_url is kept."""
    plan = load_plan(path)
    listings = plan.setdefault("listings", [])
    for i, cur in enumerate(listings):
        if cur.get("sku") == entry["sku"]:
            merged = {**cur, **entry}
            if cur.get("fb_listing_url") and not entry.get("fb_listing_url"):
                merged["fb_listing_url"] = cur["fb_listing_url"]
            listings[i] = merged
            break
    else:
        entry = {**entry, "order": len(listings) + 1}
        listings.append(entry)
    save_plan(plan, path)
    return plan


def plan_entry_for(lot_id: str, path: Path = PLAN_PATH) -> dict | None:
    for cur in load_plan(path).get("listings", []):
        if cur.get("sku") == lot_id:
            return cur
    return None


def profile_id(path: Path = PLAN_PATH) -> str | None:
    return (load_plan(path).get("account") or {}).get("profile_id")


# ───────────────────────────── ledger ─────────────────────────────

def find_existing_lot(asset: int, account: int, part: Part | None = None) -> dict | None:
    """The row for this GovDeals asset, whichever id scheme it was written under."""
    suffix = (part or Part()).suffix
    candidates = [new_lot_id(asset, account, part)]
    if not suffix:
        candidates.append(str(account))   # legacy bare-account ids (27562, 28505 …)
    row = db.fetch_one(
        "SELECT * FROM inventory WHERE govdeals_url LIKE %s AND lot_id = ANY(%s) LIMIT 1",
        (f"%/asset/{asset}/{account}%", candidates),
    )
    if row:
        return dict(row)
    row = db.fetch_one("SELECT * FROM inventory WHERE lot_id = %s", (candidates[0],))
    return dict(row) if row else None


def fetch_detail(asset: int, account: int) -> dict:
    from deals.adapters.govdeals import GovDealsAdapter
    detail = GovDealsAdapter().fetch_detail(asset, account)
    if not detail or not detail.get("assetId"):
        raise RuntimeError(f"GovDeals returned nothing for asset {asset} / account {account} "
                           "(closed, or the ids are swapped — the URL is /asset/<asset>/<account>)")
    return detail


def gallery_urls(detail: dict) -> list[str]:
    from deals.mapping import photo_paths_to_urls
    return photo_paths_to_urls(detail.get("assetPhotos") or [])


def quantity_from_detail(detail: dict) -> int | None:
    """GovDeals sells the whole lot as qty 1; the real headcount is in the words."""
    text = f"{detail.get('assetShortDesc') or ''} {clean_html(detail.get('assetLongDesc'))}"
    m = re.search(r"(?:lot of|approximately)\s*(\d{1,5})\b|\(\s*(\d{1,5})\s*\)", text, re.I)
    if m:
        return int(m.group(1) or m.group(2))
    m = re.search(r"\b(\d{2,5})\s+(?:banquet|stack|padded|folding|wood|metal|plastic|chairs?|tables?)", text, re.I)
    return int(m.group(1)) if m else None


def mirror_photos(lot_id: str, urls: list[str], log: Log = _print, *,
                  dewatermark: bool = True) -> dict | None:
    """Seller photos -> dewatermark.ai -> R2 under our key contract; stamps hero/gallery.

    Same three-layer cache + budget as run.py's phase 3 (`automation.dewatermark`):
    a hash already cleaned anywhere on this machine never hits the API again.
    Seller photos carry the tiled www.govdeals.com watermark, so shipping them
    raw is never right — `dewatermark=False` exists for tests only.
    """
    import asyncio
    import httpx
    if not urls:
        return None
    # The lot folder lives under SCRATCH_DIR (not a throwaway tmp) so the
    # dewatermark sidecar + _originals/ persist and re-runs are free.
    folder = Path(config.SCRATCH_DIR) / "lot_channels" / listing_images.key_base(lot_id)
    folder.mkdir(parents=True, exist_ok=True)
    files: list[Path] = []
    with httpx.Client(timeout=60.0, follow_redirects=True, headers=DOWNLOAD_HEADERS) as client:
        for i, url in enumerate(urls):
            ext = listing_images.guess_ext(url.split("?")[0])
            target = folder / f"{i:02d}.{ext}"
            try:
                resp = client.get(url)
                resp.raise_for_status()
            except Exception as exc:  # noqa: BLE001 — one bad photo isn't fatal
                log(f"  ! photo download failed ({type(exc).__name__}): {url[:90]}")
                continue
            if resp.content:
                target.write_bytes(resp.content)
                files.append(target)
    if not files:
        return None
    if dewatermark:
        from . import dewatermark as dw
        _phase("dewatermark", "running", lot_id=lot_id)
        cleaned = asyncio.run(dw.dewatermark(None, files, folder, lot_label=lot_id))
        dirty = [c for c in cleaned if c.parent.name == "_originals"]
        if dirty:
            log(f"  ! {len(dirty)}/{len(files)} photos still watermarked (API failed) — kept originals")
        _phase("dewatermark", "done", cleaned=len(cleaned) - len(dirty), files=len(files))
        log(f"  ✓ dewatermarked {len(cleaned) - len(dirty)}/{len(files)} via dewatermark.ai")
        files = cleaned or files
    result = listing_images.upload_lot_images(lot_id, files)
    if result:
        inventory.set_images(lot_id, result["hero_image_url"], result["image_urls"])
    return result


def redo_photos(lot_id: str, log: Log = _print) -> dict | None:
    """Re-mirror a lot's photos from GovDeals through dewatermark.ai and overwrite
    the R2 keys in place (site + catalog + plan pick the new bytes up; the URLs
    keep their shape, `?v=` cache-busters change)."""
    row = inventory.get(lot_id)
    if not row:
        raise ValueError(f"no inventory row {lot_id!r}")
    m = re.search(r"/asset/(\d+)/(\d+)", row.get("govdeals_url") or "")
    if not m:
        raise ValueError(f"{lot_id}: no govdeals_url on the row — can't refetch the seller photos")
    asset, account = int(m.group(1)), int(m.group(2))
    detail = fetch_detail(asset, account)
    urls = gallery_urls(detail)
    # honour a split part's photo subset by matching the count we already hold
    entry = plan_entry_for(lot_id) or {}
    part_idx = entry.get("photo_indexes")
    if part_idx:
        urls = [urls[i] for i in part_idx if i < len(urls)]
    log(f"{lot_id}: {len(urls)} seller photos → dewatermark → R2")
    up = mirror_photos(lot_id, urls, log=log)
    if up:
        row = inventory.get(lot_id) or row
        fresh = lot_images.resolve(row).urls
        cur = plan_entry_for(lot_id)
        if cur:
            # Keep a hand-picked photo order (cover first) when the set is the
            # same — only the bytes and the ?v= cache-busters changed.
            key = lambda u: u.split("?")[0]  # noqa: E731
            by_key = {key(u): u for u in fresh}
            ordered = [by_key[key(u)] for u in cur.get("photo_urls") or [] if key(u) in by_key]
            ordered += [u for u in fresh if u not in ordered]
            cur["photo_urls"] = ordered
            cur["cover_url"] = ordered[0] if ordered else ""
            upsert_plan_entry(cur)
    return up


# ───────────────────────────── add ─────────────────────────────

@dataclass
class AddResult:
    lot_id: str
    row: dict
    created: bool
    photos: int = 0
    fb_url: str | None = None
    fb_error: str | None = None
    channels: list[str] = field(default_factory=list)


def _phase(name: str, status: str, **extra) -> None:
    progress.emit("phase", phase=name, status=status, **extra)


def add_lot(url: str, *, price: float | None = None, split: str | None = None,
            title: str | None = None, blurb: str | None = None, chair_type: str | None = None,
            quantity: int | None = None, channels: tuple[str, ...] = CHANNELS,
            publish: bool = True, fb_city: str | None = None, fb_state: str | None = None,
            log: Log = _print) -> list[AddResult]:
    """Put a GovDeals lot on the requested channels. Returns one result per part."""
    asset, account = parse_govdeals_url(url)
    parts = parse_split(split)
    if len(parts) > 1 and (title or blurb or chair_type):
        raise ValueError("--title/--blurb/--chair-type apply to a single lot; "
                         "for a split, set per-part copy afterwards with `copy`")

    _phase("scrape", "running", url=url)
    detail = fetch_detail(asset, account)
    urls = gallery_urls(detail)
    city = (detail.get("city") or "").strip()
    state = (detail.get("state") or "").strip()
    zip_code = (detail.get("zipCode") or "").split("-")[0].strip() or None
    ends = detail.get("assetAuctionEndDate")
    short = html.unescape(detail.get("assetShortDesc") or "").strip()
    long_text = clean_html(detail.get("assetLongDesc"))
    detected_qty = quantity_from_detail(detail)
    _phase("scrape", "done", images=len(urls), city=city, state=state, ends=ends)
    log(f"GovDeals asset {asset} / account {account}: {short!r} — {city}, {state} — "
        f"{len(urls)} photos — closes {ends}")
    for skipped in ("llm", "dewatermark", "ebay"):
        _phase(skipped, "skipped")

    pid = profile_id()
    results: list[AddResult] = []
    for part in parts:
        lot_id_existing = find_existing_lot(asset, account, part)
        lot_id = lot_id_existing["lot_id"] if lot_id_existing else new_lot_id(asset, account, part)
        qty = part.quantity or quantity or detected_qty
        unit_price = part.price if part.price is not None else price
        what = chair_type or (kind_label(part) if part.kind else None)
        unit = "table" if is_table_kind(part.kind or what or "") else "chair"
        part_urls = [urls[i] for i in part.photos if i < len(urls)] if part.photos else urls

        row = lot_id_existing
        created = False
        if row is None:
            if unit_price is None:
                raise ValueError(f"{lot_id}: no price — pass --price (or a price in --split)")
            row = inventory.insert_manual(
                lot_id=lot_id,
                title=title or default_title(qty, part, what, city, state),
                quantity=qty or 0,
                price_per_chair=unit_price,
                city=city, state=state, zip_code=zip_code,
                chair_type=what,
                description=blurb or (long_text if not part.kind else short),
                status="active_bid",
                govdeals_url=govdeals_url(asset, account),
            )
            created = True
            log(f"  + ledger row {lot_id} (active_bid, ${unit_price}/{unit}, qty {qty})")
        else:
            # Never clobber hand-written copy on a lot that already exists; only
            # make it visible again and fill the blanks.
            fields: dict = {}
            if row.get("status") in ("lost", "hidden", "draft") or row.get("fake_sold_out"):
                fields["status"] = "active_bid"
                fields["fake_sold_out"] = False
            if title:
                fields["title"] = title
            if blurb:
                fields["description"] = blurb
            if unit_price is not None and row.get("price_per_chair") is None:
                fields["price_per_chair"] = unit_price
            if qty and not (row.get("quantity_remaining") or 0):
                fields["quantity_remaining"] = qty
            if not row.get("zip_code") and zip_code:
                fields["zip_code"] = zip_code
            if fields:
                row = inventory.set_fields(lot_id, **fields) or row
            if not row.get("govdeals_url"):
                db.execute("UPDATE inventory SET govdeals_url = %s WHERE lot_id = %s",
                           (govdeals_url(asset, account), lot_id))
            log(f"  = ledger row {lot_id} exists (status {row.get('status')}) — kept its copy")
        # CRM gate lives in a CRM-owned column; the bot may offer what the site shows.
        db.execute("UPDATE inventory SET crm_offerable = true WHERE lot_id = %s", (lot_id,))

        res = AddResult(lot_id=lot_id, row=row, created=created)

        # photos → R2 (idempotent; a lot that already has durable photos is left alone)
        _phase("download", "running", lot_id=lot_id)
        have = lot_images.resolve(row).urls if row else []
        if have and not part.photos:
            res.photos = len(have)
            log(f"  = {len(have)} durable photos already on R2")
        else:
            up = mirror_photos(lot_id, part_urls, log=log)
            res.photos = len(up["image_urls"]) if up else 0
            log(f"  ↑ {res.photos} photos → R2" if up else "  ! no photos uploaded")
        _phase("download", "done", files=res.photos)

        row = inventory.get(lot_id) or row
        res.row = row
        photo_urls = lot_images.resolve(row).urls

        # FB copy of record
        entry = plan_entry(
            lot_id=lot_id, title=row.get("title") or "", price=float(row.get("price_per_chair") or 0),
            city=row.get("city") or city, state=row.get("state") or state,
            zip_code=row.get("zip_code") or zip_code,
            quantity=row.get("quantity_remaining") or qty, blurb=row.get("description") or short,
            photo_urls=photo_urls, unit=unit, profile_id=pid,
            notes=f"GovDeals asset {asset} / acct {account}, auction closes {ends}. "
                  f"Ledger status active_bid — not owned yet.",
            fb_city=fb_city, fb_state=fb_state,
        )
        if part.photos:
            entry["photo_indexes"] = list(part.photos)
        prior = plan_entry_for(lot_id) or {}
        if prior.get("fb_listing_url"):
            # Live on Marketplace already: the plan's copy is what buyers see;
            # don't regenerate under it. `copy` is the explicit way to change it.
            log("  = FB plan entry kept (listing is live)")
        else:
            upsert_plan_entry(entry)
        res.channels = [c for c in ("site", "business") if c in channels]

        if "fb" in channels:
            existing_fb = (plan_entry_for(lot_id) or {}).get("fb_listing_url") or row.get("facebook_url")
            if existing_fb and "marketplace/item/" in existing_fb:
                res.fb_url = existing_fb
                res.channels.append("fb")
                _phase("facebook", "done", status_detail="already_live", url=existing_fb)
                log(f"  = already on Marketplace: {existing_fb}")
            elif not publish:
                _phase("facebook", "skipped")
                log("  · Marketplace post skipped (--no-publish); plan entry written")
            else:
                _phase("facebook", "running", lot_id=lot_id)
                url_fb, err = post_to_facebook(lot_id, log=log)
                if url_fb:
                    res.fb_url = url_fb
                    res.channels.append("fb")
                    _phase("facebook", "done", url=url_fb)
                else:
                    res.fb_error = err
                    _phase("facebook", "error", error=err)
        else:
            _phase("facebook", "skipped")
        results.append(res)
    return results


def post_to_facebook(lot_id: str, *, log: Log = _print, spacing_s: int = 0) -> tuple[str | None, str | None]:
    """Publish the plan entry for `lot_id` on the family account and stamp the URL.

    One attempt. On failure the manual path is printed and (None, error) returned —
    a browser step that needs a second round is not worth automating further.
    """
    if not FB_PROFILE.exists():
        msg = f"no Chrome profile at {FB_PROFILE} — run scripts/fb_check_account.py there first"
        log(f"  ! {msg}")
        return None, msg
    env = {**os.environ, "LISTING_CHROME_PROFILE": str(FB_PROFILE)}
    py = sys.executable
    if spacing_s:
        log(f"  … waiting {spacing_s}s before posting (Marketplace pacing)")
        time.sleep(spacing_s)
    cmd = [py, str(SCRIPTS / "post_fb_listing.py"), "--sku", lot_id, "--publish", "--keep-open", "5"]
    log(f"  $ {' '.join(cmd[1:])}")
    proc = subprocess.run(cmd, env=env, cwd=str(ROOT), text=True, capture_output=True)
    for line in (proc.stdout or "").splitlines():
        log(f"    {line}")
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()[-3:]
        err = f"post_fb_listing exit {proc.returncode}: {' | '.join(tail)}"
        log(f"  ! {err}")
        log("  manual path: open https://www.facebook.com/marketplace/create/item on the "
            f"family account and paste the copy from {PLAN_PATH.name} (sku {lot_id})")
        return None, err
    # post_fb_listing reads the new item id back off the profile itself; only
    # fall back to the full profile scrape if that came up empty.
    url = (plan_entry_for(lot_id) or {}).get("fb_listing_url")
    if not url:
        proc = subprocess.run([py, str(SCRIPTS / "fb_my_listings.py"), "--write"], env=env,
                              cwd=str(ROOT), text=True, capture_output=True)
        for line in (proc.stdout or "").splitlines()[-6:]:
            log(f"    {line}")
        url = (plan_entry_for(lot_id) or {}).get("fb_listing_url")
    if not url:
        err = "published, but the item id could not be read back — run fb_my_listings.py --write later"
        log(f"  ! {err}")
        return None, err
    inventory.set_platform_url(lot_id, "facebook", url)
    log(f"  ✓ Marketplace: {url}")
    return url, None


# ───────────────────────────── remove ─────────────────────────────

@dataclass
class RemoveResult:
    lot_id: str
    status: str
    fb_marked_sold: bool | None = None
    fb_error: str | None = None


def sold_status_for(row: dict) -> str:
    """Owned stock reads 'sold_out'; a lot we never picked up reads 'lost_sold_out'."""
    return "sold_out" if row.get("status") in ("owned", "won_pickup", "listed", "sold_out") else "lost_sold_out"


def remove_lot(lot_id: str, *, channels: tuple[str, ...] = CHANNELS, log: Log = _print) -> RemoveResult:
    """Take a lot off every channel as 'moved': fake sold-out on the site + feed,
    Mark-as-sold on Marketplace. The row and its photos stay (ALREADY MOVED strip)."""
    row = inventory.get(lot_id)
    if not row:
        raise ValueError(f"no inventory row {lot_id!r}")
    status = sold_status_for(row)
    if "site" in channels or "business" in channels:
        inventory.set_fields(lot_id, status=status, fake_sold_out=True)
        db.execute("UPDATE inventory SET crm_offerable = false WHERE lot_id = %s", (lot_id,))
        log(f"  ✓ {lot_id}: status {status}, fake_sold_out=true → site shows SOLD, feed drops it, CRM won't offer it")
    res = RemoveResult(lot_id=lot_id, status=status)
    if "fb" in channels:
        entry = plan_entry_for(lot_id) or {}
        fb_url = entry.get("fb_listing_url") or row.get("facebook_url") or ""
        if "marketplace/item/" not in fb_url:
            log("  · no live Marketplace item recorded for this lot — nothing to mark sold")
        else:
            ok, err = mark_sold_on_facebook(lot_id, log=log)
            res.fb_marked_sold, res.fb_error = ok, err
    return res


def mark_sold_on_facebook(lot_id: str, *, log: Log = _print) -> tuple[bool, str | None]:
    if not FB_PROFILE.exists():
        msg = f"no Chrome profile at {FB_PROFILE}"
        log(f"  ! {msg}")
        return False, msg
    env = {**os.environ, "LISTING_CHROME_PROFILE": str(FB_PROFILE)}
    cmd = [sys.executable, str(SCRIPTS / "fb_mark_sold.py"), "--sku", lot_id, "--confirm"]
    log(f"  $ {' '.join(cmd[1:])}")
    proc = subprocess.run(cmd, env=env, cwd=str(ROOT), text=True, capture_output=True)
    for line in (proc.stdout or "").splitlines():
        log(f"    {line}")
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()[-3:]
        err = f"fb_mark_sold exit {proc.returncode}: {' | '.join(tail)}"
        log(f"  ! {err}")
        entry = plan_entry_for(lot_id) or {}
        log(f"  manual path: open {entry.get('fb_listing_url') or 'the listing'} on the family "
            "account → Mark as sold")
        return False, err
    plan = load_plan()
    for cur in plan.get("listings", []):
        if cur.get("sku") == lot_id:
            cur["fb_status"] = "sold"
            cur["fb_marked_sold_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    save_plan(plan)
    return True, None


def restore_lot(lot_id: str, status: str = "active_bid", log: Log = _print) -> dict:
    """Undo `remove` on the ledger side (Marketplace has to be re-listed by hand —
    Renew / Delete & Relist are the spam-filter triggers, so we never automate them)."""
    row = inventory.set_fields(lot_id, status=status, fake_sold_out=False)
    if not row:
        raise ValueError(f"no inventory row {lot_id!r}")
    db.execute("UPDATE inventory SET crm_offerable = true WHERE lot_id = %s", (lot_id,))
    log(f"  ✓ {lot_id}: status {status}, fake_sold_out=false")
    return row


# ───────────────────────────── business catalog ─────────────────────────────

def write_catalog_csv(path: Path = CATALOG_CSV) -> tuple[Path, int]:
    """The exact rows the live feed serves, as a file for a manual Commerce
    Manager upload (Catalog → Data sources → Upload file)."""
    rows = build_feed_rows(inventory.list_catalog_feed())
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=FEED_COLUMNS)
        w.writeheader()
        for r in rows:
            w.writerow(r)
    return path, len(rows)


# ───────────────────────────── status ─────────────────────────────

def channel_matrix(lot_ids: list[str] | None = None) -> list[dict]:
    """Per lot: is it on the site, in the catalog feed, on Marketplace."""
    rows = inventory.list_all()
    if lot_ids:
        rows = [r for r in rows if r["lot_id"] in set(lot_ids)]
    public = {r["lot_id"] for r in inventory.list_public()}
    feed = {r["id"] for r in build_feed_rows(inventory.list_catalog_feed())}
    plan = {l.get("sku"): l for l in load_plan().get("listings", [])}
    out = []
    for r in rows:
        entry = plan.get(r["lot_id"]) or {}
        fb = entry.get("fb_listing_url") or r.get("facebook_url") or ""
        out.append({
            "lot_id": r["lot_id"],
            "title": r.get("title"),
            "status": r.get("status"),
            "qty": r.get("quantity_remaining"),
            "site": r["lot_id"] in public,
            "sold_strip": bool(r.get("fake_sold_out")) or r.get("status") in inventory.SOLD_STATUSES,
            "business": r["lot_id"] in feed,
            "fb": ("sold" if entry.get("fb_status") == "sold"
                   else ("live" if "marketplace/item/" in fb else "")),
            "fb_url": fb if "marketplace/item/" in fb else "",
        })
    return out
