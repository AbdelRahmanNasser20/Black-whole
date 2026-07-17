"""Platform-agnostic data types for the multi-platform publish flow.

`ListingData` is the normalized payload for a single lot — everything a
publish adapter could need, decoupled from the LLM extraction / GovDeals
scrape objects that produce it. The orchestrator fans one `ListingData` out
into many `PublishRequest`s (one per platform × city) and collects a
`PublishResult` for each.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ListingData:
    """Normalized, platform-neutral description of one lot to publish."""

    title: str = ""
    chair_type: str = ""
    location: str = ""
    city: str = ""
    state: str = ""
    zip_code: str = ""
    quantity: str = ""
    dimensions: str = ""
    style_suffix: str = ""
    price_per_chair: int = 0
    lot_id: str = ""
    images: list[Path] = field(default_factory=list)
    description_text: str = ""


@dataclass
class PublishRequest:
    """One fan-out unit: publish `data` to a single platform for `city`.

    `city` is the *target* city for this unit and may differ from
    `data.city` (the lot's source location) when a platform is posted across
    multiple cities. Adapters that are not city-aware can ignore it.
    """

    data: ListingData
    city: str = ""
    dry_run: bool = True

    @property
    def effective_city(self) -> str:
        return self.city or self.data.city


# Result statuses. `ok` (below) treats the first three as success.
STATUS_DRAFT = "draft"                 # a real draft was created on the platform
STATUS_DRY_RUN = "dry_run"             # simulated — nothing touched the platform
STATUS_SKIPPED_DUPLICATE = "skipped_duplicate"  # already published, left as-is
STATUS_ERROR = "error"                 # adapter raised / failed
STATUS_NO_ADAPTER = "no_adapter"       # no adapter registered for this platform


@dataclass
class PublishResult:
    """Outcome of publishing one platform × city unit."""

    platform: str
    city: str
    status: str
    url: str | None = None
    error: str | None = None
    detail: str | None = None

    @property
    def ok(self) -> bool:
        return self.status in (
            STATUS_DRAFT,
            STATUS_DRY_RUN,
            STATUS_SKIPPED_DUPLICATE,
        )
