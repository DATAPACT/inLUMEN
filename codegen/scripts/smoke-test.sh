#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -f "${ROOT_DIR}/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "${ROOT_DIR}/.env"
  set +a
fi

BASE_URL="${CODEGEN_BASE_URL:-http://127.0.0.1:${CODEGEN_PORT:-8010}}"
SERVICE_API_KEY="${CODEGEN_SERVICE_API_KEY:-}"

if [[ -z "${SERVICE_API_KEY}" ]]; then
  echo "CODEGEN_SERVICE_API_KEY is required for the authenticated smoke test." >&2
  exit 1
fi

echo "Waiting for ${BASE_URL}/health ..."
for _ in $(seq 1 30); do
  if curl -fsS "${BASE_URL}/health" >/dev/null; then
    echo "Codegen service is healthy."
    break
  fi
  sleep 1
done

curl -fsS "${BASE_URL}/health"
echo

curl -fsS \
  -X POST "${BASE_URL}/v1/generate/node-script" \
  -H "Authorization: Bearer ${SERVICE_API_KEY}" \
  -H "Content-Type: application/json" \
  --data @"${ROOT_DIR}/examples/generate-node-script-request.json" \
  | python3 -c "import json,sys; p=json.load(sys.stdin); print(p['flow_id']); print(p['generated_artifact']['validation_report']['status']); print('\\n'.join(f['filename'] for f in p['generated_artifact']['files']))"
