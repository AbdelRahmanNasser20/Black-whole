"""Multi-platform publish orchestration (BLACKWHOLE-21).

One lot fans out to many marketplaces (FB, eBay, Craigslist, ...) across many
cities through a single call to `orchestrator.publish_all`. Platforms are
discovered via a registry of self-registering adapters, so new platforms plug
in without touching the orchestrator (see `automation.publish.adapters`).

Safe by default: publishing is dry-run unless LISTING_PUBLISH_LIVE is enabled.
"""
from __future__ import annotations

from . import orchestrator, registry
from .base import PublishAdapter
from .models import ListingData, PublishRequest, PublishResult

__all__ = [
    "orchestrator",
    "registry",
    "PublishAdapter",
    "ListingData",
    "PublishRequest",
    "PublishResult",
]
