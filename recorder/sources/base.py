"""Shared contract + HTTP helpers for recorder source adapters.

Every source (govdeals, public_surplus, purple_wave, municibid, mibid, gsa)
implements `RecorderSource`. `polite_get`/`polite_post` are the ONLY way
adapters should hit a source over HTTP — they enforce the >=1s per-host
throttle and an honest desktop-Chrome User-Agent so we never look like a bot
storm. Adapters inspect `response.status_code` themselves; these helpers
never call `raise_for_status()`.
"""
from __future__ import annotations

import time
from typing import Protocol, runtime_checkable
from urllib.parse import urlparse

import requests

from recorder.models import Observation

FURNITURE_TERMS = [
    "chairs", "seating", "banquet", "folding chairs", "stackable chairs", "office furniture",
]

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

MIN_HOST_INTERVAL_SECONDS = 1.0

# module-level per-host monotonic clock — last request time.monotonic() by netloc
_last_request_at: dict[str, float] = {}


@runtime_checkable
class RecorderSource(Protocol):
    SOURCE: str

    def discover(self) -> list[Observation]:
        """Active-lot sweep over the furniture/seating scope."""
        ...

    def poll(self, lots: list[dict]) -> list[Observation]:
        """Re-check tracked lots (rows from store.tracked_active that are due)."""
        ...

    def sold_sweep(self) -> list[Observation]:
        """Sweep recently-completed lots, for sources that serve them. Else []."""
        ...


def _throttle(host: str) -> None:
    now = time.monotonic()
    last = _last_request_at.get(host)
    if last is not None:
        wait = MIN_HOST_INTERVAL_SECONDS - (now - last)
        if wait > 0:
            time.sleep(wait)
    _last_request_at[host] = time.monotonic()


def _headers(extra: dict | None) -> dict:
    merged = {"User-Agent": USER_AGENT}
    if extra:
        merged.update(extra)
    return merged


def polite_get(url, *, headers=None, params=None, timeout=30) -> requests.Response:
    _throttle(urlparse(url).netloc)
    return requests.get(url, headers=_headers(headers), params=params, timeout=timeout)


def polite_post(url, *, headers=None, json=None, timeout=30) -> requests.Response:
    _throttle(urlparse(url).netloc)
    return requests.post(url, headers=_headers(headers), json=json, timeout=timeout)
