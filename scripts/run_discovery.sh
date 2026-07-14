#!/bin/sh
# Discovery cron entrypoint for the black-whole-discovery Render cron job.
#
# Why a script instead of an inline `dockerCommand`: Render's dockerCommand
# field mangled the nested quotes in `sh -c "python a && python b"`, so the
# whole string was exec'd as one program name and every run died with exit 127
# ("command not found"). A committed script removes all quoting ambiguity — the
# cron just runs `sh scripts/run_discovery.sh`.
set -e

echo "[discovery] GovDeals scrape -> staging DB"
python auction_extractors/govdeals_chairs_extraction.py

# Public Surplus was absent here until 2026-07-14, so PS rows only ever landed
# when someone ran a scrape from the dashboard by hand. They last did on 06-17,
# and /api/auctions drops anything not re-seen within max_stale_days=2 — so the
# Auctions tab showed zero PS listings regardless of what PS actually had.
#
# PS has no JSON API and this image ships no Chromium (see Dockerfile), so run
# the plain-HTTP path and forbid the Playwright fallback. Non-fatal on purpose:
# a PS failure must never cost us the GovDeals sync below (`set -e` is armed).
echo "[discovery] Public Surplus scrape -> staging DB"
PUBLICSURPLUS_USE_API=1 PUBLICSURPLUS_ALLOW_BROWSER=0 \
  python auction_extractors/public_surplus_automation.py \
  || echo "[discovery] Public Surplus scrape FAILED — continuing with GovDeals rows only"

echo "[discovery] transfer staged listings -> Supabase"
python scripts/transfer_listings_to_supabase.py

echo "[discovery] done"
