#!/usr/bin/env bash
# Interim local entrypoint for the closing-price recorder, driven by
# ~/Library/LaunchAgents/com.blackwhole.recorder.plist every 300s until this
# moves to the Render cron (scripts/recorder_cron.sh + render.yaml's
# `recorder-run` service). See recorder/README.md "Deploy" for the full story.
#
# Deliberately hardcodes the MAIN checkout's venv python — launchd runs this
# with no shell profile, so there is no `python`/venv-activation on PATH, and
# a worktree (like the one this file may currently live in) has no venv of
# its own per the project's worktree convention.
set -euo pipefail
cd "$(dirname "$0")/.."
exec /Users/abdelnasser/Projects/blackwhole/listing_automation/.venv/bin/python -m recorder.cli run
