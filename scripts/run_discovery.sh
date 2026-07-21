#!/bin/sh
# Discovery cron entrypoint for the black-whole-discovery Render cron job.
#
# Why a script instead of an inline `dockerCommand`: Render's dockerCommand
# field mangled the nested quotes in `sh -c "python a && python b"`, so the
# whole string was exec'd as one program name and every run died with exit 127
# ("command not found"). A committed script removes all quoting ambiguity — the
# cron just runs `sh scripts/run_discovery.sh`.
set -e

# Public Surplus runs FIRST. Both scrapers share one Groq key for the quantity
# LLM; whichever runs second inherits an exhausted quota and gets 429'd on every
# chunk (→ 100% null quantities). PS used to run last and went fully dark; now
# it runs first so it gets a clean quota, and GovDeals (second) degrades
# gracefully via the 'qty unknown' path instead of vanishing.
#
# PS has no JSON API and this image ships no Chromium (see Dockerfile), so run
# the plain-HTTP path and forbid the Playwright fallback. Non-fatal on purpose:
# a PS failure must never cost us the GovDeals sync below (`set -e` is armed).
echo "[discovery] Public Surplus scrape -> staging DB"
PUBLICSURPLUS_USE_API=1 PUBLICSURPLUS_ALLOW_BROWSER=0 \
  python auction_extractors/public_surplus_automation.py \
  || echo "[discovery] Public Surplus scrape FAILED — continuing with GovDeals rows only"

echo "[discovery] GovDeals scrape -> staging DB"
python auction_extractors/govdeals_chairs_extraction.py

echo "[discovery] transfer staged listings -> Supabase"
python scripts/transfer_listings_to_supabase.py

echo "[discovery] done"
