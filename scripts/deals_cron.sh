#!/bin/sh
# Render cron entrypoint for the deals tracker: deals_cron.sh <subcommand>
# (committed script avoids the inline sh -c quote-mangling that broke
# run_discovery.sh's predecessor — see render.yaml history)
set -e
cd "$(dirname "$0")/.."
exec python -m deals.cli "$@"
