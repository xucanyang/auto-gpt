# ChatGPT 任务日志整改方案审核结论（第 2 阶段）

> 输入方案：`docs/chatgpt-task-logging-plan.md`  
> 审核方式：只读方案文档 + 抽查指定代码文件；本阶段不实现业务代码、不改流程。  
> 抽查文件：`api/tasks.py`、`api/actions.py`、`services/chatgpt_core/oauth_client.py`、`subscription_auth_capture.py`、`custom_email_recheck.py`、`invalid_account_recheck.py`、`status_probe.py`、`phone_registration_engine.py`、`plugin.py`，并额外抽查了直接相关的 `phone_service.py`、`phone_signup_client.py`。

## 1. 总体结论

第 1 阶段方案方向是**可行的**，而且优先级判断基本对：当前最该先做的不是流程重构，而是先把任务日志、任务快照 `meta`、历史 `task_logs.detail_json` 里的敏感信息兜住，再补关键阶段名和错误摘要。

但执行方案需要收窄和加保护。原因是当前代码里“日志”和“运行参数”有些地方混在一起，尤其手机号绑定和手机号注册：

- `api/tasks.py::enqueue_phone_binding_test_task()` 把 `phone_items`、`settings.proxy`、`proxy.specified` 直接放进任务 `meta`，同时又把 `dict(meta["settings"])` 传给后台 runner。这里如果执行阶段简单把 `meta.settings.proxy` 脱敏，会直接把运行时代理也脱敏掉，任务就跑不通。
- `_run_phone_binding_test()` 的 `runtime_results`、`bound_phone_lines`、`bound_phone_results` 既是 UI 结果，又会被 `_build_task_log_detail()` 原样写进历史 detail；里面现在包含 `api_url/raw_line`。
- `_run_register()` 里手机号注册成功后，会把 `phone_signup_results/raw_line` 回写进任务 meta；`phone_registration_engine.py::_panel_result()` 本身也返回完整 `api_url/raw_line`。
- `_log()` 会同时写内存日志和 stdout；`_save_task_log()` 会把 detail JSON 入库；只在入库前脱敏不够，实时任务日志和 active task meta 也要脱敏。

所以最终建议是：

1. **先做安全兜底**：统一脱敏 helper + `_log()` 文本兜底 + `_save_task_log()` detail/error 兜底 + 已知 OTP/proxy/raw_line 直出点修复。
2. **再做 display meta 与 runtime data 分离**：任务快照里只放 sanitized copy，后台 runner 使用原始参数的局部变量，不把原始 secrets 存进 `_task_store`。
3. **阶段字段和错误字段走 additive**：加 `flow/stage/error/artifacts/policy` 可以，但不要删除或重命名现有 `attempt_outcome/source/runtime_results/account_results`，避免前端和历史页炸。
4. **推迟大规模文案统一和全量 stage_results**：那部分容易变成“日志重构”，本次先别上来全覆盖。

一句话：**方案可落地，但执行版必须从“改日志文案”调整为“先建立安全日志边界”。**

## 2. 必须调整

### 2.1 必须先做统一脱敏入口，不能继续到处手写

第 1 阶段建议新增 `services/chatgpt_core/task_logging.py` 是对的。审核后建议把它定位成**纯 stdlib、无 DB/无 api 依赖**的工具模块，避免循环依赖。

最低函数集建议保留：

- `redact_log_text(value) -> str`
- `redact_proxy_url(value) -> str`
- `redact_url(value, keep_host=True) -> str`
- `mask_phone_for_log(value) -> str`
- `sanitize_task_detail(value) -> Any`
- `sanitize_error_message(value) -> str`
- 可选：`stage_event(flow, stage, message='', **fields) -> str`

必须覆盖的敏感项：

- token：`access_token`、`refresh_token`、`id_token`、`session_token`、`accessToken`、`sessionToken`、JWT、`Bearer ...`、`Authorization`。
- 密码：`password`、`login_password`、`chatgpt_phone_signup_password`。
- Cookie：`cookie`、`cookies`、`set-cookie`、`oai-client-auth-session`、`login_session`。
- OTP / 验证码 / authorization code：上下文命中时脱敏，例如 `尝试 OTP: 123456`、`收到验证码 123456`、`authorization code: ...`。不要无脑把所有 6 位数字都替换，否则号段、时间、数量会被误伤。
- 代理认证：`scheme://user:pass@host:port` -> `scheme://***:***@host:port`。
- 收码 API URL：保留 `scheme://host/path`，query/key/token 全删或替换。
- `phone----api_url` 原始行：拆成 `phone_masked/full_phone按策略` + `api_url_redacted`，不要整行入日志/detail。

现有 `phone_signup_client.redact_text()` 只能处理一部分 `ac_`、`state/code query` 和少数字段，不够作为全局兜底。执行阶段不要继续复制这类局部正则。

### 2.2 `_log()` 和 `_save_task_log()` 都要兜底，但不能只靠它们

代码现状：

- `api/tasks.py::_log()` 直接 `entry = f"[{ts}] {text}"`，然后 `_task_store.append_log()` 和 `print(entry)`。
- `api/tasks.py::_save_task_log()` 直接 `json.dumps(detail)` 入库。
- `_build_task_log_detail()` 直接把 snapshot 的 `errors/meta/logs` 放进 detail。

所以执行阶段必须：

1. `_log()` 写入 `_task_store` 和 stdout 前调用 `redact_log_text()`。
2. `_save_task_log()` 入库前调用 `sanitize_error_message(error)` 和 `sanitize_task_detail(detail)`。
3. `_build_task_log_detail()` 可以不承担主责，但返回前 sanitize 也可以；更稳的是 `_save_task_log()` 兜底，防止其他调用绕过。
4. 不能只 sanitize DB detail，因为 active task 日志和容器 stdout 在 `_save_task_log()` 之前已经泄露了。

注意：`sanitize_task_detail()` 必须保持字段类型，不能把整个 dict/list 变字符串；`runtime_results/account_results/meta.results` 这些前端依赖仍应是 list/dict。

### 2.3 任务 meta 要分 display copy 和 runtime raw data

这是第 1 阶段方案里最需要补强的点。

`enqueue_phone_binding_test_task()` 当前把这些原始内容存进 task meta：

- `phone_items`: 完整手机号、`api_url`、`raw_line`。
- `settings.proxy`: 原始代理。
- `proxy.specified`: 原始代理。

同时后台启动时传的是：

```python
args=(task_id, account_ids, phone_items, dict(meta["settings"]))
```

所以执行阶段不要这样做：

```python
meta["settings"]["proxy"] = redact_proxy_url(req.proxy)  # 这样会破坏 runner 参数
```

应该这样做：

- 先构造 `runtime_settings`，里面保留原始 `proxy`，只传给 `_run_phone_binding_test()`。
- `meta["settings"]` 只放 sanitized/display 版本，例如 `proxy_redacted`，或保留 `proxy_mode/country/failover/max_candidates/min_score`，不要放原始 `proxy`。
- `phone_items` 也一样：runner 用原始 `phone_items` 局部变量；`meta["phone_items"]` 放 `phone_items_sanitized` 或摘要，不要共享同一个 list 对象后再原地改。
- `_run_phone_binding_test().sync_meta()` 也要写 sanitized runtime results；原始 `raw_line` 只留在函数局部变量里供运行时使用，不进入 `_task_store`。

手机号注册同理：`_run_register()` 把 `chatgpt_phone_signup_results` 写入 task meta 时，需要写 sanitized copy；不要改 `account.extra` 里业务必需字段。

### 2.4 明文 OTP 是最高优先级，必须先修

审核确认方案指出的 OTP 泄露点真实存在，且比阶段名统一更紧急。

必须修的直出点：

- `services/chatgpt_core/phone_service.py::UploadedPhoneService.wait_for_code()`：
  - 当前会输出 `忽略旧验证码 {code}`、`收到验证码 {code}`。
  - 改成 `otp_received=true otp_length=6 code_time=... extracted=true/false`。
- `services/chatgpt_core/oauth_client.py`：
  - 已绑定手机号 OTP：`准备提交绑定手机号验证码: {code}`。
  - add_phone OTP：`号段短信探测已收到验证码: {code}`、`准备提交手机号验证码: {code}`。
  - 人工手机号 OTP：`准备提交人工手机号验证码: {code}`。
  - 邮箱 OTP：`尝试 OTP: {code}`、`跳过已尝试验证码: {code}`。
  - authorization code：`获取到 authorization code: {code[:20]}...`。
- `phone_signup_client.validate_phone_otp()` / `create_account()` 失败响应目前部分地方直接 `result.text[:500]`，应统一走全局 `redact_log_text()` 或至少复用增强后的 `redact_text()`。

这里不涉及业务流程，只改日志文本，安全可落地。

### 2.5 代理和收码 API URL 脱敏不能只修注册

注册代理日志已有局部脱敏，邮箱测活代理选择也有 `_redact_proxy_for_task_log()`。但手机号绑定仍存在明显口子：

- `_log_phone_binding_proxy_choice()` 直接输出 `proxy={proxy_url}`。
- `enqueue_phone_binding_test_task()` meta 记录 `settings.proxy` 和 `proxy.specified` 原文。
- `subscription_auth_capture.capture_subscription_auth_for_account()` 开始日志直接 `proxy={proxy_url or 'direct'}`，metadata 里也写 `proxy`。
- `phone_registration_engine` metadata 里写 `proxy_used` 原文。

执行阶段必须统一走 `redact_proxy_url()`。其中运行时 config 可以保留 raw proxy，但日志/detail/meta 中只能出现 redacted proxy。

收码 API URL 同理：

- `phone_items.api_url`
- `raw_line=phone----api_url`
- `bound_phone_lines`
- `phone_signup_raw_line`
- `phone_signup_result.api_url/raw_line`

这些进入任务 meta/detail 时都应该变成 sanitized copy。账号 extra 是否保留完整值属于业务数据问题，本次不要顺手改掉。

### 2.6 `detail_json` 增加统一字段可以做，但必须 additive

第 1 阶段提议增加：

- `schema_version`
- `flow`
- `summary`
- `policy`
- `stage_results`
- `error`
- `artifacts`
- `redaction_version`

方向没问题，但执行阶段必须遵守：

- 不删旧字段：`source/attempt_outcome/progress/success/skipped/errors/meta/logs/result/runtime_results/account_results/bound_phone_results`。
- 不改旧字段类型。
- 不把 `attempt_outcome` 从 success/failed 语义改成半成功语义；半成功放新增字段，例如 `artifacts.auth_level=access_token_only`、`error.code=registration_full_auth_failed`、`needs_followup_auth=true`。
- `stage_results` 先只记录关键阶段，不要把每个 HTTP step 都塞进去。否则 detail 会膨胀。

另外，第 1 阶段文档里的 JSON 示例 `stage_results` 附近有重复 `{`，最终方案别照抄。

### 2.7 邮箱测活、失效测活的两阶段语义必须保留，但不要顺手改业务

结合代码和既有设计，邮箱测活 / 失效测活的核心排障点就是：

- 第一阶段：拿并保存 `access_token`。
- 第二阶段：补抓完整 Auth / refresh_token。
- 第二阶段失败时：保留第一阶段结果。

第 1 阶段方案要求日志显式表达 `access_token_probe/access_token_checkpoint/full_auth_capture` 是对的。执行阶段只应补日志和 detail 字段，不要改变：

- `custom_email_recheck.recheck_custom_chatgpt_email()` 在第二阶段失败时仍返回 `ok=True` 且保留 stage1 payload 的行为。
- `invalid_account_recheck.recheck_invalid_chatgpt_account()` 第二阶段失败时仍保留复活结果的行为。
- `phone_binding` 中 `phone_service.completed_entries` 代表绑定成功、`auth_capture_ok=false` 代表后续 Auth 失败的边界。

这些是业务语义，不属于日志整改。

## 3. 建议保留

### 3.1 保留“先脱敏，后阶段化”的实施顺序

第 1 阶段的落地顺序建议是合理的：

1. 新增脱敏工具和测试。
2. 接入 `_log/_save_task_log` 兜底。
3. 修 OTP、authorization code、proxy、raw_line 等直出点。
4. 再补阶段名、错误摘要、policy/artifacts。

这个顺序比一上来大规模改文案稳很多。

### 3.2 保留 `status_probe.py` 不直接接任务日志

`status_probe.py::probe_local_chatgpt_status()` 当前返回结构化 `auth/subscription/codex`，内部不打 task log。第 1 阶段建议“底层保持返回结构，调用方写 compact summary”是对的。

本次不建议给 `status_probe.py` 塞 `log_fn`，否则它会从纯探测函数变成任务日志函数，边界会乱。最多在调用方：

- `plugin.py::execute_action("probe_local_status")`
- `api/chatgpt.py::probe_local_status()`
- `local_status_refresh.py`

生成 summary，不进长任务历史即可。

### 3.3 保留手机号注册和手机号绑定的业务边界

日志阶段可以共享 `phone_acquire/phone_send/phone_wait/phone_submit`，但不要把二者合并成同一种 result。

- 手机号注册：结果是新账号 `phone:+xxx`，auth 多为 `access_token_only`。
- 手机号绑定：结果是已有账号 add_phone OTP 提交完成，后续 Auth/RT 成败另算。

日志里可以统一表达，但字段含义不能互相覆盖。

### 3.4 保留现有错误分类函数，先做字段映射

当前已有：

- `subscription_auth_capture._classify_capture_error()`
- `custom_email_recheck._classify_custom_recheck_error()`
- `invalid_account_recheck._classify_recheck_error()`
- `api.tasks._phone_binding_error_status()`
- `phone_registration_engine._error_status()`

这次不应统一实现，只要最终 detail 增加类似：

```json
"error": {
  "code": "email_otp_timeout",
  "phase": "email_otp_wait",
  "retryable": true,
  "message": "...redacted..."
}
```

即可。分类函数重构留后面。

## 4. 建议推迟

### 4.1 推迟全量日志文案统一

第 1 阶段列了大量 `[FLOW][STAGE]` 文案，这个方向没问题，但一次性改完风险较高：

- 当前很多日志来自不同层：`api/tasks.py`、`api/actions.py`、`RefreshTokenRegistrationEngine`、`OAuthClient`、`PhoneRegistrationEngine`、`phone_service`。
- 部分日志依赖现有前缀做 debug 降噪，例如 `refresh_token_registration_engine._classify_log_level()`。
- 一次性改全量文本会让回归难定位。

建议第一轮只统一关键阶段，不追求所有日志都漂亮。

### 4.2 推迟“完整 schema_version=2 事件模型”

可以先加 `schema_version/redaction_version/flow/error/artifacts/policy`，但不要要求所有任务都填完整 `summary/current/stage_results`。尤其批量任务 detail 已经很大，`stage_results` 需要 compact。

### 4.3 推迟受控原始行导出接口

方案里提到如果前端仍要“一键复制原始行”，应走受控导出，不依赖 task history。这个方向对，但本次日志整改不要顺手做新导出功能。

本次先保证：

- 实时日志不输出 `[RESULT_RAW] phone----api_url`。
- `task_logs.detail_json` 不保存完整 raw line。
- 如果 UI 暂时失去从历史日志复制原始行的能力，需在最终方案里明确这是安全取舍；后续再补受控导出。

### 4.4 推迟把手机号完全脱敏到所有结果字段

完整手机号在这个项目里既是运营对象，也是手机号池和绑定结果的主键。直接把所有结果字段里的 phone 都 mask，会影响排障和后续池状态判断。

建议分层：

- 文本 logs：默认 mask。
- task history detail：至少 raw_line/API URL 脱敏；phone 是否完整保留按前端依赖决定，推荐新增 `phone_masked`，保留 `phone` 前需确认权限/展示边界。
- account extra / phone pool：本次不要改，这是业务数据。

### 4.5 推迟对 `OAuthClient._log` 的全面 level 体系改造

`OAuthClient` 里很多日志现在通过上层 engine override 成 callback；也有 `verbose=True` 时直接 print 的路径。第一轮先做文本脱敏，level/前缀统一可以后移。

## 5. 实施优先级

### P0：安全兜底，必须第一批完成

1. 新增纯工具模块：建议 `services/chatgpt_core/task_logging.py`。
2. 新增单测：建议 `tests/test_chatgpt_task_logging.py`。
3. `_log()` 接 `redact_log_text()`。
4. `_save_task_log()` 接 `sanitize_task_detail()` 和 `sanitize_error_message()`。
5. 修明文 OTP / authorization code：`phone_service.py`、`oauth_client.py`、`phone_signup_client.py`。
6. 修手机号绑定代理日志、补抓 Auth 代理日志、手机号注册 `proxy_used` 的日志/detail 脱敏。

验收目标：实时日志、stdout、`task_logs.detail_json` 不再出现 OTP/token/password/cookie/完整代理认证/完整收码 API query。

### P1：任务 meta/detail 安全化

1. `enqueue_phone_binding_test_task()`：拆 `runtime_settings` 和 display `meta.settings`；`meta.proxy.specified` 只放 redacted。
2. `meta.phone_items` 改为 sanitized copy 或摘要；runner 仍用原始 `phone_items` 局部变量。
3. `_run_phone_binding_test().sync_meta()`：`runtime_results/bound_phone_results/bound_phone_lines/account_results/errors` 写入 `_task_store` 前做 sanitized copy。
4. `_run_register()` 中手机号注册结果回写 task meta 时使用 sanitized copy，尤其 `registered_phone_lines` 和 `phone_signup_results`。
5. `subscription_auth_capture`、`custom_email_recheck`、`invalid_account_recheck` 返回给 task detail 的 `logs`、`mailbox_state`、`raw_error` 要能被兜底 sanitizer 处理。

验收目标：active task snapshot 和历史 detail 都没有 raw proxy/raw API URL/raw line。

### P2：关键阶段和错误摘要

只补关键阶段，不全量改文案：

- 注册：`access_token_checkpoint`、`full_auth_capture`。
- 手机号注册：`phone_acquire`、`phone_send`、`phone_wait`、`phone_submit`、`session_capture`。
- 手机号绑定：`account_start`、`phone_acquire`、`phone_send`、`phone_wait`、`phone_submit`、`full_auth_capture`、`result`。
- 补抓 Auth：`attempt`、`oauth_login`、`token_exchange`、`persist`、`error`。
- 邮箱测活：`access_token_probe`、`full_auth_capture`。
- 失效测活：`access_token_probe`、`followup_auth`。

最终 detail 增加：

- `flow`
- `policy`
- `error.code/error.phase/error.retryable/error.message`
- `artifacts.has_access_token/has_refresh_token/auth_level/workspace_id`
- `redaction_version`

### P3：可读性进一步整理

- 调整 `refresh_token_registration_engine._classify_log_level()` 的 info/debug 前缀。
- status probe 调用方补 compact summary。
- 对老日志前缀进行局部统一。

## 6. 验收方式

### 6.1 单元测试

新增 `tests/test_chatgpt_task_logging.py`，至少覆盖：

1. `redact_proxy_url()`：
   - `http://user:pass@1.2.3.4:8000` -> `http://***:***@1.2.3.4:8000`
   - 无认证代理不误伤 host/port。
2. `redact_url()`：
   - `https://api.example.com/sms?token=abc&key=xyz&id=1` 不保留 query token/key。
3. `redact_log_text()`：
   - `收到验证码 123456`
   - `尝试 OTP: 654321`
   - `准备提交手机号验证码: 111222`
   - `authorization code: abcdef...`
   - `Authorization: Bearer ...`
   - `accessToken/sessionToken/refresh_token/id_token/cookie/password`。
4. `raw_line`：
   - `+15551234567----https://sms.example.com/get?token=secret` 不保留完整 API URL 和 token。
5. `sanitize_task_detail()`：
   - nested dict/list 中的 `logs/meta/errors/result/runtime_results/account_results/mailbox_state` 都能脱敏。
   - `has_access_token/access_token_saved/phone_count/prefix4` 等 bool/number/普通字段不被误改类型。

### 6.2 集成/回归测试

建议 targeted 测试不要加 `|| true`，失败要处理或明确说明：

```bash
pytest -q tests/test_chatgpt_task_logging.py
pytest -q tests/test_phone_binding_assignment.py tests/test_chatgpt_phone_registration.py
pytest -q tests/test_subscription_auth_capture.py tests/test_custom_email_recheck.py tests/test_invalid_account_recheck.py
```

如果跑全量：

```bash
pytest -q
```

全量如有既有失败，执行代理需要列出失败测试和是否与日志改动相关，不能用 `|| true` 把结果糊过去。

### 6.3 数据库/日志内容验收

用一条构造 detail 或测试任务写入后检查：

- `task_logs.detail_json` 不包含：
  - `access_token` 明文值
  - `refresh_token` 明文值
  - `sessionToken/session_token` 明文值
  - `Bearer ` 后面的 token
  - `password` 明文
  - 6 位 OTP 明文上下文
  - `user:pass@` 原始代理认证
  - 收码 API query token/key
  - `phone----https://...?...` 原始行
- `detail_json` 仍包含：
  - `source`
  - `attempt_outcome`
  - `meta`
  - `logs`
  - `runtime_results/account_results` 且类型不变
  - 新增 `redaction_version`

### 6.4 手工 smoke

至少看这几类任务的实时日志和历史详情：

1. 手机号绑定：重点看 OTP、`[RESULT_RAW]`、proxy、api_url。
2. 手机号注册：重点看 `phone_signup_results/raw_line/proxy_used`。
3. 补抓 Auth：重点看 `proxy`、error retry、auth_capture payload。
4. 邮箱测活：重点看第一阶段/第二阶段日志、OTP、mailbox_state。
5. 失效测活：重点看第一阶段 AT 保存、第二阶段 followup。

### 6.5 live 验收提醒

本项目 checkout 不等于 live。执行完成后如果要验证线上行为，需要按项目约定确认容器：

- checkout 测试通过。
- 如部署，确认 `auto-gpt` / live 容器代码同步。
- 重新跑 live smoke 后再看 `/runtime/account_manager.db` 或对应 `task_logs.detail_json`。

本审核阶段不部署、不改代码。

## 7. 最终审核判断

第 1 阶段方案：**可行，但需要调整执行边界后再进入第 3 阶段最终方案。**

第 3 阶段最终方案建议采纳以下压缩版：

1. 建立统一脱敏工具与测试。
2. 接入 `_log()` / `_save_task_log()` 安全兜底。
3. 修 OTP、authorization code、proxy、raw_line、api_url 的已知泄露点。
4. 拆 active task meta 的 display copy 与 runner raw 参数，尤其手机号绑定。
5. 只补关键阶段和统一错误摘要；暂缓全量日志文案重写和完整事件模型。

最不该做的是：**把脱敏后的 meta/settings 继续拿去跑任务**。这会把日志整改变成业务回归。执行代理要特别小心这一点。
