# 更新日志 (Changelog)

本项目的所有显著更改都将记录在此文件中。

格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.0.0/)，并且本项目遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/) (语义化版本)。

## [Unreleased] (未发布)


## [1.3.5] - 2026-07-05
### 新增 (Added)
- **新增邮箱验证码 API 注册/登录收码 provider**：`core/base_mailbox.py` 新增独立 `email_api` 邮箱服务，并兼容 `api_email`、`email_otp_api`、`mail_api_otp` 别名；支持每行 `邮箱----API` 输入，自动补全无协议 API URL，轮询 JSON 响应中的 `status` 字段作为验证码来源。`status=0`、空值或非 4-8 位数字会按“尚未收到验证码”继续等待，收到有效验证码后复用现有 ChatGPT 注册与 OAuth 登录邮箱 OTP 流程。
- **支持 Gmail 一行双账号身份**：`parse_email_api_lines()` 会把 Gmail 原邮箱展开为“原地址 + 一个点号变体”两个注册身份，两个身份共用同一个接码 API；运行时按 Gmail canonical 与 API URL 建立任务级串行锁，避免单字段 `status` 在并发注册或二阶段登录时串码。账号落库时保留实际提交给 ChatGPT 的邮箱地址，不会把点号别名去点后覆盖为 canonical 邮箱。
- **补齐注册入口和配置入口**：`api/tasks.py` 在 `_prepare_register_request()` 中对 `email_api` 做后端权威解析、候选数量统计、注册数量自动展开和并发上限收敛；注册页、账号页注册弹窗与 Settings 邮箱服务配置区新增“邮箱验证码 API（email----api）”入口，可配置邮箱 API 行、轮询间隔、请求超时、默认 URL 协议与 Gmail 点号变体开关。

### 修复 (Fixed)
- **保护邮箱 API 敏感链接不进入任务日志**：`services/chatgpt_core/task_logging.py` 新增 `redact_raw_email_api_line()`，并对 `email_api_line`、`email_api_lines`、`email_api_accounts` 等字段逐行脱敏，只保留邮箱与 API host/path，移除 `s=`、token、query 和 fragment，避免接码凭证进入任务详情、历史日志和错误文本；`tests/test_chatgpt_task_logging.py` 补充结构化脱敏回归。
- **避免注册任务重复消费同一邮箱行**：`api/tasks.py` 将 mailbox 构造改为接收每次 attempt 的 `runtime_extra`，并为 `email_api` 注入 task 级共享池 key；`EmailApiMailbox` 在成功或失败 finalize 后释放同 Gmail/API 锁，任务结束时清理共享池，防止并发或代理重试时每个 worker 都从第一行重新分配邮箱。
- **确保后续登录/补抓 Auth 可恢复同一 API**：`services/chatgpt_core/plugin.py` 支持 mailbox 自定义 `export_state_config()`，避免把整批 `email_api_lines` 写进每个账号的 `chatgpt_mailbox_state.config`；成功账号只保存当前账号自己的 `api_url`、source email、Gmail root 和 variant。`services/chatgpt_core/pending_business_invites.py` 增加 `email_api` legacy state 兼容，后续 pending invite、邮箱测活和 auth recheck 能通过已保存 mailbox state 自动复用同一邮箱 API 收码。

### 优化 (Changed)
- **同步前端版本号至 v1.3.5**：`frontend/src/app/AppShell.tsx` 侧边栏版本展示更新为 `v1.3.5`，用于上线后确认邮箱验证码 API 注册/登录能力对应的静态资源已加载。

## [1.3.4] - 2026-07-05
### 优化 (Changed)
- **发布流程默认不再创建运行态备份**：`deploy.sh` 将 `.rollback-backups/deploy-*` 发布前备份改为显式 `--backup` 才创建，常规 multi/image/hot 发布只依赖 Git 提交与 live smoke，避免每次部署都复制三实例 SQLite、容器 inspect 和共享配置快照导致磁盘快速膨胀。
- **热更新脚本支持跳过备份**：`scripts/deploy-to-auto-gpt-container.sh` 增加 `SKIP_BACKUP=1`，在默认发布路径下跳过 container inspect、静态目录备份和 predeploy image commit；需要临时保守发布时仍可用 `deploy.sh "说明" --mode=hot --backup` 恢复原来的热补丁备份行为。
- **同步前端版本号至 v1.3.4**：`frontend/src/app/AppShell.tsx` 侧边栏版本展示更新为 `v1.3.4`，用于上线后确认本次发布脚本调整对应的静态资源已加载。

## [1.3.3] - 2026-07-05
### 新增 (Added)
- **新增三实例共享配置中心**：新增 `core/shared_config.py`，在 `/opt/auto-gpt/shared_config/shared_config.db` 中维护独立于账号/任务数据库的共享配置模板，使用 SQLite revision、审计记录和 JSON snapshots 替代裸文件互拷；`core/config_store.py` 按实例本地开关决定读写共享模板或本地 `configs` 表，避免共用 `account_manager.db` 导致账号、任务、日志和手机号池混库。
- **新增共享配置管理接口**：`api/config.py` 增加 `/api/config/share-state`、`/api/config/share/pull`、`/api/config/share/push`、`/api/config/share/diff` 与 `/api/config/share/audit`，支持实例开启共享、关闭共享时复制当前模板为本地基线、从共享模板拉取、显式将本实例配置推送为共享模板以及查看变更审计；常规 `/api/config` 保持纯配置对象返回，兼容现有任务入口。

### 优化 (Changed)
- **Settings 页增加共享配置状态卡片**：`frontend/src/pages/Settings.tsx` 在全局配置页顶部展示当前实例、共享/本地模式、共享 revision、最后更新来源、本地保留 key 说明，并提供“从共享拉取 / 查看差异 / 本实例推送为共享模板”等显式操作；共享模式保存时会带上 base revision，后端检测到版本冲突时返回 409，避免多实例静默覆盖。
- **三实例编排挂载共享配置目录**：`docker-compose.multi.yml` 为 `auto-gpt`、`auto-gpt-plus`、`auto-k12` 增加 `APP_INSTANCE_ID`、`SHARED_CONFIG_DB=/shared_config/shared_config.db` 与 `/opt/auto-gpt/shared_config` 挂载；`deploy.sh` 将 `shared_config/` 视为运行态敏感目录并纳入发布备份 tar，`.gitignore` 与 `.dockerignore` 防止共享模板、启动备份、密钥快照和 WAL 误入仓库或 Docker build context。
- **修正共享空值覆盖语义**：`core/shared_config.py` 增加存在性读取，`core/config_store.py` 在共享模式下即使共享模板中的值为空字符串也会按共享值返回，不再误回退到本实例旧本地值或环境变量，保证“清空某项配置”也能在共享实例间一致生效。
- **保留非 Settings 页写入的共享 key**：`api/config.py` 的共享差异和“本实例推送为共享模板”改为基于本地 `configs` 已保存全量快照，避免 `team_manager_*`、`grok2api_*` 等历史全局配置在人工推送模板时被静默删除，同时避免默认值展示层或环境变量兜底造成已同步实例仍显示大量伪差异。
- **同步前端版本号至 v1.3.3**：`frontend/src/app/AppShell.tsx` 侧边栏版本展示更新为 `v1.3.3`，用于上线后确认共享配置中心对应的静态资源已加载。

## [1.3.2] - 2026-07-05
### 新增 (Added)
- **新增 K12 独立运行实例 `auto-k12`**：`docker-compose.multi.yml` 增加第三个服务，复用统一镜像 `auto-gpt:latest`，但使用 `/opt/auto-k12/data`、`/opt/auto-k12/_ext_targets` 与 `/opt/auto-k12/external_logs` 作为独立运行态挂载；对外业务端口为 `8002`，CLIProxyAPI 端口为 `8319`，Solver 仅本机暴露为 `8891`，避免与主服务 `auto-gpt` 和 Plus 实例 `auto-gpt-plus` 的数据、日志、插件目录混用。

### 优化 (Changed)
- **扩展发布脚本到三实例闭环**：`deploy.sh` 的 multi/hot 发布、发布前 SQLite 备份、容器 inspect 归档和发布后 smoke 检查同步覆盖 `auto-k12`，上线后会同时校验 `http://127.0.0.1:8000/`、`8001` 与 `8002` 的首页和 `/api/health`，避免新增实例只写入 Compose 但未纳入发布门禁。
- **同步操作约定与前端版本号至 v1.3.2**：`AGENTS.md` 更新为主服务、Plus、K12 三实例口径，明确 `/opt/auto-k12` 仅作为数据/配置隔离目录；`frontend/src/app/AppShell.tsx` 侧边栏版本展示更新为 `v1.3.2`，用于确认新三实例编排对应的前端资源已加载。

## [1.3.1] - 2026-07-05
### 安全 (Security)
- **封闭账号详情接口的明文凭证旁路**：`api/accounts.py` 将 `/accounts/{account_id}` 与 `/accounts?detail=true` 改为沿用 compact serializer 的安全摘要模型，响应中只保留 `credentials`、`auth`、workspace variants、手机号绑定和同步状态等必要摘要；`token/password/extra_json` 中的 Access Token、Refresh Token、NextAuth session、cookies 与 ID Token 不再随详情接口返回，完整材料继续只能通过 `/accounts/{id}/secrets` 按字段读取。`tests/test_accounts_api_list_compact.py` 补充详情序列化脱敏回归，防止 K12 全空间保存后被 detail=true 绕过列表脱敏边界。

### 修复 (Fixed)
- **避免账号详情保存时误清空 Access Token**：`frontend/src/pages/Accounts.tsx` 在详情 Drawer 保存基础信息时，如果 Access Token 手动覆盖输入为空，会从 PATCH payload 中移除 `token` 字段，只保存状态等基础信息，避免后端详情脱敏后空 token 被表单原样提交并覆盖数据库中已保存的 AT。
- **Settings 页复用统一 K12 配置归一化**：`frontend/src/pages/Settings.tsx` 改为直接调用 `frontend/src/lib/chatgptK12Config.ts` 的 `buildChatGPTK12ConfigData()`，与注册页和账号页注册弹窗共享 workspace id 去重、保存所有空间默认值、join 超时/重试/轮询范围和 `capture_refresh_tokens=false` 的口径，减少后续 K12 配置字段漂移。

### 优化 (Changed)
- **同步前端版本号至 v1.3.1**：`frontend/src/app/AppShell.tsx` 侧边栏版本展示更新为 `v1.3.1`，用于上线后确认账号详情脱敏加固与 K12 Settings 配置统一对应的静态资源已加载。

## [1.3.0] - 2026-07-05
### 新增 (Added)
- **补齐 K12 注册后 workspace join 与全空间保存链路**：新增 `services/chatgpt_core/k12_workspace.py`，在 ChatGPT 注册成功并复用当前 Web session 拿到 Access Token 后，支持向配置的 K12 `workspace_id` 调用 `/backend-api/accounts/{workspace_id}/invites/request` 发起加入请求，再通过 `/backend-api/accounts/check/v4-2023-04-27` 读取当前账号可见的所有空间。捕获结果按 `workspace_artifacts` 输出，包含 free、K12、workspace 等 scope 摘要，并对每个空间复用现有 `fetch_chatgpt_session(exchange_workspace_token=true)` 交换对应 Access Token，避免重新登录、重复触发邮箱 OTP 或误走手机号绑定链路。
- **注册结果按 workspace variants 保存多条账号**：`services/chatgpt_core/access_token_only_registration_engine.py` 在 AT-only 注册成功后新增 K12/all-spaces 捕获步骤，保留 primary/free 账号不被覆盖，并将额外空间作为 linked accounts 交给现有保存逻辑落库；`services/chatgpt_core/chatgpt_registration_mode_adapter.py` 扩展 `k12` scope、artifact 去重、artifact 强度优先级、workspace 摘要与两阶段 RT 合并逻辑，确保第一阶段捕获到的 K12/全空间 AT-only variants 在第二阶段 free RT 成功后仍会保留为附加账号。
- **新增 K12 全局配置与任务参数透传**：`api/config.py` 增加 `chatgpt_k12_enabled`、`chatgpt_k12_workspace_ids`、`chatgpt_k12_save_all_spaces`、`chatgpt_k12_strict_join`、`chatgpt_k12_join_timeout_seconds`、`chatgpt_k12_join_retry_count`、`chatgpt_k12_post_join_poll_seconds`、`chatgpt_k12_capture_refresh_tokens` 配置项；`api/tasks.py` 将这些参数写入注册任务 extra 与账号 extra，任务创建日志也记录 K12 是否启用、目标数量与是否保存所有空间，方便后续排障回溯。
- **前端补齐注册页、注册弹窗、设置页和账号详情入口**：`frontend/src/pages/RegisterTaskPage.tsx` 与 `frontend/src/features/auth/components/RegisterTaskModal.tsx` 新增 K12 / Workspace 配置区，默认保存所有空间 variants、join 超时 60 秒、join 后轮询 `3,8,15` 秒，并将 Refresh Token variants 明确标记为预留禁用项；`frontend/src/pages/Settings.tsx` 新增全局 K12 / Workspace 配置分组；`frontend/src/features/accounts/components/AccountDetailModal.tsx` 新增“所有空间 / Workspace variants”摘要区，展示 scope、workspace_id、display_name、auth_level、partial_auth 和 source。

### 安全 (Security)
- **收紧 workspace variants 展示脱敏边界**：`api/accounts.py` 的 compact 账号列表只返回 workspace variants 摘要，不透出 access_token、refresh_token、id_token、session_token、cookies 或 cookie_header；账号详情页同样只展示空间摘要，完整凭证仍沿用 `/accounts/{id}/secrets` 按需读取，避免全空间保存功能把 Web session 材料泄漏到列表接口或普通详情摘要。
- **加固 K12 捕获异常与日志脱敏**：`services/chatgpt_core/k12_workspace.py` 与 `services/chatgpt_core/task_logging.py` 扩展 Bearer、`cookie_header`、NextAuth/Auth.js cookie、Python dict repr 等敏感字段脱敏；`api/tasks.py` 保存附加工作空间失败时也统一走 `sanitize_error_message()`，避免 linked workspace 的 token/cookies 因异常文本进入任务日志。

### 修复 (Fixed)
- **避免 K12 捕获影响基础注册落库**：K12 join 默认非严格模式，单个 workspace join、accounts/check 或 workspace token exchange 失败时只记录 `chatgpt_k12_join_summary`、`chatgpt_k12_join_results`、`chatgpt_all_spaces` 与 `chatgpt_k12_exchange_failures`，不会丢弃已经注册成功的基础账号；只有显式开启 `strict_join` 时才把 K12 捕获失败升级为注册失败，且严格模式现在同时覆盖目标 workspace token exchange 失败。补充 `tests/test_chatgpt_k12_workspace.py`、`tests/test_chatgpt_registration_mode_adapter.py` 与 `tests/test_accounts_api_list_compact.py`，覆盖 K12 join 请求、accounts/check 解析、全空间 artifact 生成、显式关闭 K12、compact 脱敏和两阶段 RT 合并 K12 variants。

### 优化 (Changed)
- **同步前端版本号至 v1.3.0**：`frontend/src/app/AppShell.tsx` 侧边栏版本展示更新为 `v1.3.0`，用于上线后确认 K12 workspace 注册与全空间保存对应的静态资源已加载。
- **统一 K12 前端配置口径**：新增 `frontend/src/lib/chatgptK12Config.ts`，注册页、账号页注册弹窗与设置页共用 workspace id、轮询间隔、超时与保存所有空间的归一化逻辑；`chatgpt_k12_capture_refresh_tokens` 在前端统一固定为 false 并禁用展示，明确当前版本只保存 AT-only workspace variants，避免预留开关被旧配置误触发。

## [1.2.11] - 2026-07-05
### 修复 (Fixed)
- **补齐账号详情页 Web session 凭证查看能力**：`frontend/src/features/accounts/components/AccountDetailModal.tsx` 将原先拥挤的账号详情 Modal 重构为右侧 Drawer，并新增独立“凭证材料”区；Access Token、Refresh Token、ID Token、NextAuth `session_token`、完整 `cookies` 与登录密码现在都按字段显示“已保存/未保存”，默认隐藏，点击“显示/复制”时才通过 `/api/accounts/{id}/secrets` 拉取完整内容，避免把 `extra_json` 原始结构整块铺开，同时保留状态与主表 token 的基础编辑入口。
- **扩展账号 secrets 接口覆盖注册阶段 Web 会话材料**：`api/accounts.py` 的 `/accounts/{account_id}/secrets` 新增 `cookies/cookie_header/id_token/session_token` 等字段别名与长度返回，并让详情序列化暴露 `credentials` 摘要，保证上一版注册阶段保存下来的完整 cookies/session token 能被详情页可靠发现和按需读取。
- **收敛账号列表明文凭证泄漏边界**：`api/accounts.py` 的 compact 列表序列化不再返回 `token/access_token/refresh_token` 或嵌套 `extra.access_token/extra.refresh_token`，只返回 `has_access_token/has_refresh_token/has_session_token/has_cookies/has_id_token/password_present` 等布尔摘要；`tests/test_accounts_api_list_compact.py` 增加 Web session 材料回归测试，固定列表脱敏与 secrets 按需读取契约。

### 优化 (Changed)
- **账号列表复制动作改为按需读取 secret**：`frontend/src/pages/Accounts.tsx` 的 AT、RT、密码复制按钮不再依赖列表行里的明文值，而是调用 `/accounts/{id}/secrets` 获取对应字段；列表和移动端卡片继续按布尔摘要展示“有/无”，复制成功后仍保留“已复制AT”状态反馈。
- **同步前端版本号至 v1.2.11**：`frontend/src/app/AppShell.tsx` 侧边栏版本展示更新为 `v1.2.11`，用于上线后确认账号详情凭证展示修复对应的静态资源已加载。

## [1.2.10] - 2026-07-05
### 修复 (Fixed)
- **保留注册阶段已获取的 ChatGPT Web 会话材料**：`services/chatgpt_core/chatgpt_registration_mode_adapter.py` 在 RT 两阶段注册中新增第一阶段 Web session 继承逻辑，第二阶段只补 `refresh_token/id_token` 但没有拿到 NextAuth `session_token` 或完整 `cookies` 时，会沿用第一阶段 AT-only 注册已经落地的 `session_token/cookies`，并同步填充 free workspace artifact，避免完整 Auth 成功后反而把可用于后续 Web 会话复用的材料洗空。
- **防止保存账号时空值覆盖已有 NextAuth 会话**：`core/db.py` 对 ChatGPT 账号更新增加 `session_token/cookies/cookie_header` 非空保护，新保存 payload 若这些字段为空会保留数据库旧值；补充 `tests/test_chatgpt_registration_mode_adapter.py` 与 `tests/test_save_account_web_session_preservation.py` 回归测试，固定两阶段注册和通用 `save_account()` 的持久化边界。

### 优化 (Changed)
- **同步前端版本号至 v1.2.10**：`frontend/src/app/AppShell.tsx` 侧边栏版本展示更新为 `v1.2.10`，用于上线后确认本次注册持久化修复对应的静态资源已加载。

## [1.2.9] - 2026-07-05
### 修复 (Fixed)
- **修复账号处理流水线 OAIPay 上传开关无法拉取分组的问题**：`frontend/src/pages/IdeaOaiPayPipeline.tsx` 为 `/idea-oaipay-pipeline` 的“OAIPay 上传”阶段补齐分组读取状态，当启用上传开关、打开分组下拉或点击“刷新分组”时，会调用现有 `/api/integrations/oaipay-categories` 接口拉取 OAIPay 分类，并把分类 ID、名称与库存统计展示为可搜索下拉选项；接口失败时在配置卡片内直接提示全局 OAIPay API URL / API Key 检查方向，避免只看到开关无反应。
- **避免运行状态轮询覆盖未提交的流水线配置**：`frontend/src/pages/IdeaOaiPayPipeline.tsx` 将 3 秒状态轮询改为只刷新任务状态、账号明细和日志，不再每次把历史任务配置写回表单；只有页面首次进入和启动新流水线后才同步配置，防止用户刚打开 OAIPay 上传或修改 Idea/手机号参数又被轮询重置。

### 优化 (Changed)
- **同步前端版本号至 v1.2.9**：`frontend/src/app/AppShell.tsx` 侧边栏版本展示更新为 `v1.2.9`，用于上线后确认本次 `/idea-oaipay-pipeline` 静态资源已加载。

## [1.2.8] - 2026-07-05
### 优化 (Changed)
- **规整账号处理流水线配置页布局**：`frontend/src/pages/IdeaOaiPayPipeline.tsx` 将 `/idea-oaipay-pipeline` 配置页从不等宽、不等高的 `Row/Col` 卡片拼接改为“账号来源”全宽配置区 + 四个阶段配置卡片的稳定网格结构；账号来源字段按来源、范围、筛选、注册补位目标统一排布，Idea、本地状态/Gate、手机号绑定、OAIPay 上传四个处理阶段使用一致的两列字段节奏和响应式断点，避免配置区大小随机、视觉重心歪斜。
- **补齐配置页响应式与版本展示**：`frontend/src/index.css` 新增 `idea-oaipay-config-*` 作用域样式，统一配置卡片高度、字段间距、宽字段跨列、移动端单列折叠和中等屏两列阶段布局；`frontend/src/app/AppShell.tsx` 侧边栏版本号同步更新为 `v1.2.8`，方便上线后确认新的前端资源已经加载。

## [1.2.7] - 2026-07-05
### 修复 (Fixed)
- **透出 OAIPay 上传认证失败的真实服务端错误**：`services/chatgpt_core/oaipay_upload.py` 在 OAIPay 上传接口返回 `401/403` 或其他 HTTP 错误时，新增对响应 JSON 中 `detail/message/msg/error` 的统一提取，日志与任务结果会展示 `上传失败: HTTP 401: 上传密钥无效` 这类可操作原因，不再只保留泛化的 `上传失败: HTTP 401`；`services/oaipay_sync.py` 的远端账号探测同样透出 `detail`，避免上传前探测阶段把密钥问题误报成普通不可达。
- **补充 OAIPay 401 诊断回归测试**：`tests/test_oaipay_sync.py` 新增上传与远端探测两个 401 场景，固定 FastAPI 风格 `{"detail":"上传密钥无效"}` 的错误提取行为，防止后续接口兼容重构再次吞掉真实错误详情。

### 优化 (Changed)
- **同步 OAIPay 设置页上传密钥文案**：`frontend/src/pages/Settings.tsx` 将 OAIPay API Key 标签从“管理员密码”改为“API Key / 上传密钥（gpt.cccy.me 的 UPLOAD_KEY）”，与 `gpt.cccy.me` 安全加固后的独立 `UPLOAD_KEY` 口径保持一致；侧边栏版本展示同步更新为 `v1.2.7`，方便上线后确认新前端资源已加载。

## [1.2.6] - 2026-07-05
### 新增 (Added)
- **新增账号处理流水线串联注册、本地账号、Idea、手机号与 OAIPay**：新增 `docs/idea-oaipay-pipeline/design.md` 作为方案文档，并增加 `services/idea_oaipay_pipeline/`、`api/idea_oaipay_pipeline.py` 与 `/api/idea-oaipay-pipeline/*` 接口，支持从“注册新账号”或“本地账号快照”启动同一条账号级流水线，再按配置执行 Idea 批量提交、本地状态刷新、状态 Gate、手机号绑定和 OAIPay 上传。流水线不再把 Plus 写成全局硬门槛，`status_gate` 可按 `none / account_valid / subscription_in / upload_ready` 放行，支持 Free 账号只做手机号绑定或继续上传的业务形态。
- **新增流水线持久化任务与账号明细表**：`core/db.py` 接入 `idea_oaipay_pipeline_tasks` 与 `idea_oaipay_pipeline_items`，为每个账号独立记录 `register_stage / idea_stage / check_stage / gate_stage / phone_stage / oaipay_stage / overall_status`、子任务 ID、Idea order/card 信息、手机号策略、OAIPay 远端状态和失败原因；服务启动时 `main.py` 会恢复最近仍在运行/暂停的流水线，避免长任务只存在内存态。
- **新增管理端“账号处理流水线”页面**：`frontend/src/pages/IdeaOaiPayPipeline.tsx`、`frontend/src/app/router.tsx`、`frontend/src/app/AppShell.tsx` 新增独立入口 `/idea-oaipay-pipeline`，提供运行摘要、配置表单、账号明细表、主流水线日志和子任务日志 Drawer。页面复用现有 `TaskLogPanel` 查看注册、Idea、手机号等子任务日志，明细操作支持单账号重刷状态与重传 OAIPay。

### 优化 (Changed)
- **手机号绑定与 OAIPay Gate 按策略解耦**：流水线手机号步骤支持 `disabled / best_effort / required`，`best_effort` 失败不会阻断后续 OAIPay，`required` 才会把账号停在人工处理；OAIPay 上传条件支持单独配置订阅类型与是否要求手机号，不再默认要求 Plus。手机号判定同时识别真实绑定记录 `chatgpt_phone_binding` 和 OpenAI 已绑定手机号记录 `chatgpt_bound_phone`，避免已有绑定账号被重复执行绑定。
- **子任务丢失、注册回收与无 OAIPay 场景补强**：Idea / 手机号 / 注册子任务引用在服务重启后若已从内存任务表丢失，流水线会清理 active child task 并把相关账号标记为可排查状态，不再无限卡在 polling/running；进程退出只停止调度线程，不再把数据库中的运行中任务误改为人工停止，确保下次启动可恢复最近流水线。当 OAIPay 未启用时，`oaipay_stage=disabled` 的账号也会在 Gate 与手机号策略满足后正确归档为 `done`。注册任务新增 `registered_accounts` 结构化结果快照，流水线优先按 account_id/email 回收批量注册成功账号，避免只读最后一条 `TaskLog` 导致漏收多账号注册结果。

### 修复 (Fixed)
- **补充流水线边界回归测试**：新增 `tests/test_idea_oaipay_pipeline_config.py`，覆盖 Pydantic `source.register` alias、来源/手机号策略校验、OAIPay 禁用时的账号完成归档、本地来源匹配 0 个账号的失败收口、历史任务重试拦截，以及已有手机号绑定记录的识别，防止后续把 Free 账号绑定手机号、非 Plus 放行或 OAIPay 可选流程再次写死。

## [1.2.5] - 2026-07-05
### 优化 (Changed)
- **动态代理任务表单改为默认复用全局模板**：`frontend/src/lib/taskProxySettings.ts` 调整任务代理配置读取与校验逻辑，当全局 `dynamic_proxy_template` 已保存时，注册、账号本地状态同步、手机号绑定和邮箱测活等任务弹窗不再把代理链接作为动态代理必填项；任务内“动态代理模板”字段改为“可选覆盖”，留空时后端继续使用全局模板，只要求填写出口国家并按国家改写 `region-XX`、刷新 `sid`。
- **同步动态代理表单文案与版本展示**：`frontend/src/pages/RegisterTaskPage.tsx`、`frontend/src/features/auth/components/RegisterTaskModal.tsx`、`frontend/src/pages/Accounts.tsx`、`frontend/src/pages/CustomEmailRecheckPage.tsx` 将动态代理模板标签、占位符和说明统一改为“留空使用全局模板；填写后仅本次任务覆盖”，避免操作者误以为每个任务都必须重复粘贴 Cliproxy 链接；侧边栏版本号同步更新为 `v1.2.5`。

## [1.2.4] - 2026-07-05
### 修复 (Fixed)
- **修复 Cliproxy 动态代理指定国家被 GeoIP 限流误判失败的问题**：`core/proxy_utils.py` 的动态代理候选探测现在区分“基础连通失败”“实测国家不一致”和“GeoIP 无法实测”三种状态；当 Cliproxy 模板已经按 `region-XX` 成功改写到目标国家、基础出口 IP 可用，但第三方 GeoIP 查询返回 429 或无国家时，不再把 `actual=unknown` 误判为 `country_mismatch` 并丢弃所有候选，而是记录 `actual=unverified / probe=geo_unavailable` 后允许任务继续执行。只有实测到明确的其他国家时才继续硬失败，避免 JP/US 等可用出口被探测依赖误杀。
- **修复动态代理运行态偷偷把 Cliproxy `socks5://` 改成 `http://` 的协议漂移**：`core/proxy_utils.py` 的 `normalize_proxy_url()` 不再把 Cliproxy Socks5 模板降级成 HTTP，而是统一规范为 `socks5h://` 以保持 Socks5 协议并使用远端 DNS；动态代理日志、预览接口和任务执行现在与 Cliproxy Socks5 中间服务器语义一致，同时避免本地 DNS/普通 `socks5://` 在部分 HTTPS 目标上触发 `WRONG_VERSION_NUMBER`。

### 优化 (Changed)
- **动态代理国家探测优先使用代理出口侧 Cloudflare Trace**：`services/proxy_scanner.py` 新增通过代理访问 `https://www.cloudflare.com/cdn-cgi/trace` 的国家探测路径，优先读取 `loc=XX` 作为出口国家，再回退到服务器侧 `ipapi/ipinfo` 查询；代理扫描、动态代理预览 `/api/proxies/dynamic-preview` 和任务运行前探测都能减少公共 GeoIP API 429 对国家判断的影响，并在响应中暴露 `geo_source`、`geo_unverified` 等诊断字段。
- **同步前端版本号至 v1.2.4**：`frontend/src/app/AppShell.tsx` 侧边栏底部版本展示更新为 `v1.2.4`，用于上线后确认本次动态代理修复的静态资源已经加载。

## [1.2.3] - 2026-07-05
### 优化 (Changed)
- **恢复手机号绑定面板三策略与统一号段选择器**：`frontend/src/pages/Accounts.tsx` 将原先只有“普通绑定/号段抽样”开关的面板恢复为 `普通绑定`、`限定号段绑定`、`号段抽样测试` 三种取号策略，并用同一个“号段范围”选择器承载两类号段语义。限定号段绑定会把所选号段作为正式绑定约束，号段抽样测试继续按每段 1/2 个号码执行；可用、不可用、暂不可用、已绑满号段均只作为状态提示展示，不再在前端禁点，容量与实时可用性交给后端按 `prefix_bind_enabled` / `prefix_sample_enabled` 判定。
- **压缩“只测发码/收码”控件为任务级小开关**：手机号绑定弹窗里的 `prefix_sms_probe_only` 控件从大块提示卡压缩为紧凑行内 `Switch`，并明确文案为粘贴号码、普通手机号池、限定号段、号段抽样都生效；开启后仍会自动关闭“尽量用满同一个手机号”，避免探测模式重复消耗同一号码。
- **同步前端版本号至 v1.2.3**：`frontend/src/app/AppShell.tsx` 侧边栏底部版本展示更新为 `v1.2.3`，用于上线后确认本次手机号绑定面板恢复的静态资源已经加载。

### 修复 (Fixed)
- **修复限定号段绑定缺少前端入口的问题**：任务提交时重新从 `phone_pool_mode` 推导 `prefix_bind_enabled`、`prefix_sample_enabled` 与 `selected_prefixes`，限定号段绑定会要求至少选择一个号段并把所选号段传给 `/tasks/chatgpt/phone-binding-test`；号段抽样在手动选择号段时优先使用 `selected_prefixes`，未选择时才使用“全部号段 / 测试可用号段 / 仅不可用号段”的筛选范围，避免前端看不到号段或提交后退化成普通号池取号。

## [1.2.2] - 2026-07-05
### 修复 (Fixed)
- **修复手机号池收码 API 复制仍只拿到链接本身的问题**：`frontend/src/pages/PhonePool.tsx` 新增统一的 `phoneApiCopyText()` / `copyPhoneApiLine()` 复制入口，手机号列、桌面端“收码 API”列、移动端卡片和诊断抽屉均按 `手机号----完整收码API` 格式写入剪贴板；例如 `+12094908876----https://sms.aa8.pl/api/record?token=...`，避免只复制手机号或只复制 API URL 后无法直接回填到绑定任务。
- **恢复“收码 API”列显式复制按钮**：桌面表格和移动端 API 字段重新提供带 `CopyOutlined` 的“复制完整API”按钮，底层使用项目内已验证的 Clipboard API + textarea fallback，而不是只依赖 AntD 文案复制图标，减少非安全上下文或浏览器权限导致的复制失败。

### 优化 (Changed)
- **同步前端版本号至 v1.2.2**：侧边栏底部版本展示由 `v1.2.1` 更新为 `v1.2.2`，用于上线后确认本次手机号池复制修复的静态资源已经加载。

## [1.2.1] - 2026-07-04
### 新增 (Added)
- **新增任务代理设置统一保存与复用**：代理管理页“动态代理预览”新增“保存为任务默认”入口，并新增 `frontend/src/lib/taskProxySettings.ts` 作为注册任务、批量本地状态同步、邮箱测活、手机号绑定等任务表单的统一代理配置适配层；用户在任一任务中填写的代理模式、动态代理模板/API、出口国家、失败处理、代理池候选数与最低健康分会写入 `/api/config` 的 `task_proxy_*` 全局配置，并同步镜像到 `dynamic_proxy_template` / `dynamic_proxy_default_country`，后续所有任务弹窗默认读取同一套代理参数，避免注册、本地刷新、测活、手机号绑定各自记一份导致配置漂移。
- **新增动态代理模式，支持按需指定出口国家**：后端新增 `core/dynamic_proxy.py` 与统一候选解析入口，注册任务、批量本地状态同步、邮箱测活、HME 复测和手机号绑定现在均支持 `proxy_mode=dynamic`。该模式使用任务内或全局配置的动态代理模板，按 `proxy_country_code` 改写 `region-XX`，每次候选生成刷新 `sid-xxx-t-`，并保留现有 cliproxy `socks5://` 到运行态 `http://` 的兼容转换。
- **新增动态代理出口预览接口与前端入口**：代理管理页新增“动态代理预览”卡片，调用 `/api/proxies/dynamic-preview` 生成脱敏运行代理并可按需实测出口 IP/国家；全局配置页新增动态代理默认模板、默认出口国家、国家严格匹配、运行前探测与探测超时配置，便于在任务页复用。
- **前端任务表单增加“动态代理”代理模式**：注册页、账号页注册弹窗、批量本地状态同步弹窗、邮箱测活页与手机号绑定高级设置均新增动态代理选项，并明确区分“指定代理失败回退代理池”和“动态代理失败刷新 sid 重试”的候选语义。

### 安全 (Security)
- **强化动态代理与任务日志脱敏边界**：动态代理模板、运行代理、批量本地状态同步参数、邮箱测活结果和 HME 复测详情在任务 meta、历史日志与预览接口中只保存脱敏地址；新增对 `dynamic_proxy_template`、`proxy_template` 等结构化字段的统一代理认证脱敏，避免代理账号、密码或 sid 模板信息进入可展示日志。

### 修复 (Fixed)
- **恢复 ChatGPT 账号池分页切页与每页数量选择**：根据 2026-06-17 账号列表性能审计时保留的历史实现，补回 `frontend/src/pages/Accounts.tsx` 中的 `auto-chatgpt.accounts.page-size.v1` 本地持久化、默认每页 20 条以及 10 / 20 / 50 条切换选项，并将当前页码、页大小、后端 `/accounts?page=&page_size=` 查询和 `AccountsTable` 的 `Pagination` 联动起来；修复近期回归后账号池只能固定 10 条且桌面端分页器不再显示每页数量切换的问题，避免运营跨页查看和批量选择账号时被固定页长卡住。
- **恢复手机号绑定粘贴号码的手机号池 upsert 与状态回写链路**：修复 `api/tasks.py` 在日志重构后把手动粘贴的 `手机号----收码API` 仅作为一次性任务输入、未再调用 `_import_manual_phone_entries_to_pool` 的回归问题。现在手机号绑定任务启动时会先将粘贴号码 upsert 到 `phone_pool`：新号码直接创建池记录，已存在号码会保留原有池记录并更新本次粘贴的收码 API；随后任务仍按粘贴的固定号码列表执行，不切换为动态取号，但每个号码会标记为 `pool_managed`，运行结束后继续通过 `PhonePoolRepository.record_task_status()` 回写绑定成功、短信探测成功、OpenAI 拒绝、限流、无验证码等状态。同步修正短信探测模式误继续进入绑定保存/Auth 重试的保护分支，并补充任务日志，明确展示粘贴号码启动前入池和运行后状态回写结果。
- **修复手机号绑定“只测发码/收码”前端开关丢失的问题**：补回 `frontend/src/pages/Accounts.tsx` 中全局可用的 `prefix_sms_probe_only` 表单项与本地设置保存，任务提交时同时下发 `prefix_sms_probe_only` / `sms_probe_only`，确保粘贴号码、普通手机号池、号段抽样三种模式都能进入后端短信探测分支；开启后前端会强制关闭“尽量用满同一个手机号”，任务提示也会明确显示“仅测发码/收码”，避免用户以为已开启但实际仍执行真实绑定。
- **修复批量本地状态同步动态代理异常后任务悬空的问题**：`api/tasks.py` 的 `_run_batch_probe_local_status` 增加普通 `Exception` 兜底捕获，动态代理模板缺少出口国家、缺少 `region-XX`、探测解析异常或其他未预期错误都会写入任务日志、保存失败历史并将任务状态标记为 `failed`，不再让后台任务崩溃后前端持续显示 pending/running；同时动态代理国家 fallback 与前端必填校验保持一致，缺省时使用全局默认国家。
- **修复 Idea 开通成功后账号本地状态只被外部 paid 标记覆盖的问题**：`api/tasks.py`、`api/baxigpt_cdk_pool.py` 与后台 `baxigpt_status_poller` 在上游订单确认 paid 后，改为先执行 `sync_chatgpt_account_local_status` 本地刷新，由真实 ChatGPT 探测结果统一更新账号 `status`、订阅能力和上传门禁；卡密侧 paid/order_id 仍写入 `extra.baxigpt_cdk`，并将 `local_status_refresh` 摘要同步回账号，避免仅凭外部支付成功把账号状态简单改成 `subscribed` 而遗漏订阅计划、auth/codex 状态等本地字段。
- **修复 Idea 批量提交轮询全部超时的真实任务 ID 匹配问题**：`services/chatgpt_core/baxigpt_client.py` 现在优先消费上游 `/api/task/submit` 返回的 `created_tasks` 真实任务 ID，并禁止把 Access Token 前缀生成的 `fallback_*` 当成可轮询订单；当旧上游未返回任务 ID 且无法可靠匹配时，会直接返回“上游已受理但未返回可轮询任务ID”的明确失败，避免继续轮询不存在的订单直到超时。
- **修复 Idea 上游失败原因丢失与 summary 误导问题**：`BaxiGptClient.status/query` 已兼容读取上游 `fail_reason/raw_fail_reason`，任务执行器 (`api/tasks.py`) 增加上游已受理数与轮询超时数统计，summary 不再出现“成功 0、跳过 0、失败 0”但实际全是 timeout 的错误表达。
- **修复 Idea 提交后本地状态未写入 order_id 的问题**：`api/tasks.py` 在每个账号提交成功后立即将真实 `order_id/display_id` 写入卡密提交记录和账号 `extra.baxigpt_cdk`，并为每个活动订单保留独立记录快照，避免同一卡密多账号提交时共享同一个内存记录导致最后一个账号覆盖全部轮询目标。
- **修正动态代理表单误展示代理池筛选项的问题**：注册页、账号页注册弹窗、批量本地状态同步、邮箱测活和手机号绑定的动态代理模式现在只展示动态模板、出口国家和“失败后刷新 sid 重试”，不再展示或提交“最低健康分”“最多候选/候选代理数量”等代理池专属参数；后端动态代理重试次数也改为独立的 `dynamic_proxy_max_attempts` 默认值，不再复用代理池候选数配置。
- **修复批量上传时丢失密码与手机号等附加信息的问题**：在同步账号到 OAIPay (`upload_chatgpt_account_to_cpa` 等批量操作) 过程中，修正了 `build_chatgpt_sync_account` 构造伪账户对象时遗漏了 `password` 和 `extra` 的问题，从而彻底修复了该场景下上传的账号在兑换界面只有兜底网关而没有真实专属接码链接的问题。
- **OAIPay 账号数据上传修复**：修复了上传至 OAIPay (gpt.cccy.me) 时没有包含绑定的手机号（`chatgpt_bound_phone_number`）以及本地接码网关配置（`local_phone_gateway_url` 等）的问题，现在它们会作为顶层字段被包含在 `accounts` 的对象中，使得下游接收方可以正确生成对应的 `delivery_data`。

### 优化 (Changed)
- **同步前端版本号至 v1.2.1**：侧边栏底部版本展示由 `v1.2.0` 更新为 `v1.2.1`，用于区分本次账号池分页恢复发布与 2026-07-03 的 `v1.2.0` 基线，方便上线后从 live 页面直接确认静态资源已更新。
- **OAIPay 账号数据上传优化**：
  - 增强向 OAIPay 管理系统上传账号的字段丰富度。原先 `extra_info` 仅简单上传 Access Token，现重构为将该账号的 Refresh Token、绑定的手机号 (phone) 以及手机号接收验证码的完整 API 链接 (api_url) 等字段组装合并为一个完整紧凑的 JSON 字符串（包含所有的 `token_data`），使下游接收方能一次性获取该账号的所有关键会话和关联信息。
- **优化手机号池列表的复制体验**：
  - 将手机号列原有的单纯手机号复制更新为合并复制，格式为 `手机号----API`，并在前端（PC端和移动端）移除收码 API 列独立且冗余的复制按钮，提升了数据提取的便捷性。
## [1.2.0] - 2026-07-03
### 新增 (Added)
- **前端版本号展示与规范化版本控制**：
  - **网页前端版本展示**：在侧边栏导航底部（或全局布局中）新增系统当前版本号显示，便于直观确认代码更新情况。
  - **严格遵守语义化版本控制**：完善 `AGENTS.md` 规范，对每次变更执行大/小版本更迭规范并在 Changelog 中明确标注，以此驱动项目的规范化演进。
### 新增 (Added)
- **本地状态批量同步增设日志面板与任务后台化**：
  - **实时进度与日志面板**：在 ChatGPT 账号列表页面 (`Accounts.tsx`) 触发批量同步本地状态（`probe_local_status`）时，现改为由后台任务处理并弹出实时任务日志面板 (`TaskLogPanel`)，支持实时的进度反馈与详细控制台输出查看。
  - **后台批量处理接口**：后台新增 `/chatgpt/probe-local-status/batch` 任务提交与处理端点 (`api/tasks.py`)，支持将批量状态同步任务转入异步后台执行，避免大批量处理时页面长连接阻塞与超时。
- **本地状态批量同步增设代理与延时配置功能**：
  - **前台配置弹窗支持**：在 ChatGPT 账号列表页面 (`Accounts.tsx`) 的“同步本地状态”下拉菜单中，针对选中账号及筛选账号均新增了“配置代理与延时”入口，并提供了与注册任务保持一致的参数配置弹窗。
  - **代理模式与重试容灾**：支持用户参考注册任务，为批量本地状态同步（`probe_local_status`）配置代理池自动选取、手动指定代理或直连模式；并支持配置国家缩写、最低健康度与候选数量，开启多代理候选自动重试切换（Failover）。
  - **随机延时平滑执行**：后台批量执行引擎 (`api/actions.py`) 针对批量本地状态探测等平台动作增加了区间随机延时（如 `delay_seconds` 至 `delay_max_seconds`），并在每个账号探测之间自动加入等待时间，避免并发过高触发风控限流。
- **账号同步日志强化展示网络代理与出口 IP 信息**：
  - **精细化代理使用记录**：在触发账号状态批量同步等任务时，现在会在任务执行日志中精准记录每一次探测动作所使用的代理地址（`proxy_url`）及其实际出口 IP（`exit_ip`）。当配置为多代理轮换或风控 fallback 时，日志能清晰展示对应连通性与节点网络详情。
  - **手动指定代理智能解析**：即使用户在界面中选择“手动指定代理”（`mode="specified"`），系统也会自动在底层的代理记录池中尝试回溯解析该代理的出口国家（`country`）与出口 IP；若该私有代理未曾录入本地库，还会通过实时接口 (`ip-api.com`) 进行秒级回源诊断，告别以前单调的 `specified` 占位符。
### 优化 (Changed)
- **优化 OAIPay 账号上传验证与自动分组逻辑**：
  - **放宽上传验证条件**：在账号同步（`oaipay_sync.py`）中，当向 OAIPay 上传账号时，不再强制要求账号必须包含 Access Token（`at`）或 Refresh Token（`rt`），仅需检测账号的基础有效性（`auth_level != "invalid"`）即可放行上传，提高了账号导入的兼容性。
  - **基于订阅与 RT 状态的智能自动分组**：在构建 OAIPay 上传载荷（`oaipay_upload.py`）时，新增基于账号类型及 Refresh Token（`rt`）存在与否的智能分组功能：
    - 含有 RT 的 Plus 账号默认自动归类到 `PLUS--已接美国长效`；
    - 不含 RT 的 Plus 账号自动归类到 `PLUS--未接码`；
    - 含有 RT 的 Free 账号自动归类到 `FREE--已接码带RT`。

### 修复 (Fixed)
- **修复 OAIPay 自动分组时 Plus 账号错误识别为 Free 的问题**：
  - **透传最新能力快照**：修复了在构建待上传的 `sync_account` 镜像时，因为历史缓存在内存中未随最新状态同步更新，导致 Plus 账号因缺失最新订阅快照而在底层被误分到 Free 组的问题。现在系统会将刚刚完成连通性探测的最新 `capabilities` 快照透传给上传组装器，确保账号分类严格、精准。
- **修复 Idea 批量提交功能无法弹出日志面板的问题**：
  - **移除未定义函数调用**：修复了在后端任务队列分配 (`_resolve_baxigpt_cdk_submit_accounts`) 时，调用了未定义的本地校验函数 `_baxigpt_cdk_ineligible_reason` 导致触发 500 内部服务错误的问题。通过移除该无效引用，确保账号合法性校验逻辑与全量筛选分支统一（仅校验 Access Token 存在与否），恢复了提交页面的正常运作与日志面板正常弹出。
- **修复 OAIPay 自动分组失败的问题**：
  - **动态映射分组 ID**：修正了自动分组逻辑，在向 OAIPay 提交数据前，会先拉取对方的分类列表（`/api/auto-gpt/categories`），动态将分组名称（如 `PLUS--已接美国长效`）转换为系统所需的数字 ID 进行上传，避免因为提交中文字符串导致分组无效或被归为默认。

- **修复因状态探针网络异常导致账号原有订阅状态（如 Plus）被重置丢失的问题**：
  - **后端智能 Fallback**：修改了账号能力判定策略 (`chatgpt_account_state.py`)。当探测失败、API 超时或因代理故障导致本次任务无法提取出计划信息（返回 `"unknown"`）时，将不再覆写原有的订阅级别，而是自动回退并保留账号字典 `chatgpt_capabilities` 中最后一次成功记录的 `subscription_plan`。以此确保原本为 Plus/Team 级别的账号在遭受偶然断网时不会在前端被降级或错标为 Free。
- **修复本地状态批量同步及日志面板因导入不存在的代理解析方法导致异常崩溃无法工作的问题**：
  - **纠正代理解析函数引用与候选列表调用结构**：修复了在异步批量处理函数 `_run_batch_probe_local_status` (`api/tasks.py`) 中错误导入了未定义的 `build_account_action_proxy_candidates` 与 `format_proxy_candidate_label` 导致后台任务启动即发生 `ImportError` 崩溃的问题；将其修正为调用标准的 `resolve_probe_candidate_proxies` 代理解析函数。
  - **修复代理切换与日志面板汇报逻辑**：正确处理由候选列表解析出的 `(proxy_url, proxy_pool, source)` 元组数据结构，规范化对代理名称、网络失败自动切换 (Failover) 及向代理池 (`proxy_pool`) 汇报成功与失败的状态回调，从而确保本地状态批量同步的进度展示、代理重试与延时等候日志均能在日志面板 (`TaskLogPanel`) 中正确打印和更新。
- **修复 Codex 剩余用量列表中 5 小时与 7 日/30 日窗口剩余量混淆未区分的问题**：
  - **窗口严格分离与时长校验**：后端 Codex 用量构建逻辑（`codex_usage.py`）与前台数据归一化（`Accounts.tsx`）中增设窗口时长校验（<=360分钟为 5h 短期窗口，>360分钟为 7d/30d 长期窗口），彻底解决因缺失对应短/长窗口字段而盲目互用 fallback 导致 5h 与 7d/30d 用量互串和重复展示的问题。
  - **长期窗口动态自适应标签展示**：在账号池列表（`Accounts.tsx`）及 Codex 监控列表（`CodexUsagePage.tsx`）中，根据实际检测到的额度周期（如 `window_minutes >= 20000`），将长期窗口动态精准显示为 `30d` 或 `7d`（及对应前缀 `[30d]` / `[7d]`），避免将 30 天月期用量误标为 7 天。
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
- **Idea 批量提交引擎重构 (Single Sequential Submission)**：
  - 完全模拟人工提交模式，针对多额度卡密提交到部分上游（如 `submit.cccy.me`）时可能因批量并发引发的接口不兼容与丢单报错进行深度重构。
  - **前置动态查额度**：在为每一个账号发送提交请求前，强制进行一次 `/api/task/cdk/check`，只有查到当前真实额度 `remaining > 0` 才进行单一账号提交，避免将失效或已用完的卡密推给上游。
  - **单账号隔离提交**：废除批量发送 `accounts: [...]` 的结构，将每次发包严格控制为单一 Token。
  - **失败额度回收与重试**：提交完成后若遭遇失败（如网络抖动），当前排队账号将回滚队列。在外层循环中，重新查验该卡密，若上游未扣除额度，则再次利用该卡密重试，做到多额度卡密完全零浪费。
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
- **修复 Idea 批量提交按钮无反应的问题**：
  - 修复在 `Accounts.tsx` 中 `submitBaxiCdkSubmit` 方法内因 `validateFields()` 发生校验错误时抛出未捕获的 Promise Rejection 导致页面无任何反应和反馈的问题，现在会正确捕获并提示用户检查表单参数（尤其是被折叠隐藏的高级参数区域）。
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

### 修复 (Fixed)
- 修复 `idea批量提交` 功能点击后无反应（Log面板不弹起）的Bug：
  1. 后端 500 报错修复：旧版热更新后进程未重启，导致执行了包含已废弃 `_baxigpt_cdk_ineligible_reason` 方法的脏缓存，已通过硬重启 `auto-gpt` 容器清理旧版进程态。
  2. 修复网络超时挂起：`auto-gpt` 容器通过 `host-gateway` (172.17.0.1) 访问上游 `openai-pay-submit:8789` 时超时。原因是上游服务 `docker-compose.yml` 错误地仅绑定了 `127.0.0.1` 导致容器网桥无法通信。现已将上游端口绑定调整为 `172.17.0.1:8789:8789` 并重建容器。

## 2026-07-04 20:23:12 +0800
- 规范 auto-gpt 发布门禁
- 发布模式: multi

## 2026-07-04 20:27:04 +0800
- 修复手机号绑定粘贴号码自动入池与状态回写
- 发布模式: multi

## 2026-07-04 20:37:11 +0800
- 修复手机号绑定只测发码收码前端开关
- 发布模式: multi

## 2026-07-04 20:54:27 +0800
- 恢复账号列表分页切页设置
- 发布模式: multi

## 2026-07-05 01:21:50 +0800
- 修复手机号池完整API复制按钮
- 发布模式: multi

## 2026-07-05 03:34:35 +0800
- 恢复手机号绑定三策略号段选择器并压缩只测发码开关
- 发布模式: multi

## 2026-07-05 04:14:12 +0800
- 修复 Cliproxy 动态代理按国家探测误判
- 发布模式: multi

## 2026-07-05 04:20:12 +0800
- 修正动态代理 Socks5 运行态为 socks5h
- 发布模式: multi

## 2026-07-05 04:37:06 +0800
- 动态代理任务表单默认复用全局模板
- 发布模式: multi

## 2026-07-05 05:49:09 +0800
- 新增账号处理流水线串联Idea手机号OAIPay
- 发布模式: multi

## 2026-07-05 06:12:16 +0800
- 修复 OAIPay 上传密钥文案和 401 错误详情
- 发布模式: multi

## 2026-07-05 06:57:07 +0800
- 规整账号处理流水线配置页布局
- 发布模式: multi

## 2026-07-05 07:24:34 +0800
- 修复账号处理流水线 OAIPay 分组获取
- 发布模式: hot

## 2026-07-05 19:43:13 +0800
- 保留注册阶段 ChatGPT Web session 材料
- 发布模式: multi

## 2026-07-05 20:05:33 +0800
- 重构账号详情凭证展示并补齐 Web session 查看
- 发布模式: multi

## 2026-07-05 21:07:35 +0800
- 补齐 K12 workspace 注册与全空间保存
- 发布模式: multi

## 2026-07-05 21:17:43 +0800
- 加固账号详情脱敏并统一 K12 设置
- 发布模式: multi

## 2026-07-05 21:53:20 +0800
- 新增 auto-k12 独立实例并纳入多实例发布
- 发布模式: multi

## 2026-07-05 22:33:13 +0800
- 新增三实例共享配置中心
- 发布模式: multi

## 2026-07-05 22:41:21 +0800
- 完善共享配置推送保留全量本地配置
- 发布模式: multi

## 2026-07-05 22:44:45 +0800
- 修正共享配置差异按本地快照计算
- 发布模式: multi

## 2026-07-05 22:47:31 +0800
- 排除共享配置启动备份进入构建上下文
- 发布模式: multi

## 2026-07-05 22:55:06 +0800
- 修正共享配置差异忽略环境变量兜底
- 发布模式: multi

## 2026-07-05 23:10:58 +0800
- 默认关闭发布备份并清理历史备份
- 发布模式: multi

## 2026-07-06 00:05:14 +0800
- 新增邮箱验证码 API 注册登录能力
- 发布模式: multi
