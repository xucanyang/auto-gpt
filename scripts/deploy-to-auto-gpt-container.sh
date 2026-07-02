#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONTAINER="${CONTAINER:-auto-gpt}"
STAMP="$(date +%Y%m%dT%H%M%SZ)"
BACKUP_ROOT="${BACKUP_ROOT:-$ROOT_DIR/.rollback-backups/deploy-$STAMP}"
APPLY=0
BUILD_FRONTEND=1
COPY_FRONTEND=1
COPY_BACKEND=0
RESTART=0
COMMIT_IMAGE=0
SMOKE_URL="${SMOKE_URL:-http://127.0.0.1:8000/}"

usage() {
  cat <<USAGE
Usage: $0 [--apply] [--dry-run] [--skip-build] [--frontend-only] [--backend] [--restart] [--commit-image]

Purpose:
  Build the checkout frontend and publish selected checkout files into the live
  Docker container. By default this is a dry-run and only shows what would happen.

Options:
  --apply          actually copy files into the container
  --dry-run        force dry-run mode, no writes to container
  --skip-build     do not run npm build; copy existing $ROOT_DIR/static
  --frontend-only  copy only $ROOT_DIR/static to /app/static (default)
  --backend        also copy backend source files/directories into /app
  --restart        restart the container after copying
  --commit-image   docker commit the current container before writes

Environment:
  CONTAINER=auto-gpt
  BACKUP_ROOT=$ROOT_DIR/.rollback-backups/deploy-<timestamp>
  SMOKE_URL=http://127.0.0.1:8000/
USAGE
}

log() { printf '[deploy] %s\n' "$*"; }
run() {
  log "+ $*"
  if [[ "$APPLY" == "1" ]]; then
    "$@"
  fi
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --apply) APPLY=1 ;;
    --dry-run) APPLY=0 ;;
    --skip-build) BUILD_FRONTEND=0 ;;
    --frontend-only) COPY_BACKEND=0 ;;
    --backend) COPY_BACKEND=1 ;;
    --restart) RESTART=1 ;;
    --commit-image) COMMIT_IMAGE=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

cd "$ROOT_DIR"

if ! docker inspect "$CONTAINER" >/dev/null 2>&1; then
  echo "Container not found: $CONTAINER" >&2
  exit 1
fi

log "root=$ROOT_DIR"
log "container=$CONTAINER"
log "mode=$([[ "$APPLY" == "1" ]] && echo apply || echo dry-run)"
log "backup_root=$BACKUP_ROOT"

if [[ "$BUILD_FRONTEND" == "1" ]]; then
  log "building frontend -> $ROOT_DIR/static"
  if [[ "$APPLY" == "1" ]]; then
    (cd frontend && npm run build)
  else
    log "+ (cd frontend && npm run build)"
  fi
fi

if [[ ! -f "$ROOT_DIR/static/index.html" ]]; then
  echo "Missing built frontend: $ROOT_DIR/static/index.html" >&2
  echo "Run with --apply so the script can build it, or build manually first." >&2
  exit 1
fi

mkdir -p "$BACKUP_ROOT"
log "capturing current container metadata"
docker inspect "$CONTAINER" > "$BACKUP_ROOT/container-inspect.json"

if [[ "$COMMIT_IMAGE" == "1" ]]; then
  image="auto-gpt-predeploy:$STAMP"
  run docker commit "$CONTAINER" "$image"
  log "predeploy image tag: $image"
fi

if [[ "$APPLY" == "1" ]]; then
  log "backing up /app/static from container"
  docker cp "$CONTAINER:/app/static" "$BACKUP_ROOT/static"
else
  log "+ docker cp $CONTAINER:/app/static $BACKUP_ROOT/static"
fi

if [[ "$COPY_FRONTEND" == "1" ]]; then
  run docker exec "$CONTAINER" sh -lc 'rm -rf /app/static.new && mkdir -p /app/static.new'
  run docker cp "$ROOT_DIR/static/." "$CONTAINER:/app/static.new/"
  run docker exec "$CONTAINER" sh -lc 'rm -rf /app/static.prev && if [ -d /app/static ]; then mv /app/static /app/static.prev; fi && mv /app/static.new /app/static'
fi

if [[ "$COPY_BACKEND" == "1" ]]; then
  log "backend copy enabled; this copies selected source files into /app"
  backend_items=(api core services platforms mail docker main.py requirements.txt smstome_tool.py)
  for item in "${backend_items[@]}"; do
    [[ -e "$ROOT_DIR/$item" ]] || continue
    run docker exec "$CONTAINER" sh -lc "rm -rf /app/${item}.new"
    run docker cp "$ROOT_DIR/$item" "$CONTAINER:/app/${item}.new"
    if [[ "$item" == "services" ]]; then
      # /app/services/external_logs is a bind mount in the live container.  Moving
      # the whole services directory fails with "Device or resource busy", so keep
      # that mount in place and replace everything else in-directory.
      run docker exec "$CONTAINER" sh -lc "rm -rf /app/${item}.prev && mkdir -p /app/${item}.prev && if [ -d /app/${item} ]; then cd /app/${item} && tar --exclude='./external_logs' -cf - . | (cd /app/${item}.prev && tar -xf -); fi"
      run docker exec "$CONTAINER" sh -lc "mkdir -p /app/${item} && for p in /app/${item}/* /app/${item}/.[!.]* /app/${item}/..?*; do [ -e \"\$p\" ] || continue; [ \"\$(basename \"\$p\")\" = external_logs ] && continue; rm -rf \"\$p\"; done && cd /app/${item}.new && tar --exclude='./external_logs' -cf - . | (cd /app/${item} && tar -xf -) && rm -rf /app/${item}.new"
      continue
    fi
    run docker exec "$CONTAINER" sh -lc "rm -rf /app/${item}.prev && if [ -e /app/${item} ]; then mv /app/${item} /app/${item}.prev; fi && mv /app/${item}.new /app/${item}"
  done
fi

if [[ "$RESTART" == "1" ]]; then
  run docker restart "$CONTAINER"
fi

if [[ "$APPLY" == "1" ]]; then
  log "smoke: $SMOKE_URL"
  curl -fsS "$SMOKE_URL" >/dev/null || true
else
  log "+ curl -fsS $SMOKE_URL >/dev/null || true"
fi

log "done"
