"""The publish-adapter interface.

A publish adapter turns a `PublishRequest` into a draft on one marketplace
and reports back a `PublishResult`. Adapters are plain objects (or modules)
that satisfy this Protocol and register themselves via `registry.register`.

Mirrors the discover-side `deals.adapters.base.SiteAdapter` convention already
in this repo: a `runtime_checkable` Protocol keyed by a short `.platform`
string, with self-registration so the orchestrator discovers adapters without
importing them by name.

Attributes an adapter is expected to expose:
    platform: str        canonical key, e.g. "fb", "ebay", "craigslist".
    per_city: bool        True  -> orchestrator fans this platform out over
                                   every requested city (one draft per city).
                          False -> published once, for the lot's own city.
    aliases: tuple[str]   optional extra keys that resolve to this adapter
                          (e.g. "facebook" -> the "fb" adapter, "cl" ->
                          "craigslist"). Optional.

    async def publish(ctx, request) -> PublishResult
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from .models import PublishRequest, PublishResult

if TYPE_CHECKING:  # pragma: no cover - typing only
    from playwright.async_api import BrowserContext


@runtime_checkable
class PublishAdapter(Protocol):
    platform: str
    per_city: bool

    async def publish(
        self, ctx: "BrowserContext", request: PublishRequest
    ) -> PublishResult: ...
