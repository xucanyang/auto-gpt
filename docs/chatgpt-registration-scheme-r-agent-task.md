# ChatGPT 注册方案 R — Agent 改造任务提示词

> **文档类型**：可直接粘贴/引用给实现 Agent 的完整任务说明  
> **方案代号**：R（混合：any-auto 开户运输层 + auto-gpt 7/18–7/22 Web Session 落库合同 + Codex 外置）  
> **项目路径**：`/opt/auto-gpt`  
> **对照项目**：`/opt/any-auto-register`（只读参考，禁止在对照项目里做源码开发/部署）  
> **状态**：已落地（v2.8.31，方案 R）  
> **基线审核**：2026-07-25 对照 HEAD `8788470`（v2.8.30）、历史提交与运行代码；见 §1.0  
> **落地说明**：协议 create 对齐 any-auto + 三执行器隔离 + Web Session 齐套门闩；见 changelog v2.8.31  

---

## 0. 给执行 Agent 的一句话任务

在 `/opt/auto-gpt` 实现 **方案 R**：用 any-auto 已验证可过的 **create_account 注册前半段**（协议全程同 session，或浏览器整段 Camoufox）替换当前破碎/回退中的开户路径；注册成功与落库严格对齐 **7/18–7/22 本项目 Web Session 齐套模型**（AT + session_token + cookies + account_id + email/password）；**禁止**把 Codex OAuth / add_phone 作为注册成功前置。改完后按项目约定更新 `changelog.md`、跑相关测试，并执行 `deploy.sh` 完成多实例上线自检。

---

## 1.0 当前代码基线与文档勘误（实现前必读）

> 本节是对任务文档本身的代码对照审核结论。实现 Agent **以本节修正后的基线为准**，不要被过时表述带偏。

### 1.0.1 HEAD 真实状态（`8788470` / v2.8.30）

| 项 | 现状 | 对方案 R 的含义 |
|----|------|----------------|
| 统一入口 | `plugin.register` → `chatgpt_registration_mode_adapter` → AT-only / RT engine | 改引擎分派时两边都要看，默认产品常走 adapter |
| AT-only 主路径 | **始终** `ChatGPTClient.register_complete_flow()`（curl 协议状态机） | **没有** 三执行器硬隔离；`headed/headless` 标签 ≠ Camoufox 整段注册 |
| Session 落库 | create 成功后已有 `reuse_session_and_get_tokens()`，齐套才 success | **落库门闩大体已在**；不要从零重做「会话齐套」 |
| `max_retries` | 默认 **3**；`registration_disallowed` 在 `_should_retry` 白名单 | 同身份可 3 连整流程重放（与方案 R 冲突，要改） |
| Sentinel | `prefer_browser`：create/密码流优先 Playwright Sentinel，失败再 HTTP PoW | 协议模式仍会起浏览器 **仅拿 token**；`t` 在 PoW 路径 **固定 `""`** |
| `sentinel_vm.py` | 文件在树内 | **未接线**到当前 `sentinel_token.build_sentinel_token` |
| `browser_registration.py` | ~5.7k 行仍在，worker/sentinel_browser 仍引用 | **AT-only engine 已不再调用**（v2.8.30 删掉 `_run_browser_registration`）→ 半孤儿 |
| `api/tasks.py` | `executor_type in {headless,headed}` 仍打「浏览器注册链路已启动」并锁代理/身份槽 | **文案与真实 transport 不一致**（任务层当浏览器，引擎仍协议） |
| 密码生成 | `plugin.register` 仍 `random.choices(...k=16)` | v2.8.25 强密码合同在回退后 **可能丢失**；浏览器路径需补回 |
| 单测漂移 | `tests/test_sentinel_protocol_vm.py` 期望 VM 解 `t` | **当前会失败**（实测 `'' != 'turnstile-t-value'`） |
| 版本展示 | changelog / AppShell 为 v2.8.30 | 落地后递增 |

### 1.0.2 关键提交对照（勿整库 checkout）

| Commit | 说明 | 方案 R 怎么用 |
|--------|------|----------------|
| **`e453647`（v2.8.29）** | 协议对齐 any-auto：`sentinel_vm` + `sentinel_token` 解 `t` + `chatgpt_client` dump/signup continue/密码预热 + `oauth_client` 少量 | **优先 cherry-pick / 手工迁回 create 相关 diff**；**不含** 三执行器隔离与 engine 浏览器分派 |
| **`8788470`（v2.8.30）** | 为「会话齐套才落库」整段回退注册核心；**删掉** browser 分派与 `registered_auth_pending` 空壳 success；**一并撤掉** v2.8.29 的 create 对齐（`chatgpt_client`/`sentinel_token`/`oauth_client` 回退） | **保留其落库门闩意图**；**不要**再做一次「整文件回 7/18」把 create 对齐冲掉 |
| **`64d2e17`（v2.8.24）** 及 2.8.25–26 | 三执行器、整段 Camoufox、身份槽等 | **只取分派/隔离思想与 browser 接线**；勿整段恢复那一波已知来回横跳 |
| **`b880955`（7/18）** | commit message 写「7/18 b880955 保存模型」，但该 SHA **实际是支付链接筛选/防重**，**几乎不改注册状态机** | **不要** `git checkout b880955 -- services/chatgpt_core/...` 当注册基线。落库语义请以 **v2.8.30 当前 engine 的 `reuse_session_and_get_tokens` + 7/18–7/22 成功任务日志/库存字段** 为准 |
| 7/17–7/22 成功现场 | Plus/主库大批量：`headed` 标签 + 协议 create + HTTP PoW + JP + HME | 证明旧路径曾可量产；**7/23 同路径已崩**，不能当 create 银弹 |

### 1.0.3 文档表述勘误

| 原文易误解点 | 修正 |
|--------------|------|
| 「回退到 b880955 保存模型」 | 指 **语义**（会话材料齐套才 success），不是 b880955 树就是注册代码源 |
| 「session 落库要重新打通」 | HEAD **已有** `reuse_session_and_get_tokens` 步骤；缺口是 create 过不了 / 浏览器未分派 / 材料校验是否够硬 |
| 「恢复三执行器 = 恢复 2.8.24 整包」 | 否。2.8.24–26 含 OAuth recovery、pending 空壳等，与方案 R non-goal 冲突；**只恢复隔离 + Camoufox 整段 + 齐套落库** |
| any-auto 参考路径 | 协议：`platforms/chatgpt/register.py` + `sentinel_vm.py`；浏览器：`platforms/chatgpt/browser_register.py`（文件名是 `browser_register.py`） |
| 测试列表 | `test_browser_registration_flow.py` 仍在，但 engine 未调用 browser 时部分用例可能过时；`test_sentinel_protocol_vm.py` 与现网代码已不一致，对齐后应恢复通过 |

### 1.0.4 实现时优先缺口排序（基于基线）

1. **协议 create 完整度**（迁回/重做 `e453647` 能力，且协议模式不再依赖 Playwright 开户；Sentinel 仅 HTTP+VM）  
2. **执行器分派**：`headless/headed` 重新接到 `browser_registration`（或 any-auto 等价整段 Camoufox），与 tasks 文案一致  
3. **重试策略**：disallowed / create 失败同身份默认不 3 连  
4. **任务层 / 日志**：DEBUG 降级导致 create 400 不可见（`task_logging.py` 前缀表）——create 失败应用户可见  
5. **落库**：在现有 `reuse_session` 上加固门闩与字段映射；禁止重新引入 pending 当 success  
6. **密码合同**：补回强密码（若走密码注册页）  
7. **RT 模式**（`refresh_token` adapter / `refresh_token_registration_engine`）：与 AT-only **共享 create 运输层合同**，不要只改 AT-only 一条腿  

---

## 1. 背景（必须读懂再改）

### 1.1 已证实的事实

1. **邮箱本身通常没问题**：同邮箱真实浏览器可注册；OTP 收码在失败任务里也常成功。  
2. **卡点在 `POST /api/accounts/create_account` → `HTTP 400: registration_disallowed`**。  
3. **`/opt/any-auto-register` 的 ChatGPT 注册段能过 create**；其后续失败主要在 Codex/add_phone，与本项目注册目标无关。  
4. **本项目 7/10–7/22 曾大批量成功**（尤其 Plus：单日数百～1000），路径本质是：
   - 任务标签常为 `headed`
   - 主体是 `ChatGPTClient.register_complete_flow()` + `curl_cffi` 协议开户
   - Playwright 仅尝试 Sentinel，失败则 HTTP PoW（当时 `t` 可为空仍常过）
   - 成功后 `reuse_session_and_get_tokens` 拿 Web AT / session / cookies 再 `save_account`
5. **7/23 凌晨同一条旧路径被风控打穿**（同 JP+HME+协议 create，disallowed 从可消化变成几乎不可消化）。之后的 Auth Browser 嫁接、Camoufox 后备、三执行器来回回退，把问题形态弄复杂，但不是 7/23 早上断崖的唯一原因。  
6. **v2.8.29（`e453647`）** 曾把 any-auto 协议关键段（`sentinel_vm` / `client_auth_session_dump` / signup continue / 密码页预热）接进本项目；**v2.8.30（`8788470`）** 为恢复「会话齐套才落库」回退注册核心，**连带冲掉了 create 对齐**（`sentinel_vm.py` 文件仍在但未接线）。方案 R 要求：**落库语义保留当前 v2.8.30 的 reuse_session 门闩 + 7/18–7/22 库存字段，create 完整度迁回/对齐 any-auto，二者拆开做，禁止再整段互踩。** 详见 §1.0。

### 1.2 本项目注册成功要什么（库存合同）

以 Plus 历史 free / 仅 AT 可用账号为准，注册成功入库至少包含：

| 字段 | 要求 |
|------|------|
| `email` / `password` | 必有 |
| `token` / `access_token` | 必有（Web Access Token） |
| `session_token` | 必有 |
| `cookies` / `cookie_header` | 必有 |
| `account_id` | 必有 |
| `workspace_id` | 尽量有 |
| `id_token` | 可选 |
| `refresh_token` | **可选，不是注册成功门槛** |
| 指纹 / 代理源 / mailbox 状态 | 写入 `extra` 作审计 |

**明确不是注册成功条件**：Codex token、OAuth RT 完整解、手机号绑定、`add_phone` 完成。

### 1.3 any-auto 只借用什么

**借用**：

- 协议注册状态机完整度（signup continue、密码段、Sentinel VM 解 `t`、`client_auth_session_dump`、同 session create）
- 浏览器注册「整段 Camoufox、不与协议嫁接」的隔离模型
- create 成功后在同一 runtime 继续取 session 的思路

**不借用 / 不迁入注册主链**：

- Codex CLI 回调
- 独立 OAuth recovery 作为注册收尾
- add_phone 作为开户完成条件
- any-auto 自己的库存库表/产品壳

---

## 2. 目标架构（方案 R 硬合同）

### 2.1 三执行器硬隔离

| `executor_type` | 运输层 | 规则 |
|-----------------|--------|------|
| `protocol` | 全程 `curl_cffi` 同一 Session | 失败 **不得** 切 Camoufox/Playwright 开户兜底 |
| `headless` | 全程 Camoufox（无头） | 失败 **不得** 回灌协议 Session 再 create |
| `headed` | 全程 Camoufox（有头） | 同上；与 headless 仅差窗口 |

一次 attempt 内 transport 固定。禁止：

- 协议 OTP 后另起 Playwright Chromium Auth Browser 做 `create_account`
- 协议失败后同邮箱切 Camoufox 开户「后备」
- 浏览器 Cookie 半截回灌 curl 再 POST create

### 2.2 协议路径（优先做稳，对照 any-auto）

参考 `/opt/any-auto-register` 协议邮箱注册成功路径，在本项目 `ChatGPTClient` / 协议 Sentinel 中对齐：

1. 首页 / CSRF / signin / authorize  
2. 必要时 `authorize/continue`（signup）  
3. 密码页预热 + 密码阶段 Sentinel（含 turnstile，`t` 用 VM 解，禁止固定空串当唯一策略）  
4. `user/register` 设密码（若状态机需要）  
5. 邮箱 OTP  
6. **`client_auth_session_dump` 推进状态**  
7. 同一 session `POST /api/accounts/create_account`  
8. 跟随 callback / external_url  
9. **本项目** `reuse_session_and_get_tokens`（或等价）读取 Web session 材料  
10. 材料齐套才 `success` + `save_account`

Sentinel：

- 有 `dx` 时必须走 `sentinel_vm.solve_turnstile_dx`（或 any-auto 等价实现）填真实 `t`
- 协议模式 **禁止** 为拿 Sentinel 再起有头/无头浏览器开户事务
- 拿不到合格 token 应失败并打清日志，而不是空 token 静默硬撞（可配置降级需显式且默认关闭）

### 2.3 浏览器路径（headless/headed）

- 从邮箱入口到 about_you / create / session 读取 **同一 Camoufox context**
- 实现可移植/对齐 `/opt/any-auto-register` 的 `browser_register` 思路，或整理本项目 `browser_registration.py` 使其满足隔离合同
- 结束时在浏览器上下文读 `/api/auth/session`（或项目既有等价 API），映射到与协议路径 **同一套** `RegistrationResult` / Account 字段
- **禁止** 浏览器注册成功后再强制跑独立 Codex OAuth 才算成功

### 2.4 成功门闩（两模式共用）

```text
register_success =
  email + password
  + access_token
  + session_token
  + cookies（或可还原 cookies 的 cookie_header）
  + account_id
```

- 缺任一必填 → **不得** 标业务成功，不得当可用号上传  
- `registered_auth_pending` 若保留：只能表示「远端可能已注册但本地材料不齐」；**默认不算注册成功**，禁止 Sub2API/OAIPay/CPA 上传旁路  
- `refresh_token` / `id_token`：有则存，无则不影响 success  

### 2.5 重试与身份

- `registration_disallowed`：**默认同身份 create 不无限整流程重放**（建议同邮箱同出口最多 1 次 create；或极有限且换会话材料，禁止 3 连烧号）  
- protocol：允许代理 failover（换出口）时必须 **新身份/新会话**，不得假装同一浏览器身份  
- headless/headed：若保持「启动后不换代理防分叉」，必须保证真实 transport 就是 Camoufox，文案与实现一致  
- 任务日志：`请求模式` / `effective_transport` / create 400 原文 **不得** 仅埋 DEBUG；用户可见路径要能判断死在哪一步  

### 2.6 明确不做（Non-goals）

- 不在注册成功路径实现/修复 Codex CLI auth  
- 不在注册成功路径要求 add_phone  
- 不整仓替换为 any-auto  
- 不把 `/opt/auto-gpt-plus`、`/opt/auto-plus2` 当源码开发目录  
- 不恢复退役 `auto-k12`  
- 不为了「闭环」伪造 AT/session  

---

## 3. 建议改动范围（文件级指引）

以下为主要落点，执行时可按实际结构微调，但 **合同不能软化**。

### 3.1 注册引擎与平台入口

- `services/chatgpt_core/access_token_only_registration_engine.py`  
  - **现状**：无 browser 分派，一律 `register_complete_flow`；已有 `reuse_session_and_get_tokens`  
  - **要做**：三执行器分派；协议仍 `register_complete_flow`（增强后）；浏览器走整段 `browser_registration`  
  - 收敛 `max_retries` / disallowed 重试策略（默认勿 3 连 create）  
- `services/chatgpt_core/refresh_token_registration_engine.py`  
  - RT 模式引擎若仍走协议/浏览器，**共享同一 transport 合同**，避免只修 AT-only  
- `services/chatgpt_core/plugin.py`  
  - `executor_type` → `browser_mode` 传递正确  
  - 补回强密码生成（当前回退后为弱 `random.choices` 16 位）  
- `services/chatgpt_core/chatgpt_registration_mode_adapter.py`  
  - 仍负责 AT-only / RT 模式选择与 `build_account` 字段映射；成功门闩与 extra 字段在此对齐历史库存  
- `api/tasks.py`  
  - 「浏览器注册链路已启动」等日志必须与真实 transport 一致  
  - protocol 与 browser 的代理 failover / 身份槽语义分开，禁止误标  
- `services/chatgpt_core/task_logging.py`  
  - create 失败 / `registration_disallowed` / dump / Sentinel 关键结果 **不得** 仅因前缀进 DEBUG 而从任务 UI 消失 

### 3.2 协议开户（对齐 any-auto）

- `services/chatgpt_core/chatgpt_client.py`  
  - `register_complete_flow` / `create_account` / 密码与 OTP 状态机  
  - create 前 `client_auth_session_dump`  
  - 协议 create **只用** session.post，不调 Auth Browser finalize  
- `services/chatgpt_core/oauth_client.py`  
  - 若 about_you create 仍被注册路径触达，同样遵守协议/浏览器隔离  
- `services/chatgpt_core/sentinel_token.py`  
  - 接 VM 解 `t`；删除「协议 create 固定 `t=""` 作为唯一实现」  
- `services/chatgpt_core/sentinel_vm.py`  
  - 保留/修复；确保被协议路径真正调用（文件在但不接线等于没做）  
- 只读参考：`/opt/any-auto-register/platforms/chatgpt/register.py` 及 sentinel 相关实现  

### 3.3 浏览器开户

- `services/chatgpt_core/browser_registration.py`  
  - **现状**：大文件仍在，v2.8.30 后 engine 未调用；优先 **重新接线并收敛**，避免再复制一套  
  - 只读参考：`/opt/any-auto-register/platforms/chatgpt/browser_register.py`  
- `services/chatgpt_core/sentinel_browser.py` / `sentinel_browser_worker.py`  
  - 注册主链 **不要** 再走「协议 Cookie 导入 Chromium 再 fetch create_account」  
  - Playwright Auth Browser / `create_account_via_browser` 若保留，仅限非注册主链遗留旁路，且 **protocol 执行器默认禁止调用** 

### 3.4 落库与账号状态

- 注册结果组装处（engine → plugin → `save_account`）  
- `core/db.py` 的 `save_account` 行为保持：不因一次 pending 清空已有有效凭据  
- `services/chatgpt_account_state.py`：缺 AT 禁止上传 gate 保持  
- 撤销「空壳 registered_auth_pending 当 success 入库」的默认路径（若仍存在）  

### 3.5 可删/应禁用的历史行为

- 注册主链：`create_account_via_browser` 作为 protocol 的 finalize  
- 注册主链：protocol 失败 → Camoufox 后备开户  
- 注册主链：独立浏览器 OAuth recovery / Codex 收尾  
- 同身份对 `registration_disallowed` 默认 3 次整流程重放  

### 3.6 测试

至少覆盖并扩展：

- `tests/test_chatgpt_register.py`  
- `tests/test_access_token_only_checkout.py`  
- `tests/test_register_task_controls.py`  
- `tests/test_sentinel_protocol_vm.py`（**当前对 HEAD 失败**，对齐后必须转绿）  
- `tests/test_browser_registration_flow.py`（重新接线后修过时假设）  
- `tests/test_chatgpt_registration_mode_adapter.py`  
- 协议：有 dx 时 `t` 非空；create 前 dump 被调用；protocol 不启动 browser finalize  
- 浏览器：headless/headed 不先跑协议、engine 真正调用 browser 路径  
- 落库：材料不齐不算 success；齐套字段映射正确  
- 任务控制：browser 身份锁 vs protocol failover 语义 

### 3.7 文档与版本

- 更新 `changelog.md`：写入版本号（建议 **patch/minor** 如 `v2.8.31` 或按当前 HEAD 递增），分类清晰（Fixed/Changed）  
- 前端侧栏/版本展示与 changelog 同步（项目惯例）  
- 本文件可在完成后于文首将状态改为「已落地 + commit」  

### 3.8 允许改动范围 vs 禁止改动白名单

> **原则**：方案 R 只动「ChatGPT 邮箱注册开户运输层 + 成功门闩 + 任务层注册语义」。  
> 支付、手机号池、HME 服务端、同步上传、流水线、鉴权、多实例编排等 **默认冻结**。  
> 若实现中发现必须改冻结文件才能接线，**先停手说明原因**，不得顺手重构。

#### 3.8.1 允许改动（Allow-list，注册主链相关）

下列路径 **可以** 为方案 R 修改（含对应单测）。改动应服务合同，禁止借机大重构无关逻辑。

**后端 / 注册核心**

| 路径 | 允许原因 |
|------|----------|
| `services/chatgpt_core/access_token_only_registration_engine.py` | AT-only 主引擎分派 / 重试 / 落库门闩 |
| `services/chatgpt_core/refresh_token_registration_engine.py` | RT 模式共享 transport 合同（只做必要对齐） |
| `services/chatgpt_core/chatgpt_client.py` | 协议状态机、create、dump、Sentinel 接线 |
| `services/chatgpt_core/oauth_client.py` | **仅当**注册/登录恢复与 create Sentinel 合同共享时的最小改动；禁止扩成 Codex 注册收尾 |
| `services/chatgpt_core/sentinel_token.py` | VM 解 `t`、协议 PoW |
| `services/chatgpt_core/sentinel_vm.py` | 修复/保持 VM；勿删 |
| `services/chatgpt_core/browser_registration.py` | 整段 Camoufox 注册接线与收敛 |
| `services/chatgpt_core/sentinel_browser.py` | 去掉注册主链 Auth Browser finalize 嫁接；worker 调用边界 |
| `services/chatgpt_core/sentinel_browser_worker.py` | 同上 |
| `services/chatgpt_core/plugin.py` | executor 传递、密码合同、注册入口（勿改支付/绑号 action 语义） |
| `services/chatgpt_core/chatgpt_registration_mode_adapter.py` | build_account / 模式分派与成功字段 |
| `services/chatgpt_core/task_logging.py` | create 失败等关键日志可见性 |
| `services/chatgpt_core/constants.py` / `utils.py` | 仅注册状态机必要常量/小工具 |
| `api/tasks.py` | **仅** 注册任务：executor 文案、代理 failover、身份槽与真实 transport 一致；禁止顺手改支付/绑号/pipeline 任务 |
| `core/task_runtime.py` | 仅当 `consumes_target_slot` 等注册控制语义需要时的最小改动 |

**测试**

| 路径 | 允许原因 |
|------|----------|
| `tests/test_chatgpt_register.py` | 注册合同 |
| `tests/test_access_token_only_checkout.py` | AT-only / 落库 |
| `tests/test_register_task_controls.py` | 任务控制 |
| `tests/test_sentinel_protocol_vm.py` | VM `t` |
| `tests/test_browser_registration_flow.py` | Camoufox 流 |
| `tests/test_chatgpt_registration_mode_adapter.py` | 模式/字段 |
| `tests/test_sentinel_browser.py` | 若涉及 finalize 边界 |
| `tests/test_chatgpt_plugin.py` | 密码/入口（若已有） |
| 为方案 R **新增** 的 `tests/test_*.py` | 允许 |

**版本与说明（惯例必改）**

| 路径 | 允许原因 |
|------|----------|
| `changelog.md` | 强制 |
| `frontend/src/app/AppShell.tsx`（或现行展示版本号的组件） | **仅** 版本号字符串与 changelog 同步 |
| `docs/chatgpt-registration-scheme-r-agent-task.md` | 落地后更新状态 |

**灰色地带（默认可读；改动需极小且写进汇报「为何必须」）**

| 路径 | 条件 |
|------|------|
| `services/chatgpt_account_state.py` | 仅当成功门闩/禁止上传 gate 与注册结果字段必须对齐时 |
| `core/db.py` 的 `save_account` | 仅当不改会写坏库存或 pending 覆盖有效凭据时；禁止借机改过滤/支付字段逻辑 |
| `core/playwright_proxy.py` / `core/browser_runtime.py` | 仅浏览器注册代理适配 bug 阻断 Camoufox 时 |
| `services/chatgpt_core/account_fingerprint.py` / `mailbox_state.py` | 仅字段写入兼容 |
| `frontend` 注册页（`RegisterTaskPage` / `RegisterTaskModal` 等） | **仅当** executor 说明文案与三模式合同不一致且导致误用；禁止 UI 大改 |

#### 3.8.2 禁止改动白名单（Deny-list，默认冻结）

下列范围 **默认禁止修改**。不得为「顺便优化」触碰；不得 `git checkout` 旧提交覆盖这些路径。

**目录 / 项目边界**

| 禁止 | 原因 |
|------|------|
| `/opt/any-auto-register/**` 写入 | 只读对照 |
| `/opt/auto-gpt-plus/**`、`/opt/auto-plus2/**` 源码式开发 | 仅数据/配置隔离实例 |
| `/opt/auto-k12/**` 重新接入 | 已退役 |
| `shared_config/`、各实例 `data/**`、`*.db*`、WAL、密钥 `.env` 提交进 Git | AGENTS 红线 |

**支付 / 订阅 / 长链**

| 禁止（示例，含同职责文件） | 原因 |
|---------------------------|------|
| `services/chatgpt_core/payment.py`、`payment_link_cache.py`、`pix_payment_link_cleanup.py` | 支付链路 |
| `services/chatgpt_core/long_link_*.py`、`services/long_link_history_sync.py` | Team/优惠长链 |
| `services/chatgpt_core/gopay_*.py`、`paypal_*.py` | GoPay/PayPal |
| `services/chatgpt_core/oaipay_*.py`、`services/oaipay_sync.py`、`services/idea_oaipay_pipeline/**` | OAIPay |
| `services/sub2api_sync.py`、`services/cliproxyapi_sync.py`、`services/cpa_manager.py` | 上传同步（除非只读调用既有 gate） |
| `api/idea_oaipay_pipeline.py`、支付相关 accounts 筛选大改 | 非注册 |

**手机号 / 绑号 / 号池**

| 禁止 | 原因 |
|------|------|
| `services/chatgpt_core/phone_*.py`、`bound_phone.py`、`gopay_phone.py` | 绑号与号池 |
| `services/phone_api_relay.py`、`api/phone_pool.py` | Relay/号池 API |
| `services/chatgpt_core/phone_registration_engine.py` | **手机号注册入口**；方案 R 范围是邮箱注册。除非证明改邮箱 transport 误伤且最小修复，否则不动 |

**HME / 邮箱基础设施（服务侧）**

| 禁止 | 原因 |
|------|------|
| `services/icloud_hme_*.py`、`api/icloud_hme.py` | HME 控制面/自动池；注册侧只 **调用** 现有 mailbox 接口 |
| `core/applemail_pool.py`、`core/luckmail/**` | 邮箱池实现 |
| 外置 iCloud Helper 服务代码（若在其他目录） | 非本仓注册状态机 |

**流水线 / 投递 / 外部同步**

| 禁止 | 原因 |
|------|------|
| `services/pipeline/**`、`api/pipeline.py` | 业务流水线 |
| `services/delivery_cards/**`、`api/delivery_cards.py` | 卡密投递 |
| `services/external_*`、`api/external_*.py`、`api/contribution.py` | 外部同步 |
| `services/chatgpt_sync.py`、`local_status_refresh.py` 等订阅刷新主逻辑 | 非开户 |

**鉴权 / 多实例 / 编排 / 前端无关页**

| 禁止 | 原因 |
|------|------|
| `api/auth.py`、admin 会话相关 | 管理员鉴权 |
| `docker-compose*.yml`、`Dockerfile`、`deploy.sh` **逻辑大改** | 可用现有 `deploy.sh --mode=multi`；禁止改编排拓扑 |
| `core/config_store.py` / 共享配置 schema 大改 | 易污染三实例 |
| 前端 Accounts/支付/Team/Settings 等非注册页的功能改动 | 范围外 |
| `services/chatgpt_core/codex_usage.py`、Codex OAuth recovery 专用模块 | 方案 R non-goal |
| `services/chatgpt_core/subscription_auth_capture.py` 扩成注册收尾 | 同上 |

**数据与运维**

| 禁止 | 原因 |
|------|------|
| 直接改生产 `account_manager.db`「修数据」当代码方案 | 应用代码修复 |
| 无备份的批量 SQL 删号/改 token | 事故面 |

#### 3.8.3 冲突裁决

1. **Allow 与 Deny 重叠时（如 `api/tasks.py`）**：只允许改注册任务相关函数/分支；用最小 diff；禁止同文件顺手改支付任务。  
2. **改 Deny 才能编译/接线**：在汇报中单列「突破白名单：路径 + 原因 + diff 行数」；能 revert 的旁路优先。  
3. **禁止** 用「对齐 any-auto」为借口复制支付/Codex/手机号整目录进本项目。  
4. `git diff --stat` 结束时应 **几乎只有 Allow-list + changelog + 版本号 + 相关 tests**；若出现大段 Deny 路径，视为未完成方案 R 合同。

#### 3.8.4 提交前自检命令（建议）

```bash
cd /opt/auto-gpt
git status
git diff --stat HEAD
# 人工确认 changed files 均在 §3.8.1；若命中 §3.8.2，停下说明
```

---

## 4. 实现约束（项目强制）

遵循 `/opt/auto-gpt/AGENTS.md`：

1. **只改** `/opt/auto-gpt` 源码主线。  
2. 改完必须：
   - 更新 `changelog.md`
   - 执行  
     ```bash
     /opt/auto-gpt/deploy.sh "<清晰 commit 说明>" --mode=multi
     ```
     除非用户当次明确禁止部署；本任务默认 **要部署**。  
3. 热补丁仅适用于极小 Python 修复；本次属注册主链路，**默认 multi 镜像重建**。  
4. 部署后确认三常驻实例运行：`auto-gpt` / `auto-gpt-plus` / `auto-plus2`。  
5. 不要把 `shared_config/`、实例 data、WAL 提交进 Git。  
6. 汇报用中文：做了什么、运行态、验证结果、剩余风险。  

---

## 5. 验收标准（Done Definition）

### 5.1 代码合同

- [ ] `protocol` 注册主链无 Auth Browser / Camoufox 开户嫁接  
- [ ] `headless`/`headed` 注册主链不先跑协议 create  
- [ ] 协议 Sentinel 在存在 turnstile `dx` 时能解出非空 `t`（有单测）  
- [ ] create 前存在 `client_auth_session_dump`（或 any-auto 等价状态推进，有单测/日志点）  
- [ ] 注册成功门闩要求 AT + session_token + cookies + account_id  
- [ ] 注册成功路径无 Codex OAuth / add_phone 前置  
- [ ] `registration_disallowed` 不再默认同身份 3 次整流程硬撞  
- [ ] `git diff --stat` 落在 §3.8.1 Allow-list；无未说明的 §3.8.2 Deny 路径  

### 5.2 自动化测试

- [ ] 相关 pytest 专项通过（至少注册 / Sentinel VM / 任务控制相关）  
- [ ] 若改前端版本号：前端 build 或既有 CI 步骤按项目惯例处理  

### 5.3 运行态

- [ ] `deploy.sh --mode=multi` 成功  
- [ ] 三实例 `docker ps` 均 Up  
- [ ] `curl -fsS` 访问 `8000`/`8001`/`8003` 首页 OK  

### 5.4 现场烟测（尽量做，结果写入汇报）

在可控代理与邮箱源下各跑 **少量** 任务（建议各 1 次，避免同身份连撞）：

1. **protocol** + 与 any-auto 相近的出口/邮箱策略  
   - 日志应出现协议完整链路关键点（continue / 密码 Sentinel / dump / create）  
   - 目标：create 200 或明确可诊断错误（不再静默 DEBUG）  
   - 成功则库中出现齐套 Web Session 字段  
2. **headless 或 headed** 一条  
   - 日志 transport=camoufox（或等价），无「协议半截 + 浏览器 finalize」  
   - 成功字段与 protocol 成功样本同构  

若现场仍 disallowed：在汇报中区分 **风控/代理/邮箱** vs **实现未对齐**，不要把外部风控报成「已完成方案 R」。

---

## 6. 推荐实现顺序（单任务内连续做完，不分多 PR）

不必拆成多个用户阶段；Agent 在一次任务内按下列顺序连续完成即可：

1. **确认基线**：阅读 §1.0；跑一次 `pytest tests/test_sentinel_protocol_vm.py` 确认「VM 未接线」失败形态。  
2. **协议 create 对齐 any-auto**：以 `e453647` diff 为主要补丁源迁回 `sentinel_token` / `chatgpt_client`（及必要的 `oauth_client`）能力；协议路径 **关闭** create 用的 `prefer_browser` Playwright Sentinel（或仅允许 PoW+VM）；保证 `sentinel_vm` 被调用。  
3. **冻结合同 + 分派**：engine（及 RT engine）按 executor 分派；成功门闩在现有 `reuse_session_and_get_tokens` 上加固，**禁止** 再引入 pending 空壳 success。  
4. **浏览器整段路径**：把现存 `browser_registration.py` 接回 headless/headed；对照 any-auto `browser_register.py` 补缺口；同一 RegistrationResult 字段。  
5. **任务层 / 日志 / 重试 / 密码**：tasks 文案与 transport 一致；create 400 用户可见；disallowed 默认同身份不 3 连；强密码。  
6. **测试转绿 + changelog + 版本号 + deploy.sh multi + 烟测汇报**。  

参考提交（只读，**禁止** `git checkout <old> -- .` 整树覆盖）：

- 协议 create 补丁源：`e453647`（v2.8.29）  
- 落库门闩现状：`8788470`（v2.8.30）当前 tree 的 engine `reuse_session` 段 + Plus 7/18–7/22 账号 `extra_json`  
- 三执行器/Camoufox 接线思想：`64d2e17` 等，**选择性**合并，去掉 Codex recovery / pending success  
- any-auto 源码：`/opt/any-auto-register/platforms/chatgpt/{register,browser_register,sentinel_vm}.py`  
- **不要**把 `b880955` 当作注册代码源（该提交是支付筛选） 

---

## 7. 风险与注意

1. **7/23 后外部风控可能仍紧**：实现正确 ≠ 100% 成功率；验收看合同与可诊断性，成功率对照 any-auto 同环境。  
2. **不要「为对齐而整文件覆盖」** 导致支付/手机号/HME/任务控制回归；严格遵守 **§3.8 允许/禁止白名单**。  
3. **`sentinel_vm.py` 在树里不等于已接线**——必须以调用链与测试为准。  
4. **Plus/主库库存字段** 以历史成功账号 `extra_json` 为准，不要发明另一套 key 名而不做兼容。  
5. 若必须保留「已注册但缺 AT」状态：独立状态机 + 禁止上传，且默认 UI/任务计数不当成功。  
6. **`git diff --stat` 出现支付/号池/HME 服务/pipeline/Codex 大文件** = 范围失控，先收回再继续。 

---

## 8. 汇报模板（任务结束时使用）

```text
## 方案 R 落地汇报

### 做了什么
- …

### 关键合同是否满足
- 三执行器隔离：是/否
- 协议 create 对齐 any-auto：是/否
- 成功门闩（AT+session+cookies+account_id）：是/否
- 注册路径无 Codex/add_phone 前置：是/否
- 白名单（§3.8）：diff 是否仅 Allow-list；若有突破写明路径与原因

### 测试
- pytest：…

### 部署
- commit：…
- 版本：v…
- auto-gpt / auto-gpt-plus / auto-plus2：均 Up / 异常

### 烟测
- protocol：…
- browser：…

### 剩余风险
- …
```

---

## 9. 用户可直接复制的短启动指令

把下面整段作为新会话第一条用户消息即可：

```text
请严格按 /opt/auto-gpt/docs/chatgpt-registration-scheme-r-agent-task.md 执行「方案 R」改造。

硬要求：
1. create/开户对齐 any-auto（协议同 session + VM 解 t + dump；浏览器整段 Camoufox），禁止协议与浏览器嫁接开户。
2. 注册成功与落库对齐本项目 7/18–7/22 Web Session 齐套合同（AT+session_token+cookies+account_id+email/password）；RT/Codex/add_phone 不是注册成功条件。
3. 不要在注册成功路径修 Codex auth。
4. 严格遵守文档 §3.8 允许改动 / 禁止改动白名单；禁止顺手改支付、手机号池、HME 服务、pipeline、Codex。
5. 改完更新 changelog、跑相关测试，并执行 deploy.sh --mode=multi；确认三常驻实例均在跑。
6. 汇报用文档第 8 节模板（含白名单自检）。

只改 /opt/auto-gpt。any-auto 仅只读参考。先读 §1.0 基线与 §3.8 白名单再改代码。
```

---

## 10. 文档维护

| 项 | 值 |
|----|-----|
| 路径 | `docs/chatgpt-registration-scheme-r-agent-task.md` |
| 受众 | 实现 Agent / 人工 reviewer |
| 关联结论来源 | 2026-07-25 会话：registration_disallowed 对比、7/20 前历史日志、方案 R 评审 |
| 落地后 | 更新文首状态为已完成，并注明 commit / 版本号 |
