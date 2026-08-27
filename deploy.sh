#!/usr/bin/env bash
set -Eeuo pipefail

# ==============================================================================
# auto-gpt 安全发布门禁
# - Git 变更自动写入 changelog.md 并提交
# - 禁止把运行态/密钥/抓包/依赖产物提交进仓库
# - 默认不再创建发布前备份；如需临时备份，显式追加 --backup
# - 当前常驻拓扑：phone-api-relay / auto-gpt / auto-gpt-plus / auto-plus2 / auto-plus3
# - 发布后校验四个业务实例，防止公网实例被发布流程误停
# ==============================================================================

EXPECTED_ROOT="/opt/auto-gpt"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_FILE="${ROOT_DIR}/docker-compose.multi.yml"
BACKUP_BASE="${ROOT_DIR}/.rollback-backups"
CHANGELOG_FILE="${ROOT_DIR}/changelog.md"

MSG=""
MODE="multi"      # multi / hot
DRY_RUN=0
PUSH=0
BACKUP="${AUTO_GPT_DEPLOY_BACKUP:-0}"
FRONTEND_ONLY=0
ACTIVE_SERVICES=(phone-api-relay auto-gpt auto-gpt-plus auto-plus2 auto-plus3)

usage() {
  cat <<USAGE
Usage:
  $0 "本次变更说明" [--mode=multi|hot] [--frontend-only] [--dry-run] [--push] [--backup]

Modes:
  --mode=multi    默认：构建 auto-gpt:latest 并升级 phone-api-relay / auto-gpt / auto-gpt-plus / auto-plus2 / auto-plus3
  --mode=hot      调用 scripts/deploy-to-auto-gpt-container.sh 对四个业务实例做热同步，仅适合静态/Python 小补丁
  --frontend-only 仅与 --mode=hot 同用：构建规范镜像并原子同步静态资源，不重启后端任务进程
  --backup        本次发布前额外创建 .rollback-backups/deploy-<timestamp> 运行态备份；默认关闭

Examples:
  $0 "规范 auto-gpt 发布门禁" --mode=multi
  $0 "紧急修复手机号绑定重试逻辑" --mode=hot
  $0 "修复账号页静态交互" --mode=hot --frontend-only
USAGE
}

log() { printf '[deploy] %s\n' "$*"; }
fatal() { printf '[deploy][fatal] %s\n' "$*" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --mode=*) MODE="${1#*=}" ;;
    --frontend-only) FRONTEND_ONLY=1 ;;
    --dry-run) DRY_RUN=1 ;;
    --push) PUSH=1 ;;
    --backup) BACKUP=1 ;;
    --no-backup) BACKUP=0 ;;
    -m|--message)
      shift
      MSG="${1:-}"
      ;;
    -h|--help) usage; exit 0 ;;
    -*) fatal "未知参数: $1" ;;
    *)
      if [[ -z "$MSG" ]]; then
        MSG="$1"
      else
        fatal "多余参数: $1"
      fi
      ;;
  esac
  shift
done

[[ "$MODE" =~ ^(multi|hot)$ ]] || fatal "错误的模式: $MODE，可选值 multi|hot"
[[ "$FRONTEND_ONLY" == "0" || "$MODE" == "hot" ]] || fatal "--frontend-only 只能与 --mode=hot 同用"
[[ "$BACKUP" =~ ^(0|1|false|true|no|yes|off|on)$ ]] || fatal "错误的备份开关: $BACKUP，可选 0/1/true/false"
case "${BACKUP,,}" in
  1|true|yes|on) BACKUP=1 ;;
  *) BACKUP=0 ;;
esac
if [[ -z "$MSG" && "$DRY_RUN" == "0" ]]; then
  usage >&2
  fatal "缺少提交/发布说明"
fi

[[ "$ROOT_DIR" == "$EXPECTED_ROOT" ]] || fatal "拒绝在非预期目录执行: $ROOT_DIR (expected $EXPECTED_ROOT)"
cd "$ROOT_DIR"
[[ -d .git ]] || fatal "当前目录不是 Git 仓库: $ROOT_DIR"
[[ -f "$COMPOSE_FILE" ]] || fatal "缺少 compose 文件: $COMPOSE_FILE"

if docker compose version >/dev/null 2>&1; then
  COMPOSE_CMD=(docker compose)
elif docker-compose version >/dev/null 2>&1; then
  COMPOSE_CMD=(docker-compose)
else
  fatal "未找到 docker compose / docker-compose"
fi

compose_multi() {
  "${COMPOSE_CMD[@]}" -f "$COMPOSE_FILE" "$@"
}

is_forbidden_path_py='''
import re, sys
patterns = [
    r"(^|/)\.env($|\.)",
    r"(^|/)data/",
    r"(^|/)shared_config/",
    r"(^|/)shared-config-bootstrap-backups/",
    r"(^|/)\.rollback-backups/",
    r"(^|/)_ext_targets/",
    r"(^|/)external_logs/",
    r"(^|/)node_modules/",
    r"(^|/)dist/",
    r"(^|/)__pycache__/",
    r"(^|/)\.pytest_cache/",
    r"(^|/)\.claude/",
    r"(^|/)smstome_used$",
    r"\.(db|sqlite|db-wal|db-shm|sqlite-wal|sqlite-shm|har|har\.gz|log|bak|backup|orig|zip|7z|tar|tgz|gz)$",
]
rx = [re.compile(p, re.I) for p in patterns]
for path in sys.argv[1:]:
    if any(p.search(path) for p in rx):
        sys.exit(0)
sys.exit(1)
'''

assert_no_forbidden_git_changes() {
  local tmp failures=0
  tmp="$(mktemp)"
  python3 - "$tmp" <<'PY'
import pathlib, re, subprocess, sys
out = pathlib.Path(sys.argv[1])
patterns = [
    r"(^|/)\.env($|\.)",
    r"(^|/)data/",
    r"(^|/)shared_config/",
    r"(^|/)shared-config-bootstrap-backups/",
    r"(^|/)\.rollback-backups/",
    r"(^|/)_ext_targets/",
    r"(^|/)external_logs/",
    r"(^|/)node_modules/",
    r"(^|/)dist/",
    r"(^|/)__pycache__/",
    r"(^|/)\.pytest_cache/",
    r"(^|/)\.claude/",
    r"(^|/)smstome_used$",
    r"\.(db|sqlite|db-wal|db-shm|sqlite-wal|sqlite-shm|har|har\.gz|log|bak|backup|orig|zip|7z|tar|tgz|gz)$",
]
rx = [re.compile(p, re.I) for p in patterns]

def forbidden(path: str) -> bool:
    return any(p.search(path) for p in rx)

def parse_name_status_z(args):
    raw = subprocess.check_output(args)
    parts = raw.split(b"\0")
    i = 0
    while i < len(parts) and parts[i]:
        status = parts[i].decode("utf-8", "replace")
        i += 1
        if not status:
            break
        if status[0] in {"R", "C"}:
            if i + 1 >= len(parts):
                break
            old = parts[i].decode("utf-8", "replace"); new = parts[i + 1].decode("utf-8", "replace")
            i += 2
            yield status, old
            yield status, new
        else:
            if i >= len(parts):
                break
            path = parts[i].decode("utf-8", "replace")
            i += 1
            yield status, path

failures = []
# 已跟踪或即将提交的敏感路径：只允许从 Git 中删除，不允许新增/修改/重命名进入仓库。
for label, args in [
    ("staged", ["git", "diff", "--cached", "--name-status", "-z"]),
    ("unstaged", ["git", "diff", "--name-status", "-z"]),
]:
    for status, path in parse_name_status_z(args):
        if forbidden(path) and not status.startswith("D"):
            failures.append(f"{label}\t{status}\t{path}")

# 未跟踪敏感路径如果没被 .gitignore 接住，git add -A 会误收，必须失败。
raw = subprocess.check_output(["git", "ls-files", "--others", "--exclude-standard", "-z"])
for item in raw.split(b"\0"):
    if not item:
        continue
    path = item.decode("utf-8", "replace")
    if forbidden(path):
        failures.append(f"untracked\t??\t{path}")

out.write_text("\n".join(failures), encoding="utf-8")
sys.exit(1 if failures else 0)
PY
  if [[ -s "$tmp" ]]; then
    cat "$tmp" >&2
    rm -f "$tmp"
    fatal "检测到运行态/密钥/抓包/产物路径将进入 Git；已停止发布"
  fi
  rm -f "$tmp"
}

assert_hot_scope() {
  local tmp
  tmp="$(mktemp)"
  python3 - "$tmp" <<'PY'
import pathlib, subprocess, sys
out = pathlib.Path(sys.argv[1])
allowed_prefixes = (
    "api/", "core/", "services/", "platforms/", "mail/", "frontend/", "static/",
)
allowed_files = {"main.py", "smstome_tool.py", "deploy.sh", "changelog.md", ".gitignore"}
blocked = []
raw = subprocess.check_output(["git", "diff", "--cached", "--name-only", "-z"])
raw += subprocess.check_output(["git", "diff", "--name-only", "-z"])
for item in raw.split(b"\0"):
    if not item:
        continue
    path = item.decode("utf-8", "replace")
    if path == "services/phone_api_relay.py":
        blocked.append(path + " (必须使用 --mode=multi 以更新独立 Relay 容器)")
        continue
    if path in allowed_files or path.startswith(allowed_prefixes):
        continue
    blocked.append(path)
out.write_text("\n".join(sorted(set(blocked))), encoding="utf-8")
sys.exit(1 if blocked else 0)
PY
  if [[ -s "$tmp" ]]; then
    cat "$tmp" >&2
    rm -f "$tmp"
    fatal "hot 模式只允许 Python/静态资源小补丁；以上路径需走 multi 构建发布"
  fi
  rm -f "$tmp"
}

assert_frontend_only_scope() {
  local tmp
  tmp="$(mktemp)"
  python3 - "$tmp" <<'PY'
import pathlib, subprocess, sys
out = pathlib.Path(sys.argv[1])
allowed_prefixes = ("frontend/",)
allowed_files = {"changelog.md", "deploy.sh"}
blocked = []
for args in (
    ["git", "diff", "--cached", "--name-only", "-z"],
    ["git", "diff", "--name-only", "-z"],
    ["git", "ls-files", "--others", "--exclude-standard", "-z"],
):
    raw = subprocess.check_output(args)
    for item in raw.split(b"\0"):
        if not item:
            continue
        path = item.decode("utf-8", "replace")
        if path in allowed_files or path.startswith(allowed_prefixes):
            continue
        blocked.append(path)
out.write_text("\n".join(sorted(set(blocked))), encoding="utf-8")
sys.exit(1 if blocked else 0)
PY
  if [[ -s "$tmp" ]]; then
    cat "$tmp" >&2
    rm -f "$tmp"
    fatal "--frontend-only 检测到非前端源码变更；请改用普通 hot 或 multi 发布"
  fi
  rm -f "$tmp"
}

has_repo_changes() {
  [[ -n "$(git status --porcelain=v1 --untracked-files=all)" ]]
}

append_changelog_if_needed() {
  [[ "$DRY_RUN" == "0" ]] || return 0
  has_repo_changes || return 0
  # 手工维护了详细版本条目时，不再追加一条无上下文的自动记录。
  if ! git diff --quiet -- "$CHANGELOG_FILE" || ! git diff --cached --quiet -- "$CHANGELOG_FILE"; then
    return 0
  fi
  local now
  now="$(date '+%Y-%m-%d %H:%M:%S %z')"
  [[ -f "$CHANGELOG_FILE" ]] || printf '# Changelog\n\n' > "$CHANGELOG_FILE"
  {
    printf '\n## %s\n' "$now"
    printf -- '- %s\n' "$MSG"
    printf -- '- 发布模式: %s\n' "$MODE"
  } >> "$CHANGELOG_FILE"
}

run_checks() {
  log "语法检查: deploy.sh / main.py / api/system.py / services/phone_api_relay.py"
  bash -n deploy.sh
  python3 -m py_compile main.py api/system.py services/phone_api_relay.py
  compose_multi config >/dev/null
  for path in \
    /opt/auto-gpt-register/root.env \
    /opt/auto-gpt-register/instance.env \
    /opt/auto-gpt-register/sub2api.env \
    /opt/auto-gpt-register/paypal.env \
    /opt/auto-gpt-register/data \
    /opt/auto-gpt-register/_ext_targets \
    /opt/auto-gpt-register/external_logs; do
    [[ -e "$path" ]] || fatal "auto-plus3 运行路径不存在: $path"
  done
  for network in auto-gpt_default tempmail_internal gpt-cccy-me_default team-manage_default; do
    docker network inspect "$network" >/dev/null 2>&1 || fatal "auto-plus3 依赖网络不存在: $network"
  done
}

sqlite_backup_or_copy() {
  local src="$1" dst="$2" integrity="$3"
  [[ -s "$src" ]] || return 0
  mkdir -p "$(dirname "$dst")"
  if command -v sqlite3 >/dev/null 2>&1; then
    sqlite3 "$src" ".backup '$dst'"
    sqlite3 "$dst" "PRAGMA integrity_check;" > "$integrity"
  else
    cp -a "$src" "$dst"
    printf 'sqlite3 not found; copied raw file without integrity check\n' > "$integrity"
  fi
  chmod 600 "$dst" "$integrity"
}

create_backup() {
  local stamp backup_root service data_root name db
  stamp="$(date +%Y%m%dT%H%M%S%z)"
  backup_root="${BACKUP_BASE}/deploy-${stamp}"
  if [[ "$DRY_RUN" == "1" ]]; then
    log "[dry-run] 将创建备份目录: $backup_root"
    printf '%s\n' "$backup_root"
    return 0
  fi
  mkdir -p "$backup_root"
  chmod 700 "$backup_root"
  git rev-parse HEAD > "$backup_root/git-head.before.txt"
  git status --short --ignored > "$backup_root/git-status.before.txt" || true
  compose_multi config > "$backup_root/docker-compose.multi.rendered.yml"
  python3 - "$backup_root/docker-compose.multi.rendered.yml" <<'PY'
from pathlib import Path
import re, sys
p = Path(sys.argv[1])
s = p.read_text(errors="replace")
s = re.sub(r"(?im)^(\s*(?:[A-Z0-9_]*(?:TOKEN|KEY|PASSWORD|SECRET)[A-Z0-9_]*):\s*).*$", r"\1<redacted>", s)
p.write_text(s)
PY
  for service in "${ACTIVE_SERVICES[@]}"; do
    if docker inspect "$service" >/dev/null 2>&1; then
      docker inspect "$service" > "$backup_root/${service}.inspect.before.json"
      python3 - "$backup_root/${service}.inspect.before.json" <<'PY'
from pathlib import Path
import json, re, sys
p = Path(sys.argv[1])
try:
    data = json.loads(p.read_text())
except Exception:
    raise SystemExit(0)
secret = re.compile(r"(?:TOKEN|KEY|PASSWORD|SECRET)", re.I)
def scrub(value):
    if isinstance(value, dict):
        return {k: ("<redacted>" if secret.search(str(k)) else scrub(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub(v) for v in value]
    if isinstance(value, str) and any(marker in value.upper() for marker in ("TOKEN=", "KEY=", "PASSWORD=", "SECRET=")):
        return "<redacted>"
    return value
p.write_text(json.dumps(scrub(data), ensure_ascii=False, indent=2) + "\n")
PY
    fi
  done
  for data_root in /opt/auto-gpt/data /opt/auto-gpt-plus/data /opt/auto-plus2/data /opt/auto-gpt-register/data; do
    [[ -d "$data_root" ]] || continue
    name="$(basename "$(dirname "$data_root")")"
    for db in account_manager.db team_manage.db; do
      sqlite_backup_or_copy "$data_root/$db" "$backup_root/${name}.${db}.before_deploy.bak" "$backup_root/${name}.${db}.integrity_check.txt"
    done
  done
  if [[ -d /opt/auto-gpt/shared_config ]]; then
    tar -C /opt/auto-gpt -czf "$backup_root/shared_config.before_deploy.tgz" shared_config
  fi
  if [[ -s /opt/auto-gpt-relay/phone_api_relay.db ]]; then
    sqlite_backup_or_copy /opt/auto-gpt-relay/phone_api_relay.db \
      "$backup_root/phone_api_relay.db.before_deploy.bak" \
      "$backup_root/phone_api_relay.db.integrity_check.txt"
  fi
  find "$ROOT_DIR" -maxdepth 2 -type f \( -name '*.py' -o -name 'deploy.sh' -o -name 'docker-compose.multi.yml' -o -name 'Dockerfile' \) -print0 \
    | sort -z | xargs -0 sha256sum > "$backup_root/source.sha256sums.txt" || true
  log "备份完成: $backup_root"
  printf '%s\n' "$backup_root"
}

smoke_url() {
  local label="$1" url="$2" attempt
  for attempt in $(seq 1 30); do
    if curl -fsS "$url" >/dev/null; then
      log "${label}: OK $url"
      return 0
    fi
    sleep 2
  done
  fatal "${label}: FAIL $url"
}

retire_legacy_auto_plus3() {
  local project
  if ! docker inspect auto-plus3 >/dev/null 2>&1; then
    return 0
  fi

  project="$(docker inspect -f '{{ index .Config.Labels "com.docker.compose.project" }}' auto-plus3 2>/dev/null || true)"
  [[ "$project" == "auto-gpt" ]] && return 0
  if [[ "$project" != "auto-plus3-local" ]]; then
    fatal "auto-plus3 已存在但归属未知 Compose 项目: ${project:-<none>}，拒绝自动删除"
  fi
  [[ "$BACKUP" == "1" ]] || fatal "首次将 auto-plus3 纳入 multi 前必须使用 --backup"

  log "迁移旧独立 Compose 容器 auto-plus3-local -> auto-gpt multi"
  docker stop --time 60 auto-plus3
  docker rm auto-plus3
}

smoke_after_deploy() {
  log "运行容器状态"
  docker ps -a --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}' | grep -E 'NAMES|phone-api-relay|auto-gpt|auto-plus2|auto-plus3' || true
  smoke_url "phone-api-relay health" "http://127.0.0.1:8893/health"
  smoke_url "auto-gpt health" "http://127.0.0.1:8000/api/health"
  smoke_url "auto-gpt-plus health" "http://127.0.0.1:8001/api/health"
  smoke_url "auto-plus2 health" "http://127.0.0.1:8003/api/health"
  smoke_url "auto-plus3 health" "http://127.0.0.1:18003/api/health"
  smoke_url "auto-gpt index" "http://127.0.0.1:8000/"
  smoke_url "auto-gpt-plus index" "http://127.0.0.1:8001/"
  smoke_url "auto-plus2 index" "http://127.0.0.1:8003/"
  smoke_url "auto-plus3 index" "http://127.0.0.1:18003/"
}

hot_sync_service() {
  local container="$1" health_url="$2"
  log "热同步 ${container}"
  if [[ "$BACKUP" == "1" ]]; then
    BACKUP_ROOT="$backup_root/${container}-hot" CONTAINER="$container" SMOKE_URL="$health_url" \
      scripts/deploy-to-auto-gpt-container.sh --apply --backend --restart --commit-image
  else
    SKIP_BACKUP=1 CONTAINER="$container" SMOKE_URL="$health_url" \
      scripts/deploy-to-auto-gpt-container.sh --apply --backend --restart
  fi
}

frontend_sync_service() {
  local container="$1" health_url="$2"
  log "静态资源热同步 ${container}（不重启后端）"
  if [[ "$BACKUP" == "1" ]]; then
    BACKUP_ROOT="$backup_root/${container}-frontend" CONTAINER="$container" SMOKE_URL="$health_url" \
      scripts/deploy-to-auto-gpt-container.sh --apply --skip-build --frontend-only --commit-image
  else
    SKIP_BACKUP=1 CONTAINER="$container" SMOKE_URL="$health_url" \
      scripts/deploy-to-auto-gpt-container.sh --apply --skip-build --frontend-only
  fi
}

log "root=$ROOT_DIR mode=$MODE frontend_only=$FRONTEND_ONLY dry_run=$DRY_RUN backup=$BACKUP compose=${COMPOSE_CMD[*]}"

assert_no_forbidden_git_changes
[[ "$MODE" != "hot" ]] || assert_hot_scope
[[ "$FRONTEND_ONLY" != "1" ]] || assert_frontend_only_scope
append_changelog_if_needed
assert_no_forbidden_git_changes

if [[ "$DRY_RUN" == "1" ]]; then
  log "[dry-run] Git 状态预览"
  git status --short --ignored | sed -n '1,160p'
  log "[dry-run] Compose 配置校验"
  compose_multi config >/dev/null
  exit 0
fi

run_checks

log "Git staging"
git add -A
assert_no_forbidden_git_changes

if git diff --cached --quiet; then
  log "没有源码变更，跳过 Git commit"
else
  git commit -m "$MSG"
  log "Git commit: $(git log -1 --oneline)"
fi

if [[ "$PUSH" == "1" ]]; then
  upstream="$(git rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null || true)"
  [[ -n "$upstream" ]] || fatal "当前分支没有 upstream，拒绝盲推；请先 git branch --set-upstream-to=..."
  log "push -> $upstream"
  git push
fi

backup_root=""
if [[ "$BACKUP" == "1" ]]; then
  backup_root="$(create_backup | tail -n 1)"
else
  log "发布前备份: 已跳过（默认关闭；如需临时备份追加 --backup）"
fi

case "$MODE" in
  multi)
    log "Compose build canonical image: auto-gpt"
    compose_multi build auto-gpt
    retire_legacy_auto_plus3
    log "Compose up -d --no-build --remove-orphans: ${ACTIVE_SERVICES[*]}"
    compose_multi up -d --no-build --remove-orphans "${ACTIVE_SERVICES[@]}"
    ;;
  hot)
    if [[ "$FRONTEND_ONLY" == "1" ]]; then
      log "构建宿主机静态资源"
      (cd frontend && npm run build)
      log "构建规范镜像 auto-gpt:latest（不重建运行中容器）"
      compose_multi build auto-gpt
      frontend_sync_service auto-gpt http://127.0.0.1:8000/api/health
      frontend_sync_service auto-gpt-plus http://127.0.0.1:8001/api/health
      frontend_sync_service auto-plus2 http://127.0.0.1:8003/api/health
      frontend_sync_service auto-plus3 http://127.0.0.1:18003/api/health
    else
      hot_sync_service auto-gpt http://127.0.0.1:8000/api/health
      hot_sync_service auto-gpt-plus http://127.0.0.1:8001/api/health
      hot_sync_service auto-plus2 http://127.0.0.1:8003/api/health
      hot_sync_service auto-plus3 http://127.0.0.1:18003/api/health
    fi
    ;;
esac

smoke_after_deploy

log "发布完成"
if [[ "$BACKUP" == "1" ]]; then
  log "备份目录: $backup_root"
else
  log "备份目录: 未创建（默认关闭）"
fi
log "当前版本: $(git log -1 --oneline)"
