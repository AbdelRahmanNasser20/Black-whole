#!/usr/bin/env bash
# Launch the listing-automation web app (public site + admin dashboard).
#
#   ./scripts/run_app.sh
#
# First run bootstraps a local .venv and installs deps; later runs reuse it.
# Public site:  http://127.0.0.1:8765/
# Admin:        http://127.0.0.1:8765/admin
set -euo pipefail

# Resolve repo root from this script's location (worktree-safe; no hardcoded path).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "$REPO"

VENV="${REPO}/.venv"
PY="${VENV}/bin/python"

# Bootstrap the venv on first run (needs Python 3.11+ per pyproject.toml).
if [ ! -x "$PY" ]; then
  BOOTSTRAP=""
  for cand in python3.11 python3.12 python3.13 python3; do
    if command -v "$cand" >/dev/null 2>&1; then
      ver="$("$cand" -c 'import sys; print("%d%d" % sys.version_info[:2])' 2>/dev/null || echo 0)"
      if [ "$ver" -ge 311 ] 2>/dev/null; then BOOTSTRAP="$cand"; break; fi
    fi
  done
  if [ -z "$BOOTSTRAP" ]; then
    echo "error: need Python 3.11+ to create the venv (none found)." >&2
    exit 1
  fi
  echo ">> creating venv at ${VENV} (using ${BOOTSTRAP})"
  "$BOOTSTRAP" -m venv "$VENV"
  "$PY" -m pip install --quiet --upgrade pip
  echo ">> installing dependencies (pip install -e .)"
  "$PY" -m pip install --quiet -e .
fi

URL="http://127.0.0.1:8765/admin"
# Open the dashboard in the default browser once the server is up (best-effort).
( sleep 2; command -v open >/dev/null 2>&1 && open "$URL" ) &

echo ">> starting server — admin at ${URL} (Ctrl-C to stop)"
exec "$PY" -m automation.web
