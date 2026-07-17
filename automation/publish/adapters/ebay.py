"""eBay publish adapter (reference adapter, BLACKWHOLE-9).

Thin wrapper over `automation.ebay.create_draft`. eBay listings are national,
not location-scoped, so this adapter is NOT per-city: the orchestrator
publishes it once (using the lot's own city) regardless of how many cities are
requested for the FB / Craigslist fan-out.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from automation import ebay

from ..models import STATUS_DRAFT, STATUS_DRY_RUN, PublishRequest, PublishResult
from ..registry import register

if TYPE_CHECKING:  # pragma: no cover - typing only
    from playwright.async_api import BrowserContext


class EbayAdapter:
    platform = "ebay"
    per_city = False

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
                detail=f"[dry-run] would create eBay draft for {data.chair_type!r}",
            )
        url = await ebay.create_draft(
            ctx=ctx,
            title=data.title,
            chair_type=data.chair_type,
            location=data.location,
            city=city,
            state=data.state,
            zip_code=data.zip_code,
            quantity=data.quantity,
            dimensions=data.dimensions,
            price_each=data.price_per_chair,
            lot_id=data.lot_id,
            images=data.images,
            description_text=data.description_text,
        )
        return PublishResult(
            platform=self.platform, city=city, status=STATUS_DRAFT, url=url
        )


register(EbayAdapter())
