"""Which photo variant a lot shows on which channel — the one watermark rule.

The operator's rule (2026-10-03, replaces "every public photo is watermarked"):

* A lot we **own or have won** (anything but `active_bid`): the clean photo
  everywhere — black-whole.com, FB (Marketplace / Page / catalog / ads), eBay,
  Craigslist, the Google feed, the CRM. No watermark anywhere.
* A lot **still being bid on** (`active_bid`): the tiled BLACKWHOLE watermark
  on **black-whole.com only**. Every other channel gets the clean copy.
* The flip is automatic: the variant is derived from `inventory.status` at read
  time, so the moment a lot leaves `active_bid` every surface serves clean.

"Clean" means dewatermarked (the GovDeals mark is removed by
`automation/dewatermark.py` first, always) and without our own watermark. The
mirror/re-frame disguise from `image_disguise` stays on both variants.

R2 layout (opaque `p/<hmac>/` keys from `listing_images`):

    p/<hmac>/h.c.jpg       hero, clean        (always)
    p/<hmac>/<tok>.c.jpg   gallery, clean     (always)
    p/<hmac>/h.jpg         hero, watermarked  (uploaded while active_bid)
    p/<hmac>/<tok>.jpg     gallery, watermarked

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
_CLEAN_SUFFIX = ".c.jpg"
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
    """URL whose object key sits under the disguised `p/` namespace."""
    return urlsplit(url or "").path.lstrip("/").startswith(OPAQUE_PREFIX)


def is_clean(url: str | None) -> bool:
    """True unless the URL names a watermarked object.

    Legacy (non-`p/`) keys never carried our watermark, so they count as clean.
    """
    if not url or not is_opaque(url):
        return True
    return _split(url)[0].endswith(_CLEAN_SUFFIX)


def clean_url(url: str | None) -> str | None:
    """The clean twin of a photo URL (`…/x.jpg` → `…/x.c.jpg`); others unchanged."""
    if not url or not is_opaque(url):
        return url
    path, rest = _split(url)
    if path.endswith(_CLEAN_SUFFIX) or not path.endswith(_PLAIN_SUFFIX):
        return url
    return path[: -len(_PLAIN_SUFFIX)] + _CLEAN_SUFFIX + rest


def watermarked_url(url: str | None) -> str | None:
    """The watermarked twin of a clean opaque URL (`…/x.c.jpg` → `…/x.jpg`)."""
    if not url or not is_opaque(url):
        return url
    path, rest = _split(url)
    if not path.endswith(_CLEAN_SUFFIX):
        return url
    return path[: -len(_CLEAN_SUFFIX)] + _PLAIN_SUFFIX + rest


def clean_path(object_path: str) -> str:
    """Object key of the clean variant (`p/x/h.jpg` → `p/x/h.c.jpg`)."""
    if object_path.endswith(_CLEAN_SUFFIX) or not object_path.endswith(_PLAIN_SUFFIX):
        return object_path
    return object_path[: -len(_PLAIN_SUFFIX)] + _CLEAN_SUFFIX


def watermarked_path(object_path: str) -> str:
    """Object key of the watermarked variant (`p/x/h.c.jpg` → `p/x/h.jpg`)."""
    if not object_path.endswith(_CLEAN_SUFFIX):
        return object_path
    return object_path[: -len(_CLEAN_SUFFIX)] + _PLAIN_SUFFIX


def variant_url(url: str | None, *, watermark: bool) -> str | None:
    return watermarked_url(url) if watermark else clean_url(url)


def url_for(url: str | None, *, status: str | None, channel: str | None) -> str | None:
    """The URL a `channel` should use for this photo of a lot in `status`."""
    return variant_url(url, watermark=wants_watermark(status, channel))
