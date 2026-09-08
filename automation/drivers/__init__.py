"""Platform listing drivers.

Each driver exposes `NAME` and an async `create(ctx, content, *, publish, ...)`
that returns ``{"item_id", "url", "published"}``. Resolve by platform name via
``REGISTRY``.

Implemented: eBay (proven). Facebook/OfferUp/Craigslist are tracked here for the
roadmap — OfferUp is intentionally absent because OfferUp item posting is
app-only on the web (see the design spec).
"""
from . import ebay

REGISTRY = {
    ebay.NAME: ebay,
}


def get(platform: str):
    try:
        return REGISTRY[platform]
    except KeyError:
        raise ValueError(
            f"no driver for platform {platform!r}; have {sorted(REGISTRY)}")
