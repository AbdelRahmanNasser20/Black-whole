#!/usr/bin/env bash
# Build step for the Superset workspace run. Creates the .venv and installs deps.
# Runs on workspace creation (config.json "setup") and is reused by run.sh.
# Idempotent — safe to re-run.
set -euo pipefail

REPO="${SUPERSET_WORKSPACE_PATH:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
cd "$REPO"

# .env is gitignored, so worktrees don't get it. Copy it from the root repo.
if [ ! -f "${REPO}/.env" ] && [ -n "${SUPERSET_ROOT_PATH:-}" ] && [ -f "${SUPERSET_ROOT_PATH}/.env" ]; then
  echo ">> copying .env from ${SUPERSET_ROOT_PATH}"
  cp "${SUPERSET_ROOT_PATH}/.env" "${REPO}/.env"
fi

# The auction scrape cache is gitignored too, so worktrees start with an empty
# listings.db. Seed it from the root repo when ours is missing or empty.
SRC_CACHE="${SUPERSET_ROOT_PATH:-}/auction_extractors/state/listings.db"
DST_CACHE="${REPO}/auction_extractors/state/listings.db"
if [ -n "${SUPERSET_ROOT_PATH:-}" ] && [ -f "$SRC_CACHE" ]; then
  rows=0
  if command -v sqlite3 >/dev/null 2>&1 && [ -f "$DST_CACHE" ]; then
    rows="$(sqlite3 -readonly "$DST_CACHE" 'SELECT count(*) FROM listings' 2>/dev/null || echo 0)"
  fi
  if [ ! -f "$DST_CACHE" ] || [ "${rows:-0}" -eq 0 ] 2>/dev/null; then
    echo ">> seeding auction cache from ${SUPERSET_ROOT_PATH}"
    mkdir -p "$(dirname "$DST_CACHE")"
    cp "$SRC_CACHE" "$DST_CACHE"
  fi
fi

VENV="${REPO}/.venv"
PY="${VENV}/bin/python"

if [ ! -x "$PY" ]; then
  BOOTSTRAP=""
  for cand in python3.11 python3.12 python3.13 python3; do
    if command -v "$cand" >/dev/null 2>&1; then
      ver="$("$cand" -c 'import sys; print("%d%d" % sys.version_info[:2])' 2>/dev/null || echo 0)"
      if [ "$ver" -ge 311 ] 2>/dev/null; then BOOTSTRAP="$cand"; break; fi
    fi
  done
  [ -n "$BOOTSTRAP" ] || { echo "error: need Python 3.11+ to create the venv (none found)." >&2; exit 1; }
  echo ">> creating venv at ${VENV} (using ${BOOTSTRAP})"
  "$BOOTSTRAP" -m venv "$VENV"
fi

"$PY" -m pip install --quiet --upgrade pip
echo ">> installing dependencies (pip install -e .)"
"$PY" -m pip install --quiet -e .
echo ">> setup complete"
