#!/usr/bin/env bash
# Render cron entrypoint for the closing-price recorder: recorder_cron.sh <subcommand>
# (committed script — mirrors scripts/deals_cron.sh, which exists to avoid
# the inline `sh -c` quote-mangling that broke run_discovery.sh's predecessor)
set -euo pipefail
cd "$(dirname "$0")/.."
exec python -m recorder.cli "$@"
