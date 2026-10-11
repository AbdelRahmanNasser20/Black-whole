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

# Per-host proxy for the sources that block the operator's Egypt IP
# (publicsurplus.com: connect timeouts/refused; mibid.michigan.gov: HTTP 403 —
# recorder/README.md "Per-host proxy"). launchd passes NO environment, so the
# defaults live here; a value in the environment or in `.env` (loaded by
# automation/config.py at import) always wins — `.env` beats the defaults
# because we only export when neither defines the key.
ENV_FILE=".env"
default_env() {   # default_env NAME VALUE
    local name="$1" value="$2"
    if [ -n "${!name:-}" ]; then return; fi
    if [ -f "$ENV_FILE" ] && grep -qE "^[[:space:]]*(export[[:space:]]+)?${name}=" "$ENV_FILE"; then return; fi
    export "${name}=${value}"
}
default_env RECORDER_PROXY_URL   "socks5h://127.0.0.1:1081"
default_env RECORDER_PROXY_HOSTS "publicsurplus.com,mibid.michigan.gov"

exec /Users/abdelnasser/Projects/blackwhole/listing_automation/.venv/bin/python -m recorder.cli run
