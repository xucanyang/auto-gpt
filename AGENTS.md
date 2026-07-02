# auto-gpt Codex 操作约定

这是 ChatGPT 账号自动注册与资源池管理项目。最容易踩坑的点：checkout 不等于线上运行态，live 行为通常以 Docker 容器为准。

## 运行真相

- 唯一源代码与主开发路径：`/opt/auto-gpt`（所有功能研发、Git提交、前端 npm build 均在此进行）
- Plus 数据隔离路径：`/opt/auto-gpt-plus`（不再维护源码，仅保留 `data/`、`external_logs/`、`_ext_targets/` 与 `.env` 作数据驱动）
- 统一镜像名称：`auto-gpt:latest`
- 多实例编排配置：`docker-compose.multi.yml`
- 双实例映射：
  - **主服务实例 (`auto-gpt`)**：端口 `0.0.0.0:8000->8000/tcp`，`0.0.0.0:8317->8317/tcp`，`127.0.0.1:8889->8889/tcp`
    - 数据卷：`/opt/auto-gpt/data -> /runtime` 等
  - **Plus 增强实例 (`auto-gpt-plus`)**：端口 `0.0.0.0:8001->8000/tcp`，`0.0.0.0:8318->8317/tcp`，`127.0.0.1:8890->8889/tcp`
    - 数据卷：`/opt/auto-gpt-plus/data -> /runtime` 等

本地文件修改不会自动影响容器里的代码或静态资源。需要明确部署到容器、重启或重建镜像，并做 live smoke。

## 自动化部署与 Git 归档约定（Agent 强制操作）

> [!IMPORTANT]
> **【Agent 主动执行原则：改完立即执行，严禁等待用户提醒！】**
> **当 AI Agent 在处理任务时完成了对任何代码、前端资源或配置文件的修改（通过工具写入、重构或替换）后，在结束当次轮次或向用户汇报之前，Agent 必须直接主动地使用终端工具执行 `/opt/auto-gpt/deploy.sh "<归纳本次修改内容>"`！绝对不要询问用户“是否需要提交/发版”，也绝对不要等待用户口头吩咐，改完代码立即自动执行发版！**

任何 Agent 或开发者在本项目 (`/opt/auto-gpt`) 中完成代码编写、前端构建或 BUG 修复后，**严禁只停留在工作区或手动拼装零散命令**，必须强制执行根目录下的自动化集成脚本 `./deploy.sh`，实现“Git 版本存档 + 容器上线 + 网页自检”一键闭环：

1. **常规功能开发/架构同步/依赖变更**（推荐默认模式）：
   ```bash
   /opt/auto-gpt/deploy.sh "<清晰明确的 Git Commit 描述>" --mode=multi
   ```
   * 脚本会自动执行 `git add .` + `git commit`，通过 `docker-compose.multi.yml` 编译更新 `auto-gpt:latest` 镜像，并同时平滑升级主服务 (`8000`) 与 Plus 服务 (`8001`)。
2. **极其紧急且轻微的 Python/前端逻辑热补丁**（秒级热更新）：
   ```bash
   /opt/auto-gpt/deploy.sh "<热补丁修复说明>" --mode=hot
   ```
   * 脚本在完成 Git 存档后，将直接调用底层热更新脚本把变动文件通过 `docker cp` 热注入两端运行中的容器，无需重新构建 Docker 镜像。
3. **如需自动推送到远程分支**：在命令参数尾部追加 `--push` 即可。

## 修改原则

- 前端改动：统一在 `/opt/auto-gpt/frontend/` build，再同步到容器实际静态目录；不要只停在 checkout。
- 后端改动：确认容器内 `/app` 代码是否更新，必要时使用多实例编排重建镜像，并重启对应服务。
- 数据库操作前：SQLite 用 `.backup`，然后 `PRAGMA integrity_check`；容器/镜像改动前优先 `docker commit` + `docker save`。操作时务必认清您修改的是主服务还是 Plus 服务的挂载目录。
- 资源池语义必须分清来源、绑定、释放、失败原因、复用规则，不要把账号库存、源系统库存、兑换券库存混成一张表。
- ChatGPT account 的 `used`、复制 AT、订阅、手机号绑定、邮箱绑定、auth 补抓状态要分开表达，不能因为一个动作误改另一个状态。
- 跨项目接入 `/root/account-delivery` 或源系统时，保持本地解析/本地库存和上游状态边界，不要把 plaintext 账号复制成两份库存。

## 推荐验证

```bash
cd /opt/auto-gpt
pytest tests -q || true
cd frontend && npm run build

# 使用多实例编排构建并查看状态
docker compose -f docker-compose.multi.yml build
docker ps | grep auto-gpt

# 分别验证两个不同功能网页的连通性
curl -fsS http://127.0.0.1:8000/ >/dev/null || true
curl -fsS http://127.0.0.1:8001/ >/dev/null || true
```

如需把 checkout 热加载发布到调试容器，优先使用：

```bash
/opt/auto-gpt/scripts/deploy-to-auto-gpt-container.sh --dry-run
```
