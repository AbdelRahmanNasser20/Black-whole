"""Public URLs for lots and cities — the ONE place a storefront path is built.

Why (SEO city pages + clean URLs, 2026-10-07): lot pages lived at
`/listings/gd-{asset}-{account}` — a GovDeals id in the URL, the sitemap, the
feeds and the Product JSON-LD `sku`. Google reads it as an opaque code, and
anyone can paste it back into GovDeals to find the auction (the biggest open
leak after the image disguise). A lot now gets a descriptive slug
(`2500-wire-frame-stacking-chairs-pittsburgh-pa`) stored in `inventory.slug`
(migration 023). The id route still works and 301s to the slug, so every
Facebook post, feed row and short link ever sent keeps resolving.

Rules:
- `public_path(row)` is the only way to build a lot URL. Templates use the
  `lot_url` Jinja global, feeds and the map call it directly.
- A row without a slug (column missing, backfill not run, brand-new lot)
  falls back to `/listings/{lot_id}` — nothing here can break a page.
- Slugs never change once set (URLs are promises); `make_slug` is only for
  rows that have none.
- City pages: `/chairs/{city-slug}` from `city_slug(city, state)`.
"""

from __future__ import annotations

import re
import unicodedata
from urllib.parse import quote

SLUG_MAX = 80
_NON_SLUG = re.compile(r"[^a-z0-9]+")
_LEADING_COUNT = re.compile(r"^\s*(?:lot\s+of\s+)?~?\s*\d[\d,]*\s*(?:×|x)?\s+", re.I)
_TRAILING_PAREN = re.compile(r"\s*\([^()]*\)\s*$")


def slugify(text: str, *, max_len: int = SLUG_MAX) -> str:
    """ASCII, lowercase, hyphen-separated, trimmed at a hyphen boundary."""
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    text = _NON_SLUG.sub("-", text.lower()).strip("-")
    if len(text) > max_len:
        text = text[:max_len].rsplit("-", 1)[0] if "-" in text[:max_len] else text[:max_len]
    return text.strip("-")


def _short_title(title: str) -> str:
    t = _TRAILING_PAREN.sub("", (title or "").strip())
    t = _LEADING_COUNT.sub("", t)
    # Keep the product words, drop the descriptor tail after the first dash.
    t = re.split(r"\s+[—–|·]\s+|\s+-\s+", t, maxsplit=1)[0]
    return t.strip(" ,;:") or (title or "").strip()


def make_slug(row: dict) -> str:
    """`{qty}-{product words}-{city}-{state}` for a row that has no slug yet.
    Collision handling (a 4-char suffix) is the caller's job — it needs the DB."""
    qty = row.get("quantity_original") or row.get("quantity_remaining")
    bits: list[str] = []
    try:
        if qty and int(qty) > 0:
            bits.append(str(int(qty)))
    except (TypeError, ValueError):
        pass
    bits.append(_short_title(row.get("title") or "") or "chair-lot")
    for key in ("city", "state"):
        v = (row.get(key) or "").strip()
        if v:
            bits.append(v)
    return slugify(" ".join(bits)) or slugify(str(row.get("lot_id") or "lot"))


def next_free_slug(base: str, lot_id: str, is_taken) -> str | None:
    """The slug to store for a row whose natural slug is `base`: `base` if
    free, else `base-xxxx` (4 hex chars from the lot id), else `base-xxxx-N`.
    `base` is shortened first so the suffix always survives SLUG_MAX — the
    one collision scheme shared by inventory.assign_slug and
    scripts/backfill_slugs.py. `is_taken(slug) -> bool` is the caller's."""
    import hashlib

    base = slugify(base) or "lot"
    if not is_taken(base):
        return base
    suffix = hashlib.sha1(str(lot_id).encode()).hexdigest()[:4]
    for n in range(0, 50):
        tail = f"-{suffix}" if n == 0 else f"-{suffix}-{n + 1}"
        head = slugify(base, max_len=SLUG_MAX - len(tail))
        candidate = f"{head}{tail}"
        if not is_taken(candidate):
            return candidate
    return None


def public_path(row: dict) -> str:
    """Site-relative lot URL: the slug when the row has one, else the id."""
    slug = (row or {}).get("slug")
    key = slug if isinstance(slug, str) and slug.strip() else str((row or {}).get("lot_id") or "")
    return f"/listings/{quote(key, safe='')}"


def city_slug(city: str | None, state: str | None) -> str:
    return slugify(" ".join(p for p in ((city or "").strip(), (state or "").strip()) if p))


def city_path(city: str | None, state: str | None) -> str:
    return f"/chairs/{city_slug(city, state)}"
