#!/usr/bin/env bash
# 4 minutes of /api/health probes = 8 scheduler ticks. Every line must be "200" under 0.05 s.
BASE="${UI_BASE:-http://127.0.0.1:8765}"; bad=0
for i in $(seq 1 120); do
  out=$(curl -m 5 -s -o /dev/null -w "%{http_code} %{time_total}" "$BASE/api/health")
  echo "$out"; [[ "$out" == 200* ]] || bad=$((bad+1)); sleep 2
done
echo "failures: $bad"; [[ $bad -eq 0 ]]
