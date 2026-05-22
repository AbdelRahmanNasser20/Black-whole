"""Dewatermark pipeline — API-only, with idempotency + post-call quality re-check + budget caps.

Per-image flow on every run:
  1. Hash the original (sha256 of bytes).
  2. Per-folder sidecar hit + on-disk output present + still-clean? → skip, emit `cache_hit`.
  3. Global API-response cache hit? → write bytes into folder, quality-check, save sidecar.
  4. Budget-guarded API call. Post-call quality re-check. Success is cached globally so
     future runs (same hash, any lot) are free. Failures leave the original in
     `_originals/` and emit `dewatermark:degraded` — no silent fallback.

Run `python -m automation.dewatermark verify <path>` and `... stats` for offline checks.
"""
from __future__ import annotations

import asyncio
import argparse
import base64
import shutil
import sys
from pathlib import Path
from typing import Optional

import httpx
from playwright.async_api import BrowserContext

from . import progress
from . import dewatermark_cache as cache
from .config import (
    API_CACHE_DIR,
    DEWATERMARK_API_KEY,
    DEWATERMARK_API_URL,
    USAGE_LOG_PATH,
)
from .dewatermark_cache import RunBudget
from .quality import watermark_likely_present


def _archive_originals(folder: Path, images: list[Path]) -> dict[str, Path]:
    """Move watermarked originals into ``_originals/`` and return a name→path map.

    Every reference to the "original" downstream (sha, API upload body, byte
    -identity check) MUST use the returned archived path, not the input path.
    Leaving the watermarked file in the folder root would let it sit next to
    the cleaned output (different extensions coexist) and contaminate the
    inventory folder the user actually browses.

    Idempotent: if an archived copy already exists from a prior run, the root
    copy (if any) is just removed. Same-file edge case is a no-op.
    """
    archive = folder / "_originals"
    archive.mkdir(exist_ok=True)
    out: dict[str, Path] = {}
    for p in images:
        dst = archive / p.name
        if dst.exists():
            try:
                if p.exists() and p.resolve() != dst.resolve():
                    p.unlink()
            except OSError:
                pass
        elif p.exists():
            shutil.move(str(p), str(dst))
        out[p.name] = dst
    return out


async def _api_call_one(
    client: httpx.AsyncClient,
    src: Path,
) -> tuple[Optional[bytes], Optional[int], Optional[str]]:
    """Returns (image_bytes, http_status, error_string). One retry on 5xx/network."""
    last_err: Optional[str] = None
    last_status: Optional[int] = None
    for attempt in (1, 2):
        try:
            with src.open("rb") as f:
                files = {"original_preview_image": (src.name, f, "image/jpeg")}
                r = await client.post(
                    DEWATERMARK_API_URL,
                    headers={"X-API-KEY": DEWATERMARK_API_KEY or ""},
                    files=files,
                    timeout=120.0,
                )
            last_status = r.status_code
            req_id = r.headers.get("x-request-id") or ""
            if 500 <= r.status_code < 600 and attempt == 1:
                last_err = f"HTTP {r.status_code} req={req_id} {r.text[:120]}"
                await asyncio.sleep(1.5)
                continue
            if r.status_code != 200:
                return None, r.status_code, f"HTTP {r.status_code} req={req_id} {r.text[:200]}"
            data = r.json()
            b64 = (data.get("edited_image") or {}).get("image")
            if not b64:
                return None, r.status_code, f"no edited_image in response req={req_id}"
            return base64.b64decode(b64), r.status_code, None
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            if attempt == 1:
                await asyncio.sleep(1.5)
                continue
            return None, last_status, last_err
    return None, last_status, last_err


def _publish_clean(
    folder: Path,
    src_name: str,
    payload: bytes,
    extension: str,
) -> Path:
    """Write cleaned bytes into folder root with predictable name. Returns the path."""
    stem = Path(src_name).stem
    out = folder / f"{stem}{extension}"
    out.write_bytes(payload)
    return out


def _verify_clean_or_revert(
    original: Path,
    cleaned: Path,
) -> bool:
    """Run the bottom-right histogram check on the cleaned output.
    Returns True if clean, False if the watermark still appears present."""
    try:
        return not watermark_likely_present(original, cleaned)
    except Exception:
        return False


async def dewatermark(
    ctx: BrowserContext | None,
    images: list[Path],
    folder: Path,
    *,
    lot_label: Optional[str] = None,
) -> list[Path]:
    if not images:
        return []

    archived = _archive_originals(folder, images)
    sidecar = cache.load_sidecar(folder)
    budget = RunBudget()
    lot = lot_label or folder.name

    final_paths: list[Path] = []
    needs_processing: list[Path] = []
    hashes: dict[str, str] = {}  # filename → sha
    failed: list[tuple[Path, str]] = []  # (original_path, reason)

    # ── Layer 1: per-folder sidecar
    for img in images:
        original = archived[img.name]
        sha = cache.sha256_of_file(original)
        hashes[img.name] = sha
        entry = sidecar.get(sha)
        if entry and cache.sidecar_entry_is_fresh(entry, folder):
            out = folder / entry["output_filename"]
            # Re-check quality of the cached output before trusting it.
            if _verify_clean_or_revert(original, out):
                progress.emit(
                    "dewatermark", event="cache_hit", layer="sidecar",
                    image=img.name, sha=sha, output=out.name,
                )
                final_paths.append(out)
                continue
            # Sidecar is stale (output drifted). Drop it and re-process.
            sidecar.pop(sha, None)
        needs_processing.append(img)

    # ── Layer 2: global API response cache
    needs_api: list[Path] = []
    for img in needs_processing:
        sha = hashes[img.name]
        original = archived[img.name]
        payload = cache.load_cached_response(sha)
        if payload is None:
            needs_api.append(img)
            continue
        out = _publish_clean(folder, img.name, payload, ".png")
        if _verify_clean_or_revert(original, out):
            cache.update_sidecar_entry(
                folder, sha,
                source_filename=img.name, output_filename=out.name,
                method="api_cache", status="clean", verified_clean=True,
            )
            progress.emit(
                "dewatermark", event="cache_hit", layer="global",
                image=img.name, sha=sha, output=out.name,
            )
            final_paths.append(out)
        else:
            # Cached bytes don't actually clean this image (very unusual — a
            # hash collision or corrupted cache). Treat as miss.
            try:
                out.unlink()
            except OSError:
                pass
            needs_api.append(img)

    # ── Layer 3: API call (budget-guarded, post-call re-check, never silent fallback)
    if needs_api and not DEWATERMARK_API_KEY:
        for img in needs_api:
            cache.update_sidecar_entry(
                folder, hashes[img.name],
                source_filename=img.name, output_filename=None,
                method="api", status="failed", verified_clean=False,
                failure_reason="no_api_key",
            )
            failed.append((img, "no_api_key"))
            progress.emit(
                "dewatermark", event="api_failed", image=img.name,
                reason="no_api_key",
            )
        needs_api = []

    if needs_api:
        async with httpx.AsyncClient() as client:
            for img in needs_api:
                sha = hashes[img.name]
                original = archived[img.name]
                bstatus = budget.check()
                if not bstatus.allowed:
                    cache.update_sidecar_entry(
                        folder, sha,
                        source_filename=img.name, output_filename=None,
                        method="api", status="failed", verified_clean=False,
                        failure_reason=bstatus.reason,
                    )
                    failed.append((img, bstatus.reason or "budget"))
                    progress.emit(
                        "dewatermark", event="budget_exceeded",
                        image=img.name, reason=bstatus.reason,
                    )
                    continue

                budget.record_call()
                payload, http_status, err = await _api_call_one(client, original)
                bytes_in = original.stat().st_size
                bytes_out = len(payload) if payload else 0
                cache.log_api_call(
                    lot=lot, image=img.name, sha=sha,
                    http_status=http_status, bytes_in=bytes_in, bytes_out=bytes_out,
                    ok=payload is not None, error=err,
                )
                progress.emit(
                    "dewatermark", event="api_call",
                    image=img.name, http_status=http_status,
                    ok=payload is not None,
                )

                if payload is None:
                    cache.update_sidecar_entry(
                        folder, sha,
                        source_filename=img.name, output_filename=None,
                        method="api", status="failed", verified_clean=False,
                        api_calls_delta=1, failure_reason=err or "api_error",
                    )
                    failed.append((img, err or "api_error"))
                    progress.emit(
                        "dewatermark", event="api_failed",
                        image=img.name, reason=err or "api_error",
                    )
                    continue

                # Provisionally write to root and re-check quality.
                out = _publish_clean(folder, img.name, payload, ".png")
                if not _verify_clean_or_revert(original, out):
                    try:
                        out.unlink()
                    except OSError:
                        pass
                    cache.update_sidecar_entry(
                        folder, sha,
                        source_filename=img.name, output_filename=None,
                        method="api", status="failed", verified_clean=False,
                        api_calls_delta=1, failure_reason="post_quality_check_failed",
                    )
                    failed.append((img, "post_quality_check_failed"))
                    progress.emit(
                        "dewatermark", event="api_failed",
                        image=img.name, reason="post_quality_check_failed",
                    )
                    continue

                # Clean! Cache globally so future runs of this hash are free.
                cache.store_cached_response(sha, payload)
                cache.update_sidecar_entry(
                    folder, sha,
                    source_filename=img.name, output_filename=out.name,
                    method="api", status="clean", verified_clean=True,
                    api_calls_delta=1,
                )
                progress.emit(
                    "dewatermark", event="api_clean",
                    image=img.name, output=out.name,
                )
                final_paths.append(out)

    if failed:
        progress.emit(
            "dewatermark", event="degraded",
            failed_count=len(failed),
            failed=[{"image": p.name, "reason": r} for p, r in failed],
        )
        names = ", ".join(p.name for p, _ in failed)
        print(f"[dewatermark: {len(failed)} image(s) NOT cleaned (originals retained in _originals/): {names}]")

    return final_paths


# ───────────────────────────── CLI ─────────────────────────────

def _find_original_for(path: Path) -> Optional[Path]:
    """Look for an `_originals/` dir at this path's level or any parent."""
    extensions = (".jpg", ".jpeg", ".png", ".webp")
    candidate_names = [path.name] + [f"{path.stem}{ext}" for ext in extensions]
    cur = path.parent
    seen = set()
    for _ in range(4):  # this dir, parent, grandparent, great-grandparent
        if cur in seen:
            break
        seen.add(cur)
        originals_dir = cur / "_originals"
        if originals_dir.exists():
            for name in candidate_names:
                cand = originals_dir / name
                if cand.exists():
                    return cand
        cur = cur.parent
    return None


def _cli_verify(path: Path) -> int:
    if not path.exists():
        print(f"verify: file not found: {path}", file=sys.stderr)
        return 2
    original = _find_original_for(path)
    if original is None:
        print(
            f"verify: no matching `_originals/<name>.{{jpg,jpeg,png,webp}}` found "
            f"walking up from {path.parent}; nothing to compare against."
        )
        return 3
    flagged = watermark_likely_present(original, path)
    if flagged:
        print(f"watermarked: {path.name} (vs {original})")
        return 1
    print(f"clean: {path.name} (vs {original})")
    return 0


def _cli_stats() -> int:
    s = cache.stats_summary()
    print(
        f"today: {s['today_calls']} calls, {s['today_failures']} failures | "
        f"this month: {s['month_calls']} calls, {s['month_failures']} failures | "
        f"all-time: {s['total_calls']} calls | "
        f"global cache: {s['cache_entries']} cleaned hashes on disk"
    )
    print(f"  log: {USAGE_LOG_PATH}")
    print(f"  cache: {API_CACHE_DIR}")
    return 0


def _cli() -> int:
    parser = argparse.ArgumentParser(prog="python -m automation.dewatermark")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_verify = sub.add_parser("verify", help="Quality-check an image against its _originals/ counterpart (no API call).")
    p_verify.add_argument("path", type=Path)
    sub.add_parser("stats", help="Print API call counts and global cache size.")
    args = parser.parse_args()
    if args.cmd == "verify":
        return _cli_verify(args.path)
    if args.cmd == "stats":
        return _cli_stats()
    return 2


if __name__ == "__main__":
    raise SystemExit(_cli())
