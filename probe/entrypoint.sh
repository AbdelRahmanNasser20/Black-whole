#!/usr/bin/env bash
set -euo pipefail
# Headed Chromium needs a display; xvfb-run supplies a virtual one.
# -a auto-picks a free display; 24-bit depth matters for Chromium.
exec xvfb-run -a -s "-screen 0 1366x768x24" python /app/probe_govdeals.py
