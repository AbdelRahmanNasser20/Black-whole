#!/usr/bin/env bash
# Local entrypoint for the closed-lot blob archiver, driven by
# ~/Library/LaunchAgents/com.blackwhole.archive-raw.plist every 6h.
#
# Why this exists alongside render.yaml's `deals-archive-raw` cron: that cron
# needs the five R2_* values set in the Render dashboard's blackwhole-secrets
# group, and until they are, `env_config()` returns None and the run exits
# non-zero rather than nulling blobs with nowhere to put them. Meanwhile the
# database keeps growing — it went 452 MB -> 530 MB in the three days after
# the 2026-08-28 purge, back over the free tier's 500 MB read-only ceiling,
# purely because nothing was draining `raw`.
#
# So this is the belt to the cloud cron's braces. Both are idempotent and
# state-defined: whichever runs first archives what is pending, and the other
# prints "nothing pending" and exits 0. Running both is harmless.
#
# Hardcodes the MAIN checkout's venv python for the same reason
# recorder_local.sh does: launchd runs this with no shell profile, so there is
# no venv activation on PATH.
set -euo pipefail
cd "$(dirname "$0")/.."
exec /Users/abdelnasser/Projects/blackwhole/listing_automation/.venv/bin/python \
    -m deals.cli archive-raw --limit 6000
