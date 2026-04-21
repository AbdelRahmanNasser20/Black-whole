"""Idempotency + cost guardrails for the dewatermark.ai API.

Three layers, consulted in order before any API call:
  1. Per-folder sidecar (`<folder>/.dewatermark_state.json`) — knows whether
     this image was already cleaned in THIS lot, by what method, and whether
     the output on disk is still verified clean.
  2. Global API response cache (`~/.listing_automation/api_cache/<sha256>.png`)
     — raw bytes the API returned, keyed by input hash. Survives across lots,
     re-runs, even fresh clones. Once a hash is here, no future run on any
     machine ever needs to call the API for that image again.
  3. Budget caps + offline mode — checked just before the actual HTTP call.
     Per-run counter is in-memory; per-day counter is read from the JSONL log.

Every API outcome (success or failure) appends one line to
`~/.listing_automation/logs/dewatermark_usage.jsonl` so `stats` can audit.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from .config import (
    API_CACHE_DIR,
    DEWATERMARK_OFFLINE,
    MAX_API_CALLS_PER_DAY,
    MAX_API_CALLS_PER_RUN,
    USAGE_LOG_PATH,
)

SIDECAR_NAME = ".dewatermark_state.json"


# ───────────────────────────── hashing ─────────────────────────────

def sha256_of_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(64 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


# ───────────────────────────── sidecar ─────────────────────────────

def load_sidecar(folder: Path) -> dict:
    p = folder / SIDECAR_NAME
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except Exception:
        return {}


def save_sidecar(folder: Path, data: dict) -> None:
    p = folder / SIDECAR_NAME
    p.write_text(json.dumps(data, indent=2, sort_keys=True))


def sidecar_entry_is_fresh(entry: dict, folder: Path) -> bool:
    """Verify the entry still describes a usable on-disk output."""
    if entry.get("status") != "clean":
        return False
    name = entry.get("output_filename")
    if not name:
        return False
    out = folder / name
    return out.exists() and out.stat().st_size > 0


def update_sidecar_entry(
    folder: Path,
    sha: str,
    *,
    source_filename: str,
    output_filename: Optional[str],
    method: str,
    status: str,
    verified_clean: bool,
    api_calls_delta: int = 0,
    failure_reason: Optional[str] = None,
) -> None:
    data = load_sidecar(folder)
    prev = data.get(sha, {})
    now = datetime.now(timezone.utc).isoformat()
    entry = {
        "source_filename": source_filename,
        "output_filename": output_filename,
        "method": method,
        "status": status,
        "verified_clean": verified_clean,
        "api_calls": int(prev.get("api_calls", 0)) + api_calls_delta,
        "first_processed": prev.get("first_processed", now),
        "last_processed": now,
    }
    if failure_reason:
        entry["failure_reason"] = failure_reason
    data[sha] = entry
    save_sidecar(folder, data)


# ────────────────────────── global API cache ──────────────────────────

def cache_path_for(sha: str) -> Path:
    # `.bin` not `.png`: the API may return either jpeg or png bytes; using
    # `.bin` avoids accidentally treating a non-PNG file as a valid PNG image.
    return API_CACHE_DIR / f"{sha}.bin"


def load_cached_response(sha: str) -> Optional[bytes]:
    p = cache_path_for(sha)
    if not p.exists() or p.stat().st_size == 0:
        return None
    return p.read_bytes()


def store_cached_response(sha: str, payload: bytes) -> Path:
    p = cache_path_for(sha)
    p.write_bytes(payload)
    return p


def cache_size() -> int:
    if not API_CACHE_DIR.exists():
        return 0
    return sum(1 for p in API_CACHE_DIR.iterdir() if p.is_file() and p.suffix == ".bin")


# ───────────────────────────── usage log ─────────────────────────────

def log_api_call(
    *,
    lot: str,
    image: str,
    sha: str,
    http_status: Optional[int],
    bytes_in: int,
    bytes_out: int,
    ok: bool,
    error: Optional[str] = None,
) -> None:
    USAGE_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "lot": lot,
        "image": image,
        "sha256": sha,
        "http_status": http_status,
        "bytes_in": bytes_in,
        "bytes_out": bytes_out,
        "ok": ok,
    }
    if error:
        entry["error"] = error
    with USAGE_LOG_PATH.open("a") as f:
        f.write(json.dumps(entry) + "\n")


def _read_usage_log() -> list[dict]:
    if not USAGE_LOG_PATH.exists():
        return []
    out = []
    for line in USAGE_LOG_PATH.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def calls_in_last_24h() -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    n = 0
    for entry in _read_usage_log():
        ts = entry.get("ts", "")
        try:
            when = datetime.fromisoformat(ts)
        except Exception:
            continue
        if when >= cutoff:
            n += 1
    return n


# ───────────────────────────── budget guard ─────────────────────────────

@dataclass
class BudgetStatus:
    allowed: bool
    reason: Optional[str] = None  # "offline_mode" | "budget_run" | "budget_day" | None


class RunBudget:
    """Per-run API-call counter. Created once per pipeline invocation."""

    def __init__(
        self,
        *,
        max_per_run: int = MAX_API_CALLS_PER_RUN,
        max_per_day: int = MAX_API_CALLS_PER_DAY,
        offline: bool = DEWATERMARK_OFFLINE,
    ) -> None:
        self.max_per_run = max_per_run
        self.max_per_day = max_per_day
        self.offline = offline
        self.calls_this_run = 0

    def check(self) -> BudgetStatus:
        if self.offline:
            return BudgetStatus(False, "offline_mode")
        if self.calls_this_run >= self.max_per_run:
            return BudgetStatus(False, "budget_run")
        if calls_in_last_24h() >= self.max_per_day:
            return BudgetStatus(False, "budget_day")
        return BudgetStatus(True)

    def record_call(self) -> None:
        self.calls_this_run += 1


# ───────────────────────────── stats ─────────────────────────────

def stats_summary() -> dict:
    entries = _read_usage_log()
    now = datetime.now(timezone.utc)
    today = now.date()
    month = (today.year, today.month)

    today_calls = today_fail = 0
    month_calls = month_fail = 0
    for e in entries:
        try:
            when = datetime.fromisoformat(e.get("ts", ""))
        except Exception:
            continue
        d = when.date()
        if d == today:
            today_calls += 1
            if not e.get("ok"):
                today_fail += 1
        if (d.year, d.month) == month:
            month_calls += 1
            if not e.get("ok"):
                month_fail += 1
    return {
        "today_calls": today_calls,
        "today_failures": today_fail,
        "month_calls": month_calls,
        "month_failures": month_fail,
        "total_calls": len(entries),
        "cache_entries": cache_size(),
    }
