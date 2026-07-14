#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="${COMPOSE_FILE:-$ROOT_DIR/docker-compose.multi.yml}"
PROJECT_NAME="${PROJECT_NAME:-auto-gpt}"
SERVICE="${SERVICE:-auto-gpt-plus}"
CONTAINER="${CONTAINER:-auto-gpt-plus}"
IMAGE_REPO="${IMAGE_REPO:-auto-gpt}"
STAMP="${RELEASE_TAG:-$(date +%Y%m%dT%H%M%SZ)}"
IMAGE="${IMAGE:-$IMAGE_REPO:$STAMP}"
ROLLBACK_IMAGE="${ROLLBACK_IMAGE:-$IMAGE_REPO:rollback-$STAMP}"
BACKUP_ROOT="${BACKUP_ROOT:-$ROOT_DIR/.rollback-backups/image-release-$STAMP}"
SMOKE_URL="${SMOKE_URL:-http://127.0.0.1:8001/api/health}"
CAMOUFOX_VERSION="${CAMOUFOX_VERSION:-135.0.1}"
CAMOUFOX_RELEASE="${CAMOUFOX_RELEASE:-beta.24}"

APPLY=0
SKIP_BUILD=0
NO_CACHE=0
AUTO_ROLLBACK=1
BACKUP_DB=1
FULL_RUNTIME_BACKUP=0

usage() {
  cat <<USAGE
Usage: $0 [--apply] [--dry-run] [--image IMAGE] [--tag TAG] [--skip-build]
          [--no-cache] [--no-db-backup] [--full-runtime-backup] [--no-auto-rollback]

Build and deploy a versioned Docker image using docker compose, while preserving
runtime data on mounted volumes and creating rollback anchors first.

Defaults:
  project:         $PROJECT_NAME
  service:         $SERVICE
  container:       $CONTAINER
  image:           $IMAGE
  rollback image:  $ROLLBACK_IMAGE
  backup root:     $BACKUP_ROOT

Options:
  --apply                execute the release; default is dry-run
  --dry-run              print planned writes without changing live runtime
  --image IMAGE          deploy/build this image tag instead of the timestamp tag
  --tag TAG              use IMAGE_REPO:TAG and matching backup stamp
  --skip-build           deploy an existing local image tag
  --no-cache             pass --no-cache to docker build
  --no-db-backup         skip SQLite .backup snapshots
  --full-runtime-backup  also tar mounted runtime directories; can be large
  --no-auto-rollback     do not recreate from rollback image if smoke fails
USAGE
}

log() { printf '[image-release] %s\n' "$*"; }

run() {
  log "+ $*"
  if [[ "$APPLY" == "1" ]]; then
    "$@"
  fi
}

if docker compose version >/dev/null 2>&1; then
  COMPOSE_CMD=(docker compose)
elif docker-compose version >/dev/null 2>&1; then
  COMPOSE_CMD=(docker-compose)
else
  echo "docker compose / docker-compose not found" >&2
  exit 1
fi

compose() {
  APP_IMAGE="$IMAGE" "${COMPOSE_CMD[@]}" -p "$PROJECT_NAME" -f "$COMPOSE_FILE" "$@"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --apply) APPLY=1 ;;
    --dry-run) APPLY=0 ;;
    --image)
      IMAGE="${2:?missing image}"
      shift
      ;;
    --tag)
      STAMP="${2:?missing tag}"
      IMAGE="$IMAGE_REPO:$STAMP"
      ROLLBACK_IMAGE="$IMAGE_REPO:rollback-$STAMP"
      BACKUP_ROOT="$ROOT_DIR/.rollback-backups/image-release-$STAMP"
      shift
      ;;
    --skip-build) SKIP_BUILD=1 ;;
    --no-cache) NO_CACHE=1 ;;
    --no-db-backup) BACKUP_DB=0 ;;
    --full-runtime-backup) FULL_RUNTIME_BACKUP=1 ;;
    --no-auto-rollback) AUTO_ROLLBACK=0 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

cd "$ROOT_DIR"

if [[ ! -f "$COMPOSE_FILE" ]]; then
  echo "Compose file not found: $COMPOSE_FILE" >&2
  exit 1
fi

log "root=$ROOT_DIR"
log "compose=$COMPOSE_FILE"
log "project=$PROJECT_NAME service=$SERVICE container=$CONTAINER"
log "compose_cmd=${COMPOSE_CMD[*]}"
log "mode=$([[ "$APPLY" == "1" ]] && echo apply || echo dry-run)"
log "image=$IMAGE"
log "rollback_image=$ROLLBACK_IMAGE"
log "backup_root=$BACKUP_ROOT"

log "validating compose config"
compose config >/dev/null

if [[ "$SKIP_BUILD" != "1" ]]; then
  build_args=(
    docker build
    --build-arg "CAMOUFOX_VERSION=$CAMOUFOX_VERSION"
    --build-arg "CAMOUFOX_RELEASE=$CAMOUFOX_RELEASE"
    -t "$IMAGE"
    -t "$IMAGE_REPO:latest"
  )
  if [[ "$NO_CACHE" == "1" ]]; then
    build_args+=(--no-cache)
  fi
  build_args+=(.)
  run "${build_args[@]}"
else
  log "skip build; expecting local image to exist: $IMAGE"
  if [[ "$APPLY" == "1" ]]; then
    docker image inspect "$IMAGE" >/dev/null
  else
    log "+ docker image inspect $IMAGE"
  fi
fi

if [[ "$APPLY" == "1" ]]; then
  mkdir -p "$BACKUP_ROOT"
  compose config > "$BACKUP_ROOT/compose-config.before.yml"
  if docker inspect "$CONTAINER" >/dev/null 2>&1; then
    docker inspect "$CONTAINER" > "$BACKUP_ROOT/container-inspect.before.json"
    docker inspect "$CONTAINER" --format '{{.Image}}' > "$BACKUP_ROOT/container-image-id.before.txt"
    docker inspect "$CONTAINER" --format '{{.Config.Image}}' > "$BACKUP_ROOT/container-image-name.before.txt"
    log "committing current live container for image rollback"
    docker commit "$CONTAINER" "$ROLLBACK_IMAGE" >/dev/null
    printf '%s\n' "$ROLLBACK_IMAGE" > "$BACKUP_ROOT/rollback-image.txt"
  else
    log "live container not found; skipping rollback image commit"
  fi
  printf '%s\n' "$IMAGE" > "$BACKUP_ROOT/release-image.txt"
  {
    printf 'stamp=%s\n' "$STAMP"
    printf 'image=%s\n' "$IMAGE"
    printf 'rollback_image=%s\n' "$ROLLBACK_IMAGE"
    printf 'project=%s\n' "$PROJECT_NAME"
    printf 'service=%s\n' "$SERVICE"
    printf 'container=%s\n' "$CONTAINER"
    printf 'compose_file=%s\n' "$COMPOSE_FILE"
    printf 'smoke_url=%s\n' "$SMOKE_URL"
  } > "$BACKUP_ROOT/release.env"
else
  log "+ mkdir -p $BACKUP_ROOT"
  log "+ ${COMPOSE_CMD[*]} config > $BACKUP_ROOT/compose-config.before.yml"
  log "+ docker inspect/commit current $CONTAINER as $ROLLBACK_IMAGE"
fi

backup_sqlite_db() {
  local source_path="$1"
  local name="$2"
  local tmp_path="/tmp/${name}-${STAMP}.db"
  if ! docker exec "$CONTAINER" sh -lc "[ -s '$source_path' ]"; then
    return 0
  fi
  log "backing up $source_path"
  docker exec "$CONTAINER" sh -lc "rm -f '$tmp_path' && sqlite3 '$source_path' \".backup '$tmp_path'\""
  docker cp "$CONTAINER:$tmp_path" "$BACKUP_ROOT/$name.db"
  sqlite3 "$BACKUP_ROOT/$name.db" "PRAGMA integrity_check;" > "$BACKUP_ROOT/$name.integrity_check.txt"
  docker exec "$CONTAINER" sh -lc "rm -f '$tmp_path'"
}

if [[ "$BACKUP_DB" == "1" ]]; then
  if [[ "$APPLY" == "1" && "$(docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null || true)" == "true" ]]; then
    backup_sqlite_db "/runtime/account_manager.db" "account_manager"
    backup_sqlite_db "/runtime/team_manage.db" "team_manage.historical"
  else
    log "+ sqlite .backup /runtime/account_manager.db and historical /runtime/team_manage.db"
  fi
fi

if [[ "$FULL_RUNTIME_BACKUP" == "1" ]]; then
  if [[ "$APPLY" == "1" ]]; then
    log "creating full runtime tarball; this may be large"
    tar -C "$ROOT_DIR" -czf "$BACKUP_ROOT/runtime-volumes.tgz" data _ext_targets external_logs
  else
    log "+ tar -C $ROOT_DIR -czf $BACKUP_ROOT/runtime-volumes.tgz data _ext_targets external_logs"
  fi
fi

run env APP_IMAGE="$IMAGE" "${COMPOSE_CMD[@]}" -p "$PROJECT_NAME" -f "$COMPOSE_FILE" up -d --no-build --force-recreate "$SERVICE"

smoke() {
  local attempt
  for attempt in $(seq 1 30); do
    if curl -fsS "$SMOKE_URL" >/dev/null; then
      return 0
    fi
    sleep 2
  done
  return 1
}

if [[ "$APPLY" == "1" ]]; then
  log "smoke: $SMOKE_URL"
  if smoke; then
    docker inspect "$CONTAINER" > "$BACKUP_ROOT/container-inspect.after.json"
    log "release ok"
    log "backup_root=$BACKUP_ROOT"
  else
    log "smoke failed"
    if [[ "$AUTO_ROLLBACK" == "1" && -f "$BACKUP_ROOT/rollback-image.txt" ]]; then
      rollback_image="$(cat "$BACKUP_ROOT/rollback-image.txt")"
      log "auto rollback to $rollback_image"
      env APP_IMAGE="$rollback_image" "${COMPOSE_CMD[@]}" -p "$PROJECT_NAME" -f "$COMPOSE_FILE" up -d --no-build --force-recreate "$SERVICE"
      smoke || true
    fi
    exit 1
  fi
else
  log "+ curl -fsS $SMOKE_URL with retries"
fi

log "done"
