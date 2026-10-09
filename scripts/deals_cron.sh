#!/bin/sh
# Render cron entrypoint for the deals tracker: deals_cron.sh <subcommand>
# (committed script avoids the inline sh -c quote-mangling that broke
# run_discovery.sh's predecessor — see render.yaml history)
set -e
cd "$(dirname "$0")/.."

# Short-lived crons use the Supabase TRANSACTION pooler (port 6543), not the
# SESSION pooler (5432). The session pooler has 15 slots and the long-lived
# processes (web, recorder) hold them; a cron that competes for one can wait
# on, or starve, the web. Transaction mode hands a slot back after every
# statement, which is all a cron needs. Set DEALS_SESSION_POOLER=1 to keep
# 5432 (LISTEN/NOTIFY or anything else that needs a sticky session).
if [ "${DEALS_SESSION_POOLER:-0}" != "1" ] && [ -n "${BLACKWHOLE_DB_URL:-}" ]; then
  BLACKWHOLE_DB_URL=$(printf '%s' "$BLACKWHOLE_DB_URL" | sed 's#:5432/#:6543/#')
  export BLACKWHOLE_DB_URL
fi

exec python -m deals.cli "$@"
