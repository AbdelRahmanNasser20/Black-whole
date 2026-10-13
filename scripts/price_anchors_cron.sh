#!/bin/sh
# Render cron entrypoint: price_anchors_cron.sh run
# Short-lived cron → Supabase TRANSACTION pooler (6543), same reason as
# deals_cron.sh: the 15-slot session pooler (5432) belongs to web + recorder.
set -e
cd "$(dirname "$0")/.."
if [ -n "${BLACKWHOLE_DB_URL:-}" ]; then
  BLACKWHOLE_DB_URL=$(printf '%s' "$BLACKWHOLE_DB_URL" | sed 's#:5432/#:6543/#')
  export BLACKWHOLE_DB_URL
fi
exec python scripts/price_anchors.py "$@"
