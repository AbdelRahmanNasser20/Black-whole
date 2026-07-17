"""Fan a single lot out across platforms and cities.

`publish_all` is the one entry point run.py calls. It resolves each requested
platform to a registered adapter and issues one publish per (platform, city),
collecting a `PublishResult` for every unit. It never raises for a single
adapter failure — a raised adapter becomes an ERROR result so one bad platform
can't sink the others.

Safety: dry-run by default. Real drafts are created only when
LISTING_PUBLISH_LIVE is enabled (see registry.live_publishing_enabled) or a
caller explicitly passes dry_run=False.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from . import registry
from .models import (
    STATUS_ERROR,
    STATUS_NO_ADAPTER,
    STATUS_SKIPPED_DUPLICATE,
    ListingData,
    PublishRequest,
    PublishResult,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from playwright.async_api import BrowserContext


def _dedupe(seq) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in seq:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


async def publish_all(
    ctx: "BrowserContext | None",
    data: ListingData,
    platforms: list[str],
    cities: list[str] | None = None,
    *,
    dry_run: bool | None = None,
    force_republish: bool = False,
    published: dict[str, str] | None = None,
) -> list[PublishResult]:
    """Publish `data` to every `platforms` × `cities` combination.

    Args:
        ctx: live browser context (unused in dry-run; may be None then).
        data: the normalized lot payload.
        platforms: requested platform keys/aliases, e.g. ["fb", "ebay", "cl"].
        cities: target cities; empty/None -> just the lot's own city.
        dry_run: force dry-run/live; None -> env-gated default (dry-run).
        force_republish: ignore `published` and (re)create drafts anyway.
        published: platform-key -> existing draft URL, used to skip duplicates.
    """
    registry.load_builtin()
    if dry_run is None:
        dry_run = not registry.live_publishing_enabled()
    published = published or {}

    platform_keys = _dedupe(p.strip().lower() for p in platforms if p and p.strip())
    city_targets = _dedupe(c.strip() for c in (cities or []) if c and c.strip())
    if not city_targets:
        city_targets = [data.city or ""]

    results: list[PublishResult] = []
    for platform in platform_keys:
        adapter = registry.get(platform)
        if adapter is None:
            results.append(
                PublishResult(
                    platform=platform,
                    city="*",
                    status=STATUS_NO_ADAPTER,
                    error=(
                        f"no publish adapter registered for {platform!r}; "
                        f"available: {registry.available()}"
                    ),
                )
            )
            continue

        # Duplicate guard: if this platform is already published (by canonical
        # key or alias) and we're not forcing, skip without touching it.
        canonical = getattr(adapter, "platform", platform)
        existing_url = published.get(platform) or published.get(canonical)
        if existing_url and not force_republish:
            results.append(
                PublishResult(
                    platform=canonical,
                    city=data.city or "",
                    status=STATUS_SKIPPED_DUPLICATE,
                    url=existing_url,
                )
            )
            continue

        per_city = getattr(adapter, "per_city", True)
        adapter_cities = city_targets if per_city else [data.city or (city_targets[0] if city_targets else "")]
        for city in _dedupe(adapter_cities):
            request = PublishRequest(data=data, city=city, dry_run=dry_run)
            try:
                results.append(await adapter.publish(ctx, request))
            except Exception as e:  # one bad unit must not sink the rest
                results.append(
                    PublishResult(
                        platform=canonical,
                        city=city,
                        status=STATUS_ERROR,
                        error=repr(e),
                    )
                )
    return results


def build_listing_data(primary, meta, price_per_chair: int, images) -> ListingData:
    """Assemble a `ListingData` from the run's LLM extraction + scrape meta.

    Kept here (not in run.py) so the mapping lives beside the type it builds
    and run.py stays thin. Uses getattr so a partial extraction can't crash
    the publish phase.
    """
    def g(obj, name, default=""):
        return getattr(obj, name, default) or default

    return ListingData(
        title=g(primary, "title"),
        chair_type=g(primary, "chair_type"),
        location=g(primary, "location"),
        city=g(primary, "city") or g(meta, "city"),
        state=g(primary, "state") or g(meta, "state"),
        zip_code=g(primary, "zip_code") or g(meta, "zip_code"),
        quantity=g(primary, "quantity"),
        dimensions=g(primary, "dimensions"),
        style_suffix=g(primary, "style_suffix"),
        price_per_chair=price_per_chair,
        lot_id=g(meta, "lot_id"),
        images=list(images or []),
        description_text=g(primary, "description_text"),
    )
