"""Shared contract + HTTP helpers for recorder source adapters.

Every source (govdeals, public_surplus, purple_wave, municibid, mibid, gsa)
implements `RecorderSource`. `polite_get`/`polite_post` are the ONLY way
adapters should hit a source over HTTP — they enforce the >=1s per-host
throttle and an honest desktop-Chrome User-Agent so we never look like a bot
storm. Adapters inspect `response.status_code` themselves; these helpers
never call `raise_for_status()`.

Per-host proxy (2026-10-09): `RECORDER_PROXY_URL` (e.g. `socks5h://127.0.0.1:1081`,
the Pi's residential US SOCKS5 tunnel) + `RECORDER_PROXY_HOSTS` (comma list of
hostnames, suffix match: `publicsurplus.com,mibid.michigan.gov`). Only requests
to a listed host carry `proxies=`; every other host (GovDeals, GSA, Purple
Wave, Municibid) stays direct. A listed host whose proxy is not listening
raises `ProxyUnavailable` BEFORE any network call — never a silent direct
request from a blocked IP (the site would just 403 again and the breaker
would open with a misleading reason).

Fetch contract for `discover()`: a fetch that FAILED (network, 403/429,
page-shape drift) raises `SourceFetchFailed`; a fetch that succeeded and
matched nothing returns `[]`. The breaker (recorder/health.py) counts the
former as a failed attempt and the latter as a success — mibid's furniture
filter legitimately matches 0 lots most days.
"""
from __future__ import annotations

import os
import socket
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


class SourceFetchFailed(Exception):
    """`discover()` could not fetch its source (as opposed to "fetched fine,
    matched 0 lots", which returns `[]`). The CLI records it as a failed
    breaker attempt with this message as `last_error`. `url` (optional) names
    the host whose last transport error gets appended — gsa must NOT pass it
    (its error text can carry the api_key)."""

    def __init__(self, message: str, *, url: str | None = None):
        detail = last_failure(url) if url else None
        super().__init__(f"{message} — last error: {detail}" if detail else message)


class ProxyUnavailable(requests.exceptions.ConnectionError):
    """A host is routed through `RECORDER_PROXY_URL` but the proxy is not
    reachable (or the URL is unset). Raised before any network call. A
    ConnectionError on purpose: adapters already read that as "fetch failed",
    and public_surplus's sweep stops after the first one."""


# --- per-host proxy ----------------------------------------------------------

PROXY_URL_ENV = "RECORDER_PROXY_URL"
PROXY_HOSTS_ENV = "RECORDER_PROXY_HOSTS"
PROXY_PROBE_TIMEOUT_S = 2.0

# Last transport-level failure per host (exception text or `HTTP <code>`),
# so SourceFetchFailed can say WHY the breaker is opening.
_last_failure: dict[str, str] = {}


def _hostname(url_or_host: str) -> str:
    parsed = urlparse(url_or_host if "//" in url_or_host else f"//{url_or_host}")
    return (parsed.hostname or "").lower()


def proxy_hosts() -> list[str]:
    raw = os.getenv(PROXY_HOSTS_ENV) or ""
    return [h.strip().lower().lstrip(".") for h in raw.split(",") if h.strip()]


def is_proxied_host(host: str) -> bool:
    host = host.lower()
    return any(host == h or host.endswith("." + h) for h in proxy_hosts())


def _socket_open(host: str, port: int, timeout: float = PROXY_PROBE_TIMEOUT_S) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def proxies_for(url: str) -> dict[str, str] | None:
    """`proxies=` for `requests` when the URL's host is listed in
    RECORDER_PROXY_HOSTS, else None (direct). Raises ProxyUnavailable when the
    host is listed but RECORDER_PROXY_URL is unset or its socket is closed."""
    host = _hostname(url)
    if not host or not is_proxied_host(host):
        return None
    proxy_url = (os.getenv(PROXY_URL_ENV) or "").strip()
    if not proxy_url:
        raise ProxyUnavailable(
            f"{host} is listed in {PROXY_HOSTS_ENV} but {PROXY_URL_ENV} is unset — "
            "set the proxy URL (socks5h://127.0.0.1:1081) or drop the host from the list; "
            "never fetched direct")
    parsed = urlparse(proxy_url)
    phost, pport = parsed.hostname, parsed.port
    if not phost or not pport:
        raise ProxyUnavailable(
            f"{PROXY_URL_ENV}={proxy_url!r} has no host:port — {host} not fetched")
    if not _socket_open(phost, pport):
        raise ProxyUnavailable(
            f"recorder proxy {proxy_url} is not listening ({PROXY_URL_ENV}); {host} is routed "
            "through it and was NOT fetched direct — start the tunnel (`proxy on` / "
            "`proxy doctor`) or drop the host from "
            f"{PROXY_HOSTS_ENV}")
    return {"http": proxy_url, "https": proxy_url}


def last_failure(url_or_host: str) -> str | None:
    return _last_failure.get(_hostname(url_or_host))


def _note_failure(host: str, text: str) -> None:
    _last_failure[host.lower()] = text[:300]


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


def _send(method, url, *, headers, timeout, **kw) -> requests.Response:
    host = urlparse(url).netloc
    hostname = _hostname(url)
    _check_rate_limit(host)
    try:
        proxies = proxies_for(url)        # before the throttle: no sleep for a dead proxy
    except ProxyUnavailable as e:
        _note_failure(hostname, str(e))
        raise
    _throttle(host)
    try:
        resp = method(url, headers=_headers(headers), timeout=timeout, proxies=proxies, **kw)
    except requests.exceptions.RequestException as e:
        _note_failure(hostname, f"{type(e).__name__}: {e}")
        raise
    _note_response(host, resp)
    if resp.status_code != 200:
        _note_failure(hostname, f"HTTP {resp.status_code} on {getattr(resp, 'url', url)}")
    return resp


def polite_get(url, *, headers=None, params=None, timeout=DEFAULT_TIMEOUT) -> requests.Response:
    return _send(requests.get, url, headers=headers, timeout=timeout, params=params)


def polite_post(url, *, headers=None, json=None, timeout=DEFAULT_TIMEOUT) -> requests.Response:
    return _send(requests.post, url, headers=headers, timeout=timeout, json=json)
