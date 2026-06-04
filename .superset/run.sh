#!/usr/bin/env bash
# Workspace run command for Superset (press ⌘G → "Run Workspace Command").
# Builds first if needed (delegates to setup.sh), then starts the web app.
#   Public site:  http://127.0.0.1:8765/
#   Admin:        http://127.0.0.1:8765/admin
set -euo pipefail

REPO="${SUPERSET_WORKSPACE_PATH:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
cd "$REPO"

PY="${REPO}/.venv/bin/python"
# Self-heal: build the venv on first run so ⌘G works on a fresh worktree.
if [ ! -x "$PY" ]; then
  bash "${REPO}/.superset/setup.sh"
fi

echo ">> starting web app — admin at http://127.0.0.1:8765/admin (Ctrl-C to stop)"
exec "$PY" -m automation.web
