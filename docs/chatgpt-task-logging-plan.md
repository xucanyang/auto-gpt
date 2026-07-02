# ChatGPT 任务日志全面整改方案（只改日志，不重构流程）

> 阶段 1 方案设计文档。本文只分析和设计日志整改，不要求改注册、登录、OAuth、手机号、邮箱、状态探测等业务流程。

## 0. 背景和范围

当前项目里注册、手机号注册、手机号绑定、补抓 Auth、邮箱测活、失效测活、本地 token 测活/状态探测，底层大量复用 ChatGPT/OAuth/邮箱/手机号能力，但任务壳、日志、`task_logs.detail_json` 和账号 `extra` 记录各有一套。主线程已有结论：**不应现在重构流程**，但日志已经需要先统一，否则排障时会继续把“注册 OTP”“OAuth 登录 OTP”“手机号 add_phone”“手机号 signup”“本地 token probe”混成一类问题。

本方案覆盖：

- 注册：`api/tasks.py::_run_register`、`services/chatgpt_core/plugin.py::ChatGPTPlatform.register`、`services/chatgpt_core/chatgpt_registration_mode_adapter.py`、`services/chatgpt_core/refresh_token_registration_engine.py`
- 手机号注册：`services/chatgpt_core/phone_registration_engine.py`、`services/chatgpt_core/phone_signup_client.py`
- 手机号绑定：`api/tasks.py::_run_phone_binding_test`、`api/actions.py::_execute_chatgpt_resume_subscription_auth`、`services/chatgpt_core/oauth_client.py::_handle_add_phone_verification`
- 补抓 Auth：`api/tasks.py::_run_resume_subscription_auth` / `_run_batch_resume_subscription_auth`、`services/chatgpt_core/subscription_auth_capture.py`
- 邮箱测活：`api/tasks.py::_run_custom_email_recheck` / `_run_batch_custom_email_recheck`、`services/chatgpt_core/custom_email_recheck.py`
- 失效测活：`api/tasks.py::_run_invalid_recheck`、`services/chatgpt_core/invalid_account_recheck.py`
- 本地 token 测活/状态探测：`services/chatgpt_core/status_probe.py`、`services/chatgpt_core/local_status_refresh.py`、`services/chatgpt_core/plugin.py::execute_action("probe_local_status")`、`api/chatgpt.py::probe_local_status`

非范围：不调整任务编排、不合并流程、不改变手机号池复用策略、不改变账号状态机、不改 UI 结构、不新增数据库列。只允许后续执行阶段改日志输出、日志 detail/meta、脱敏函数和测试。

## 1. 现状问题

### 1.1 任务日志有两个层面，但没有统一事件模型

当前实时日志由 `api/tasks.py::_log(task_id, msg, level="info")` 追加到内存任务快照并 `print` 到容器 stdout。它只约定了 `level=debug` 时加 `[DEBUG]` 前缀，最终格式是：

```text
[HH:MM:SS] message
```

历史日志由 `api/tasks.py::_save_task_log(...)` 写入 `task_logs`，一条 `task_id` 只保留/更新一条记录，详情来自 `_build_task_log_detail(...)`，包含：

- `task_id/status_snapshot/progress/success/skipped/errors/cashier_urls/source/meta/logs`
- 各任务额外传入的 `attempt_outcome/email/account_id/result/runtime_results/...`

数据库模型 `core/db.py::TaskLog` 只有 `task_id/platform/email/status/error/detail_json/created_at`，`core/db.py::_ensure_task_log_schema()` 只保证 `task_id` 列和索引。

问题：实时日志是自由文本，历史 detail 是自由 JSON；没有统一 `flow/stage/event/error_code/sensitive-safe summary`。这导致后续任务历史页只能靠 `attempt_outcome` 和 `source` 粗略判断。

### 1.2 日志前缀和阶段命名分裂

同一个底层 OAuth 登录，在不同链路里有不同叫法：

- 注册引擎：`[主链路]`、`[阶段]`、`[注册]`、`[登录链路]`
- 补抓 Auth：`[补抓]`
- 邮箱测活：`[邮箱测活] 第一阶段`
- 失效测活：`[失效测活]`
- 手机号绑定：`[手机号绑定]`
- 手机号注册：`[手机号注册]`、`[手机号注册链路]`
- 手机号服务：`[号码测试]`、`[接码网关]`
- OAuth 内部：`步骤1/2/3`、`状态步进[...]`、`获取到 authorization code...`

`services/chatgpt_core/refresh_token_registration_engine.py::_classify_log_level` 已经做了部分 info/debug 降噪，但它只覆盖 refresh-token 注册引擎里的消息，对手机号注册、手机号绑定、custom/invalid recheck、status probe 不统一。

### 1.3 敏感信息存在明显泄露风险

已经有局部脱敏：

- `api/tasks.py::_redact_proxy_for_task_log()` / `_run_register` 内部 `_redact_proxy_for_log()` 会把代理账号密码替换为 `***:***`。
- `services/chatgpt_core/phone_signup_client.py::redact_text()` 会处理部分 token/password/session/cookie/code query。
- `RegistrationResult.to_dict()` 会截断 token。

但全局还不够：

- `api/tasks.py::enqueue_phone_binding_test_task` 的 `meta.settings.proxy`、`meta.proxy.specified` 当前记录原始代理；同一个文件里 custom email proxy meta 已脱敏，但 phone-binding meta 没脱敏。
- `api/tasks.py::_build_task_log_detail()` 会把内存 `logs` 原样写入 `detail_json`，任何实时日志泄露都会永久进入 DB。
- `services/chatgpt_core/phone_service.py::UploadedPhoneService.wait_for_code()` 直接记录 `[号码测试] {phone} 收到验证码 {code}`，并记录忽略旧验证码 `{code}`。
- `services/chatgpt_core/oauth_client.py` 多处记录 OTP/code：`准备提交绑定手机号验证码: {code}`、`准备提交人工手机号验证码: {code}`、`尝试 OTP: {code}`、`获取到 authorization code: {code[:20]}...`。
- `services/chatgpt_core/oauth_client.py::adopt_browser_context`、`login_and_get_tokens` 相关日志会输出完整 `device_id`；这不是 token，但仍属于可关联指纹，建议降到 debug 并短显。
- 手机号、API URL、`raw_line` 目前常完整进入日志/detail。手机号是否完全敏感取决于运营需求；验证码、token、密码、cookie、完整代理认证、完整收码 API URL 一定要脱敏。

### 1.4 task meta/detail 记录过大且安全边界不清

`_build_task_log_detail()` 默认把 snapshot `meta` 和 `logs` 整体写入 `detail_json`。手机号绑定尤其重：

- `enqueue_phone_binding_test_task()` 的 `meta.phone_items` 包含完整手机号和 API URL，`settings.proxy` / `proxy.specified` 可能是原始代理。
- `_run_phone_binding_test()` 最终 detail 写入 `bound_phone_lines`、`bound_phone_results`、`runtime_results`、`account_results`，其中 `raw_line` 常是 `phone----api_url`。
- 手机号注册 `phone_registration_engine.py::_panel_result()` 返回 `api_url/raw_line/phone`，注册保存时进入账号 extra 和 task meta。
- 邮箱测活、失效测活、补抓 Auth 的 result payload 可能包含 `mailbox_state`。这个字段是业务需要，但不适合完整进入任务历史 summary；如果写入 detail，需要确保没有邮箱服务 token、原始邮件正文、验证码。

### 1.5 错误分类各自实现，日志没有稳定错误码

已有分类函数：

- `services/chatgpt_core/subscription_auth_capture.py::_classify_capture_error()`
- `services/chatgpt_core/invalid_account_recheck.py::_classify_recheck_error()`
- `services/chatgpt_core/custom_email_recheck.py::_classify_custom_recheck_error()`
- `api/tasks.py::_phone_binding_error_status()`
- `services/chatgpt_core/phone_registration_engine.py::_error_status()`

这些分类值不完全一致，例如 `temporary_auth_error/network_failed/browser_error/account_auth_error/login_blocked/add_phone_required/phone_verification_failed/api_no_code/openai_rejected`。这次不合并分类函数，但日志和 detail 应统一记录：`error_code`、`retryable`、`error_phase`、`raw_error_redacted`。

### 1.6 本地状态探测几乎没有任务级日志

`services/chatgpt_core/status_probe.py::probe_local_chatgpt_status()` 返回结构化结果，包含 `auth/subscription/codex`，但本身无 `log_fn`。调用点：

- `services/chatgpt_core/plugin.py::execute_action("probe_local_status")`
- `api/chatgpt.py::probe_local_status()` / `check_subscription()`
- `services/chatgpt_core/local_status_refresh.py::sync_chatgpt_account_local_status()` / `schedule_chatgpt_local_status_refresh_for_account_id()`

手动 action 只返回 message，后台 refresh 失败只 `logger.warning`，成功没有可检索的任务历史。后续日志整改应至少让调用方记录 compact summary，而不是让 `status_probe` 打散日志。

## 2. 统一日志规范

### 2.1 日志目标

日志分三类，边界必须清楚：

1. **实时任务日志**：给操作者看当前卡在哪一步，默认 info 简洁，debug 可展开。
2. **任务历史 detail/meta**：给事后排障和批量统计，必须结构化、可过滤、可脱敏保存。
3. **账号 extra 业务状态**：保存业务事实，例如 Auth 捕获结果、手机号绑定结果、本地状态探测结果；不等于任务日志，不要塞大量原始日志。

### 2.2 统一文本格式

实时日志建议统一为：

```text
[FLOW][STAGE] message key=value key=value
```

保留现有 `_log()` 时间戳，不再手写时间。`FLOW` 和 `STAGE` 用固定小词典，中文前缀可保留，但建议中英混合稳定：

| 任务 | FLOW |
|---|---|
| 普通注册 / RT 注册 / AT-only 注册 | `注册` |
| 手机号注册 | `手机号注册` |
| 手机号绑定 | `手机号绑定` |
| 补抓 Auth | `补抓Auth` |
| 邮箱测活 | `邮箱测活` |
| 失效测活 | `失效测活` |
| 本地状态探测 | `本地状态` |
| 公共 OAuth 子链路 | `OAuth` 或 `登录链路` |
| 公共邮箱 OTP | `邮箱OTP` |
| 公共手机号 OTP | `手机OTP` |
| 代理选择 | `代理` |
| 接码服务 | `接码` |

推荐 stage：

```text
prepare
proxy_select
homepage_probe
mailbox_prepare
register_submit
email_otp_send
email_otp_wait
email_otp_submit
phone_acquire
phone_send
phone_wait
phone_submit
oauth_bootstrap
oauth_login
oauth_workspace
token_exchange
access_token_checkpoint
full_auth_capture
persist
local_status_probe
retry
summary
```

示例：

```text
[注册][prepare] attempt=1/5 target_success=3 mode=refresh_token source=manual
[代理][proxy_select] attempt=1 candidate=2/5 source=pool country=US score=91 proxy=http://***:***@1.2.3.4:8000
[邮箱OTP][wait] purpose=registration_subject provider=icloud_hme timeout=600s before_ids=3
[注册][access_token_checkpoint] ok=true account_id=acc_*** saved_account_id=123 auth_level=access_token_only
[补抓Auth][full_auth_capture] ok=false error_code=missing_refresh_token retryable=true attempts=2
```

### 2.3 level 规范

沿用 `_log(level="debug")` 机制，但后续执行阶段应建立一个小 helper，不让每个模块自己判断。建议：

- `info`：阶段开始/结束、用户需要决策的信息、成功/失败摘要、重试摘要、手机号/邮箱/代理选择摘要。
- `warning`：可恢复但影响结果，例如代理失败切下一个、Auth 抓取失败但手机号已绑定、OTP 冷却、号码被 OpenAI 拒绝。
- `error`：当前账号/当前任务终止，或者最终失败。
- `debug`：HTTP 状态推进、redirect、FlowState、CSRF/device_id/sentinel、workspace 候选、原始响应摘要、重复轮询信息。

现状里 `refresh_token_registration_engine.py::_classify_log_level()` 已把很多底层 OAuth 状态降到 debug。执行阶段应该把这个思路扩到所有 ChatGPT 任务日志入口，而不是只靠该引擎。

### 2.4 task detail/meta 统一结构

不新增 DB 列，在 `detail_json` 内增加统一结构即可。建议所有这批任务最终都写：

```json
{
  "schema_version": 2,
  "task_id": "task_...",
  "source": "phone_binding_test",
  "flow": "phone_binding",
  "attempt_outcome": "phone_binding_test_failed",
  "summary": {
    "total": 10,
    "success": 3,
    "skipped": 2,
    "failed": 5,
    "started_at": "...",
    "finished_at": "..."
  },
  "current": {
    "attempt_index": 3,
    "account_id": 123,
    "email": "a@example.com",
    "phone_masked": "+1555***1234",
    "stage": "phone_wait"
  },
  "policy": {
    "registration_mode": "refresh_token",
    "allow_add_phone_verification": false,
    "allow_existing_phone_verification": true,
    "phone_sms_probe_only": false,
    "proxy_mode": "pool"
  },
  "stage_results": [
    {
      "stage": "email_otp_wait",
      "ok": true,
      "duration_ms": 12345,
      "provider": "icloud_hme"
    }
  ],
  "error": {
    "code": "email_otp_timeout",
    "phase": "email_otp_wait",
    "retryable": true,
    "message": "未收到邮箱验证码"
  },
  "artifacts": {
    "has_access_token": true,
    "has_refresh_token": false,
    "auth_level": "access_token_only",
    "workspace_id": "..."
  },
  "redaction_version": 1
}
```

兼容要求：保留现有字段 `source/attempt_outcome/meta/logs/result/runtime_results/account_results`，前端现有读取不能断。新增字段只是补充，不要删除旧字段，除非是明显敏感原文。

### 2.5 task meta 应记录什么

实时 `meta` 用于 UI 展示和任务恢复感知，建议只放“状态摘要”和“可展示结果”，不要放 secrets。

建议记录：

- `flow/source/current_stage/current_email/current_account_id/current_index`
- `policy`：注册模式、手机号策略、代理模式、timeout、重试次数、是否短信探测。
- `proxy_summary`：mode/country/failover/max_candidates/min_score/specified_redacted。
- `phone_summary`：phone_count、prefix summary、selected_prefixes、use_pool、reuse_phone_until_unusable。
- `runtime_results`：仅放脱敏后的手机号、状态、原因摘要、是否收码、是否提交 OTP、是否 auth_capture_ok。
- `account_results`：account_id/email/status/error_code/retryable/reason_redacted。

不应记录：

- `password`
- access/refresh/id/session token
- cookies
- OTP/验证码
- authorization code
- 完整代理认证
- 完整收码 API URL 中的 token/key/query
- 原始邮箱正文
- 原始 OAuth session/cookie payload

## 3. 脱敏规则

### 3.1 建议新增统一脱敏 helper

执行阶段建议新增或放入现有工具模块，例如：

- `services/chatgpt_core/logging_utils.py`
- 或 `core/logging_utils.py`

提供：

```python
redact_log_text(text: Any) -> str
redact_proxy_url(proxy: Any) -> str
mask_phone(phone: Any) -> str
redact_url(url: Any, *, keep_host=True) -> str
sanitize_task_detail(value: Any) -> Any
sanitize_error_message(message: Any) -> str
```

不要每个任务继续复制 `_redact_proxy_for_task_log()`。

### 3.2 字段级脱敏

| 类型 | 规则 |
|---|---|
| `access_token/refresh_token/id_token/session_token/cookies/cookie/authorization` | 只允许布尔 `has_xxx` 或前后各 4 位摘要；默认 `<redacted>` |
| `password` | 永远 `<redacted>`，detail/meta 不保存 |
| 邮箱 OTP / 手机 OTP / authorization code | 实时日志和 detail 都不保存明文；可记录 `otp_received=true`、`otp_length=6`、`code_time` |
| 手机号 | UI 结果可按业务需要保存完整手机号；普通日志建议 `+国家码****后4`。对 `raw_line` 一律不直接输出 |
| 收码 API URL | 只保存 `scheme://host/path`，query 全删或 `?token=<redacted>`；`raw_line` 拆成 `phone_masked + api_host` |
| 代理 URL | `scheme://***:***@host:port`；无认证时可保留 host:port；query 全删 |
| device_id / oai-session-id / csrf / sentinel / state | debug 里最多短显前后 4-6 位，默认不进 detail |
| mailbox_state | 允许 provider/email/account_id/before_ids_count；不保存 provider token、原始 message、验证码、cookies |
| 原始响应 body | 默认不进 info；debug 也先过 `redact_log_text`，最长 300-500 字符 |

### 3.3 必须修的已知泄露点

1. `services/chatgpt_core/phone_service.py::UploadedPhoneService.wait_for_code()`：
   - 当前记录验证码明文：`收到验证码 {code}`、`忽略旧验证码 {code}`。
   - 改为：`收到验证码 length=6 extracted=true code_time=...`，旧验证码同理只记录 `length` 和 `code_time`。

2. `services/chatgpt_core/oauth_client.py`：
   - `准备提交绑定手机号验证码: {code}`、`准备提交人工手机号验证码: {code}`、`尝试 OTP: {code}` 改成 `otp_received=true length=6`。
   - `获取到 authorization code: {code[:20]}...` 改成 `authorization_code_received=true code_len=...`。
   - `add_phone 触发响应体(raw/json)`、`add_phone 状态响应体(raw)` 需要统一 `redact_log_text` 后再截断，最好降 debug。

3. `api/tasks.py::enqueue_phone_binding_test_task()`：
   - `meta.settings.proxy` 和 `meta.proxy.specified` 当前可能是原始代理。改成 `proxy_redacted` / `specified_redacted`；如必须保留原值给运行时，运行参数和 task meta 分开，不把原值写入 `_task_store.meta`。

4. `api/tasks.py::_run_phone_binding_test()`：
   - `[RESULT_RAW] {raw_line}` 当前会输出 `phone----api_url`。改成 `[RESULT_RAW] phone=<masked/full按业务> api=<redacted_url>`，或新增 `export_raw_available=true` 但不进日志。

5. `services/chatgpt_core/phone_registration_engine.py::_panel_result()`：
   - 返回 `api_url/raw_line` 会进入任务 meta/detail 和账号 extra。账号 extra 业务需要可保留，但任务日志 detail 应使用 sanitized copy。

6. `api/tasks.py::_build_task_log_detail()`：
   - detail 写入前应对 `logs/meta/errors/error/result/runtime_results/account_results` 做递归脱敏，避免遗漏。

## 4. 各任务具体日志点

### 4.1 注册（普通邮箱注册 / RT 注册 / AT-only 注册）

关键路径：

- `api/tasks.py::enqueue_register_task()` 创建 task log，已有 `extra_flags`。
- `api/tasks.py::_run_register()` 控制尝试、代理、保存账号、写最终 task log。
- `services/chatgpt_core/plugin.py::ChatGPTPlatform.register()` 分发普通注册和手机号注册。
- `services/chatgpt_core/chatgpt_registration_mode_adapter.py::RefreshTokenChatGPTRegistrationAdapter._run_two_stage_registration()` 处理 AT checkpoint -> full Auth。
- `services/chatgpt_core/refresh_token_registration_engine.py::RefreshTokenRegistrationEngine.run()` 执行主注册/OAuth 链路。

现状：

- `_run_register()` 有 `[账号]`、`[代理]`、`[OK]`、`[FAIL]`、`[SKIP_SAVE]`，但阶段语义不稳定。
- `plugin.py::register()` 会记录 `ChatGPT 注册核心链路 proxy=...`，已有代理认证脱敏。
- refresh-token 引擎已经有 `_log_stage()` 和 `_classify_log_level()`，是目前最接近日志规范的一块。
- two-stage adapter 只在第二阶段开始记录 `[注册] 第二阶段：使用注册邮箱抓取 free Auth/RT`，但 stage1 保存、stage2 失败、保留 AT-only 的日志不够结构化。

改造点：

1. `enqueue_register_task()` 的 detail/meta 增加：
   - `flow="registration"`
   - `policy.registration_entry`、`policy.registration_mode`、`policy.mail_provider`、`policy.two_stage_enabled`、`policy.proxy_mode`
   - `summary.requested_count/concurrency/delay_seconds`

2. `_run_register()` 每个 attempt 开始记录：
   - `[注册][prepare] attempt=i target_success=n mode=... entry=...`
   - 代理记录改为 `[代理][proxy_select] candidate=x/y source=... proxy=redacted`

3. `chatgpt_registration_mode_adapter.py::_run_two_stage_registration()` 增加关键阶段日志：
   - `[注册][access_token_checkpoint] start`
   - `[注册][access_token_checkpoint] saved saved_account_id=... has_access_token=true`
   - `[注册][full_auth_capture] start scope=free`
   - `[注册][full_auth_capture] success has_refresh_token=true workspace_id=...`
   - `[注册][full_auth_capture] failed keep_checkpoint=true error_code=... retryable=...`

4. 最终 `_save_task_log` detail 增加：
   - `artifacts.has_access_token/has_refresh_token/auth_level/workspace_id`
   - `stage_results` 至少记录 checkpoint 和 full_auth 两个阶段。
   - 二阶段失败但保留 AT 时，`attempt_outcome` 不应只是 `success`，建议补充 `auth_capture_required=true`、`error.code="registration_full_auth_failed"`、`artifacts.auth_level="access_token_only"`。

5. 保留 `refresh_token_registration_engine.py::_classify_log_level()`，但把 allowed info prefix 更新到统一前缀，减少裸 `步骤N` 出现在 info。

### 4.2 手机号注册

关键路径：

- `api/tasks.py::enqueue_register_task()` 会识别 `chatgpt_registration_entry in {phone, phone_signup, sms, sms_signup}` 并写 `meta.phone_signup`。
- `api/tasks.py::_run_register()` 对 phone signup 设置 `chatgpt_registration_mode=access_token_only`，`mailbox=None`。
- `services/chatgpt_core/plugin.py::ChatGPTPlatform.register()` 分支到 `PhoneRegistrationEngine`。
- `services/chatgpt_core/phone_registration_engine.py::PhoneRegistrationEngine.run()` 执行手机号注册或已注册手机号登录续跑。
- `services/chatgpt_core/phone_signup_client.py` 执行具体 `user/register`、`phone-otp/send/resend/validate`、`create_account`、callback/session 捕获。

现状：

- 日志前缀有 `[手机号注册]` 和 `[手机号注册链路]`，阶段大致清楚。
- `PhoneSignupClient.redact_text()` 已局部处理 token/password/session/cookie/code query。
- `_panel_result()` 返回完整 `phone/api_url/raw_line`，`result.metadata` 里 `proxy_used` 是原始代理。
- OTP 明文主要来自 shared `UploadedPhoneService` 和 OAuth/add-phone 路径，不在 phone_signup_client 的 validate 日志里直接输出，但 `phone_service` 会输出。

改造点：

1. `PhoneRegistrationEngine.run()` 统一日志：
   - `[手机号注册][phone_acquire] attempt=x/y source=pool|uploaded prefix4=...`
   - `[手机号注册][route_detect] route=signup|existing_login final_path=...`
   - `[手机号注册][phone_send] accepted=true phone_masked=...`
   - `[手机号注册][phone_wait] timeout=... resend=x/y`
   - `[手机号注册][phone_submit] ok=true otp_length=6`
   - `[手机号注册][session_capture] ok=true has_access_token=true account_id=...`
   - `[手机号注册][persist] status=registered_phone_signup account_email=phone:+***1234`

2. 不在普通 info 输出完整手机号；如业务 UI 仍要展示完整手机号，放在受控结果字段，不出现在 `logs`。

3. `result.metadata` 和 task meta/detail 拆分：
   - 运行时需要的账号 extra 可保留业务必要字段。
   - task detail 使用 sanitized `phone_signup_results`：`phone_masked`、`prefix4`、`api_host`、`status`、`code_received`、`code_time`、`reason_redacted`。

4. `PhoneSignupClient` 所有 HTTP 失败响应统一走 `redact_text()`，`phone-otp/validate 失败` 当前直接 `result.text[:500]`，也应改成 `redact_text(result.text, phone)`。

### 4.3 手机号绑定

关键路径：

- `api/tasks.py::enqueue_phone_binding_test_task()` 构造 account/phone/prefix/proxy meta。
- `api/tasks.py::_run_phone_binding_test()` 编排账号、号码、代理、Auth 调用、结果保存。
- `api/actions.py::_execute_chatgpt_resume_subscription_auth()` 调用补抓 Auth。
- `services/chatgpt_core/subscription_auth_capture.py::capture_subscription_auth_for_account()` 进入 `OAuthClient.login_and_get_tokens(...)`。
- `services/chatgpt_core/oauth_client.py::_handle_add_phone_verification()` 执行 add_phone 发码、收码、提交。

现状：

- `_run_phone_binding_test()` 日志比较丰富，但函数很胖，状态都靠自由文本。
- 已有 `_phone_binding_error_status()` 和 `_phone_binding_status_label()`，可以直接用作日志 `error_code/status`。
- 成功绑定但 Auth/RT 失败的边界已经表达：`auth_capture_ok=false`、`auth_error`、`used_for_binding_auth_failed`。
- 存在敏感输出：`[RESULT_RAW] raw_line`，完整 `phone`，`phone_service` 输出验证码，`OAuthClient` 输出验证码。

改造点：

1. `enqueue_phone_binding_test_task()` 的 meta 分离：
   - 运行参数传给 `_run_phone_binding_test()` 可以保留原值。
   - `_task_store.meta` 只保存脱敏后的 `phone_items_sanitized`、`proxy_summary`，不保存完整代理认证/完整 API URL。

2. `_run_phone_binding_test()` 增加/规范阶段：
   - `[手机号绑定][prepare] accounts=n phones=n use_pool=true prefix_sample=false sms_probe_only=false`
   - `[手机号绑定][account_start] index=x/y account_id=... email=...`
   - `[手机号绑定][phone_acquire] phone_masked=... prefix4=... source=pool|manual`
   - `[代理][proxy_select] candidate=x/y source=... proxy=redacted`
   - `[手机号绑定][auth_login] start allow_add_phone=true allow_existing_phone_otp=true`
   - `[手机号绑定][phone_send] accepted=true touched=true`
   - `[手机号绑定][phone_wait] received=true otp_length=6 elapsed_ms=...`
   - `[手机号绑定][phone_submit] submitted=true sms_probe_only=false`
   - `[手机号绑定][full_auth_capture] ok=true|false auth_retry_attempts=n`
   - `[手机号绑定][result] status=bound|openai_rejected|api_no_code|account_auth_error retryable=...`

3. 结果 detail：
   - `runtime_results` 每项补充 `error_code`、`error_phase`、`retryable`、`phone_touched`、`sms_sent`、`otp_received`、`otp_submitted`、`auth_capture_ok`。
   - `bound_phone_lines` 不再写完整 raw line；如前端导出必须保留，新增受控字段 `export_lines` 且只在专门导出接口使用，不进任务历史日志。

4. 保留“手机号绑定成功 ≠ Auth 抓取成功”的日志语义：
   - `status=bound` 表示 OTP 已提交成功。
   - `auth_capture_ok=false` 表示后续 RT/full auth 失败。
   - 不因为 Auth 失败把绑定日志改成失败绑定。

### 4.4 补抓 Auth

关键路径：

- `api/tasks.py::enqueue_resume_subscription_auth_task()` / `_run_resume_subscription_auth()`
- `api/tasks.py::_run_batch_resume_subscription_auth()`
- `api/actions.py::_execute_chatgpt_resume_subscription_auth()`
- `services/chatgpt_core/subscription_auth_capture.py::capture_subscription_auth_for_account()`

现状：

- enqueue 阶段 meta 已记录 account_id/email 和三个手机号策略字段。
- `_run_resume_subscription_auth()` 只把 legacy `allow_phone_verification` 放入最终 detail，没把 `allow_add_phone_verification/allow_existing_phone_verification` 回写到最终 detail。
- `subscription_auth_capture` 已有 `_build_auth_capture_payload()`，包含 `has_access_token/has_refresh_token/attempts/scope/workspace_artifacts`，这是比较好的结构化基础。
- 开始日志里 `proxy={proxy_url or 'direct'}` 没脱敏。

改造点：

1. `capture_subscription_auth_for_account()` 开始日志使用脱敏代理：
   - `[补抓Auth][prepare] account_id=... email=... scope=free proxy=redacted allow_add_phone=... allow_existing_phone_otp=...`

2. 每次 attempt：
   - `[补抓Auth][attempt] index=x/y`
   - `[补抓Auth][oauth_login] start source=subscription_auth_capture`
   - `[补抓Auth][token_exchange] ok=true has_access_token=true has_refresh_token=true`
   - `[补抓Auth][persist] ok=true status=registered auth_level=refresh_token`

3. 失败日志：
   - 使用 `_classify_capture_error()` 的 `error_code/retryable` 进入实时日志和 detail。
   - 重试日志记录 `retry_after_seconds`，不要重复输出长 raw error。

4. 最终 `_save_task_log` detail 增加：
   - `flow="auth_capture"`
   - `policy.allow_phone_verification/add/existing`
   - `auth_capture` compact payload
   - `error.code/retryable/phase/message`

### 4.5 邮箱测活

关键路径：

- `api/tasks.py::_run_custom_email_recheck()` / `_run_batch_custom_email_recheck()`
- `services/chatgpt_core/custom_email_recheck.py::recheck_custom_chatgpt_email()`
- `custom_email_recheck.py::_capture_access_token_without_refresh_token()`
- 后续 full auth 仍复用 `RefreshTokenRegistrationEngine` / `OAuthClient`

现状：

- 第一阶段大量日志：`第一阶段状态起点`、`状态推进[1/20]`，可读但会污染 info。
- `_build_success_payload()` / `_build_failure_payload()` 已有 `status/retryable/recoverable/has_access_token/has_refresh_token/mailbox_state`。
- 批量任务结果会把 `result` payload 放入 `meta.results`。
- error 分类复用 invalid recheck 并扩展 `email_otp_timeout/otp_rate_limited`。

改造点：

1. 明确两阶段日志：
   - `[邮箱测活][access_token_probe] start skip=false`
   - `[邮箱测活][access_token_probe] ok=true saved_account_id=... revived_existing=true has_access_token=true`
   - `[邮箱测活][full_auth_capture] start`
   - `[邮箱测活][full_auth_capture] ok=true has_refresh_token=true`
   - `[邮箱测活][full_auth_capture] failed keep_stage1=true error_code=...`

2. `_capture_access_token_without_refresh_token()` 的状态推进类日志降 debug：
   - `第一阶段状态起点`
   - `第一阶段状态推进[x/20]`
   - `authorize 停在 ...`

3. 邮箱 OTP 日志要带 `purpose`：
   - `purpose=custom_email_recheck_login_otp`
   - 不要和注册 OTP 混淆。

4. 最终 detail：
   - `flow="custom_email_recheck"`
   - `stage_results=[access_token_probe, full_auth_capture]`
   - `result.status` 映射到统一 `error.code`
   - `mailbox_state_summary` 代替完整 mailbox state 进入 task history，完整 state 如业务需要只进账号 extra。

### 4.6 失效测活

关键路径：

- `api/tasks.py::_run_invalid_recheck()`
- `api/actions.py::_execute_chatgpt_invalid_recheck()`
- `services/chatgpt_core/invalid_account_recheck.py::recheck_invalid_chatgpt_account()`
- `invalid_account_recheck.py::_capture_access_token_without_refresh_token()`
- followup 调用 `custom_email_recheck.recheck_custom_chatgpt_email(... skip_access_token_probe=True)`

现状：

- `_classify_recheck_error()` 已提供 `account_deactivated/password_invalid/login_blocked/network_failed/otp_rate_limited`。
- `_build_recheck_payload()` 有 `status/recoverable/message/attempts/has_access_token/allow_*`。
- `_run_invalid_recheck()` 的最终 task log detail 只有 `attempt_outcome/email/account_id/source`，没有带 `invalid_recheck` payload。

改造点：

1. `recheck_invalid_chatgpt_account()` 增加阶段日志：
   - `[失效测活][access_token_probe] start account_id=...`
   - `[失效测活][access_token_probe] ok=true has_access_token=true recovered=true`
   - `[失效测活][persist] status=recovered_access_token saved_account_id=...`
   - `[失效测活][followup_auth] start source=custom_email_recheck skip_access_token_probe=true`
   - `[失效测活][followup_auth] ok=true|false has_refresh_token=...`

2. `_run_invalid_recheck()` 最终 detail 应包含：
   - `flow="invalid_recheck"`
   - `invalid_recheck` compact payload
   - `error.code/retryable/recoverable`
   - `artifacts.has_access_token/has_refresh_token/final_auth_level`

3. 明确失败语义：
   - `account_deactivated/password_invalid` 是终态，不应该标记 retryable。
   - `network_failed/otp_rate_limited/login_blocked` 不是账号真实死亡，日志要写 `kept_original_status=true`。

### 4.7 本地 token 测活 / 状态探测

关键路径：

- `services/chatgpt_core/status_probe.py::probe_local_chatgpt_status()`
- `services/chatgpt_core/local_status_refresh.py::sync_chatgpt_account_local_status()`、`schedule_chatgpt_local_status_refresh_for_account_id()`、`summarize_status_refresh()`
- `services/chatgpt_core/plugin.py::execute_action("probe_local_status")`
- `api/chatgpt.py::probe_local_status()` / `check_subscription()`

现状：

- `probe_local_chatgpt_status()` 返回结构清晰：`auth/subscription/codex`，内部不打日志。
- 手动 action 返回 message：`认证=..., 订阅=..., Codex=...`。
- 后台 refresh 成功基本无 task log，失败只 logger warning。

改造点：

1. 不建议让 `status_probe.py` 直接接 `_log()`；它是底层探测函数，保持返回结构即可。

2. 在调用方记录 compact summary：
   - `[本地状态][token_refresh] source=refresh_token ok=true|false http_status=... error_code=...`
   - `[本地状态][backend_me] state=valid|invalid http_status=...`
   - `[本地状态][subscription] plan=plus|free|unknown active_until=...`
   - `[本地状态][codex] state=available|limited|not_checked gate=...`

3. `local_status_refresh.py::schedule_chatgpt_local_status_refresh_for_account_id()` 当前只有失败 warning。建议成功也用 `logger.info` 记录 compact summary，但不要进入 task history，除非它由某个任务触发并有 `task_id`。

4. 手动 API `api/chatgpt.py::probe_local_status()` 可以返回 `probe_summary`，但不新增 task log；这是接口级探测，不是长任务。

## 5. 公共实现建议（后续执行阶段）

### 5.1 新增日志工具，不碰业务流程

建议实现最小公共工具：

```text
services/chatgpt_core/task_logging.py
```

包含：

- `redact_log_text`
- `redact_proxy_url`
- `redact_url`
- `mask_phone_for_log`
- `sanitize_task_detail`
- `compact_error`
- `stage_event(flow, stage, message, **fields)`：返回字符串，不直接写日志

为什么放 `services/chatgpt_core`：这批整改主要是 ChatGPT 任务，不想影响全项目其他平台。若后续要泛化，再移到 `core/`。

### 5.2 在 `api/tasks.py::_log()` 和 `_save_task_log()` 入口兜底

最小安全兜底：

- `_log()` 写入前调用 `redact_log_text(text)`。
- `_save_task_log()` 写入前对 `error` 和 `detail` 调用 sanitize。
- `_build_task_log_detail()` 可以保留原结构，但返回前 sanitize，或在 `_save_task_log()` 里 sanitize。推荐在 `_save_task_log()` 兜底，避免漏掉所有调用。

注意：兜底脱敏不能破坏前端依赖字段类型，例如 `meta.results` 仍应是 list，不要整体变字符串。

### 5.3 不要一次性改所有日志文案

执行顺序建议：

1. 先加脱敏工具和 `_log/_save_task_log` 兜底，修 OTP/token/proxy/raw_line 泄露。
2. 再改重点任务的阶段日志：手机号绑定、补抓 Auth、邮箱测活/失效测活、注册 two-stage、手机号注册。
3. 最后补 task detail 的统一字段。
4. 本地状态探测只补 compact summary，不接入长任务日志。

这样即便文案没完全统一，也先解决安全风险。

## 6. 实施步骤

### Step 1：新增日志/脱敏工具与测试

- 新增 `services/chatgpt_core/task_logging.py`。
- 新增测试 `tests/test_chatgpt_task_logging.py`。
- 覆盖：token、cookie、password、OTP、authorization code、proxy auth、URL query、raw_line、phone masking、递归 detail sanitize。

### Step 2：接入全局兜底

- `api/tasks.py::_log()`：写入 task store 和 print 前脱敏。
- `api/tasks.py::_save_task_log()`：`error/detail` 入库前脱敏。
- 保持 `[DEBUG]` 前缀逻辑不变。

验收重点：历史任务详情里不再有验证码/token/完整代理认证。

### Step 3：手机号和 OTP 泄露点修复

- `services/chatgpt_core/phone_service.py::UploadedPhoneService.wait_for_code()`：不输出 OTP 明文。
- `services/chatgpt_core/oauth_client.py`：所有 OTP / authorization code / raw response 输出改为脱敏或布尔摘要。
- `services/chatgpt_core/phone_signup_client.py::validate_phone_otp()` 失败响应走 `redact_text()`。

### Step 4：代理和 raw_line 元数据修复

- `api/tasks.py::enqueue_phone_binding_test_task()`：task meta 中代理字段脱敏；`phone_items` 改为 sanitized copy。
- `_run_phone_binding_test()`：`[RESULT_RAW]` 不输出原始 `phone----api_url`。
- 手机号注册 task meta/detail 同理 sanitization。

### Step 5：补统一阶段字段

按任务逐步补：

- 注册/two-stage：checkpoint/full_auth_capture stage。
- 手机号注册：phone_acquire/phone_send/phone_wait/phone_submit/session_capture。
- 手机号绑定：account_start/phone_acquire/auth_login/phone_submit/full_auth_capture/result。
- 补抓 Auth：attempt/oauth_login/token_exchange/persist/error。
- 邮箱测活：access_token_probe/full_auth_capture。
- 失效测活：access_token_probe/followup_auth。
- 本地状态：调用方 compact summary。

### Step 6：最终验收与回归

- 运行单测：`pytest -q tests/test_chatgpt_task_logging.py tests/test_chatgpt_phone_registration.py tests/test_subscription_auth_capture.py tests/test_custom_email_recheck.py tests/test_invalid_account_recheck.py || true`
- 前端 build 不一定由日志整改直接触发；如果改了 task detail shape 或 TaskLogPanel 展示再跑 `cd frontend && npm run build`。
- 手工 smoke 至少看：任务日志实时流、任务历史详情、手机号绑定结果、邮箱测活失败详情。

## 7. 验收清单

### 安全脱敏

- [ ] 实时日志中没有 access_token / refresh_token / id_token / session_token / cookie。
- [ ] 实时日志中没有密码。
- [ ] 实时日志中没有邮箱 OTP、手机 OTP、authorization code 明文。
- [ ] 实时日志中没有完整代理认证；只能看到 `***:***@host:port`。
- [ ] `task_logs.detail_json` 中没有上述敏感信息。
- [ ] 收码 API URL query/token 不进入 task history。
- [ ] `[RESULT_RAW]` 不再输出完整 `phone----api_url`。

### 可读性

- [ ] 注册日志能看出 `access_token_checkpoint` 和 `full_auth_capture` 两阶段。
- [ ] 手机号注册日志能区分 `phone_signup` 和 `phone_existing_login`。
- [ ] 手机号绑定日志能区分：未进 add_phone、OpenAI 拒号、API 未收码、已绑定但 Auth 失败、短信探测只收码未提交。
- [ ] 补抓 Auth 日志能看出 `scope`、手机号策略、attempt、error_code、retryable。
- [ ] 邮箱测活日志能区分第一阶段 AT probe 和第二阶段 full auth。
- [ ] 失效测活日志能区分账号真实停用、密码错、网络/限流、登录额外验证。
- [ ] 本地状态探测有 compact summary：auth/subscription/codex。

### 兼容性

- [ ] 现有任务历史列表仍能读取 `source/attempt_outcome/progress/success/skipped/failed/meta_summary`。
- [ ] 前端依赖的 `runtime_results/account_results/bound_phone_results` 类型不变。
- [ ] 账号 extra 的业务字段不因日志脱敏而丢失必要状态。
- [ ] 不新增 DB migration，不改 `TaskLog` 表结构。

## 8. 风险与注意点

1. **不要误脱敏业务必需字段**  
   账号保存仍需要 token/password/cookies 等业务材料；脱敏只能发生在日志、task detail、可展示 meta，不要在 `save_account()` 前改业务对象。

2. **不要把手机号注册和手机号绑定合并**  
   两者可以共享脱敏和阶段名，但业务语义不同。日志里必须保留 `flow=phone_signup` vs `flow=phone_binding`。

3. **不要让 detail 过度膨胀**  
   当前 `logs` 已经整体写入 detail。新增 `stage_results` 要 compact，不要把每个 HTTP step 都结构化塞进去。

4. **不要破坏现有前端结果展示**  
   `runtime_results` 等字段若从完整 URL 改为脱敏 URL，前端若有“一键复制原始行”需求，应该另走受控导出，不应依赖任务日志 detail。

5. **stdout 也是日志泄露面**  
   `_log()` 会 `print(entry)`。只改 DB sanitize 不够，必须实时日志先脱敏。

6. **live 容器和 checkout 可能漂移**  
   后续执行和验收要按项目约定确认容器代码/静态资源是否同步。本阶段只写文档，不部署。

## 9. 非目标

- 不重构注册/绑定/测活流程。
- 不统一所有错误分类函数的实现，只统一日志字段表达。
- 不新增任务系统、日志表、外部日志服务。
- 不改手机号池复用、prefix 逻辑、Auth 抓取策略。
- 不改账号状态判定策略。
- 不要求把所有底层 HTTP step 都变成结构化事件。

## 10. 建议的最终落地判断

这次日志整改优先级应是：

1. **安全脱敏兜底**：先确保实时日志和历史 detail 不再泄露 token/password/OTP/完整代理认证/完整接码 API。
2. **关键阶段命名**：把几个任务共同的 `access_token_probe / access_token_checkpoint / full_auth_capture / phone_send / phone_wait / phone_submit / token_exchange / persist` 固定下来。
3. **结构化错误摘要**：每个任务最终 detail 至少有 `error.code/error.phase/error.retryable/error.message`。
4. **保留业务边界**：手机号注册、手机号绑定、补抓 Auth、邮箱测活、失效测活仍是独立任务，只共享日志规范。

这样做不碰流程，但能马上降低排障成本，也给以后真正重构留下统一观测面。
