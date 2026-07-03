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

echo "[discovery] transfer staged listings -> Supabase"
python scripts/transfer_listings_to_supabase.py

echo "[discovery] done"
