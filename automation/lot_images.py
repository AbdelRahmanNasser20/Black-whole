"""One resolver for "where are this lot's photos?" (BLACKWHOLE-31).

Every surface that shows a chair photo used to re-derive the answer itself: the
public site had `_hero_src` / `_gallery_srcs`, the CRM had `resolve_image_paths`
/ `image_urls_for` / `sendable_image_paths`, and scripts hand-joined
`DOWNLOAD_ROOT / folder_name`. They disagreed in the one case that mattered —
**a host that isn't the operator's laptop**. The remote poller resolved photos
off `folder_path`, that directory doesn't exist on the server, so it sent
nothing. That cost five buyers on lot 31225 alone (~945 chairs).

The rule this module encodes, once:

    durable public URLs from the DB  →  local disk  →  nothing

Never the other way around, and never an assumption about which host is
running. A lot whose photos are in object storage resolves identically from the
laptop, the Render web service, and the poller.

Design constraints (why this file has no imports you'd expect):

* **Stdlib only.** No psycopg, no httpx, no `automation.config` at import time.
  The CRM repo deploys without this repo's dependency tree, so the module has
  to be importable anywhere — including vendored.
* **DB access is injected.** Callers that already hold a row pass it in; callers
  that only have a lot id pass their own `fetch_row` (this repo's
  `automation.db.fetch_one`, the CRM's `db.pool.fetch_one`, whatever). No
  connection is opened here.

Storage backend note: object keys are written as hero `<key>.<ext>` plus gallery
`<key>/NN.<ext>` (see `listing_images` / `r2_images`). File 0 is uploaded under
*both* keys, so hero and gallery[0] are the same photo at two URLs — hence
`gallery else [hero]` below rather than "hero + gallery", which double-attached
the first photo on every send.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

# Photo folders the pipeline creates but must never publish: `_originals/` are
# the pre-dewatermark files, `_screenshots/` are debug captures of the GovDeals
# page. Only top-level files in a lot folder are real listing photos.
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".heic"}

# Fallback root for rows whose `folder_path` is NULL (hand-added `owned` lots
# carry only `folder_name`). Overridable so a non-macOS host isn't stuck with a
# hardcoded Desktop path.
DEFAULT_PICTURES_DIR = os.environ.get(
    "BLACKWHOLE_PICTURES_DIR",
    os.path.expanduser("~/Desktop/Banquet chiars Pictures"),
)

# Columns a row needs for a full resolve. Exported so callers can widen a SELECT
# instead of guessing (a missing column silently degrades to "no photos").
ROW_COLUMNS = (
    "lot_id",
    "hero_image_url",
    "image_urls",
    "folder_path",
    "folder_name",
    "hero_image",
)

_SELECT = f"SELECT {', '.join(ROW_COLUMNS)} FROM inventory WHERE lot_id = %(lot_id)s"


@dataclass(frozen=True)
class LotImages:
    """Resolved photos for one lot.

    Two different questions, two different answers, which is why `hero` isn't
    just `urls[0]`:

    * `urls` — the **set** of photos, deduped. What you attach to a message or
      render as a gallery. Never contains the same photo twice.
    * `hero` — the **cover**. `hero_image_url` is a dedicated column the
      operator controls, so it wins here even though its object is a duplicate
      of `urls[0]` in the set sense.

    `local_paths` only has entries on a host that actually holds the folder, and
    exists for callers that need real files (Messenger stages uploads from disk).
    """

    lot_id: str | None = None
    urls: list[str] = field(default_factory=list)
    hero: str | None = None
    local_paths: list[str] = field(default_factory=list)
    source: str = "none"  # "db" | "local" | "none"

    @property
    def count(self) -> int:
        return len(self.urls) or len(self.local_paths)

    def __bool__(self) -> bool:
        return bool(self.urls or self.local_paths)


def _clean_urls(value: object) -> list[str]:
    """Normalize a JSONB `image_urls` value into a deduped list of http URLs."""
    if not isinstance(value, (list, tuple)):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in value:
        url = str(item or "").strip()
        if url.startswith("http") and url not in seen:
            seen.add(url)
            out.append(url)
    return out


def lot_folder(row: dict) -> str | None:
    """The lot's on-disk folder, or None when this host doesn't have it.

    `folder_path` is authoritative (it survives the folder being moved between
    `_active/` and `_lost/`); `folder_name` under the pictures root is the
    fallback for rows that never went through the pipeline.
    """
    path = (row.get("folder_path") or "").strip()
    if path and os.path.isdir(path):
        return path
    name = (row.get("folder_name") or "").strip()
    if name:
        candidate = os.path.join(DEFAULT_PICTURES_DIR, name)
        if os.path.isdir(candidate):
            return candidate
    return None


def local_image_paths(row: dict) -> list[str]:
    """Absolute paths to a lot's top-level photos, hero first then alphabetical.

    Subdirectories are skipped by construction, which is what keeps `_originals`
    and `_screenshots` out of every listing.
    """
    folder = lot_folder(row)
    if not folder:
        return []
    try:
        names = os.listdir(folder)
    except OSError:
        return []
    files = [
        n
        for n in names
        if not n.startswith(".")
        and os.path.splitext(n)[1].lower() in IMAGE_EXTS
        and os.path.isfile(os.path.join(folder, n))
    ]
    hero = (row.get("hero_image") or "").strip()
    files.sort(key=lambda n: (n != hero, n.lower()))
    return [os.path.join(folder, n) for n in files]


def resolve(row: dict) -> LotImages:
    """Resolve one already-fetched inventory row. The core of this module.

    The gallery (`image_urls`) is the set; the hero is only folded into it when
    the gallery is empty. File 0 is uploaded under both the hero key and
    `<key>/00.<ext>`, so unioning them would attach the same photo twice.
    """
    row = row or {}
    lot_id = row.get("lot_id")
    lot_id = str(lot_id) if lot_id is not None else None

    hero = str(row.get("hero_image_url") or "").strip()
    hero = hero if hero.startswith("http") else None

    urls = _clean_urls(row.get("image_urls"))
    if not urls and hero:
        urls = [hero]

    paths = local_image_paths(row)
    if urls:
        return LotImages(lot_id=lot_id, urls=urls, hero=hero or urls[0],
                         local_paths=paths, source="db")
    if paths:
        return LotImages(lot_id=lot_id, local_paths=paths, source="local")
    return LotImages(lot_id=lot_id)


def resolve_lot(lot_id: str | None, *, fetch_row=None) -> LotImages:
    """Resolve by lot id, reading the row through `fetch_row`.

    `fetch_row(sql, params) -> dict | None`. Defaults to this repo's
    `automation.db.fetch_one`; the CRM passes `db.pool.fetch_one`. Any DB error
    degrades to an empty result — a photo lookup must never break a send.
    """
    if not lot_id:
        return LotImages()
    if fetch_row is None:
        try:
            from automation.db import fetch_one as fetch_row  # type: ignore
        except Exception:
            return LotImages(lot_id=str(lot_id))
    try:
        row = fetch_row(_SELECT, {"lot_id": str(lot_id)})
    except Exception:
        return LotImages(lot_id=str(lot_id))
    if not row:
        return LotImages(lot_id=str(lot_id))
    row = dict(row)
    row.setdefault("lot_id", str(lot_id))
    return resolve(row)


def image_urls(row_or_lot_id, *, fetch_row=None) -> list[str]:
    """Host-independent photo URLs for a lot. Accepts a row or a lot id."""
    if isinstance(row_or_lot_id, dict):
        return resolve(row_or_lot_id).urls
    return resolve_lot(row_or_lot_id, fetch_row=fetch_row).urls


def has_usable_images(row_or_lot_id, *, fetch_row=None) -> bool:
    """True when the lot has at least one URL any host can fetch.

    Deliberately ignores local disk: this answers "can the bot show a buyer a
    photo", and the bot doesn't run on the laptop.
    """
    return bool(image_urls(row_or_lot_id, fetch_row=fetch_row))


def hero_src(row: dict, *, local_route: str = "/image") -> str | None:
    """Cover image for a web template: durable URL, else the local serving route.

    The `/image/<folder>/<name>` fallback only renders where the folder exists,
    which is exactly the dev-laptop case it's there for.
    """
    resolved = resolve(row)
    if resolved.hero:
        return resolved.hero
    folder = (row.get("folder_name") or "").strip()
    hero = (row.get("hero_image") or "").strip()
    if folder and hero:
        return f"{local_route.rstrip('/')}/{folder}/{hero}"
    if folder and resolved.local_paths:
        return f"{local_route.rstrip('/')}/{folder}/{os.path.basename(resolved.local_paths[0])}"
    return None


def gallery_srcs(row: dict, *, local_route: str = "/image") -> list[str]:
    """Ordered gallery for a web template. Same precedence as `hero_src`."""
    resolved = resolve(row)
    if resolved.urls:
        return resolved.urls
    folder = (row.get("folder_name") or "").strip()
    if folder and resolved.local_paths:
        return [
            f"{local_route.rstrip('/')}/{folder}/{os.path.basename(p)}"
            for p in resolved.local_paths
        ]
    return []


def storage_backend(url: str | None) -> str:
    """Which backend a URL points at: `r2` | `supabase` | `other` | `none`.

    Supabase Storage on this project is egress-restricted (HTTP 402 on every
    public object), so a `supabase` answer means the photo is *dead*, not merely
    elsewhere. The audit script keys off this.
    """
    u = (url or "").strip()
    if not u:
        return "none"
    if re.search(r"\.r2\.dev/|r2\.cloudflarestorage\.com/", u):
        return "r2"
    if "supabase.co/storage/" in u:
        return "supabase"
    return "other"
