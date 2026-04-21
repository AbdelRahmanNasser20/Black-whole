"""Dewatermark pipeline with idempotency, post-API quality re-check, and budget caps.

Per-image flow on every run:
  1. Hash the original (sha256 of bytes).
  2. Per-folder sidecar hit + on-disk output present + still-clean? → skip, emit `cache_hit`.
  3. Global API-response cache hit? → write bytes into folder, quality-check, save sidecar.
  4. IOPaint local pass (batch). Each result that passes quality is shipped as method=iopaint.
  5. For images IOPaint couldn't clean: budget-guarded API call. Post-call quality re-check.
     A successful clean is cached globally so future runs (same hash, any lot) are free.
     A failure is NEVER silently swapped for the watermarked IOPaint output — we leave
     the originals in `_originals/` and emit `dewatermark:degraded`.

Run `python -m automation.dewatermark verify <path>` and `... stats` for offline checks.
"""
from __future__ import annotations

import asyncio
import argparse
import base64
import shutil
import subprocess
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


def _archive_originals(folder: Path, images: list[Path]) -> Path:
    archive = folder / "_originals"
    archive.mkdir(exist_ok=True)
    for p in images:
        dst = archive / p.name
        if not dst.exists():
            shutil.copy2(p, dst)
    return archive


async def _run_iopaint(images: list[Path], folder: Path) -> dict[str, Path]:
    """Returns {original_filename: cleaned_path} for everything IOPaint produced."""
    mask_dir = folder / "_masks"
    mask_dir.mkdir(exist_ok=True)
    cleaned_dir = folder / "_cleaned_local"
    cleaned_dir.mkdir(exist_ok=True)

    try:
        from PIL import Image, ImageDraw  # type: ignore
    except ImportError:
        print("[iopaint: PIL not available, skipping]")
        return {}

    for img in images:
        im = Image.open(img).convert("RGB")
        w, h = im.size
        mask = Image.new("L", (w, h), 0)
        draw = ImageDraw.Draw(mask)
        draw.rectangle((int(w * 0.55), int(h * 0.82), w, h), fill=255)
        mask.save(mask_dir / img.name)

    cmd = [
        "iopaint", "run",
        "--model", "lama",
        "--device", "cpu",
        "--image", str(folder),
        "--mask", str(mask_dir),
        "--output", str(cleaned_dir),
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
    except FileNotFoundError:
        print("[iopaint: not installed, skipping local pass]")
        return {}
    _out, err = await proc.communicate()
    if proc.returncode != 0:
        print(f"[iopaint failed rc={proc.returncode}]\n{err.decode()[-400:]}")
        return {}

    # IOPaint preserves the source filename (jpg → jpg, png → png) since v1.6.
    # Older builds wrote .png regardless. Map back by stem so either works.
    by_stem: dict[str, Path] = {p.stem: p for p in cleaned_dir.iterdir() if p.is_file()}
    out: dict[str, Path] = {}
    for img in images:
        if img.stem in by_stem:
            out[img.name] = by_stem[img.stem]
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
            if 500 <= r.status_code < 600 and attempt == 1:
                last_err = f"HTTP {r.status_code} {r.text[:120]}"
                await asyncio.sleep(1.5)
                continue
            if r.status_code != 200:
                return None, r.status_code, f"HTTP {r.status_code} {r.text[:200]}"
            data = r.json()
            b64 = (data.get("edited_image") or {}).get("image")
            if not b64:
                return None, r.status_code, "no edited_image in response"
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

    _archive_originals(folder, images)
    sidecar = cache.load_sidecar(folder)
    budget = RunBudget()
    lot = lot_label or folder.name

    final_paths: list[Path] = []
    needs_processing: list[Path] = []
    hashes: dict[str, str] = {}  # filename → sha
    failed: list[tuple[Path, str]] = []  # (original_path, reason)

    # ── Layer 1: per-folder sidecar
    for img in images:
        sha = cache.sha256_of_file(img)
        hashes[img.name] = sha
        entry = sidecar.get(sha)
        if entry and cache.sidecar_entry_is_fresh(entry, folder):
            out = folder / entry["output_filename"]
            # Re-check quality of the cached output before trusting it.
            if _verify_clean_or_revert(img, out):
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
    still_unprocessed: list[Path] = []
    for img in needs_processing:
        sha = hashes[img.name]
        payload = cache.load_cached_response(sha)
        if payload is None:
            still_unprocessed.append(img)
            continue
        out = _publish_clean(folder, img.name, payload, ".png")
        if _verify_clean_or_revert(img, out):
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
            # The cached bytes don't actually clean this image (very unusual —
            # would mean a hash collision or corrupted cache). Treat as miss.
            try:
                out.unlink()
            except OSError:
                pass
            still_unprocessed.append(img)

    # ── Layer 3: IOPaint local pass (batch)
    iopaint_results: dict[str, Path] = {}
    if still_unprocessed:
        iopaint_results = await _run_iopaint(still_unprocessed, folder)

    needs_api: list[Path] = []
    for img in still_unprocessed:
        local_clean = iopaint_results.get(img.name)
        if local_clean and _verify_clean_or_revert(img, local_clean):
            stem = img.stem
            out = folder / f"{stem}{local_clean.suffix}"
            shutil.copy2(local_clean, out)
            cache.update_sidecar_entry(
                folder, hashes[img.name],
                source_filename=img.name, output_filename=out.name,
                method="iopaint", status="clean", verified_clean=True,
            )
            progress.emit(
                "dewatermark", event="iopaint_clean",
                image=img.name, output=out.name,
            )
            final_paths.append(out)
        else:
            needs_api.append(img)

    # ── Layer 4: API fallback (budget-guarded, post-call re-check, never silent fallback)
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
                payload, http_status, err = await _api_call_one(client, img)
                bytes_in = img.stat().st_size
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
                if not _verify_clean_or_revert(img, out):
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
