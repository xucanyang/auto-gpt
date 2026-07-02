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

## Changelog 更新维护约定（Agent 强制记录）

> [!IMPORTANT]
> **【Changelog 实时更新原则：每次代码/配置修改必填日志！】**
> **当 AI Agent 在本项目中进行任何功能添加、逻辑重构、Bug 修复、UI 改动或系统架构调整后，在执行 `deploy.sh` 部署上线前，必须主动打开并编辑 `/opt/auto-gpt/changelog.md` 文档，将本次修改详细、规范地记录在 `## [Unreleased] (未发布)` 或对应日期的版本条目中！**

编写 `changelog.md` 需遵循以下规范：
1. **格式与语义化版本**：遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.0.0/) 和 [Semantic Versioning](https://semver.org/lang/zh-CN/) (语义化版本)。
2. **分类清晰**：根据修改性质，将其归入恰当的小节标题下：
   - `### 新增 (Added)`：新功能、新接口、新页面或新配置项；
   - `### 优化 (Changed)`：现有功能改进、交互体验提升、性能优化、容器编排重构；
   - `### 修复 (Fixed)`：Bug 修复、异常容错、兼容性解决；
   - `### 安全 (Security)`：漏洞修补、风控拦截优化、权限与凭证加固；
   - `### 移除 (Removed)` / `### 废弃 (Deprecated)`：废弃或下线的历史功能。
3. **内容专业详实**：严禁随意敷衍一句话概括。需结合业务场景、前后端关联说明修改的背景与实际效果，并在重点功能与问题修复中提及对应的文件、模块或接口路径，以便于日后开发和维护回溯。

## 修改原则

- **文档与日志更新**：发生代码或配置修改时，必须在 `/opt/auto-gpt/changelog.md` 对应分类下追加本次改动的详细说明。
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
