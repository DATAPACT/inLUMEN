#!/usr/bin/env bash
# Incremental backup of the production inLUMEN data with restic, meant to run
# weekly from cron on the deployment host:
#
#   scripts/backup_vm.sh           back up, then apply the retention policy
#   scripts/backup_vm.sh --check   also verify the repository afterwards
#
# Each run makes one restic snapshot of /data containing:
#   staging/postgres.sql  pg_dump of the running database (consistent, no downtime;
#                     plain SQL so unchanged rows deduplicate between weeks)
#   staging/neo4j/    copy of the Neo4j data volume. Neo4j Community has no
#                     online backup, so Neo4j is stopped for the copy only,
#                     usually a few seconds; graph requests fail meanwhile.
#   minio/            MinIO data, read while running
#   runner_artifacts/ runner artifacts, read while running
#   env.production    .env.production (secrets; the repository is encrypted)
#
# restic stores only chunks it has not seen before, so every run after the
# first is incremental, while every snapshot is still a full restore point.
# Restore with `restic restore <snapshot> --target <dir>`; see
# docs/production-multi-user.md.
#
# Settings (environment):
#   INLUMEN_DEPLOY_DIR           checkout with .env.production
#                                (default: this script's checkout)
#   INLUMEN_RESTIC_REPO          repository directory
#                                (default: ~/backups/inlumen-restic)
#   INLUMEN_RESTIC_PASSWORD_FILE repository password; keep a copy elsewhere,
#                                without it the backups cannot be read
#                                (default: ~/.config/inlumen-backup/restic-password)
#   INLUMEN_RESTIC_IMAGE         default: restic/restic:0.19.1
#   INLUMEN_BACKUP_KEEP_WEEKLY   default: 8
#   INLUMEN_BACKUP_KEEP_MONTHLY  default: 6
set -Eeuo pipefail

DIR="${INLUMEN_DEPLOY_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
REPO="${INLUMEN_RESTIC_REPO:-$HOME/backups/inlumen-restic}"
PASSWORD_FILE="${INLUMEN_RESTIC_PASSWORD_FILE:-$HOME/.config/inlumen-backup/restic-password}"
IMAGE="${INLUMEN_RESTIC_IMAGE:-restic/restic:0.19.1}"
KEEP_WEEKLY="${INLUMEN_BACKUP_KEEP_WEEKLY:-8}"
KEEP_MONTHLY="${INLUMEN_BACKUP_KEEP_MONTHLY:-6}"
PROJECT=inlumen
HOST_TAG=inlumen-vm
STAGE="$HOME/backups/.inlumen-backup-staging"
CACHE="$HOME/.cache/inlumen-restic"

log() { printf '[%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

cd "$DIR"
[ -f .env.production ] || { log "$DIR/.env.production is missing"; exit 1; }
[ -s "$PASSWORD_FILE" ] || { log "Password file $PASSWORD_FILE is missing or empty"; exit 1; }

# Deploys take the same lock; wait for a running deploy to finish.
exec 9>"$DIR/.deploy.lock"
flock -w 3600 9 || { log "A deploy held the lock for an hour; no backup taken"; exit 1; }

container() {  # container <service>: running container ID of a project service
  docker ps -q --filter "label=com.docker.compose.project=$PROJECT" \
    --filter "label=com.docker.compose.service=$1"
}

env_value() {  # env_value <key> <default>
  local value
  value=$(sed -n "s/^$1=//p" .env.production | tail -1)
  printf '%s' "${value:-$2}"
}

# Runs as root in the container so it can read every volume.
restic() {
  docker run --rm -i --hostname "$HOST_TAG" \
    -e RESTIC_REPOSITORY=/repo -e RESTIC_PASSWORD_FILE=/run/restic-password \
    -v "$REPO:/repo" -v "$PASSWORD_FILE:/run/restic-password:ro" \
    -v "$CACHE:/root/.cache/restic" \
    -v "$STAGE:/data/staging:ro" \
    -v "${PROJECT}_minio_data:/data/minio:ro" \
    -v "${PROJECT}_runner_artifacts:/data/runner_artifacts:ro" \
    -v "$DIR/.env.production:/data/env.production:ro" \
    "$IMAGE" "$@"
}

clean_stage() {  # The copies belong to root and the Neo4j user.
  docker run --rm -v "$STAGE:/s" --entrypoint sh "$IMAGE" -c 'rm -rf /s/* /s/.[!.]*' 2>/dev/null || true
}

NEO4J_ID=""
cleanup() {
  if [ -n "$NEO4J_ID" ]; then docker start "$NEO4J_ID" >/dev/null || log "Could not restart Neo4j!"; fi
  clean_stage
}
trap cleanup EXIT

mkdir -p "$REPO" "$STAGE" "$CACHE" "$(dirname "$PASSWORD_FILE")"
chmod 700 "$REPO" "$STAGE"
clean_stage

if ! restic cat config >/dev/null 2>&1; then
  log "Creating restic repository in $REPO"
  restic init
fi

pg_id=$(container postgres)
[ -n "$pg_id" ] || { log "Postgres is not running"; exit 1; }
log "Dumping Postgres"
docker exec "$pg_id" pg_dump -U "$(env_value POSTGRES_USER inlumen)" -d "$(env_value POSTGRES_DB inlumen)" \
  >"$STAGE/postgres.sql.partial"
mv "$STAGE/postgres.sql.partial" "$STAGE/postgres.sql"

NEO4J_ID=$(container neo4j)
[ -n "$NEO4J_ID" ] || { log "Neo4j is not running"; exit 1; }
log "Copying Neo4j data (Neo4j stopped)"
docker stop "$NEO4J_ID" >/dev/null
docker run --rm -v "${PROJECT}_neo4j_data:/src:ro" -v "$STAGE:/dst" --entrypoint sh "$IMAGE" \
  -c 'cp -a /src /dst/neo4j'
docker start "$NEO4J_ID" >/dev/null
NEO4J_ID=""
log "Neo4j started again"

log "Backing up to $REPO"
restic backup --host "$HOST_TAG" --tag weekly /data
restic forget --host "$HOST_TAG" --keep-weekly "$KEEP_WEEKLY" --keep-monthly "$KEEP_MONTHLY" --prune
if [ "${1:-}" = --check ]; then restic check; fi
restic snapshots --host "$HOST_TAG" --compact | tail -n 4
log "Backup finished"
