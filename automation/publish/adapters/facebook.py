"""Facebook Marketplace publish adapter (reference adapter).

Thin wrapper over `automation.facebook.create_draft`. Per-city: FB listings
are location-scoped, so the orchestrator fans this out over every requested
city (BLACKWHOLE-7 covers making cross-city posting robust; here each city is
simply passed through as the draft's location).
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from automation import facebook

from ..models import STATUS_DRAFT, STATUS_DRY_RUN, PublishRequest, PublishResult
from ..registry import register

if TYPE_CHECKING:  # pragma: no cover - typing only
    from playwright.async_api import BrowserContext


class FacebookAdapter:
    platform = "fb"
    aliases = ("facebook",)
    per_city = True

    async def publish(
        self, ctx: "BrowserContext", request: PublishRequest
    ) -> PublishResult:
        data = request.data
        city = request.effective_city
        if request.dry_run:
            return PublishResult(
                platform=self.platform,
                city=city,
                status=STATUS_DRY_RUN,
                detail=f"[dry-run] would create FB draft for {data.chair_type!r} in {city}",
            )
        url, _rendered = await facebook.create_draft(
            ctx=ctx,
            title=data.title,
            price_per_chair=data.price_per_chair,
            chair_type=data.chair_type,
            location=data.location,
            quantity=data.quantity,
            dimensions=data.dimensions,
            style_suffix=data.style_suffix,
            images=data.images,
            description_text=data.description_text,
            city=city,
            state=data.state,
            zip_code=data.zip_code,
            sku=data.lot_id,
        )
        return PublishResult(
            platform=self.platform, city=city, status=STATUS_DRAFT, url=url
        )


register(FacebookAdapter())
