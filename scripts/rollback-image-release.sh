#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="${COMPOSE_FILE:-$ROOT_DIR/docker-compose.yml}"
PROJECT_NAME="${PROJECT_NAME:-auto-gpt}"
SERVICE="${SERVICE:-app}"
CONTAINER="${CONTAINER:-auto-gpt}"
SMOKE_URL="${SMOKE_URL:-http://127.0.0.1:8000/}"

APPLY=0
BACKUP_ROOT=""

usage() {
  cat <<USAGE
Usage: $0 BACKUP_ROOT [--apply] [--dry-run]

Rollback the app container to the rollback image recorded by a prior
deploy-image-release.sh run. Runtime volumes are preserved. SQLite snapshots in
BACKUP_ROOT are not restored automatically, to avoid overwriting newer data.
USAGE
}

log() { printf '[image-rollback] %s\n' "$*"; }

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

while [[ $# -gt 0 ]]; do
  case "$1" in
    --apply) APPLY=1 ;;
    --dry-run) APPLY=0 ;;
    -h|--help) usage; exit 0 ;;
    --*) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
    *)
      if [[ -n "$BACKUP_ROOT" ]]; then
        echo "Unexpected argument: $1" >&2
        usage >&2
        exit 2
      fi
      BACKUP_ROOT="$1"
      ;;
  esac
  shift
done

if [[ -z "$BACKUP_ROOT" ]]; then
  usage >&2
  exit 2
fi

if [[ ! -f "$BACKUP_ROOT/rollback-image.txt" ]]; then
  echo "Missing rollback image marker: $BACKUP_ROOT/rollback-image.txt" >&2
  exit 1
fi

ROLLBACK_IMAGE="$(cat "$BACKUP_ROOT/rollback-image.txt")"
if [[ -z "$ROLLBACK_IMAGE" ]]; then
  echo "Rollback image marker is empty" >&2
  exit 1
fi

cd "$ROOT_DIR"

log "root=$ROOT_DIR"
log "compose=$COMPOSE_FILE"
log "project=$PROJECT_NAME service=$SERVICE container=$CONTAINER"
log "compose_cmd=${COMPOSE_CMD[*]}"
log "mode=$([[ "$APPLY" == "1" ]] && echo apply || echo dry-run)"
log "backup_root=$BACKUP_ROOT"
log "rollback_image=$ROLLBACK_IMAGE"

if [[ "$APPLY" == "1" ]]; then
  docker image inspect "$ROLLBACK_IMAGE" >/dev/null
else
  log "+ docker image inspect $ROLLBACK_IMAGE"
fi

APP_IMAGE="$ROLLBACK_IMAGE" "${COMPOSE_CMD[@]}" -p "$PROJECT_NAME" -f "$COMPOSE_FILE" config >/dev/null
run env APP_IMAGE="$ROLLBACK_IMAGE" "${COMPOSE_CMD[@]}" -p "$PROJECT_NAME" -f "$COMPOSE_FILE" up -d --no-build --force-recreate "$SERVICE"

if [[ "$APPLY" == "1" ]]; then
  log "smoke: $SMOKE_URL"
  ok=0
  for _ in $(seq 1 30); do
    if curl -fsS "$SMOKE_URL" >/dev/null; then
      ok=1
      break
    fi
    sleep 2
  done
  if [[ "$ok" != "1" ]]; then
    echo "Rollback smoke failed" >&2
    exit 1
  fi
  docker inspect "$CONTAINER" > "$BACKUP_ROOT/container-inspect.rollback.json"
else
  log "+ curl -fsS $SMOKE_URL with retries"
fi

log "done"
