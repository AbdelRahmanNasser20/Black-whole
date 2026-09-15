#!/usr/bin/env python3
"""Regenerate `automation/zip_centroids.py` — the committed 3-digit ZIP-prefix
centroid table the freight estimator geocodes lanes with.

Why a generated table instead of a library: `pgeocode` (the CRM's choice) drags
in pandas + numpy and downloads a ~10 MB dataset on first use. The web container
must resolve a ZIP with zero network and zero heavy deps, so we precompute the
~900 US 3-digit prefixes once, here, and commit the result (~30 KB). A prefix
centroid is accurate to roughly ±30 road miles, which moves an LTL estimate by
about ±$10 — well inside the quoted range spread.

Sources, in order of preference:
  1. GeoNames "US.zip" postal dump — https://download.geonames.org/export/zip/US.zip
     Licensed CC BY 4.0 (https://creativecommons.org/licenses/by/4.0/);
     credit is carried into the generated file's header.
  2. A local pgeocode cache (`~/.cache/pgeocode/US.txt` or `~/.pgeocode/US.txt`),
     which is the same GeoNames data as CSV with a header row.

This script is dev-only — nothing at runtime imports it. Coordinates are never
hand-written: if every source fails it exits non-zero rather than guess.

Usage:
    ./.venv/bin/python scripts/gen_zip_centroids.py            # download + write
    ./.venv/bin/python scripts/gen_zip_centroids.py --local    # local cache only
"""
from __future__ import annotations

import argparse
import csv
import io
import os
import sys
import urllib.request
import zipfile
from collections import defaultdict
from datetime import date
from pathlib import Path

GEONAMES_URL = "https://download.geonames.org/export/zip/US.zip"
LOCAL_CANDIDATES = (
    Path.home() / ".cache" / "pgeocode" / "US.txt",
    Path.home() / ".pgeocode" / "US.txt",
)
OUT_PATH = Path(__file__).resolve().parents[1] / "automation" / "zip_centroids.py"


def _rows_from_geonames_zip(blob: bytes):
    """Yield (zip5, lat, lon) from the GeoNames US.zip archive (TSV, no header).

    Columns: country, postal_code, place, admin1, admin1_code, admin2,
             admin2_code, admin3, admin3_code, latitude, longitude, accuracy
    """
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        with zf.open("US.txt") as fh:
            for raw in io.TextIOWrapper(fh, encoding="utf-8"):
                parts = raw.rstrip("\n").split("\t")
                if len(parts) < 11:
                    continue
                yield parts[1], parts[9], parts[10]


def _rows_from_pgeocode_csv(path: Path):
    """Yield (zip5, lat, lon) from a pgeocode-cached US.txt (CSV with header)."""
    with path.open("r", encoding="utf-8", newline="") as fh:
        for rec in csv.DictReader(fh):
            yield rec.get("postal_code"), rec.get("latitude"), rec.get("longitude")


def _fetch_geonames() -> bytes:
    req = urllib.request.Request(
        GEONAMES_URL, headers={"User-Agent": "blackwhole-zip-centroids/1.0"}
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return resp.read()


def load_rows(local_only: bool = False):
    """(rows_iterable, provenance_string). Raises SystemExit if nothing works."""
    if not local_only:
        try:
            blob = _fetch_geonames()
            return list(_rows_from_geonames_zip(blob)), (
                f"GeoNames {GEONAMES_URL} (CC BY 4.0), downloaded {date.today().isoformat()}"
            )
        except Exception as exc:  # noqa: BLE001 — fall through to the local cache
            print(f"[warn] GeoNames download failed: {exc}", file=sys.stderr)
    for path in LOCAL_CANDIDATES:
        if path.exists():
            return list(_rows_from_pgeocode_csv(path)), (
                f"local pgeocode cache {path} (GeoNames data, CC BY 4.0), "
                f"read {date.today().isoformat()}"
            )
    raise SystemExit(
        "no ZIP source available (GeoNames download failed and no local pgeocode "
        "cache found) — refusing to fabricate coordinates"
    )


def build_centroids(rows) -> dict[str, tuple[float, float]]:
    """Mean (lat, lon) per 3-digit ZIP prefix over every ZIP that carries one."""
    acc: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0, 0])
    for zip5, lat, lon in rows:
        z = (zip5 or "").strip()
        if len(z) != 5 or not z.isdigit():
            continue
        try:
            la, lo = float(lat), float(lon)
        except (TypeError, ValueError):
            continue
        if not (-90 <= la <= 90) or not (-180 <= lo <= 180):
            continue
        bucket = acc[z[:3]]
        bucket[0] += la
        bucket[1] += lo
        bucket[2] += 1
    return {
        prefix: (round(s_lat / n, 4), round(s_lon / n, 4))
        for prefix, (s_lat, s_lon, n) in sorted(acc.items())
        if n
    }


def render(centroids: dict[str, tuple[float, float]], provenance: str) -> str:
    lines = [
        '"""3-digit US ZIP-prefix centroids — GENERATED, do not hand-edit.',
        "",
        "Mean (latitude, longitude) of every 5-digit ZIP sharing a 3-digit prefix.",
        "Read by `automation.freight_estimate.zip_to_latlon` so a freight lane can",
        "be measured with zero dependencies and zero network at runtime (pgeocode,",
        "the CRM's choice, pulls pandas + numpy + a ~10 MB first-use download).",
        "",
        "A prefix centroid is good to roughly ±30 road miles — about ±$10 on an LTL",
        "estimate, well inside the quoted range spread.",
        "",
        f"Provenance: {provenance}",
        "Data © GeoNames, licensed CC BY 4.0 (https://creativecommons.org/licenses/by/4.0/).",
        "",
        "Regenerate:",
        "    ./.venv/bin/python scripts/gen_zip_centroids.py",
        f'"""',
        "from __future__ import annotations",
        "",
        f"# {len(centroids)} prefixes",
        "PREFIX_CENTROIDS: dict[str, tuple[float, float]] = {",
    ]
    for prefix, (lat, lon) in centroids.items():
        lines.append(f'    "{prefix}": ({lat}, {lon}),')
    lines.append("}")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--local", action="store_true", help="skip the download, use a local cache"
    )
    ap.add_argument("--out", default=str(OUT_PATH), help="output module path")
    args = ap.parse_args()

    rows, provenance = load_rows(local_only=args.local)
    centroids = build_centroids(rows)
    if len(centroids) < 800:
        raise SystemExit(
            f"only {len(centroids)} prefixes derived — source looks truncated, "
            "refusing to write a partial table"
        )
    out = Path(args.out)
    out.write_text(render(centroids, provenance), encoding="utf-8")
    print(f"wrote {out} — {len(centroids)} prefixes ({out.stat().st_size / 1024:.1f} KB)")
    print(f"source: {provenance}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
