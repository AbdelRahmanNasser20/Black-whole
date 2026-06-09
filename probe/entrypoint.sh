#!/usr/bin/env bash
set -euo pipefail
# Headed Chromium needs a display. Start Xvfb ourselves in the background and
# exec python directly — do NOT use `exec xvfb-run`, which becomes PID 1 and
# hangs in rt_sigsuspend (its shell trap/wait child-reaping breaks as PID 1),
# stalling the run even though the browser works fine.
Xvfb :99 -screen 0 1366x768x24 -nolisten tcp &
export DISPLAY=:99
# Give Xvfb a moment to come up before Chromium connects.
for _ in $(seq 1 20); do
  [ -e /tmp/.X11-unix/X99 ] && break
  sleep 0.25
done
exec python /app/probe_govdeals.py
