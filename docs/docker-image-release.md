# Docker 镜像发布与回滚

本项目的生产运行方式以 Docker 镜像为准：代码和依赖进入镜像，数据库、日志、外部目标等运行态数据继续走挂载目录。
目前系统采用**“单代码库、统一镜像 (`auto-gpt:latest`)、四个常驻业务实例 (`auto-gpt`、`auto-gpt-plus`、`auto-plus2`、`auto-plus3`) + `phone-api-relay`”**架构。

## 发布边界

- 镜像内容：后端代码、前端构建产物、Python/Node/浏览器依赖、入口脚本。
- 挂载数据：主库 `data`、`external_logs`、`_ext_targets` 以及各实例隔离挂载卷 `/opt/auto-gpt-plus/data`、`/opt/auto-plus2/data`、`/opt/auto-gpt-register/data` 等。
- 发布流程不会把运行数据打进镜像。
- 回滚默认只回滚镜像，不自动覆盖数据库，避免丢掉发布后的新增数据。

## 测试与生产隔离

发布前的 Python、浏览器和注册链路测试必须遵循 [Docker 测试规范](testing-in-docker.md)。测试镜像应与生产镜像共用同一提交和运行依赖层，但使用独立的测试依赖、一次性容器和临时数据目录。

`docker-compose.multi.yml` 是生产编排文件，包含真实运行数据挂载、共享配置和常驻重启策略，禁止用于完整 pytest。测试服务不得读取生产 `.env`，不得挂载 `/opt/auto-gpt/data` 或 `/opt/auto-gpt/shared_config`，也不得占用生产端口和浏览器资源。

## 标准生产发版

生产发布统一走根目录门禁，同时升级四个常驻业务实例和 `phone-api-relay`，发布结束后五个活动服务都必须保持运行：

```bash
/opt/auto-gpt/deploy.sh "清晰的发布说明" --mode=multi --backup
```

发布脚本会完成 Git 归档、统一镜像构建、四个业务实例与 Relay 重建、健康检查和 HTTP smoke。首次接管旧的 `auto-plus3-local` 容器时必须显式使用 `--backup`；备份会覆盖 `/opt/auto-gpt-register/data` 的关键 SQLite 文件，但不会复制数据库到其它实例或重新挂载 Team Manager。

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

只做人工 Compose 检查时，必须显式确认四个常驻业务实例和 Relay：

```bash
# 1. 编译构建唯一的最新镜像 (auto-gpt:latest)
docker compose -f docker-compose.multi.yml build

# 2. 同时重启升级四个业务实例与 Relay（保持各自数据卷不变）
docker compose -f docker-compose.multi.yml up -d --remove-orphans \
  phone-api-relay auto-gpt auto-gpt-plus auto-plus2 auto-plus3
```

发布后必须确认 `auto-gpt`、`auto-gpt-plus`、`auto-plus2`、`auto-plus3` 和 `phone-api-relay` 均处于运行/健康状态；`auto-k12` 已从 Compose 移除且不得重新接入。旧的 `docker-compose.registration-node.yml` 仅保留为回滚/审计参考，不得与 multi 同时启动。

## 手动回滚

用发布时生成的备份目录回滚：

```bash
scripts/rollback-image-release.sh .rollback-backups/image-release-YYYYmmddTHHMMSSZ --apply
```

数据库快照会留在对应备份目录，但默认不自动恢复。只有确认要丢弃发布后的新数据时，才手动恢复数据库。

## 何时使用热补丁脚本

`deploy-to-auto-gpt-container.sh` 只适合非常小的紧急热补丁（直接同步文件到运行中容器的 `/app` 目录）。常规发布使用 `deploy.sh --mode=multi`，避免 checkout、容器代码和静态资源长期漂移。
