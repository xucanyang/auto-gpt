# 更新日志 (Changelog)

本项目的所有显著更改都将记录在此文件中。

格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.0.0/)，并且本项目遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/) (语义化版本)。

## [Unreleased] (未发布)
### 新增 (Added)
- **本地状态批量同步增设日志面板与任务后台化**：
  - **实时进度与日志面板**：在 ChatGPT 账号列表页面 (`Accounts.tsx`) 触发批量同步本地状态（`probe_local_status`）时，现改为由后台任务处理并弹出实时任务日志面板 (`TaskLogPanel`)，支持实时的进度反馈与详细控制台输出查看。
  - **后台批量处理接口**：后台新增 `/chatgpt/probe-local-status/batch` 任务提交与处理端点 (`api/tasks.py`)，支持将批量状态同步任务转入异步后台执行，避免大批量处理时页面长连接阻塞与超时。
- **本地状态批量同步增设代理与延时配置功能**：
  - **前台配置弹窗支持**：在 ChatGPT 账号列表页面 (`Accounts.tsx`) 的“同步本地状态”下拉菜单中，针对选中账号及筛选账号均新增了“配置代理与延时”入口，并提供了与注册任务保持一致的参数配置弹窗。
  - **代理模式与重试容灾**：支持用户参考注册任务，为批量本地状态同步（`probe_local_status`）配置代理池自动选取、手动指定代理或直连模式；并支持配置国家缩写、最低健康度与候选数量，开启多代理候选自动重试切换（Failover）。
  - **随机延时平滑执行**：后台批量执行引擎 (`api/actions.py`) 针对批量本地状态探测等平台动作增加了区间随机延时（如 `delay_seconds` 至 `delay_max_seconds`），并在每个账号探测之间自动加入等待时间，避免并发过高触发风控限流。

### 修复 (Fixed)
- **修复手机绑定成功后 RT 与手机 API 信息未在账号列表保存展示的问题**：
  - **解决并发持久化冲突**：修复在执行手机绑定或更新账号状态时，由于 `session.refresh()` 遗漏导致内存中历史字段字典覆盖最新写入数据的问题，确保获取到的 `refresh_token`、`chatgpt_phone_binding`、`chatgpt_bound_phone` 实时写入 DB。
  - **兼容旧版与新版数据读取 fallback**：重构并优化账号列表状态构建函数与号码绑定模块（`bound_phone.py` / `accounts.py`），支持在历史兼容字段 (`chatgpt_bound_phone`) 缺失或字段名调整时，自动从新版字典字段读取号码与 API 渠道，确保绑定信息在 UI 上稳健展示。

## [1.1.0] - 2026-07-02
### 新增 (Added)
- **项目规范与日志体系沉淀**：
  - 建立 [`changelog.md`](file:///opt/auto-gpt/changelog.md) 更新日志文档，并将其维护规范写入 [`AGENTS.md`](file:///opt/auto-gpt/AGENTS.md)，强制约束 AI Agent 和开发者在发生代码、功能或配置变更后必填更新日志，确立项目演进追踪标准。
- **Codex 额度与监控体系全面整合**：
  - **账号池列表富文本展示**：将 Codex 额度用量与运行状态监控深度整合至 ChatGPT 账号池列表的高级富文本字段中，支持直观显示每个账号的实时剩余配额与授权状态。
  - **监控列表与状态列恢复**：100% 恢复 7 月 1 日原版 Codex 用量列的设计与交互逻辑，修复并恢复了独立的 Codex 额度监控列表页面与账号池 Codex 状态列展示。
- **Idea 批量提交与多额度自动容灾支持**：
  - **单卡多额度批量提交 (`feat(idea)`)**：重构并全面升级 Idea 提交模块，升级为 Idea 批量提交引擎。原生支持单卡绑定多额度与自动负载分配。
  - **故障隔离与智能重试**：新增提交失败自动标记无资格（ineligible）的熔断风控机制，并在当前账号/卡号失败时自动选取并重试其他可用账号，极大提高了批量业务提交的最终成功率。
  - **前后端诊断文案同步**：实现前台 UI 页面提示与后台服务运行日志中关于 Idea 批量提交及异常诊断文案的实时精确同步，提升问题定位效率。
- **本地短信验证码网关支持 (Local SMS Gateway)**：
  - 新增本地短信验证码网关 (`sms-verification-gateway` / `smstome_tool.py`)，支持本地接收、解析与处理短信验证码（OTP），完善自动化注册链路闭环。
- **外部订阅链接服务与 API 扩展 (External Subscription Link API)**：
  - 新增外部订阅链接生成与管理 API，支持一键生成、查询和分发订阅链接。
  - 增加外部订阅链接的本地有效性自检功能，防止分发失效链路。
  - 完善接口指引文档，新增外部订阅 API 的详细使用说明 (`docs: add external subscription API usage help`)。

### 优化 (Changed)
- **OAIPay 账号状态探测与上传兼容性优化**：
  - 全面增强 OAIPay 账号状态的自动探测能力与接口数据上传兼容性，支持批量多候选目标查询与灵活的高可用上传接口。
  - 引入容错降级逻辑：在网络探测不可连接或超时的情况下，系统不会直接丢弃任务，而是继续尝试直接发起强制上传流程，提升弱网及复杂网络环境下的账号存活利用率。
- **团队邀请与自动新注册导流优化 (ChatGPT Team Onboarding)**：
  - 深度优化 ChatGPT Team 团队邀请链条与自动化导流注册的交互流程，减少邀请链接在多步跳转中的丢单率。
- **Docker 多实例架构与自动化容器编排重构**：
  - **编排网络规范化**：统一将多实例 Docker Compose (`docker-compose.multi.yml`) 与默认编排的网络命名和子网配置规范为 `auto-gpt_default`，消除网桥冲突。
  - **并发构建优化**：移除多实例 Compose 文件中冗余的 `build` 构建块定义，避免在双实例或多容器并行拉起时发生 Docker 镜像标签 (Image Tag) 的并发写锁与标签覆盖冲突。
- **工程开发规范升级**：
  - 升级项目的 AI 与开发者操作约定 (`AGENTS.md`)，强制规定任何 Agent 在完成工作区代码、前端资源或配置文件修改后，必须主动执行根目录下的 `./deploy.sh` 进行版本存档、容器编译与在线服务更新。

### 修复 (Fixed)
- **前端一键复制 Access Token 修复**：
  - 修复了后端在序列化紧凑版账号列表 (`compact account list serializer`) 时遗漏 Access Token 字段的问题，确保前端管理界面中“一键复制 AT”功能能够准确获取并剪贴完整的 Token 数据。
- **已支付 Checkout (Already-Paid) 流程处理修复**：
  - 修复当遇到已经完成支付 (Already Paid) 的 Checkout 会话时系统报错卡死的问题，现能够正确识别状态并平滑继续后续业务或归档流程。
  - 将已支付但关联注册失败的 Checkout 会话准确计入系统“失败注册”分类监控指标中，避免数据大盘统计失真。
  - 优化并重构对已支付 Checkout 返回值的分类解析算法，避免将成功付款的凭证误判为网络异常。
  - 对无 Refresh Token (No-RT) 的 Checkout 会话，将其默认结算货币统一规范并设置为美元 (`USD`)。
- **外部订阅与兑换逻辑修复**：
  - 修复外部订阅链接可能因高并发请求被重复声明或二次领用 (Duplicate Claims) 的并发竞争 Bug。
  - 修复前端兑换页面 (`fix(ui): ensure redeem confirm always triggers request`) 中，兑换确认弹窗在特殊点击事件下无法触发后台发包请求的交互失灵问题。
- **凭证上传与手动授权加固**：
  - 加固手动授权登录 (Manual Auth) 与 `sub2api` 系统的凭证上传管道，增加了针对非法 Token 解析与超时中断的异常防护。

---

## [1.0.0] - 历史基线与核心架构
### 记录 (Project Baseline)
确立了 ChatGPT 账号自动注册、资源池管理与自动化交付的核心体系结构：
- **核心框架**：基于 Python/FastAPI（与异步调度系统）构建的高性能后端服务，搭配多实例数据卷驱动的 SQLite 数据库集群（`/opt/auto-gpt` 为主要研发源码与前端构建路径，`/opt/auto-gpt-plus` 为增强实例运行态与数据池隔离路径）。
- **自动化注册与风控处理**：
  - 集成 Web UI 账号池可视管理、自动化批量注册、工作流状态同步及代理池调度机制。
  - 内置本地 Turnstile Solver 自动化拉起模块、Sentinel 验证重试策略、OTP 短信验证码优先复用以及 Cloudflare Cookie 智能预热修复算法。
  - 沿袭 `any-auto-register` 系列优秀的插件架构体系与 Web UI 任务调度能力，并融入 Cloudflare Worker 临时邮箱等核心反风控套件。
- **多容器双实例全栈部署体系**：
  - **主服务实例 (`auto-gpt`)**：对外服务端口 mapping `0.0.0.0:8000->8000/tcp`、`0.0.0.0:8317->8317/tcp`。
  - **Plus 增强实例 (`auto-gpt-plus`)**：增强服务端口 mapping `0.0.0.0:8001->8000/tcp`、`0.0.0.0:8318->8317/tcp`。
  - **一键运维自动化**：配备由 `deploy.sh` 驱动的极速 CI/CD 工具链，支持常规多实例热升级 (`--mode=multi`) 与极速秒级容器代码热注入 (`--mode=hot`)。
- **多维度资源管理**：
  - 实现了 ChatGPT 账号库存、使用状态 (`used`)、Access Token 管理、订阅状态、关联绑定手机号与邮箱等核心元数据的精细化边界管理与解析隔离。
