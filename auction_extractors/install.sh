#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if [[ ! -d .venv ]]; then
  python3 -m venv .venv
fi
.venv/bin/pip install -U pip
.venv/bin/pip install -r requirements.txt
.venv/bin/playwright install chromium
echo ""
echo "Next: cp .env.example .env && edit .env"
echo "Run:  ./run_all.sh"
