# auto-gpt Codex 操作约定

## 最高优先级例外：只读 / 先讨论 / 禁止变更

当用户明确说“先讨论 / 先理解需求 / 只分析 / 只读 / 不修改”，或本次任务明确要求“不部署 / 不提交 / 不写文件 / 限定写入范围”时，本节覆盖下方所有自动部署、自动提交和 changelog 规则：

- 只允许做现场核实、读取文件、查日志、运行不会改变状态的诊断命令。
- 禁止写入任何非授权文件，禁止改 `changelog.md`，禁止格式化、构建产物落盘或生成临时补丁文件。
- 禁止执行 `git add`、`git commit`、`git push`、`deploy.sh`、`docker cp`、容器重启、compose rebuild、systemd restart。
- 如果只做方案/分析，最终只汇报结论、风险和下一步建议，不要为了“闭环”擅自上线。

## Scope / 项目定位

这是 ChatGPT 账号自动注册与资源池管理项目。最容易踩坑的点：checkout 不等于线上运行态，live 行为通常以 Docker 容器为准。

本规则只适用于 `/opt/auto-gpt` 源码主线；`/opt/auto-gpt-plus` 与 `/opt/auto-plus2` 只作为实例的数据/配置隔离目录，不能在这些目录做源码开发或部署。`/opt/auto-k12` 是已退役实例的冷数据目录，不再参与编排、发布或功能研发。

## 运行真相

- 唯一源代码与主开发路径：`/opt/auto-gpt`（所有功能研发、Git提交、前端 npm build 均在此进行）
- Plus 数据隔离路径：`/opt/auto-gpt-plus`（不再维护源码，仅保留 `data/`、`external_logs/`、`_ext_targets/` 与 `.env` 作数据驱动）
- Plus2 数据隔离路径：`/opt/auto-plus2`（不维护源码，仅保留 `data/`、`external_logs/`、`_ext_targets/` 与 `.env` 作数据驱动）
- 退役 K12 冷数据：`/opt/auto-k12`（禁止作为源码或运行实例重新接入；历史数据库、日志和 `.env` 只用于审计/回滚）
- 共享配置路径：`/opt/auto-gpt/shared_config`（三个常驻业务实例共同挂载，仅保存共享全局配置模板、revision、审计和快照；严禁提交进 Git）
- 统一镜像名称：`auto-gpt:latest`
- 多实例编排配置：`docker-compose.multi.yml`
- 实例映射：
  - **主服务常驻实例 (`auto-gpt`)**：端口 `127.0.0.1:8000->8000/tcp`，`127.0.0.1:8317->8317/tcp`，`127.0.0.1:8889->8889/tcp`
    - 数据卷：`/opt/auto-gpt/data -> /runtime` 等
  - **Plus 常驻实例 (`auto-gpt-plus`)**：端口 `127.0.0.1:8001->8000/tcp`，`127.0.0.1:8318->8317/tcp`，`127.0.0.1:8890->8889/tcp`
    - 数据卷：`/opt/auto-gpt-plus/data -> /runtime` 等
  - **Plus2 常驻实例 (`auto-plus2`)**：端口 `127.0.0.1:8003->8000/tcp`，`127.0.0.1:8320->8317/tcp`，`127.0.0.1:8892->8889/tcp`
    - 数据卷：`/opt/auto-plus2/data -> /runtime` 等

`auto-gpt`、`auto-gpt-plus`、`auto-plus2` 都是常驻业务实例。常规发布、热更新和故障恢复后必须确认三者全部处于运行状态；禁止在发布结束后手动停止任一业务实例。只有退役的 `auto-k12` 必须保持停止且不得重新接入编排。

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
   * 脚本会自动执行 `git add -A` + `git commit`，通过 `docker-compose.multi.yml` 编译更新 `auto-gpt:latest` 镜像，并平滑升级 `auto-gpt` (`8000`)、`auto-gpt-plus` (`8001`)、`auto-plus2` (`8003`) 与 `phone-api-relay`；发布完成后不得再手动停止任一业务实例。
2. **极其紧急且轻微的 Python/前端逻辑热补丁**（秒级热更新）：
   ```bash
   /opt/auto-gpt/deploy.sh "<热补丁修复说明>" --mode=hot
   ```
   * 脚本在完成 Git 存档后，将直接调用底层热更新脚本把变动文件注入三个常驻业务容器并重启对应后端，无需重新构建 Docker 镜像。
3. **如需自动推送到远程分支**：在命令参数尾部追加 `--push` 即可。

## Changelog 更新维护约定（Agent 强制记录）

> [!IMPORTANT]
> **【Changelog 实时更新原则：每次代码/配置修改必填日志！】**
> **当 AI Agent 在本项目中进行任何功能添加、逻辑重构、Bug 修复、UI 改动或系统架构调整后，在执行 `deploy.sh` 部署上线前，必须主动打开并编辑 `/opt/auto-gpt/changelog.md` 文档，将本次修改详细、规范地记录在 `## [Unreleased] (未发布)` 或对应日期的版本条目中！**

编写 `changelog.md` 需遵循以下规范：
1. **格式与语义化版本**：遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.0.0/) 和 [Semantic Versioning](https://semver.org/lang/zh-CN/) (语义化版本)。
   - **大更新（Major/Minor）**：如果是核心功能重构、增加新模块、架构大幅调整，需增加更大的版本号（如 `v1.1.0` -> `v1.2.0` 或 `v2.0.0`）。
   - **小更新（Patch）**：如果是日常 Bug 修复、文案调整、微调优化，需增加较小的版本号（如 `v1.1.0` -> `v1.1.1`）。
   - **前端同步展示**：每次更新版本号后，**必须**同步更新网页前端（如 `frontend/src/` 中的相关组件如全局导航栏或侧边栏底部），在 UI 上明确展示当前的版本号。
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
- 数据库操作前：SQLite 用 `.backup`，然后 `PRAGMA integrity_check`；容器/镜像改动前优先 `docker commit` + `docker save`。操作时务必认清修改的是主服务、Plus 还是 Plus2 的独立挂载目录。
- 共享配置只允许通过 `shared_config/shared_config.db` 和 `core/config_store.py` 的模式开关同步；不要共用不同实例的 `account_manager.db`，也不要把 `shared_config/` 快照或 WAL 文件加入 Git。
- 资源池语义必须分清来源、绑定、释放、失败原因、复用规则，不要把账号库存、源系统库存、兑换券库存混成一张表。
- ChatGPT account 的 `used`、复制 AT、订阅、手机号绑定、邮箱绑定、auth 补抓状态要分开表达，不能因为一个动作误改另一个状态。
- 跨项目接入 `/root/account-delivery` 或源系统时，保持本地解析/本地库存和上游状态边界，不要把 plaintext 账号复制成两份库存。

## 汇报口径

默认用中文简洁汇报：做了什么、现在运行态怎样、验证结果、还剩什么风险。不要主动粘贴长代码、长命令输出或大段日志；涉及主服务、Plus、Plus2 或退役 K12 冷数据时必须明确写清对象，避免混报。每次部署汇报必须明确确认三个常驻业务实例均在运行。

## 推荐验证

```bash
cd /opt/auto-gpt

# 正式测试必须遵循 docs/testing-in-docker.md 的隔离测试容器规范。
# 不要在宿主机直接运行 pytest，也不要在常驻业务容器内 docker exec pytest。
cd frontend && npm run build

# 使用多实例编排构建并查看三个常驻业务实例状态
docker compose -f docker-compose.multi.yml build
test "$(docker ps --format '{{.Names}}' | grep -Ec '^(auto-gpt|auto-gpt-plus|auto-plus2)$')" -eq 3

# 分别验证三个常驻实例网页的连通性，并确认退役端口未恢复
curl -fsS http://127.0.0.1:8000/ >/dev/null
curl -fsS http://127.0.0.1:8001/ >/dev/null
curl -fsS http://127.0.0.1:8003/ >/dev/null
! curl -fsS --max-time 2 http://127.0.0.1:8002/ >/dev/null
```

如需把 checkout 热加载发布到调试容器，优先使用：

```bash
/opt/auto-gpt/scripts/deploy-to-auto-gpt-container.sh --dry-run
```
