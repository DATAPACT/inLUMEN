#!/usr/bin/env bash
# Deploy one commit's published GHCR images to the production Compose project.
#
#   scripts/deploy_vm.sh <40-character commit SHA>
#
# Run on the deployment host, normally by the Deploy workflow. Steps:
#   1. Pull that commit's images (nothing is stopped yet; a failed pull
#      leaves the running deployment untouched).
#   2. Back up the project's volumes with scripts/backup_volumes.py. This
#      stops the inLUMEN containers for the length of the backup.
#   3. Check out the commit, so the Compose file matches the images.
#   4. Start the services. Compose runs `migrate` before the backend.
#   5. Wait until backend, codegen, runner and frontend are healthy and run
#      the expected images, and until the public URL answers.
#   6. On any failure in 3-5, put back the previous checkout and image
#      settings and start them again. The database is NOT rolled back: if a
#      migration ran, restore it from the backup taken in step 2.
#
# Settings (environment):
#   INLUMEN_DEPLOY_DIR      checkout with docker-compose-prod.yml and
#                           .env.production (required)
#   INLUMEN_BACKUP_DIR      where backups go (default: ~/backups/inlumen)
#   INLUMEN_BACKUP_KEEP     backups to keep (default: 3)
#   INLUMEN_SKIP_BACKUP     "true" skips step 2
#   INLUMEN_IMAGE_REGISTRY  image prefix (default: ghcr.io/datapact)
#   INLUMEN_HEALTH_URL      public URL to check (optional)
#   INLUMEN_HEALTH_TIMEOUT  seconds to wait for health (default: 600)
#
# The chosen images are written to .env.deploy next to .env.production, which
# holds no secrets. Compose reads .env.production first and .env.deploy second.
set -Eeuo pipefail

COMMIT="${1:-}"
[[ "$COMMIT" =~ ^[0-9a-f]{40}$ ]] || { echo "usage: $0 <40-character commit SHA>" >&2; exit 2; }

DIR="${INLUMEN_DEPLOY_DIR:?INLUMEN_DEPLOY_DIR is required}"
BACKUP_ROOT="${INLUMEN_BACKUP_DIR:-$HOME/backups/inlumen}"
BACKUP_KEEP="${INLUMEN_BACKUP_KEEP:-3}"
REGISTRY="${INLUMEN_IMAGE_REGISTRY:-ghcr.io/datapact}"
HEALTH_TIMEOUT="${INLUMEN_HEALTH_TIMEOUT:-600}"
PROJECT=inlumen
SERVICES=(backend codegen runner frontend)

cd "$DIR"
[ -f .env.production ] || { echo "$DIR/.env.production is missing" >&2; exit 1; }

# One deploy at a time, whoever starts it.
exec 9>"$DIR/.deploy.lock"
flock -n 9 || { echo "Another deploy is running" >&2; exit 1; }

log() { printf '[%s] %s\n' "$(date -u +%H:%M:%S)" "$*"; }

compose() {
  local env_files=(--env-file .env.production)
  [ -f .env.deploy ] && env_files+=(--env-file .env.deploy)
  docker compose -p "$PROJECT" "${env_files[@]}" -f docker-compose-prod.yml "$@"
}

write_env() {  # write_env <file> <sha>
  cat >"$1" <<EOF
# Written by scripts/deploy_vm.sh. Images for commit $2.
INLUMEN_BACKEND_IMAGE=$REGISTRY/inlumen-backend:$2
INLUMEN_CODEGEN_IMAGE=$REGISTRY/inlumen-codegen-service:$2
INLUMEN_RUNNER_IMAGE=$REGISTRY/inlumen-runner-service:$2
INLUMEN_FRONTEND_IMAGE=$REGISTRY/inlumen-frontend:$2
INLUMEN_APP_PULL_POLICY=missing
EOF
}

expected_image() {  # expected_image <service>
  case "$1" in
    backend) echo "$REGISTRY/inlumen-backend:$COMMIT" ;;
    codegen) echo "$REGISTRY/inlumen-codegen-service:$COMMIT" ;;
    runner) echo "$REGISTRY/inlumen-runner-service:$COMMIT" ;;
    frontend) echo "$REGISTRY/inlumen-frontend:$COMMIT" ;;
  esac
}

wait_healthy() {  # wait_healthy [check_images]
  local deadline=$((SECONDS + HEALTH_TIMEOUT)) service id state image pending
  while :; do
    pending=()
    for service in "${SERVICES[@]}"; do
      id=$(compose ps -q "$service" 2>/dev/null || true)
      if [ -z "$id" ]; then pending+=("$service:missing"); continue; fi
      state=$(docker inspect -f '{{.State.Status}}/{{if .State.Health}}{{.State.Health.Status}}{{end}}' "$id")
      [ "$state" = "running/healthy" ] || pending+=("$service:$state")
      if [ "${1:-}" = check_images ]; then
        image=$(docker inspect -f '{{.Config.Image}}' "$id")
        [ "$image" = "$(expected_image "$service")" ] || { log "$service runs $image, expected $(expected_image "$service")"; return 1; }
      fi
    done
    if [ ${#pending[@]} -eq 0 ]; then
      if [ -z "${INLUMEN_HEALTH_URL:-}" ] || curl -fsS -o /dev/null --max-time 10 "$INLUMEN_HEALTH_URL"; then
        return 0
      fi
      pending+=("public-url")
    fi
    if [ $SECONDS -ge $deadline ]; then log "Not healthy after ${HEALTH_TIMEOUT}s: ${pending[*]}"; return 1; fi
    sleep 5
  done
}

record() {  # record <result>
  mkdir -p state/deployments
  local file
  file="state/deployments/$(date -u +%Y%m%dT%H%M%SZ)-${COMMIT:0:12}.json"
  jq -n --arg commit "$COMMIT" --arg previous "$PREV_HEAD" --arg result "$1" \
    --arg backup "${BACKUP_DIR:-}" --arg by "${GITHUB_ACTOR:-$USER}" \
    --arg run "${GITHUB_SERVER_URL:-}/${GITHUB_REPOSITORY:-}/actions/runs/${GITHUB_RUN_ID:-}" \
    '{commit: $commit, previous_commit: $previous, result: $result, backup: $backup, by: $by, run: $run}' >"$file"
  log "Recorded $file"
}

PREV_HEAD=$(git rev-parse HEAD)
PREV_ENV=$(mktemp)
if [ -f .env.deploy ]; then cp .env.deploy "$PREV_ENV"; else rm -f "$PREV_ENV"; fi

if [ "$PREV_HEAD" = "$COMMIT" ] && [ -f .env.deploy ] && grep -q ":$COMMIT\$" .env.deploy \
  && HEALTH_TIMEOUT=0 wait_healthy check_images; then
  log "Commit $COMMIT is already deployed and healthy; nothing to do."
  exit 0
fi

# 1. Pull first, while the current deployment keeps running.
git fetch --quiet origin
git cat-file -e "$COMMIT^{commit}"
NEXT_ENV="$DIR/.env.deploy.next"
write_env "$NEXT_ENV" "$COMMIT"
log "Pulling images for $COMMIT"
docker compose -p "$PROJECT" --env-file .env.production --env-file "$NEXT_ENV" \
  -f docker-compose-prod.yml pull --quiet migrate "${SERVICES[@]}"

# 2. Back up.
if [ "${INLUMEN_SKIP_BACKUP:-false}" = true ]; then
  log "Skipping backup (INLUMEN_SKIP_BACKUP=true)"
else
  BACKUP_DIR="$BACKUP_ROOT/$(date -u +%Y%m%dT%H%M%SZ)-before-${COMMIT:0:12}"
  mkdir -p "$BACKUP_ROOT" && chmod 700 "$BACKUP_ROOT"
  log "Backing up volumes to $BACKUP_DIR (inLUMEN is stopped meanwhile)"
  python3 scripts/backup_volumes.py backup "$BACKUP_DIR" --project "$PROJECT"
  cp .env.production "$BACKUP_DIR/env.production"
  chmod 600 "$BACKUP_DIR/env.production"
  # Keep only the newest backups.
  find "$BACKUP_ROOT" -mindepth 1 -maxdepth 1 -type d -name '*-before-*' | sort -r | tail -n +"$((BACKUP_KEEP + 1))" | xargs -r rm -rf \
    || log "Could not delete some old backups in $BACKUP_ROOT"
fi

rollback() {
  trap - ERR
  log "Deploy failed; restoring $PREV_HEAD"
  git -c advice.detachedHead=false checkout --quiet --detach "$PREV_HEAD"
  if [ -f "$PREV_ENV" ]; then cp "$PREV_ENV" .env.deploy; else rm -f .env.deploy; fi
  # Without .env.deploy the services use local images, which Compose builds.
  if compose up -d --remove-orphans && wait_healthy; then
    log "Previous version is running again."
    record rolled-back
  else
    log "Previous version is NOT healthy. Check: docker compose -p $PROJECT ps"
    record rollback-failed
  fi
  exit 1
}

# 3-5. Switch, start, check.
trap rollback ERR
git -c advice.detachedHead=false checkout --quiet --detach "$COMMIT"
mv "$NEXT_ENV" .env.deploy
log "Starting services"
compose up -d --no-build --remove-orphans
migrate_id=$(compose ps -a -q migrate)
[ "$(docker inspect -f '{{.State.ExitCode}}' "$migrate_id")" = 0 ] || { log "Migration failed"; compose logs --tail 50 migrate; false; }
log "Waiting for health checks"
wait_healthy check_images
trap - ERR

record deployed
rm -f "$PREV_ENV"

# Keep the images of this deploy and the previous one; remove older ones.
keep="$COMMIT"
[ "$PREV_HEAD" != "$COMMIT" ] && keep="$keep|$PREV_HEAD"
docker image ls --format '{{.Repository}}:{{.Tag}}' \
  | grep -E "^$REGISTRY/inlumen-[a-z-]+:[0-9a-f]{40}\$" \
  | grep -vE ":($keep)\$" \
  | xargs -r docker image rm >/dev/null || true
docker image prune -f >/dev/null || true
log "Deployed $COMMIT"
