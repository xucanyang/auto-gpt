# Docker 镜像发布与回滚

本项目的生产运行方式以 Docker 镜像为准：代码和依赖进入镜像，数据库、日志、外部目标等运行态数据继续走挂载目录。
目前系统采用**“单代码库、统一镜像 (`auto-gpt:latest`)、多实例并存 (`auto-gpt` 和 `auto-gpt-plus`)”**架构。

## 发布边界

- 镜像内容：后端代码、前端构建产物、Python/Node/浏览器依赖、入口脚本。
- 挂载数据：主库 `data`、`external_logs`、`_ext_targets` 以及 Plus 版隔离挂载卷 `/opt/auto-gpt-plus/data` 等。
- 发布流程不会把运行数据打进镜像。
- 回滚默认只回滚镜像，不自动覆盖数据库，避免丢掉发布后的新增数据。

## 标准镜像发版（单实例模式）

先 dry-run 看计划：

```bash
scripts/deploy-image-release.sh --dry-run
```

确认后执行（默认构建 `auto-gpt` 镜像更新主服务）：

```bash
scripts/deploy-image-release.sh --apply
```

发布脚本会做这些事：

1. 校验 Compose 配置。
2. 构建带时间戳的 `auto-gpt` 镜像及 `latest` 标签。
3. 提交当前 live 容器为回滚镜像。
4. 备份关键 SQLite 数据库快照。
5. 用新镜像重建容器，保留挂载数据。
6. 做 HTTP smoke。
7. smoke 失败时自动切回回滚镜像。

## 多实例联合发版与编排模式 (`docker-compose.multi.yml`)

对于同时运行主网页 (`8000`) 与 Plus 网页 (`8001`) 的双实例环境，推荐直接使用根目录的综合编排配置进行全量升级：

```bash
# 1. 编译构建唯一的最新镜像 (auto-gpt:latest)
docker compose -f docker-compose.multi.yml build

# 2. 同时重启升级两个服务实例 (保持各自数据卷不变)
docker compose -f docker-compose.multi.yml up -d
```

## 手动回滚

用发布时生成的备份目录回滚：

```bash
scripts/rollback-image-release.sh .rollback-backups/image-release-YYYYmmddTHHMMSSZ --apply
```

数据库快照会留在对应备份目录，但默认不自动恢复。只有确认要丢弃发布后的新数据时，才手动恢复数据库。

## 何时使用热补丁脚本

`deploy-to-auto-gpt-container.sh` 只适合非常小的紧急热补丁（直接同步文件到运行中容器的 `/app` 目录）。常规发布用镜像发布或 Compose 编排升级，避免 checkout、容器代码和静态资源长期漂移。
