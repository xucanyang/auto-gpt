# Docker 镜像发布与回滚

本项目的生产运行方式以 Docker 镜像为准：代码和依赖进入镜像，数据库、日志、外部目标等运行态数据继续走挂载目录。
目前系统采用**“单代码库、统一镜像 (`auto-gpt:latest`)、两个常驻实例 (`auto-gpt-plus` 和 `auto-plus2`) + 主服务 standby (`auto-gpt`)”**架构。

## 发布边界

- 镜像内容：后端代码、前端构建产物、Python/Node/浏览器依赖、入口脚本。
- 挂载数据：主库 `data`、`external_logs`、`_ext_targets` 以及 Plus 版隔离挂载卷 `/opt/auto-gpt-plus/data` 等。
- 发布流程不会把运行数据打进镜像。
- 回滚默认只回滚镜像，不自动覆盖数据库，避免丢掉发布后的新增数据。

## 标准生产发版

生产发布统一走根目录门禁，同时升级两个常驻实例并保持主服务停止：

```bash
/opt/auto-gpt/deploy.sh "清晰的发布说明" --mode=multi --backup
```

发布脚本会完成 Git 归档、统一镜像构建、Plus/Plus2 重建、standby 停止检查和 HTTP smoke。显式 `--backup` 会保存三个常规数据目录中的 `account_manager.db` 及历史 `team_manage.db`，但不会重新挂载或运行 Team Manager。

## 单实例镜像工具

`scripts/deploy-image-release.sh` 仅用于明确需要单独验证 `auto-gpt-plus` 的维护场景，默认目标为 `auto-gpt-plus:8001`，不会启动 `auto-gpt`：

```bash
scripts/deploy-image-release.sh --dry-run
scripts/deploy-image-release.sh --apply
```

该脚本会做这些事：

1. 校验 Compose 配置。
2. 构建带时间戳的 `auto-gpt` 镜像及 `latest` 标签。
3. 提交当前 live 容器为回滚镜像。
4. 备份关键 SQLite 数据库快照。
5. 用新镜像重建容器，保留挂载数据。
6. 做 HTTP smoke。
7. smoke 失败时自动切回回滚镜像。

## 手动检查多实例编排

只做人工 Compose 检查时，必须显式指定两个常驻服务：

```bash
# 1. 编译构建唯一的最新镜像 (auto-gpt:latest)
docker compose -f docker-compose.multi.yml build

# 2. 同时重启升级 Plus / Plus2（保持各自数据卷不变）
docker compose -f docker-compose.multi.yml up -d --remove-orphans auto-gpt-plus auto-plus2
```

`auto-gpt` 仅在显式启用 `standby` profile 时出现；日常发布禁止启用该 profile。`auto-k12` 已从 Compose 移除。

## 手动回滚

用发布时生成的备份目录回滚：

```bash
scripts/rollback-image-release.sh .rollback-backups/image-release-YYYYmmddTHHMMSSZ --apply
```

数据库快照会留在对应备份目录，但默认不自动恢复。只有确认要丢弃发布后的新数据时，才手动恢复数据库。

## 何时使用热补丁脚本

`deploy-to-auto-gpt-container.sh` 只适合非常小的紧急热补丁（直接同步文件到运行中容器的 `/app` 目录）。常规发布使用 `deploy.sh --mode=multi`，避免 checkout、容器代码和静态资源长期漂移。
