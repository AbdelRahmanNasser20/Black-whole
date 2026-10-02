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

# Batch-level "this looks systemic, not per-lot" thresholds, shared by
# public_surplus's 401 block guard and the source breaker (recorder/health.py):
# at least this many lots AND at least this fraction of the batch.
BLOCK_SUSPECT_MIN_COUNT = 3
BLOCK_SUSPECT_MIN_FRACTION = 0.8

# (connect, read). A dead host used to cost 30 s per lot on the CONNECT alone
# (~46k Public Surplus connect timeouts in the recorder log); 10 s is plenty
# for a TLS handshake to a healthy host.
DEFAULT_TIMEOUT = (10, 30)

# module-level per-host monotonic clock — last request time.monotonic() by netloc
_last_request_at: dict[str, float] = {}

# After a 429, every request to that host fails fast (no network, no sleep)
# until Retry-After has passed — the caller's batch ends at once instead of
# queueing more requests behind a rate limit. Unparseable/absent Retry-After
# = 60 s.
RETRY_AFTER_DEFAULT_S = 60.0
RETRY_AFTER_MAX_S = 3600.0
_rate_limited_until: dict[str, float] = {}


class RateLimited(requests.exceptions.RequestException):
    """Raised by polite_get/polite_post while a host's Retry-After window is
    open. A RequestException on purpose: every adapter already treats that as
    "fetch failed for this lot, emit nothing"."""


def _retry_after_s(resp) -> float:
    raw = (getattr(resp, "headers", None) or {}).get("Retry-After")
    try:
        return max(0.0, min(float(raw), RETRY_AFTER_MAX_S))
    except (TypeError, ValueError):
        return RETRY_AFTER_DEFAULT_S


def _check_rate_limit(host: str) -> None:
    until = _rate_limited_until.get(host)
    if until is not None:
        if time.monotonic() < until:
            raise RateLimited(f"{host} rate-limited (429) — Retry-After window still open")
        _rate_limited_until.pop(host, None)


def _note_response(host: str, resp) -> None:
    if getattr(resp, "status_code", None) == 429:
        _rate_limited_until[host] = time.monotonic() + _retry_after_s(resp)


class PollBudget:
    """Per-source, per-batch failure accounting for `poll()`.

    `record(ok)` after each lot; `exhausted` turns True after
    `max_consecutive` failures in a row (default 10, RECORDER_POLL_MAX_CONSECUTIVE_FAILURES)
    and the adapter stops the batch — a dead host costs ten lots, not the
    whole tracked set. `stats()` is what the CLI reads for the breaker."""

    def __init__(self, max_consecutive: int | None = None):
        if max_consecutive is None:
            import os
            try:
                max_consecutive = int(os.getenv("RECORDER_POLL_MAX_CONSECUTIVE_FAILURES") or 10)
            except ValueError:
                max_consecutive = 10
        self.max_consecutive = max(1, max_consecutive)
        self.attempted = 0
        self.failed = 0
        self.consecutive = 0
        self.aborted = False

    def record(self, ok: bool) -> None:
        self.attempted += 1
        if ok:
            self.consecutive = 0
            return
        self.failed += 1
        self.consecutive += 1
        if self.consecutive >= self.max_consecutive:
            self.aborted = True

    @property
    def exhausted(self) -> bool:
        return self.aborted

    def stats(self, total: int) -> dict:
        return {"attempted": self.attempted, "failed": self.failed, "aborted": self.aborted,
                "skipped": max(0, total - self.attempted)}


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


def polite_get(url, *, headers=None, params=None, timeout=DEFAULT_TIMEOUT) -> requests.Response:
    host = urlparse(url).netloc
    _check_rate_limit(host)
    _throttle(host)
    resp = requests.get(url, headers=_headers(headers), params=params, timeout=timeout)
    _note_response(host, resp)
    return resp


def polite_post(url, *, headers=None, json=None, timeout=DEFAULT_TIMEOUT) -> requests.Response:
    host = urlparse(url).netloc
    _check_rate_limit(host)
    _throttle(host)
    resp = requests.post(url, headers=_headers(headers), json=json, timeout=timeout)
    _note_response(host, resp)
    return resp
