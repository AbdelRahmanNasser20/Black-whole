"""Adapter registry + convention-based discovery.

Adapters register themselves at import time (see the built-ins in
`automation/publish/adapters/`). `load_builtin()` discovers every module in
that package and imports it, so dropping a new file there — e.g. the
Craigslist adapter from BLACKWHOLE-20 — is enough to make it available; no
edits to the orchestrator or this module are required.

Two registration points exist for adapters that live *outside* this package:
    1. Call `registry.register(MyAdapter())` at import time, then ensure your
       module is imported (e.g. list it in LISTING_PUBLISH_ADAPTERS).
    2. Set env LISTING_PUBLISH_ADAPTERS to a comma-separated list of dotted
       module paths; `load_builtin()` imports each after the built-ins.
"""
from __future__ import annotations

import importlib
import os
import pkgutil

from .base import PublishAdapter

# canonical platform key -> adapter
_REGISTRY: dict[str, PublishAdapter] = {}
# alias -> canonical platform key
_ALIASES: dict[str, str] = {}
_BUILTINS_LOADED = False


def register(adapter: PublishAdapter) -> PublishAdapter:
    """Register `adapter` under its `.platform` (and any `.aliases`)."""
    platform = getattr(adapter, "platform", None)
    if not platform:
        raise ValueError("publish adapter must define a non-empty .platform")
    _REGISTRY[platform] = adapter
    for alias in getattr(adapter, "aliases", ()) or ():
        _ALIASES[alias] = platform
    return adapter


def unregister(platform: str) -> None:
    """Remove an adapter and its aliases (used by tests)."""
    _REGISTRY.pop(platform, None)
    for alias in [a for a, canon in _ALIASES.items() if canon == platform]:
        _ALIASES.pop(alias, None)


def get(platform: str) -> PublishAdapter | None:
    """Resolve a platform key or alias to an adapter, or None."""
    if platform in _REGISTRY:
        return _REGISTRY[platform]
    canonical = _ALIASES.get(platform)
    if canonical:
        return _REGISTRY.get(canonical)
    return None


def available() -> list[str]:
    """Canonical platform keys currently registered, sorted."""
    return sorted(_REGISTRY)


def live_publishing_enabled() -> bool:
    """Whether real drafts may be created. OFF unless explicitly enabled.

    The publish flow is dry-run by default; set LISTING_PUBLISH_LIVE=1 to let
    adapters drive the live browser and create real marketplace drafts.
    """
    raw = os.getenv("LISTING_PUBLISH_LIVE")
    if raw is None:
        return False
    return raw.strip().lower() in ("1", "true", "yes", "on")


def load_builtin(*, force: bool = False) -> list[str]:
    """Import every adapter module so it self-registers. Idempotent.

    A broken or dependency-missing adapter (e.g. a work-in-progress
    Craigslist module) is skipped with a warning — it must never prevent the
    other adapters from loading.
    """
    global _BUILTINS_LOADED
    if _BUILTINS_LOADED and not force:
        return available()

    from . import adapters  # local import to avoid a cycle at module load

    for modinfo in pkgutil.iter_modules(adapters.__path__):
        if modinfo.name.startswith("_"):
            continue
        _safe_import(f"{adapters.__name__}.{modinfo.name}")

    extra = os.getenv("LISTING_PUBLISH_ADAPTERS", "")
    for dotted in [p.strip() for p in extra.split(",") if p.strip()]:
        _safe_import(dotted)

    _BUILTINS_LOADED = True
    return available()


def _safe_import(dotted: str) -> None:
    try:
        importlib.import_module(dotted)
    except Exception as e:  # never let one bad adapter break the rest
        print(f"[publish] skipped adapter {dotted!r}: {e!r}")
