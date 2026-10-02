#!/usr/bin/env python
"""Move scraped-data prefixes out of the PUBLIC image bucket into the private one.

Why: `R2_BUCKET` (blackwhole-images) is served publicly at `R2_PUBLIC_BASE`.
The closed-auction raw archive (`archive/deal_lots_raw/…`) and the
listing-snapshot backups (`archive/listing_snapshots/…`) were written there, so
anyone who guessed a key could download the whole dataset. They belong in
`LOT_ARCHIVE_R2_BUCKET` (private, see `r2_images.private_bucket()`).

Three steps, each safe to re-run:

    python scripts/migrate_r2_prefix_private.py             # dry run (default)
    python scripts/migrate_r2_prefix_private.py --apply     # copy + verify + manifest
    python scripts/migrate_r2_prefix_private.py --delete    # delete public copies

`--apply` server-side copies every public key to the same key in the private
bucket, then stream-reads BOTH objects and records size + sha256 of each in a
manifest (`~/.blackwhole/backups/r2_migrate_<stamp>.json`, rewritten after
every key so an interrupted run leaves a usable partial). A key whose private
copy already matches is not copied again — resume is just "run it again".

`--delete` (latest manifest, or `--manifest PATH`) refuses unless EVERY entry
matched, re-HEADs each private object for its size, deletes only the manifest's
keys from the public bucket, and finally HEADs each public URL expecting 404.
Keys that appeared under the prefix after the manifest was written are left
alone and reported.
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from automation import config, r2_images  # noqa: E402,F401  (config loads .env)

PREFIXES = ("archive/deal_lots_raw/", "archive/listing_snapshots/")
MANIFEST_DIR = Path.home() / ".blackwhole" / "backups"
CHUNK = 1 << 20


def _client(cfg: dict):
    """S3 client with short connect / bounded read timeouts — the network here
    drops, and a hung socket must fail and retry rather than stall forever."""
    import boto3
    from botocore.config import Config
    return boto3.client(
        "s3", endpoint_url=cfg["endpoint"],
        aws_access_key_id=cfg["access_key"],
        aws_secret_access_key=cfg["secret_key"], region_name="auto",
        config=Config(signature_version="s3v4", connect_timeout=10,
                      read_timeout=60,
                      retries={"max_attempts": 8, "mode": "adaptive"}))


def _retry(fn, what: str, tries: int = 5):
    for i in range(tries):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001 - network flake: back off, retry
            if i == tries - 1:
                raise
            wait = 2 ** i
            print(f"    retry {what} in {wait}s ({type(e).__name__}: {e})", flush=True)
            time.sleep(wait)


def list_keys(s3, bucket: str, prefixes=PREFIXES) -> dict[str, int]:
    out: dict[str, int] = {}
    for pfx in prefixes:
        token = None
        while True:
            kw = {"Bucket": bucket, "Prefix": pfx}
            if token:
                kw["ContinuationToken"] = token
            page = _retry(lambda: s3.list_objects_v2(**kw), f"list {pfx}")
            for o in page.get("Contents", []):
                out[o["Key"]] = o["Size"]
            if not page.get("IsTruncated"):
                break
            token = page["NextContinuationToken"]
    return out


def digest(s3, bucket: str, key: str) -> tuple[int, str]:
    """(size, sha256) by streaming the object — never holds it whole."""
    def go():
        body = s3.get_object(Bucket=bucket, Key=key)["Body"]
        h, n = hashlib.sha256(), 0
        for chunk in body.iter_chunks(CHUNK):
            h.update(chunk)
            n += len(chunk)
        return n, h.hexdigest()
    return _retry(go, f"read {bucket}/{key}")


def head_size(s3, bucket: str, key: str) -> int | None:
    """Size of the object, or None when it does not exist (404 is an answer,
    not a flake — it is never retried)."""
    from botocore.exceptions import ClientError

    def go():
        try:
            return s3.head_object(Bucket=bucket, Key=key)["ContentLength"]
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                return None
            raise
    return _retry(go, f"head {key}", tries=3)


def public_status(url: str) -> int:
    req = urllib.request.Request(url, method="HEAD",
                                 headers={"User-Agent": "blackwhole-migrate/1"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def _write(path: Path, manifest: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(manifest, indent=2))
    tmp.replace(path)


def do_apply(s3, pub: str, priv: str, keys: dict[str, int]) -> Path:
    MANIFEST_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    path = MANIFEST_DIR / f"r2_migrate_{stamp}.json"
    manifest = {"created": stamp, "source_bucket": pub, "dest_bucket": priv,
                "prefixes": list(PREFIXES), "entries": []}
    for i, (key, size) in enumerate(sorted(keys.items()), 1):
        src_size, src_sha = digest(s3, pub, key)
        dst_size = head_size(s3, priv, key)
        copied = False
        if dst_size != src_size or digest(s3, priv, key)[1] != src_sha:
            _retry(lambda: s3.copy_object(
                Bucket=priv, Key=key,
                CopySource={"Bucket": pub, "Key": key}), f"copy {key}")
            copied = True
        d_size, d_sha = digest(s3, priv, key)
        ok = (src_size, src_sha) == (d_size, d_sha) == (size, src_sha)
        manifest["entries"].append({
            "key": key, "listed_size": size,
            "src_size": src_size, "src_sha256": src_sha,
            "dst_size": d_size, "dst_sha256": d_sha,
            "copied": copied, "match": ok})
        _write(path, manifest)
        print(f"  [{i}/{len(keys)}] {'OK ' if ok else 'BAD'} "
              f"{'copied' if copied else 'present'} {size:>11,}  {key}", flush=True)
    bad = [e for e in manifest["entries"] if not e["match"]]
    manifest["all_match"] = not bad
    _write(path, manifest)
    print(f"\n  manifest: {path}\n  {len(keys) - len(bad)}/{len(keys)} matched")
    if bad:
        print("  MISMATCH — do NOT run --delete")
    return path


def latest_manifest() -> Path | None:
    files = sorted(glob.glob(str(MANIFEST_DIR / "r2_migrate_*.json")))
    return Path(files[-1]) if files else None


def do_delete(s3, pub: str, priv: str, mpath: Path, public_base: str) -> int:
    m = json.loads(mpath.read_text())
    if m.get("source_bucket") != pub or m.get("dest_bucket") != priv:
        sys.exit(f"manifest buckets {m.get('source_bucket')}->{m.get('dest_bucket')} "
                 f"!= current {pub}->{priv}")
    entries = m["entries"]
    if not entries or not m.get("all_match") or not all(e["match"] for e in entries):
        sys.exit("manifest is not all-match — refusing to delete anything")
    for e in entries:   # private copy must still be there, same size
        if head_size(s3, priv, e["key"]) != e["dst_size"]:
            sys.exit(f"private copy of {e['key']} missing/changed — refusing")
    print(f"  manifest {mpath.name}: {len(entries)} keys verified in {priv}")

    for e in entries:
        _retry(lambda: s3.delete_object(Bucket=pub, Key=e["key"]),
               f"delete {e['key']}")
        print(f"  deleted public {e['key']}", flush=True)

    leftover = set(list_keys(s3, pub)) - {e["key"] for e in entries}
    if leftover:
        print(f"\n  NOTE {len(leftover)} public key(s) not in manifest, left alone:")
        for k in sorted(leftover):
            print(f"    {k}")

    print("\n  public URL HEAD (expect 404):")
    fails = 0
    for e in entries:
        st = public_status(f"{public_base}/{e['key']}")
        fails += st != 404
        print(f"    {st}  {e['key']}")
    print(f"\n  {len(entries) - fails}/{len(entries)} public URLs return 404")
    return 1 if fails else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--apply", action="store_true", help="copy + verify + manifest")
    g.add_argument("--delete", action="store_true", help="delete verified public copies")
    ap.add_argument("--manifest", help="manifest for --delete (default: latest)")
    a = ap.parse_args()

    cfg = r2_images.env_config()
    if not cfg:
        sys.exit("R2 is not configured — see .env")
    try:
        priv = r2_images.private_bucket()
    except r2_images.PrivateBucketNotConfigured as e:
        sys.exit(str(e))
    pub, s3 = cfg["bucket"], _client(cfg)

    if a.delete:
        mp = Path(a.manifest) if a.manifest else latest_manifest()
        if not mp or not mp.exists():
            sys.exit("no manifest — run --apply first")
        return do_delete(s3, pub, priv, mp, cfg["public_base"])

    keys = list_keys(s3, pub)
    print(f"  {pub} -> {priv}: {len(keys)} keys, "
          f"{sum(keys.values()) / 1e6:.1f} MB under {', '.join(PREFIXES)}")
    if not a.apply:
        present = list_keys(s3, priv)
        for k, sz in sorted(keys.items()):
            tag = "present" if present.get(k) == sz else "to-copy"
            print(f"    {tag:<8} {sz:>11,}  {k}")
        print("\n  DRY RUN — re-run with --apply")
        return 0
    if not keys:
        print("  nothing to migrate")
        return 0
    do_apply(s3, pub, priv, keys)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
