"""Short-TTL memo for read-only admin JSON handlers + write invalidation.

Why: the admin's tables are tiny (tens of rows) but every tab open is one or
more pooler round trips. Flipping Inventory → Tracking → Inventory re-ran the
same reads each time. A memo of a few seconds makes a revisit instant, and
every successful write through the API (POST/PATCH/PUT/DELETE) drops the whole
memo, so the UI never shows a stale row after its own edit. Out-of-process
writers (run.py, the Render crons) are bounded by the TTL — keep it short.

Only wraps SYNC handlers (`def`, run in FastAPI's threadpool). The wrapper
keeps the handler's signature, so FastAPI still resolves query params.
"""
from __future__ import annotations

import functools
import inspect
import os
import threading
import time
from typing import Any, Callable

DEFAULT_TTL = float(os.getenv("ADMIN_READ_CACHE_TTL", "15"))

# Hard cap on memo entries. Public lot pages key on the URL, so a crawler
# walking many lots must not grow the dict without bound.
MAX_ENTRIES = int(os.getenv("READ_CACHE_MAX_ENTRIES", "1000"))

_lock = threading.Lock()
_store: dict[tuple, tuple[float, Any, float]] = {}  # key -> (stored_at, value, ttl)
_stats = {"hits": 0, "misses": 0, "invalidations": 0}


def _freeze(v: Any) -> Any:
    try:
        hash(v)
        return v
    except TypeError:
        return repr(v)


def _evict(now: float) -> None:
    """Drop expired entries; if still over the cap, drop the oldest. Caller
    holds `_lock`."""
    for k in [k for k, (at, _v, ttl) in _store.items() if now - at >= ttl]:
        del _store[k]
    if len(_store) > MAX_ENTRIES:
        for k, _ in sorted(_store.items(), key=lambda kv: kv[1][0])[: len(_store) - MAX_ENTRIES]:
            del _store[k]


def invalidate_all() -> None:
    with _lock:
        _store.clear()
        _stats["invalidations"] += 1


def stats() -> dict:
    with _lock:
        return {**_stats, "entries": len(_store)}


def cached(ttl: float | None = None) -> Callable:
    """Memoise a sync handler's return value for `ttl` seconds, keyed on its
    bound arguments (defaults applied, so `f()` and `f(status=None)` share)."""
    ttl_s = DEFAULT_TTL if ttl is None else float(ttl)

    def deco(fn: Callable) -> Callable:
        if inspect.iscoroutinefunction(fn):
            raise TypeError(f"readcache.cached wraps sync handlers only: {fn.__qualname__}")
        sig = inspect.signature(fn)

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            if ttl_s <= 0:
                return fn(*args, **kwargs)
            bound = sig.bind_partial(*args, **kwargs)
            bound.apply_defaults()
            key = (fn.__module__, fn.__qualname__,
                   tuple((k, _freeze(v)) for k, v in sorted(bound.arguments.items())))
            now = time.monotonic()
            with _lock:
                hit = _store.get(key)
                if hit is not None and now - hit[0] < ttl_s:
                    _stats["hits"] += 1
                    return hit[1]
                _stats["misses"] += 1
            val = fn(*args, **kwargs)
            with _lock:
                stamp = time.monotonic()
                _store[key] = (stamp, val, ttl_s)
                if len(_store) > MAX_ENTRIES:
                    _evict(stamp)
            return val

        return wrapper

    return deco


_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


async def invalidate_on_write_middleware(request, call_next):
    """Drop the memo after any successful write. Failed writes (4xx/5xx)
    changed nothing, so the memo stays."""
    response = await call_next(request)
    if request.method in _WRITE_METHODS and response.status_code < 400:
        invalidate_all()
    return response
