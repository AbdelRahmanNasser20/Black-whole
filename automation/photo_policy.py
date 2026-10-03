"""Which photo variant a lot shows on which channel — the one watermark rule.

The operator's rule (2026-10-03, replaces "every public photo is watermarked"):

* A lot we **own or have won** (anything but `active_bid`): the clean photo
  everywhere — black-whole.com, FB (Marketplace / Page / catalog / ads), eBay,
  Craigslist, the Google feed, the CRM. No watermark anywhere.
* A lot **still being bid on** (`active_bid`): the tiled BLACKWHOLE watermark
  on **black-whole.com only**. Every other channel gets the clean copy.
* The flip is automatic: the variant is derived from `inventory.status` at read
  time, so the moment a lot leaves `active_bid` every surface serves clean.

"Clean" means the actual photo: dewatermarked (the GovDeals mark is removed
by `automation/dewatermark.py` first, always), web-optimised, metadata
stripped — **no** mirror, **no** re-frame, **no** watermark. The full
`image_disguise` recipe (mirror + re-frame + tiled watermark) is applied only
to the watermarked variant, i.e. the site copy of an `active_bid` lot.

R2 layout (opaque `p/<hmac>/` keys from `listing_images`):

    p/<hmac>/h.o.jpg       hero, clean (original)   always
    p/<hmac>/<tok>.o.jpg   gallery, clean           always
    p/<hmac>/h.jpg         hero, disguised + watermarked   only while active_bid
    p/<hmac>/<tok>.jpg     gallery, disguised + watermarked
    p/<hmac>/h.c.jpg       2026-09 catalog twin (mirrored, unwatermarked) — legacy, unused

`inventory.hero_image_url` / `image_urls` store the **clean** URLs, so any
reader that skips this module (the CRM repo, an old script) gets a clean photo.
Only `variant_url(..., watermark=True)` — reached through
`wants_watermark(status, "site")` — ever names a watermarked object.

Stdlib only: `lot_images` imports this and has to stay vendorable.
"""
from __future__ import annotations

from urllib.parse import urlsplit

# Channel names. `site` is the only one that can carry a watermark; the rest are
# listed so callers and tests share one vocabulary (matches
# `automation.channels.CHANNELS` plus the surfaces that aren't sync channels).
SITE = "site"
CHANNELS = (
    SITE, "fb_catalog", "google", "ebay", "fb_marketplace", "craigslist",
    "fb_page", "ads", "crm",
)

# The single status whose site photos carry the watermark.
WATERMARK_STATUS = "active_bid"

# Lots we actually hold (or held and sold). Their own folder photos win over
# the GovDeals scrape when we have any (`photo_sync`). `lost*`/`hidden` are
# not ours; they still never get a watermark, they just keep what R2 has.
OWNED_STATUSES = ("owned", "won_pickup", "listed", "draft", "sold_out")

OPAQUE_PREFIX = "p/"
# `.o.jpg` = the original photo (dewatermarked, web-optimised, untouched).
# `.c.jpg` = the 2026-09 FB-catalog twin: mirrored/re-framed, no watermark. It
# is still on R2 but no longer a variant anything serves; both map to `.o.jpg`.
_CLEAN_SUFFIX = ".o.jpg"
_LEGACY_TWIN_SUFFIX = ".c.jpg"
_PLAIN_SUFFIX = ".jpg"


def wants_watermark(status: str | None, channel: str | None) -> bool:
    """True only for black-whole.com showing a lot we're still bidding on."""
    return channel == SITE and (status or "").strip() == WATERMARK_STATUS


def is_owned(status: str | None) -> bool:
    return (status or "").strip() in OWNED_STATUSES


def keeps_watermark_variant(status: str | None) -> bool:
    """Whether an upload for a lot in `status` also writes the watermarked copy."""
    return (status or "").strip() == WATERMARK_STATUS


def _split(url: str) -> tuple[str, str]:
    path, sep, query = url.partition("?")
    return path, sep + query


def is_opaque(url: str | None) -> bool:
    """URL whose object key sits under the opaque `p/` namespace."""
    return urlsplit(url or "").path.lstrip("/").startswith(OPAQUE_PREFIX)


def _stem(object_path: str) -> str | None:
    """`p/x/tok` for any variant of `p/x/tok(.o|.c)?.jpg`; None if not a variant key."""
    for suffix in (_CLEAN_SUFFIX, _LEGACY_TWIN_SUFFIX, _PLAIN_SUFFIX):
        if object_path.endswith(suffix):
            return object_path[: -len(suffix)]
    return None


def clean_path(object_path: str) -> str:
    """Object key of the clean variant (`p/x/h.jpg` → `p/x/h.o.jpg`)."""
    stem = _stem(object_path)
    return stem + _CLEAN_SUFFIX if stem is not None else object_path


def watermarked_path(object_path: str) -> str:
    """Object key of the watermarked variant (`p/x/h.o.jpg` → `p/x/h.jpg`)."""
    stem = _stem(object_path)
    return stem + _PLAIN_SUFFIX if stem is not None else object_path


def _map_url(url: str | None, fn) -> str | None:
    if not url or not is_opaque(url):
        return url
    path, rest = _split(url)
    return fn(path) + rest


def is_clean(url: str | None) -> bool:
    """True unless the URL names a disguised/watermarked object.

    Legacy (non-`p/`) keys never carried our watermark, so they count as clean.
    """
    if not url or not is_opaque(url):
        return True
    return _split(url)[0].endswith(_CLEAN_SUFFIX)


def clean_url(url: str | None) -> str | None:
    """The clean twin of a photo URL (`…/x.jpg` → `…/x.o.jpg`); others unchanged."""
    return _map_url(url, clean_path)


def watermarked_url(url: str | None) -> str | None:
    """The watermarked twin of an opaque URL (`…/x.o.jpg` → `…/x.jpg`)."""
    return _map_url(url, watermarked_path)


def variant_url(url: str | None, *, watermark: bool) -> str | None:
    return watermarked_url(url) if watermark else clean_url(url)


def url_for(url: str | None, *, status: str | None, channel: str | None) -> str | None:
    """The URL a `channel` should use for this photo of a lot in `status`."""
    return variant_url(url, watermark=wants_watermark(status, channel))
