#!/usr/bin/env bash
# Run the auction_extractors scrapers end-to-end.
# Triggered by launchd (com.listing-automation.daily-scrape.plist) at 06:00 local.
# Manual trigger: `./scripts/daily_scrape.sh` — same entry point the scheduler uses.
set -uo pipefail

PROJECT="/Users/abdelnasser/Desktop/Black_whole_projects/listing_automation"
PY="${PROJECT}/.venv/bin/python"
LOG_DIR="${HOME}/.listing_automation/logs"
TS="$(date +%Y%m%d_%H%M%S)"
LOG="${LOG_DIR}/scrape_${TS}.log"

mkdir -p "$LOG_DIR"
cd "${PROJECT}/auction_extractors"

{
  echo "=== $(date -Iseconds) daily_scrape starting ==="
  echo "pwd: $(pwd)"
  echo "python: $PY"
  echo ""

  fail=0
  echo "--- GovDeals ---"
  "$PY" govdeals_chairs_extraction.py || fail=1
  echo ""
  echo "--- Public Surplus ---"
  "$PY" public_surplus_automation.py || fail=$((fail + 1))
  echo ""
  echo "--- BidSpotter ---"
  "$PY" bidspotter_automation.py || fail=$((fail + 1))
  echo ""

  echo "=== $(date -Iseconds) daily_scrape finished (fail=$fail) ==="
  exit "$fail"
} >> "$LOG" 2>&1
