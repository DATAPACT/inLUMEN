#!/bin/bash
set -euo pipefail

pids=()

cleanup() {
  for pid in "${pids[@]}"; do
    if kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null || true
    fi
  done
  wait || true
}

trap cleanup EXIT INT TERM

python -u minio_api.py &
pids+=("$!")

python -u neo4j_api.py &
pids+=("$!")

python -u analytics_api.py &
pids+=("$!")

wait -n "${pids[@]}"
