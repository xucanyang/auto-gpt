#!/usr/bin/env bash
set -Eeuo pipefail

# ==============================================================================
# auto-gpt 安全发布门禁
# - Git 变更自动写入 changelog.md 并提交
# - 禁止把运行态/密钥/抓包/依赖产物提交进仓库
# - 默认不再创建发布前备份；如需临时备份，显式追加 --backup
# - 当前常驻拓扑：auto-gpt-plus / auto-plus2；auto-gpt 保持已停止的 standby 容器
# - 发布后只校验常驻实例，绝不因发布意外重新拉起 standby 实例
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
ACTIVE_SERVICES=(auto-gpt-plus auto-plus2)
STANDBY_SERVICES=(auto-gpt)
ALL_SERVICES=("${ACTIVE_SERVICES[@]}" "${STANDBY_SERVICES[@]}")

usage() {
  cat <<USAGE
Usage:
  $0 "本次变更说明" [--mode=multi|hot] [--dry-run] [--push] [--backup]

Modes:
  --mode=multi    默认：构建 auto-gpt:latest 并升级 auto-gpt-plus / auto-plus2；auto-gpt 保持停止
  --mode=hot      调用 scripts/deploy-to-auto-gpt-container.sh 对多容器做热同步，仅适合静态/Python 小补丁
  --backup        本次发布前额外创建 .rollback-backups/deploy-<timestamp> 运行态备份；默认关闭

Examples:
  $0 "规范 auto-gpt 发布门禁" --mode=multi
  $0 "紧急修复手机号绑定重试逻辑" --mode=hot
USAGE
}

log() { printf '[deploy] %s\n' "$*"; }
fatal() { printf '[deploy][fatal] %s\n' "$*" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --mode=*) MODE="${1#*=}" ;;
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

has_repo_changes() {
  [[ -n "$(git status --porcelain=v1 --untracked-files=all)" ]]
}

append_changelog_if_needed() {
  [[ "$DRY_RUN" == "0" ]] || return 0
  has_repo_changes || return 0
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
  log "语法检查: deploy.sh / main.py / api/system.py"
  bash -n deploy.sh
  python3 -m py_compile main.py api/system.py
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
  git rev-parse HEAD > "$backup_root/git-head.before.txt"
  git status --short --ignored > "$backup_root/git-status.before.txt" || true
  compose_multi config > "$backup_root/docker-compose.multi.rendered.yml"
  for service in "${ALL_SERVICES[@]}"; do
    if docker inspect "$service" >/dev/null 2>&1; then
      docker inspect "$service" > "$backup_root/${service}.inspect.before.json"
    fi
  done
  for data_root in /opt/auto-gpt/data /opt/auto-gpt-plus/data /opt/auto-plus2/data; do
    [[ -d "$data_root" ]] || continue
    name="$(basename "$(dirname "$data_root")")"
    for db in account_manager.db team_manage.db; do
      sqlite_backup_or_copy "$data_root/$db" "$backup_root/${name}.${db}.before_deploy.bak" "$backup_root/${name}.${db}.integrity_check.txt"
    done
  done
  if [[ -d /opt/auto-gpt/shared_config ]]; then
    tar -C /opt/auto-gpt -czf "$backup_root/shared_config.before_deploy.tgz" shared_config
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

smoke_after_deploy() {
  log "运行容器状态"
  docker ps -a --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}' | grep -E 'NAMES|auto-gpt|auto-plus2' || true
  smoke_url "auto-gpt-plus health" "http://127.0.0.1:8001/api/health"
  smoke_url "auto-plus2 health" "http://127.0.0.1:8003/api/health"
  smoke_url "auto-gpt-plus index" "http://127.0.0.1:8001/"
  smoke_url "auto-plus2 index" "http://127.0.0.1:8003/"
}

stop_standby_services() {
  local service state
  for service in "${STANDBY_SERVICES[@]}"; do
    if ! docker inspect "$service" >/dev/null 2>&1; then
      log "standby ${service}: 容器不存在，跳过"
      continue
    fi
    state="$(docker inspect -f '{{.State.Status}}' "$service")"
    if [[ "$state" == "running" ]]; then
      log "停止 standby ${service}（保留容器和全部数据卷）"
      docker stop "$service" >/dev/null
    else
      log "standby ${service}: 已停止（state=${state}）"
    fi
  done
}

log "root=$ROOT_DIR mode=$MODE dry_run=$DRY_RUN backup=$BACKUP compose=${COMPOSE_CMD[*]}"

assert_no_forbidden_git_changes
[[ "$MODE" != "hot" ]] || assert_hot_scope
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
    log "Compose build: $COMPOSE_FILE"
    compose_multi build
    log "Compose up -d --remove-orphans: ${ACTIVE_SERVICES[*]}"
    compose_multi up -d --remove-orphans "${ACTIVE_SERVICES[@]}"
    stop_standby_services
    ;;
  hot)
    log "热同步 auto-gpt-plus"
    if [[ "$BACKUP" == "1" ]]; then
      BACKUP_ROOT="$backup_root/auto-gpt-plus-hot" CONTAINER=auto-gpt-plus SMOKE_URL=http://127.0.0.1:8001/api/health \
        scripts/deploy-to-auto-gpt-container.sh --apply --backend --restart --commit-image
    else
      SKIP_BACKUP=1 CONTAINER=auto-gpt-plus SMOKE_URL=http://127.0.0.1:8001/api/health \
        scripts/deploy-to-auto-gpt-container.sh --apply --backend --restart
    fi
    log "热同步 auto-plus2"
    if [[ "$BACKUP" == "1" ]]; then
      BACKUP_ROOT="$backup_root/auto-plus2-hot" CONTAINER=auto-plus2 SMOKE_URL=http://127.0.0.1:8003/api/health \
        scripts/deploy-to-auto-gpt-container.sh --apply --backend --restart --commit-image
    else
      SKIP_BACKUP=1 CONTAINER=auto-plus2 SMOKE_URL=http://127.0.0.1:8003/api/health \
        scripts/deploy-to-auto-gpt-container.sh --apply --backend --restart
    fi
    stop_standby_services
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
