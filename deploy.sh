#!/usr/bin/env bash
set -Eeuo pipefail

# ==============================================================================
# auto-gpt 集成式 Git 管理与自动化部署脚本
# 参考 openai-pay-submit 自动 Git 流程，并与本项目多实例构建脚本深度组合
# ==============================================================================

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

MSG=""
MODE="multi"      # 默认多实例编排升级模式 (multi / image / hot)
DRY_RUN=0
PUSH=0

usage() {
  cat <<USAGE
⚠️ 使用说明:
  $0 "<Commit日志>" [--mode=multi|image|hot] [--dry-run] [--push]

参数:
  <Commit日志>    必填：本次更新的内容描述 (必须作为第一个参数或者通过 -m 传入)
  --mode=multi    默认：使用 docker-compose.multi.yml 构建 auto-gpt:latest 并更新两端容器
  --mode=image    单库发版：调用 scripts/deploy-image-release.sh --apply 执行标准备份与发版
  --mode=hot      热补丁：调用 scripts/deploy-to-auto-gpt-container.sh 把静态/Python代码热同步到两端运行中容器
  --dry-run       仅查看 Git 状态与执行计划，不实际提交 Git 或修改容器
  --push          Git commit 成功后自动 push 到当前远程分支 (origin/当前分支)

示例:
  $0 "feat: 同步最新版 plus 功能并优化架构" --mode=multi --push
  $0 "fix: 紧急修复手机号绑定重试逻辑" --mode=hot
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --mode=*) MODE="${1#*=}" ;;
    --dry-run) DRY_RUN=1 ;;
    --push) PUSH=1 ;;
    -m|--message)
      shift
      MSG="${1:-}"
      ;;
    -h|--help) usage; exit 0 ;;
    -*) echo "未知参数: $1" >&2; usage >&2; exit 2 ;;
    *)
      if [[ -z "$MSG" ]]; then
        MSG="$1"
      else
        echo "多余的参数: $1" >&2; usage >&2; exit 2
      fi
      ;;
  esac
  shift
done

if [[ -z "$MSG" && "$DRY_RUN" == "0" ]]; then
  echo "⚠️ 错误：缺少 Git 提交日志！"
  usage
  exit 1
fi

echo "===================================================================="
echo "📝 第一步：自动 Git 版本管理与工作区追踪..."
echo "===================================================================="
echo "当前工作目录：$ROOT_DIR"
echo "部署目标模式：$MODE ($([[ "$DRY_RUN" == "1" ]] && echo "模拟预览 Dry-Run" || echo "实际执行 Apply"))"

if [[ "$DRY_RUN" == "1" ]]; then
  echo "-> [Dry-Run] 工作区状态预览："
  git status -s
else
  echo "-> 正在执行 git add ."
  git add .

  if git diff-index --quiet HEAD -- 2>/dev/null && [[ -z "$(git status --porcelain)" ]]; then
    echo "ℹ️ 工作区干净，没有检测到任何新的文件变更，跳过 git commit。"
  else
    echo "-> 正在生成 Git 提交记录 commit..."
    git commit -m "$MSG"
    echo "✅ Git 提交完成！当前版本号：$(git log -1 --oneline)"
  fi

  if [[ "$PUSH" == "1" ]]; then
    branch="$(git branch --show-current)"
    echo "-> 正在推送至远程分支 origin/$branch..."
    git push origin "$branch"
    echo "✅ 远程仓库推送完成！"
  fi
fi

echo ""
echo "===================================================================="
echo "🐳 第二步：组合调用 Docker 脚本执行容器化部署 ($MODE 模式)..."
echo "===================================================================="

case "$MODE" in
  multi)
    echo "ℹ️ 模式 [multi]：利用 docker-compose.multi.yml 将新代码构建为 auto-gpt:latest 并重启双实例..."
    if [[ "$DRY_RUN" == "1" ]]; then
      echo "-> [Dry-Run] 执行命令：docker-compose -f docker-compose.multi.yml config"
      docker-compose -f docker-compose.multi.yml config >/dev/null
      echo "✅ Compose 配置校验通过。"
    else
      echo "-> 正在执行 docker-compose -f docker-compose.multi.yml build..."
      docker-compose -f docker-compose.multi.yml build
      echo "-> 正在执行 docker-compose -f docker-compose.multi.yml up -d --remove-orphans..."
      docker-compose -f docker-compose.multi.yml up -d --remove-orphans
    fi
    ;;

  image)
    echo "ℹ️ 模式 [image]：调用底层脚本 deploy-image-release.sh 进行标准发版与数据库快照保护..."
    if [[ "$DRY_RUN" == "1" ]]; then
      scripts/deploy-image-release.sh --dry-run
    else
      scripts/deploy-image-release.sh --apply
    fi
    ;;

  hot)
    echo "ℹ️ 模式 [hot]：调用 deploy-to-auto-gpt-container.sh 将静态资源与 Python 代码热更新至两端容器..."
    if [[ "$DRY_RUN" == "1" ]]; then
      echo "-> [Dry-Run] 主容器 (auto-gpt):"
      CONTAINER=auto-gpt scripts/deploy-to-auto-gpt-container.sh --dry-run
      echo "-> [Dry-Run] Plus 增强容器 (auto-gpt-plus):"
      CONTAINER=auto-gpt-plus scripts/deploy-to-auto-gpt-container.sh --dry-run
    else
      echo "-> 热更新主容器 (auto-gpt)..."
      CONTAINER=auto-gpt scripts/deploy-to-auto-gpt-container.sh --apply --backend
      echo "-> 热更新 Plus 增强容器 (auto-gpt-plus)..."
      CONTAINER=auto-gpt-plus scripts/deploy-to-auto-gpt-container.sh --apply --backend
    fi
    ;;

  *)
    echo "❌ 错误的模式: $MODE (可选值: multi, image, hot)" >&2
    exit 1
    ;;
esac

echo ""
echo "===================================================================="
echo "🔍 第三步：现网服务健康校验 (Smoke Test)..."
echo "===================================================================="

if [[ "$DRY_RUN" == "1" ]]; then
  echo "ℹ️ [Dry-Run] 跳过实际 HTTP Smoke 校验。"
else
  echo "-> 运行中的 auto-gpt 相关容器："
  docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}" | grep -E "NAMES|auto-gpt" || true

  echo "-> 检查主服务网页 (8000 端口)..."
  if curl -fsS http://127.0.0.1:8000/ >/dev/null 2>&1; then
    echo "   🟢 http://127.0.0.1:8000/ 正常响应"
  else
    echo "   🔴 http://127.0.0.1:8000/ 响应失败，请检查容器日志！"
  fi

  echo "-> 检查 Plus 增强服务网页 (8001 端口)..."
  if curl -fsS http://127.0.0.1:8001/ >/dev/null 2>&1; then
    echo "   🟢 http://127.0.0.1:8001/ 正常响应"
  else
    echo "   🟡 http://127.0.0.1:8001/ 尚未启动或响应失败 (如仅运行单容器可忽略)"
  fi
fi

echo ""
echo "===================================================================="
echo "🎉 部署与管理操作完毕！"
echo "💡 温馨提示："
echo "   - 回滚代码：可使用 git log / git checkout 切换至旧版本；"
echo "   - 回滚镜像：可执行 scripts/rollback-image-release.sh <备份目录> --apply；"
echo "   - 数据库快照默认由 deploy-image-release.sh 保留在 .rollback-backups/ 下。"
echo "===================================================================="
