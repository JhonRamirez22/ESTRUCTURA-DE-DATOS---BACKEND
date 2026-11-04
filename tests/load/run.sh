#!/bin/sh
# Uso: [RAMP=60s] tests/load/run.sh <usuarios> [duración]   (la API local debe estar en :8100)
VUS=${1:-200}; DUR=${2:-60s}
k6 run --quiet --no-color -e VUS=$VUS -e DURATION=$DUR -e RAMP=${RAMP:-15s} tests/load/k6_mixed.js 2>&1 \
  | grep -E "server_errors_5xx|shed_503|bandeja_llena|http_req_duration|http_req_failed|http_reqs|checks_|login_ms|read_ms|iterations|THRESHOLD|✓|✗" | cut -c1-150
