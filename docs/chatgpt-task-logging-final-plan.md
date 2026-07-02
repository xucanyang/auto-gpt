# ChatGPT 任务日志整改最终可执行方案（第 3 阶段）

> 输入文档：
>
> - `docs/chatgpt-task-logging-plan.md`
> - `docs/chatgpt-task-logging-plan-review.md`
>
> 本文是第 3 阶段最终方案。**只形成可执行 patch 方案，不实现代码。**  第 4 阶段执行代理应按本文落地；第 5 阶段审核代理应按本文验收。

## 1. 最终结论

第 1 阶段方案方向成立，第 2 阶段审核指出的收窄也必须采纳。最终执行范围定为：

1. **先建立日志安全边界**：统一脱敏工具 + `_log()` 实时日志兜底 + `_save_task_log()` 历史 detail 兜底。
2. **修已知泄露点**：OTP / authorization code / token / cookie / password / 完整代理认证 / 收码 API query / `phone----api_url` 原始行。
3. **拆 runtime raw data 和 display sanitized copy**：后台 runner 继续用原始 `proxy/api_url/raw_line/token`，任务实时日志、active task `meta`、历史 `task_logs.detail_json` 只保存 sanitized copy。
4. **只补关键阶段与错误摘要**：让注册、手机号注册、手机号绑定、补抓 Auth、邮箱测活、失效测活的阶段能看懂；不重写全部日志文案，不做完整事件模型。

最关键的一条：

> **不能把脱敏后的 `proxy/api_url/raw_line/token/password/cookie` 用于业务执行。脱敏只发生在日志、task display meta、历史 detail、可展示错误消息。运行时参数和账号持久化对象仍使用原始数据。**

这次不是流程重构。任何改变注册/登录/OAuth/手机号池/邮箱测活/账号状态机行为的改动，都不属于本轮。

---

## 2. 执行边界

### 2.1 本轮要改的文件

第 4 阶段执行代理按以下文件最小 patch：

| 文件 | 改动类型 | 目的 |
|---|---|---|
| `services/chatgpt_core/task_logging.py` | 新增 | 统一脱敏、结构化日志小工具，纯 stdlib，无 DB/API 依赖 |
| `api/tasks.py` | 修改 | `_log()` / `_save_task_log()` 兜底；phone-binding meta/detail 分 display/runtime；手机号注册 task meta 安全化 |
| `services/chatgpt_core/phone_service.py` | 修改 | `UploadedPhoneService.wait_for_code()` 不再打印验证码明文 |
| `services/chatgpt_core/oauth_client.py` | 修改 | OTP / authorization code / 手机号 / raw response / add_phone 相关日志脱敏 |
| `services/chatgpt_core/phone_signup_client.py` | 修改 | HTTP 失败响应统一脱敏；`validate_phone_otp()` / `create_account()` 不直出原始 body |
| `services/chatgpt_core/phone_registration_engine.py` | 修改 | 手机号注册日志脱敏；task-facing metadata 通过外层 sanitize，不改业务结果 |
| `services/chatgpt_core/subscription_auth_capture.py` | 修改 | 补抓 Auth 代理日志脱敏；错误与 metadata 增加安全摘要 |
| `services/chatgpt_core/custom_email_recheck.py` | 修改 | 本地 `action_logs` 写入前脱敏；补第一阶段/第二阶段关键日志和错误摘要 |
| `services/chatgpt_core/invalid_account_recheck.py` | 修改 | 失效测活 action/followup 日志脱敏；补关键阶段和错误摘要 |
| `tests/test_chatgpt_task_logging.py` | 新增 | 覆盖脱敏工具、detail sanitize、`_log/_save_task_log` 入口兜底建议用例 |

可选但不作为第一轮强制：

| 文件 | 建议 |
|---|---|
| `services/chatgpt_core/plugin.py` | 只在已有 action 返回本地状态探测 summary 时可加 compact summary；不要把 `status_probe.py` 改成任务日志函数 |
| `services/chatgpt_core/local_status_refresh.py` | 可增加 `logger.info` compact summary；不进 task history |
| `api/chatgpt.py` | 可返回 `probe_summary`；不新增长任务日志 |

### 2.2 本轮坚决不改的东西

这些不要碰，哪怕看起来顺手：

1. **不重构流程**
   - 不合并注册 / 手机号注册 / 手机号绑定 / 补抓 Auth / 邮箱测活 / 失效测活。
   - 不改 `OAuthClient.login_and_get_tokens()` 的状态推进逻辑。
   - 不改邮箱 OTP、手机号 OTP 的等待、重发、提交策略。
   - 不改手机号池选择、prefix 抽样、prefix 绑定、同号复用逻辑。

2. **不改业务数据保存语义**
   - 不在 `save_account()` 前把账号对象里的 token/password/cookie 脱敏。
   - 不清洗 `account.extra` 里的业务必需原始字段，除非字段只用于展示且确认没有业务依赖。
   - 不改 `PhoneRegistrationEngine.to_account()` 的 token / session / cookies 保存行为。
   - 不改手机号绑定“手机号已绑定但 Auth 抓取失败仍算绑定”的语义。
   - 不改邮箱测活 / 失效测活“两阶段：先 AT，再 full Auth；第二阶段失败保留第一阶段”的语义。

3. **不改存储结构**
   - 不新增 DB 列。
   - 不改 `TaskLog` 表结构。
   - 不做新的日志表、事件表、外部日志服务。

4. **不做 UI 大改**
   - 本轮不重做任务详情页。
   - 如果历史日志里不再能复制完整 `phone----api_url`，这是安全取舍；后续如要复制原始行，应做受控导出接口，不从 task history/detail 拿。

5. **不把 `_panel_result()` 直接改成脱敏结果**
   - `services/chatgpt_core/phone_registration_engine.py::_panel_result()` 当前返回的 `api_url/raw_line` 会被账号 extra 使用。不要在这个函数里直接把业务结果脱敏。
   - 正确做法：业务结果保持原始；写入 task meta/detail/log 前生成 sanitized copy。

---

## 3. 核心原则：runtime raw，display sanitized

### 3.1 数据分层

本轮必须明确三层：

| 层 | 是否可保留原始数据 | 说明 |
|---|---:|---|
| runtime 局部变量 / runner 参数 | 可以 | 例如原始 `proxy`、`api_url`、`raw_line`、OTP、token，用于真正执行请求 |
| 账号业务保存 | 暂时可以 | 例如 token、session、部分 phone_signup 原始字段；本轮不改变业务保存 |
| 实时日志 / stdout / active task meta / `task_logs.detail_json` | 不可以 | 必须脱敏；这是本轮主要目标 |

### 3.2 禁止模式

第 4 阶段执行代理必须避免这些写法：

```python
# 禁止：把 meta.settings 变成 display copy 后继续传给 runner
meta["settings"]["proxy"] = redact_proxy_url(req.proxy)
background_tasks.add_task(_run_phone_binding_test, task_id, account_ids, phone_items, dict(meta["settings"]))
```

```python
# 禁止：原地 sanitize runtime list，后续业务还要用它
for item in phone_items:
    item["api_url"] = redact_url(item["api_url"])
```

```python
# 禁止：在 save_account 前脱敏账号 extra
account.extra["access_token"] = "[REDACTED]"
save_account(account)
```

### 3.3 正确模式

```python
runtime_settings = {
    # 原始 proxy，仅传给 runner
    "proxy": str(req.proxy or ""),
    ...
}

display_settings = {
    **runtime_settings,
    "proxy": redact_proxy_url(runtime_settings["proxy"]),
    "proxy_redacted": redact_proxy_url(runtime_settings["proxy"]),
}

runtime_phone_items = phone_items
safe_phone_items = [sanitize_phone_item(item) for item in runtime_phone_items]

meta = {
    "phone_items": safe_phone_items,
    "settings": display_settings,
    ...
}

background_tasks.add_task(
    _run_phone_binding_test,
    task_id,
    account_ids,
    runtime_phone_items,
    runtime_settings,
)
```

`_run_phone_binding_test()` 内也一样：

```python
# runtime_results / bound_phone_lines 可在函数局部保留原始，用于当前业务逻辑
runtime_results.append(raw_result)
bound_phone_lines.append(raw_line)

# 写 task store 时只写 safe copy
_task_store.update_meta(task_id, {
    "runtime_results": sanitize_task_detail(runtime_results),
    "bound_phone_lines": [redact_raw_phone_line(line) for line in bound_phone_lines],
})
```

---

## 4. 统一脱敏工具设计

### 4.1 新增文件

新增：

```text
services/chatgpt_core/task_logging.py
```

定位：

- 纯工具模块，只依赖 stdlib。
- 不 import `api.tasks`、`core.db`、`config_store`、`requests` 等，避免循环依赖和副作用。
- 主要服务 ChatGPT 任务日志；后续如要泛化再迁移到 `core/`。

### 4.2 建议常量

```python
REDACTION_VERSION = "chatgpt-task-logging-redaction-v1"
REDACTED = "[REDACTED]"
REDACTED_TOKEN = "[REDACTED_TOKEN]"
REDACTED_OTP = "[REDACTED_OTP]"
REDACTED_URL = "[REDACTED_URL]"
```

### 4.3 必须提供的函数名

最低函数集：

```python
def redact_proxy_url(value: Any) -> str:
    """scheme://user:pass@host:port -> scheme://***:***@host:port。无认证代理保留 host/port。"""


def redact_url(value: Any, *, keep_host: bool = True) -> str:
    """保留 scheme/host/path，去掉 query/fragment；query 中 token/key/code/password 等绝不保留。"""


def mask_phone_for_log(value: Any) -> str:
    """手机号日志展示：保留国家码/前几位和后 2-4 位，中间 ***。无法识别则返回原文脱敏版。"""


def redact_raw_phone_line(value: Any) -> str:
    """处理 phone----api_url 原始行：phone 可保留或 mask，api_url 必须去 query/token。"""


def sanitize_phone_item(item: Mapping[str, Any]) -> dict[str, Any]:
    """手机号 item 展示 copy：不改变原对象；api_url/raw_line/proxy/reason 脱敏，保留 line_no/prefix4/status 等。"""


def sanitize_phone_result(item: Mapping[str, Any]) -> dict[str, Any]:
    """手机号结果展示 copy：字段类型保持 dict；raw_line/api_url/reason/error 脱敏，保留 status/code_received/code_time/prefix4。"""


def redact_log_text(value: Any) -> str:
    """自由文本日志兜底脱敏。"""


def sanitize_error_message(value: Any) -> str:
    """错误消息兜底脱敏，返回 str。"""


def sanitize_task_detail(value: Any) -> Any:
    """递归 sanitize dict/list/tuple；保持字段类型，不把整个对象转字符串。"""


def compact_error(
    message: Any,
    *,
    code: str = "",
    phase: str = "",
    retryable: bool | None = None,
    recoverable: bool | None = None,
) -> dict[str, Any]:
    """统一错误摘要。"""


def stage_event(flow: str, stage: str, message: str = "", **fields: Any) -> str:
    """只生成日志字符串，不写日志。fields 统一过 redact_log_text/sanitize。"""
```

可选辅助：

```python
def summarize_mailbox_state(value: Any) -> dict[str, Any]:
    """mailbox_state 展示摘要：provider/email/has_state/before_count，不含 token、原文邮件、验证码。"""
```

### 4.4 `sanitize_task_detail()` 规则

递归处理：

1. `dict`：按 key 语义处理；返回 dict。
2. `list/tuple`：逐项处理；list 返回 list，tuple 可返回 tuple 或 list，但建议保持原类型。
3. `str`：走 `redact_log_text()`。
4. `bool/int/float/None`：原样返回。

敏感 key 规则：

| key 类型 | 处理 |
|---|---|
| `access_token/accessToken/refresh_token/id_token/session_token/sessionToken/bearer/authorization` | 替换为 `[REDACTED_TOKEN]`；`has_access_token` 这种布尔摘要不动 |
| `password/login_password/chatgpt_phone_signup_password` | 替换为 `[REDACTED]` |
| `cookie/cookies/set-cookie/oai-client-auth-session/login_session` | 替换为 `[REDACTED]` |
| `proxy/proxy_url/proxy_used/specified` | `redact_proxy_url()`；如不是 URL 再走文本脱敏 |
| `api_url/detail_url/continue_url/callback_url/url/endpoint` | `redact_url()`；OAuth callback 的 `code/state` query 必须删 |
| `raw_line/phone_signup_raw_line/bound_phone_lines` | `redact_raw_phone_line()` |
| `logs/errors/reason/message/raw_error/error` | `redact_log_text()` |
| `mailbox_state/raw_message/body/html/text/content` | 原文邮件正文不进 detail；替换摘要或 `[REDACTED]` |
| `phone` | structured detail 暂可保留以兼容运营 UI，但建议同时提供 `phone_masked`；文本 logs 默认 mask |

不要无脑替换所有 6 位数字。只在以下上下文命中时替换 OTP：

- 中文：`验证码`、`短信码`、`邮箱码`、`动态码`、`一次性代码`、`授权码`。
- 英文：`otp`、`one-time code`、`verification code`、`authorization code`、`auth code`。
- 结构字段：`code/otp/verification_code/email_otp/phone_otp`。

### 4.5 `redact_log_text()` 必须覆盖的文本

必须覆盖：

- `收到验证码 123456`
- `忽略旧验证码 123456`
- `尝试 OTP: 654321`
- `准备提交手机号验证码: 111222`
- `authorization code: abcdef...`
- `Authorization: Bearer eyJ...`
- `accessToken/sessionToken/refresh_token/id_token`
- `password=xxx`
- `Cookie: ...` / `set-cookie: ...`
- `http://user:pass@1.2.3.4:8000`
- `https://sms.example.com/get?token=secret&key=abc`
- `+15551234567----https://sms.example.com/get?token=secret`

---

## 5. `api/tasks.py` 最小 patch 清单

### 5.1 import

在顶部合适位置引入：

```python
from services.chatgpt_core.task_logging import (
    REDACTION_VERSION,
    redact_log_text,
    redact_proxy_url,
    redact_raw_phone_line,
    sanitize_error_message,
    sanitize_phone_item,
    sanitize_phone_result,
    sanitize_task_detail,
)
```

如担心 import 时机，可在函数内局部 import；但工具模块必须纯 stdlib，正常顶层 import 应安全。

### 5.2 `_log()`

当前位置：`api/tasks.py::_log(task_id, msg, level="info")`。

改法：

1. 保留现有时间戳和 `[DEBUG]` 前缀逻辑。
2. 生成 `entry` 前先 sanitize 文本。
3. 写 `_task_store.append_log()` 和 `print()` 的必须是同一个 sanitized entry。

伪代码：

```python
text = str(msg or "")
# 先按 level 加 [DEBUG]
if normalized_level == "debug" and not text.lstrip().upper().startswith("[DEBUG]"):
    text = f"[DEBUG] {text}"
# 再统一脱敏
text = redact_log_text(text)
entry = f"[{ts}] {text}"
_task_store.append_log(task_id, entry)
print(entry)
```

注意：不要只在 `print()` 前脱敏，task store 也要脱敏。

### 5.3 `_save_task_log()`

当前位置：`api/tasks.py::_save_task_log(platform, email, status, error="", detail=None)`。

改法：

1. 入库前对 `error` 调 `sanitize_error_message()`。
2. 入库前对 `detail` 调 `sanitize_task_detail()`。
3. 给 detail additive 增加 `redaction_version`，不要删旧字段。
4. `json.dumps()` 使用 sanitized detail。

伪代码：

```python
safe_error = sanitize_error_message(error)
safe_detail = sanitize_task_detail(detail or {})
if isinstance(safe_detail, dict):
    safe_detail.setdefault("redaction_version", REDACTION_VERSION)
...
log.error = safe_error
log.detail_json = json.dumps(safe_detail, ensure_ascii=False)
```

这是一道兜底，能保护所有绕过显式 sanitize 的历史 detail。

### 5.4 `_build_task_log_detail()`

当前位置：`api/tasks.py::_build_task_log_detail(task_id, extra=None)`。

建议：

- 保留现有结构：`task_id/status_snapshot/progress/success/skipped/errors/cashier_urls/source/meta/logs`。
- 可在返回前 `detail.setdefault("schema_version", 1)` 或只在 `_save_task_log()` 里加 `redaction_version`。第一轮不强制 `schema_version=2`。
- 不要在这里做复杂业务转换；最终兜底在 `_save_task_log()`。
- 但如果执行代理愿意加一层安全，可以返回前 `return sanitize_task_detail(detail)`。即使加了，也不能替代 `_save_task_log()` 兜底。

最终建议：**`_save_task_log()` 必须 sanitize；`_build_task_log_detail()` 可保持结构不变。**

### 5.5 `enqueue_phone_binding_test_task()`

当前位置：`api/tasks.py::enqueue_phone_binding_test_task()`。

当前风险：

- `meta["settings"]["proxy"]` 保存原始代理。
- `meta["proxy"]["specified"]` 保存原始代理。
- `meta["phone_items"]` 保存原始 `api_url/raw_line`。
- runner 参数当前来自 `dict(meta["settings"])`，如果直接脱敏 meta 会把 runner 搞挂。

最终改法：

1. 构造 `runtime_settings`，里面保留原始值，只传 `_run_phone_binding_test()`。
2. 构造 `display_settings`，里面所有敏感字段脱敏，只放进 task meta。
3. `phone_items` 原始 list 只作为 runner 参数；`meta["phone_items"]` 放 sanitized copy。
4. `parse_errors` 如含原始 line，也过 `sanitize_task_detail()`。
5. background thread / background task 都改为传 `runtime_settings`，不要再传 `dict(meta["settings"])`。

建议形态：

```python
runtime_settings = {
    "timeout_seconds": timeout_seconds,
    ...,
    "proxy": str(req.proxy or ""),
    "proxy_mode": str(req.proxy_mode or "pool"),
    ...,
}

display_settings = dict(runtime_settings)
display_settings["proxy"] = redact_proxy_url(runtime_settings.get("proxy"))
display_settings["proxy_redacted"] = display_settings["proxy"]

runtime_phone_items = phone_items
safe_phone_items = [sanitize_phone_item(item) for item in runtime_phone_items]

meta = {
    ...,
    "phone_items": safe_phone_items,
    "settings": display_settings,
    "proxy": {
        "mode": ...,
        "specified": redact_proxy_url(req.proxy),
        "specified_redacted": redact_proxy_url(req.proxy),
        ...,
    },
}
...
args=(task_id, account_ids, runtime_phone_items, dict(runtime_settings))
```

兼容要求：

- `meta["phone_items"]` 仍是 list。
- `meta["settings"]` 仍是 dict。
- `meta["proxy"]` 仍是 dict。
- 旧字段名可以保留，但值必须是 display-safe。
- 如果担心前端读取 `settings.proxy`，可保留 `settings.proxy` 为 redacted，同时新增 `settings.proxy_redacted`。

### 5.6 `_run_phone_binding_test()`

当前位置：`api/tasks.py::_run_phone_binding_test()`。

要点：

#### 5.6.1 `sync_meta()`

当前直接把局部 `runtime_results/account_results/bound_phone_lines/bound_phone_results/errors` 写入 `_task_store`。

改为：

```python
def sync_meta() -> None:
    safe_runtime_results = [sanitize_phone_result(item) for item in runtime_results]
    safe_bound_results = [sanitize_phone_result(item) for item in bound_phone_results]
    updates = {
        "runtime_results": safe_runtime_results,
        "account_results": sanitize_task_detail(account_results),
        "bound_phone_lines": [redact_raw_phone_line(line) for line in bound_phone_lines],
        "bound_phone_results": safe_bound_results,
        "runtime_errors": [sanitize_error_message(err) for err in errors],
        ...
    }
```

不要修改局部 `runtime_results` / `bound_phone_lines` 本体。

#### 5.6.2 `_log_phone_binding_proxy_choice()`

当前直接输出 `proxy={proxy_url}`。

改为：

```python
safe_proxy = redact_proxy_url(proxy_url)
_log(task_id, f"[代理] 手机号绑定核心链路 {index}/{total_count}: source={source} proxy={safe_proxy}")
```

虽然 `_log()` 已兜底，但这里主动用 redacted，日志更可控。

#### 5.6.3 `format_result_log_line()`

当前日志展示完整手机号、reason 可能带 URL/token。

改为：

- 文本日志中的手机号使用 `mask_phone_for_log()`。
- `reason` 使用 `redact_log_text(reason)[:160]`。
- `api_expired_date/code_time/status` 保留。

#### 5.6.4 `[RESULT_RAW]`

当前：

```python
_log(task_id, f"[RESULT_RAW] {result.get('raw_line') or ''}")
```

最终必须改。两种可选：

- 推荐：不再输出 `[RESULT_RAW]`，改为 `[RESULT_SAFE] {redact_raw_phone_line(...)}`。
- 如果前端或人工已经依赖 `[RESULT_RAW]` 前缀，保留前缀但值必须 redacted：`[RESULT_RAW] [REDACTED_RAW_LINE] ...`。但名字会误导，推荐改成 `[RESULT_SAFE]`。

本轮不提供原始行导出。需要原始导出以后做受控接口。

#### 5.6.5 最终 `_save_task_log()` detail

传 detail 时可以继续传局部 raw，因为 `_save_task_log()` 会兜底；但建议显式传 safe copy，降低风险：

```python
"bound_phone_lines": [redact_raw_phone_line(line) for line in bound_phone_lines],
"bound_phone_results": [sanitize_phone_result(item) for item in bound_phone_results],
"runtime_results": [sanitize_phone_result(item) for item in runtime_results],
"account_results": sanitize_task_detail(account_results),
"error": compact_error(...),
"flow": "phone_binding",
"policy": {...},
"artifacts": {...},
```

旧字段必须保留，类型不变。

### 5.7 `_run_register()` 中手机号注册 task meta

当前位置：`api/tasks.py::_run_register()` 内 `chatgpt_registration_entry == "phone_signup"` 后，当前把 `phone_results`、`registered_phone_lines`、`runtime_results` 写入 task meta。

最终改法：

1. `account.extra` 不改，仍保留业务所需原始字段。
2. 写 `_task_store.update_meta()` 前生成 sanitized copy。
3. `registered_phone_lines` 不再保存完整 `phone----api_url`，改为 redacted line。
4. `runtime_results` 保存 `[sanitize_phone_result(item) ...]`。
5. `phone_signup.last_phone` 如果进入 meta，建议写 full phone + `last_phone_masked` 二选一：
   - 为兼容可保留 `last_phone`，但 `_save_task_log()` 会兜底；
   - 更稳是新增 `last_phone_masked`，后续前端优先展示 masked。

建议：

```python
safe_item = sanitize_phone_result(item)
runtime_results.append(safe_item)
if raw_line:
    registered_phone_lines.append(redact_raw_phone_line(raw_line))
...
"phone_signup": {
    "enabled": True,
    "last_phone": last_phone,              # 如前端依赖可保留
    "last_phone_masked": mask_phone_for_log(last_phone),
}
```

### 5.8 其他 `api/tasks.py` 细节

- `_log_custom_email_proxy_choice()`、`_log_register_proxy_choice()` 如仍用局部 `_redact_proxy_for_task_log()`，可以保留，但建议内部切到 `redact_proxy_url()`，逐步减少重复。
- `_save_task_log()` 后所有 detail 都会 sanitize，所以补抓 Auth、邮箱测活、失效测活的历史日志会自动兜底。
- 不要为了日志整改修改 `TaskLog` schema 或 `_ensure_task_log_schema()`。

---

## 6. 各核心模块最小日志改点

## 6.1 `services/chatgpt_core/phone_service.py`

目标函数：

```text
UploadedPhoneService.wait_for_code()
```

当前风险：

- `忽略旧验证码 {code}` 明文。
- `收到验证码 {code}` 明文。
- 完整手机号在文本日志中出现。

最小改法：

- 引入 `mask_phone_for_log` / `redact_log_text`。
- 返回值 `code` 保持原始，不影响业务。
- 日志改为：

```text
[号码测试] +1555***1234 忽略旧验证码 otp_received=true otp_length=6 code_time=...
[号码测试] +1555***1234 收到验证码 otp_received=true otp_length=6 code_time=... extracted=true
```

注意：`last_code` 仍可保存原始值作为运行时状态，不能脱敏，否则 OTP 提交会失败。

## 6.2 `services/chatgpt_core/oauth_client.py`

目标：不改 OAuth 流程，只改日志文本。

必须修的日志类型：

1. 已绑定手机号二次验证：
   - 目标函数：`_handle_existing_phone_otp_verification()` 及其调用的提交/重试日志。
   - 手机号文本日志使用 `mask_phone_for_log()`。
   - OTP 提交日志不写 code，只写 `otp_present=true otp_length=6`。

2. add_phone 新绑：
   - 目标函数：`_handle_add_phone_verification()`。
   - `步骤5: add_phone 选择手机号 ... {entry.phone}` 改为 masked。
   - `准备提交手机号验证码: {code}` 改为 `准备提交手机号验证码 otp_present=true otp_length=6`。
   - `号段短信探测已收到验证码: {code}` 改为 `code_received=true otp_length=6`。

3. 人工手机号 OTP：
   - 所有 `准备提交人工手机号验证码: {code}` 改为不含 code。

4. 邮箱 OTP：
   - `尝试 OTP: {code}`、`跳过已尝试验证码: {code}` 改为 `otp_attempted=true otp_length=...`。
   - 日志带 purpose，例如 `purpose=email_login_otp` / `purpose=custom_email_recheck_login_otp`，不要混成注册 OTP。

5. authorization code：
   - `获取到 authorization code: {code[:20]}...` 改为 `authorization_code_received=true code_length=...`。
   - OAuth callback URL 进入日志时必须通过 `redact_url()` 去掉 query。

6. raw response：
   - 任何 `result.text` / response body 进入日志前先 `redact_log_text()`，再截断。
   - 不改变请求 payload，不改变 response 解析。

不做：

- 不重构 `_log` level 体系。
- 不改变 `verbose=True` 的行为结构，只确保输出文本脱敏。
- 不改变 `FlowState` 状态机判断。

## 6.3 `services/chatgpt_core/phone_signup_client.py`

目标：手机号注册协议客户端不直出敏感响应。

最小改法：

1. 保留现有 `redact_text()` 名称，内部可改为调用全局 `redact_log_text()`，避免影响调用方。

   建议：

   ```python
   def redact_text(text: str, phone: str = "") -> str:
       redacted = redact_log_text(text)
       if phone:
           redacted = redacted.replace(phone, mask_phone_for_log(phone))
       return redacted
   ```

2. `validate_phone_otp()` 当前：

   ```python
   raise RuntimeError(f"phone-otp/validate 失败: HTTP {result.status} {result.text[:500]}")
   ```

   改为：

   ```python
   raise RuntimeError(f"phone-otp/validate 失败: HTTP {result.status} {redact_text(result.text, self.phone)[:500]}")
   ```

   如果类里没有 `self.phone`，用传入 phone 或空字符串；核心是走 `redact_text()`。

3. `create_account()` 当前也直接 `result.text[:500]`，同样改。

4. 所有 `continue_url` / callback URL 打印继续使用 `short_url()`，但要确认 `short_url()` 不保留 `code/state` query；如保留，内部改为调用 `redact_url()`。

不做：

- 不改变 `json={"code": code}` 提交。
- 不改变 `follow_chatgpt_callback_and_capture()` token 捕获逻辑。

## 6.4 `services/chatgpt_core/phone_registration_engine.py`

目标：手机号注册任务日志安全，但业务结果不变。

最小改法：

1. 日志中的手机号使用 `mask_phone_for_log()`。
2. 日志中的代理使用 `redact_proxy_url()`。
3. 不在 `_panel_result()` 里直接脱敏 `api_url/raw_line`，因为它会进入业务 metadata / account extra。
4. 如需要给 task meta 用展示结果，新增局部 helper 或使用 `sanitize_phone_result(panel_result)` 生成 safe copy。实际 task meta 写入主要在 `api/tasks.py::_run_register()` 处理。
5. `result.metadata` 可 additive 增加：

   ```python
   "proxy_used_redacted": redact_proxy_url(self.proxy_url),
   "redaction_version": REDACTION_VERSION,
   ```

   但不要删除原 `proxy_used`，除非确认无业务依赖。历史 detail 会由 `_save_task_log()` 兜底。

6. 阶段日志只补关键点，不大改文案：

```text
[手机号注册][phone_acquire] phone=+1555***1234 source=pool|manual prefix4=1555
[手机号注册][phone_send] accepted=true flow=phone_signup|phone_existing_login
[手机号注册][phone_wait] code_received=true code_time=... extracted=true
[手机号注册][phone_submit] ok=true
[手机号注册][session_capture] ok=true has_access_token=true auth_level=access_token_only
```

不做：

- 不合并手机号注册和手机号绑定。
- 不改变 prefix 状态回写。
- 不改变 `to_account()` 保存字段。

## 6.5 `services/chatgpt_core/subscription_auth_capture.py`

目标：补抓 Auth / 手机号绑定共用的 Auth 捕获日志安全。

最小改法：

1. 开始日志中的 `proxy={proxy_url}` 改为 `proxy={redact_proxy_url(proxy_url)}`。
2. `result.metadata` additive 增加 `proxy_redacted`；不要强制删除 `proxy` raw，避免意外影响账号 extra 或排障字段。
3. `_build_auth_capture_payload()` 或最终 payload 增加 compact 字段：

```json
{
  "flow": "resume_subscription_auth",
  "stage": "full_auth_capture",
  "error": {
    "code": "missing_refresh_token",
    "phase": "token_exchange",
    "retryable": true,
    "message": "...redacted..."
  },
  "artifacts": {
    "has_access_token": true,
    "has_refresh_token": false,
    "auth_level": "access_token_only",
    "workspace_id": "..."
  },
  "redaction_version": "chatgpt-task-logging-redaction-v1"
}
```

4. 失败日志使用 `sanitize_error_message(last_error)`。
5. 保留 `_classify_capture_error()`，不重构分类函数。

不做：

- 不改变 retry 次数和 retry delay。
- 不改变 `allow_add_phone_verification` / `allow_existing_phone_verification` 的策略解析。
- 不改变持久化 `_persist_subscription_auth_result()` 的业务字段。

## 6.6 `services/chatgpt_core/custom_email_recheck.py`

目标：邮箱测活 action logs / detail 安全，并清楚表达两阶段。

最小改法：

1. 本模块内部 `_log()` 当前先 `action_logs.append(text)`，再 `_log_to(log_fn, text)`。必须改为 append 前脱敏：

```python
safe_text = redact_log_text(text)
action_logs.append(safe_text)
_log_to(log_fn, safe_text)
```

这样即使上层 `_log()` 兜底，返回 payload 里的 `logs` 也不会带原始内容。

2. 失败 payload 中的 `raw_error` / `message` 调 `sanitize_error_message()` 或通过 `compact_error()` 生成展示错误。可保留内部 raw 变量用于控制流，但返回 data/detail 只给 safe copy。

3. 补关键阶段日志：

```text
[邮箱测活][access_token_probe] start email=...
[邮箱测活][access_token_probe] ok=true has_access_token=true saved_account_id=...
[邮箱测活][full_auth_capture] start skip_access_token_probe=false|true
[邮箱测活][full_auth_capture] ok=false has_refresh_token=false error_code=...
```

4. 邮箱 OTP purpose 建议在日志中固定：`purpose=custom_email_recheck_login_otp`。
5. 最终返回 data 可 additive 增加：

```json
{
  "flow": "custom_email_recheck",
  "stage_results": [
    {"stage": "access_token_probe", "ok": true, "has_access_token": true},
    {"stage": "full_auth_capture", "ok": false, "has_refresh_token": false}
  ],
  "error": {"code": "...", "phase": "...", "retryable": true, "message": "..."},
  "artifacts": {"has_access_token": true, "has_refresh_token": false, "auth_level": "access_token_only"}
}
```

不做：

- 不改变第二阶段失败时 `ok=True` 且保留第一阶段结果的设计。
- 不改变 `skip_access_token_probe=True` 的 followup 路径。
- 不改 mailbox provider 行为。

## 6.7 `services/chatgpt_core/invalid_account_recheck.py`

目标：失效测活两阶段日志清楚，返回 payload 安全。

最小改法：

1. 本模块所有 `_log()` 进入 `action_logs` 或返回 payload 前，使用 `redact_log_text()`。
2. 第一阶段日志改为更稳定的 stage 名：

```text
[失效测活][access_token_probe] start account_id=... email=...
[失效测活][access_token_probe] ok=true has_access_token=true saved_account_id=...
[失效测活][persist] status=recovered_access_token auth_level=access_token_only
```

3. 第二阶段 followup：

```text
[失效测活][followup_auth] start source=custom_email_recheck skip_access_token_probe=true
[失效测活][followup_auth] ok=false has_refresh_token=false error_code=...
```

4. `_classify_recheck_error()` 保留，只把结果映射到返回 payload 的 `error` 字段：

```json
"error": {
  "code": "network_failed",
  "phase": "access_token_probe",
  "retryable": true,
  "recoverable": true,
  "message": "...redacted..."
}
```

5. 明确终态：
   - `account_deactivated/password_invalid`：`retryable=false`。
   - `network_failed/otp_rate_limited/login_blocked`：不要把账号真实死亡写死，日志建议带 `kept_original_status=true`。

不做：

- 不改变 `recheck_custom_chatgpt_email(... skip_access_token_probe=True)` followup 调用。
- 不改变 `_persist_recheck_success()` / `_persist_recheck_followup_result()` 的业务字段。

---

## 7. 统一 detail 字段：只 additive

本轮不强推完整 `schema_version=2` 事件模型，但每类任务最终 detail 可逐步加以下字段。所有字段都是 additive。

### 7.1 通用字段

```json
{
  "redaction_version": "chatgpt-task-logging-redaction-v1",
  "flow": "phone_binding|phone_signup|registration|resume_subscription_auth|custom_email_recheck|invalid_recheck",
  "policy": {},
  "error": {
    "code": "",
    "phase": "",
    "retryable": null,
    "recoverable": null,
    "message": ""
  },
  "artifacts": {
    "has_access_token": false,
    "has_refresh_token": false,
    "auth_level": "",
    "workspace_id": ""
  }
}
```

### 7.2 必须保留的旧字段

这些字段不删、不改类型：

```text
source
attempt_outcome
status_snapshot
progress
success
skipped
errors
cashier_urls
meta
logs
result
runtime_results
account_results
bound_phone_results
bound_phone_lines
```

注意：`bound_phone_lines` 的值会从原始行变成 redacted 行，这是安全取舍；类型仍是 list。

### 7.3 各任务 flow 建议

| 任务 | flow |
|---|---|
| 普通邮箱注册 / RT 注册 / AT-only 注册 | `registration` |
| 手机号注册 | `phone_signup` |
| 手机号绑定 | `phone_binding` |
| 补抓 Auth | `resume_subscription_auth` |
| 邮箱测活 | `custom_email_recheck` |
| 失效测活 | `invalid_recheck` |
| 本地 token 测活 / 状态探测 | `local_status_probe` |

### 7.4 关键 stage 建议

只补这些关键阶段，不全量改所有 HTTP step：

| flow | stages |
|---|---|
| `registration` | `access_token_checkpoint`、`full_auth_capture`、`persist` |
| `phone_signup` | `phone_acquire`、`phone_send`、`phone_wait`、`phone_submit`、`session_capture`、`persist` |
| `phone_binding` | `account_start`、`proxy_select`、`phone_acquire`、`phone_send`、`phone_wait`、`phone_submit`、`full_auth_capture`、`result` |
| `resume_subscription_auth` | `attempt`、`oauth_login`、`token_exchange`、`persist`、`error` |
| `custom_email_recheck` | `access_token_probe`、`full_auth_capture`、`persist` |
| `invalid_recheck` | `access_token_probe`、`persist`、`followup_auth` |
| `local_status_probe` | `token_refresh`、`backend_me`、`subscription`、`codex` |

---

## 8. 测试方案

### 8.1 新增测试文件

新增：

```text
tests/test_chatgpt_task_logging.py
```

### 8.2 单元测试用例

#### 8.2.1 `redact_proxy_url()`

用例：

```python
http://user:pass@1.2.3.4:8000 -> http://***:***@1.2.3.4:8000
socks5://u:p@example.com:1080 -> socks5://***:***@example.com:1080
http://1.2.3.4:8000 -> http://1.2.3.4:8000
"" -> ""
```

断言：

- 不包含 `user:pass@` / `u:p@`。
- host/port 仍可见。

#### 8.2.2 `redact_url()`

用例：

```python
https://sms.example.com/get?token=abc&key=xyz&id=1
https://auth.openai.com/callback?code=SECRET&state=STATE
```

断言：

- 不包含 `token=abc` / `key=xyz` / `code=SECRET`。
- 保留 `https://sms.example.com/get`。

#### 8.2.3 `redact_log_text()`

输入必须覆盖：

```text
[号码测试] +15551234567 收到验证码 123456，时间 2026-06-21 01:02:03
尝试 OTP: 654321
准备提交手机号验证码: 111222
获取到 authorization code: abcdef1234567890
Authorization: Bearer eyJhbGciOi...
accessToken":"secret-token"
sessionToken=secret-session
refresh_token=secret-refresh
Cookie: oai-client-auth-session=secret
password=super-secret
proxy=http://user:pass@1.2.3.4:8000
+15551234567----https://sms.example.com/get?token=secret
```

断言：

- 不包含原 OTP：`123456/654321/111222`。
- 不包含 `abcdef1234567890`。
- 不包含 token/cookie/password 原值。
- 不包含 `user:pass@`。
- 不包含 `token=secret`。
- 返回仍是 str。

#### 8.2.4 `redact_raw_phone_line()`

输入：

```text
+15551234567----https://sms.example.com/get?token=secret&key=abc
```

断言：

- 不包含 `token=secret` / `key=abc`。
- 输出包含 redacted URL host/path。
- 如策略 mask phone，则不包含完整 `+15551234567`；如 structured detail 允许保留 phone，则至少必须提供 `phone_masked` 的测试由 `sanitize_phone_item()` 覆盖。

#### 8.2.5 `sanitize_phone_item()` / `sanitize_phone_result()`

输入 dict 包含：

```python
{
    "phone": "+15551234567",
    "api_url": "https://sms.example.com/get?token=secret",
    "raw_line": "+15551234567----https://sms.example.com/get?token=secret",
    "reason": "phone-otp/validate 失败: code 123456 token abc",
    "status": "bound",
    "code_received": True,
    "prefix4": "1555",
}
```

断言：

- `status/code_received/prefix4` 保持。
- `api_url/raw_line/reason` 脱敏。
- 原 dict 未被原地修改。

#### 8.2.6 `sanitize_task_detail()`

输入 nested dict/list：

```python
{
    "source": "phone_binding_test",
    "attempt_outcome": "phone_binding_test_failed",
    "meta": {
        "settings": {"proxy": "http://user:pass@1.2.3.4:8000"},
        "phone_items": [{"raw_line": "+15551234567----https://sms.example.com/get?token=secret"}],
    },
    "logs": ["收到验证码 123456", "Authorization: Bearer secret"],
    "runtime_results": [{"api_url": "https://sms.example.com/get?key=abc"}],
    "mailbox_state": {"raw_message": "Your code is 123456", "provider": "icloud_hme"},
    "has_access_token": True,
    "phone_count": 1,
}
```

断言：

- `source/attempt_outcome` 保留。
- `has_access_token` 仍是 bool。
- `phone_count` 仍是 int。
- `meta/logs/runtime_results/mailbox_state` 内敏感值消失。
- list/dict 类型不变。

#### 8.2.7 `_log()` 入口兜底

建议测试方式：

- 创建/注册一个 dummy task，或 monkeypatch `_task_store.append_log` 捕获 entry。
- 调用 `_log(task_id, "收到验证码 123456 Authorization: Bearer secret")`。
- 断言捕获的 entry 不含 `123456` / `secret`。

如果导入 `api.tasks` 成本太高，至少测试 `redact_log_text()`，但第 4 阶段最好加 `_log()` 的入口测试。

#### 8.2.8 `_save_task_log()` 入口兜底

建议测试方式：

- 用项目现有测试 DB fixture 或 monkeypatch `Session(engine)` 边界。
- 传入 detail 包含 raw token/proxy/raw_line。
- 断言最终写入的 `detail_json` 不含原敏感值，且包含 `redaction_version`。

如果测试 DB fixture 成本高，可把 `_save_task_log()` 内部 sanitize 抽成极小 helper，如 `_prepare_task_log_payload_for_storage(error, detail)`，单测 helper；但不要为了测试大幅改代码结构。

### 8.3 回归测试命令

第 4 阶段执行后建议运行：

```bash
pytest -q tests/test_chatgpt_task_logging.py
pytest -q tests/test_phone_binding_assignment.py tests/test_chatgpt_phone_registration.py
pytest -q tests/test_subscription_auth_capture.py tests/test_custom_email_recheck.py tests/test_invalid_account_recheck.py
```

不要用 `|| true` 糊过去。失败要列出：

- 哪些是既有失败。
- 哪些与本轮日志改动有关。
- 如果只是 fixture 环境缺失，也要说明。

如果执行代理改到了前端展示字段，再跑：

```bash
cd frontend && npm run build
```

本方案不要求前端改动；没改前端就不强制 build。

---

## 9. 手工 smoke / 内容验收

第 5 阶段审核代理至少检查这些：

### 9.1 实时日志 / stdout

搜索实时日志和容器 stdout，确认不出现：

```text
Bearer eyJ
access_token 明文
refresh_token 明文
sessionToken/session_token 明文
Cookie: 原文
password 原文
收到验证码 123456
尝试 OTP: 123456
authorization code: abc
user:pass@
phone----https://...?...token=
```

### 9.2 `task_logs.detail_json`

构造或实际跑一条任务后检查 detail：

- 不包含 token/cookie/password/OTP/完整代理认证/收码 API query。
- `source/attempt_outcome/meta/logs/runtime_results/account_results/bound_phone_results` 仍存在，类型不变。
- 有 `redaction_version`。
- 手机号绑定 detail 里 `bound_phone_lines` 不再是完整 `phone----api_url` 原始行。

### 9.3 手机号绑定 smoke

重点看：

- `meta.settings.proxy` 是 redacted。
- runner 仍能使用原始代理执行，不出现 `***:***@host` 被拿去请求的错误。
- `meta.phone_items` / `runtime_results` / `bound_phone_results` 中 `api_url/raw_line` 是 redacted。
- 号码池状态回写仍使用原始 phone / api_url，不受 display copy 影响。
- 手机号已绑定但 full auth 失败时，仍记录绑定成功 + `auth_capture_ok=false`，语义不变。

### 9.4 手机号注册 smoke

重点看：

- `phone_signup_results` 写入 task meta 时不带完整 `api_url/raw_line`。
- 账号保存仍有需要的 token/session/account extra。
- 日志能区分 `phone_signup` 和 `phone_existing_login`。
- OTP 不明文出现。

### 9.5 补抓 Auth / 邮箱测活 / 失效测活 smoke

重点看：

- 补抓 Auth 开始日志中的 proxy redacted。
- 邮箱测活日志区分 `access_token_probe` 和 `full_auth_capture`。
- 失效测活日志区分 `access_token_probe` 和 `followup_auth`。
- 第二阶段失败时仍保留第一阶段 AT 结果，业务语义不变。
- 返回 payload/logs/detail 中 raw_error 已脱敏。

---

## 10. 执行顺序建议

第 4 阶段执行代理按这个顺序做，别一口气改全文案：

### P0：脱敏工具和测试

1. 新增 `services/chatgpt_core/task_logging.py`。
2. 新增 `tests/test_chatgpt_task_logging.py`。
3. 单测先覆盖 helper，不碰业务。

### P1：全局入口兜底

1. `api/tasks.py::_log()` 接 `redact_log_text()`。
2. `api/tasks.py::_save_task_log()` 接 `sanitize_task_detail()` / `sanitize_error_message()`。
3. 确认现有任务日志不会因 sanitize 改类型而前端报错。

### P2：phone-binding runtime/display 拆分

1. `enqueue_phone_binding_test_task()` 拆 `runtime_settings` 和 display `meta.settings`。
2. `meta.phone_items` 改为 sanitized copy。
3. `_run_phone_binding_test().sync_meta()` 写 safe copy。
4. `[RESULT_RAW]` 改为 safe 输出或删除。

### P3：OTP / code / proxy 已知泄露点

1. `phone_service.py` 去掉 OTP 明文。
2. `oauth_client.py` 去掉 OTP / authorization code 明文。
3. `phone_signup_client.py` 失败响应走脱敏。
4. `subscription_auth_capture.py` proxy 日志脱敏。
5. `phone_registration_engine.py` 手机号/代理日志脱敏。

### P4：阶段和错误摘要

1. 邮箱测活、失效测活、补抓 Auth 补关键 stage。
2. detail additive 加 `flow/error/artifacts/policy/redaction_version`。
3. 不追求所有 HTTP step 都结构化。

### P5：回归与内容验收

1. 跑测试。
2. 抽查实时日志和 `task_logs.detail_json`。
3. 如果需要上 live，按项目约定部署到 `auto-gpt` 容器后再做 live smoke；checkout 成功不等于线上成功。

---

## 11. 自检清单（第 3 阶段方案自检）

### 11.1 是否触碰业务流程？

本方案不要求改：

- 任务入口、任务类型、任务编排。
- OAuth 状态机。
- 注册、测活、手机号绑定流程。
- 邮箱/手机号 OTP 发送、等待、提交策略。
- retry 策略和代理候选选择策略。
- 账号保存和手机号池状态写回语义。

只要求改日志、display meta、历史 detail、可展示错误摘要。

结论：**不触碰业务流程。**

### 11.2 是否会破坏 task runner？

风险点是 phone-binding：原代码用 `dict(meta["settings"])` 作为 runner 参数。如果直接把 `meta.settings.proxy` 脱敏，runner 会拿 `***:***@host` 去请求。

本方案已明确：

- 新建 `runtime_settings`，保留原始 proxy，只传 runner。
- `meta.settings` 是 display copy，不再作为 runner 参数来源。
- `phone_items` 原始 list 传 runner，`meta.phone_items` 使用 sanitized copy。
- `sync_meta()` 写 safe copy，不修改局部 runtime list。

结论：**按方案执行不会破坏 task runner。** 执行代理必须重点检查这一点。

### 11.3 是否保留旧字段兼容？

本方案要求：

- 旧字段不删除。
- 旧字段类型不变。
- 新字段 additive。
- `bound_phone_lines` 类型仍是 list，但值从 raw line 变 redacted line，这是安全取舍。
- `runtime_results/account_results/bound_phone_results` 仍是 list/dict，不转成字符串。

结论：**兼容字段结构；少数字段内容安全化。**

### 11.4 是否覆盖敏感信息？

覆盖范围：

- token：`access_token/refresh_token/id_token/session_token/accessToken/sessionToken/Bearer/Authorization`。
- 密码：`password/login_password/chatgpt_phone_signup_password`。
- Cookie/session：`cookie/cookies/set-cookie/oai-client-auth-session/login_session`。
- OTP：邮箱 OTP、手机 OTP、人工 OTP、旧验证码、authorization code。
- 代理：`scheme://user:pass@host:port`。
- URL：收码 API query、OAuth callback query、`code/state/token/key`。
- 原始行：`phone----api_url`。
- 邮件正文：`mailbox_state.raw_message/body/html/text/content`。

结论：**覆盖第 1/2 阶段指出的敏感面。**

### 11.5 是否避免过度设计？

本方案推迟：

- 完整 `schema_version=2` 事件模型。
- 全量日志文案统一。
- 全量错误分类函数重构。
- 受控原始行导出接口。
- UI 大改。
- 本地状态探测接入长任务日志。

结论：**方案已收窄到可安全落地的 patch。**

---

## 12. 第 4 阶段执行代理注意事项

1. 先看 `git status`，不要覆盖其他代理/用户已有改动。
2. 新增工具和测试后先跑 `pytest -q tests/test_chatgpt_task_logging.py`。
3. 改 `api/tasks.py` 时最容易踩坑的是 `meta.settings` 和 runner 参数，不要从 sanitized meta 再取 runtime 参数。
4. 改 `phone_registration_engine.py` 时不要把 `_panel_result()` 的业务返回直接脱敏。
5. 改 `oauth_client.py` 时只动日志文本，不动状态判断和请求 payload。
6. `sanitize_task_detail()` 必须保持类型，尤其 list/dict/bool/int。
7. 如果某处必须保留完整 phone 供运营识别，至少保证 `api_url/raw_line/token/proxy/OTP` 被脱敏，并新增 `phone_masked`。
8. 如果测试发现前端依赖完整 raw_line，从安全角度仍不要把 raw_line 放回 task history；记录为后续“受控导出接口”需求。

---

## 13. 第 5 阶段审核代理验收重点

审核不只看测试是否过，还要看有没有违反边界：

1. 搜索代码确认没有把 `redact_proxy_url(req.proxy)` 传给 runner。
2. 搜索 `_task_store.update_meta`，确认写入的是 safe copy。
3. 搜索日志直出：`验证码 {code}`、`OTP: {code}`、`authorization code: {code}`、`[RESULT_RAW] {raw_line}` 是否消失。
4. 搜索 `result.text[:500]`，确认进入日志/异常前经过 `redact_text()` / `redact_log_text()`。
5. 检查 `task_logs.detail_json` sanitize 兜底确实在 `_save_task_log()` 入库前执行。
6. 检查 `account.extra` / `save_account()` 前没有被全局 sanitizer 误伤。
7. 跑 targeted tests，并说明失败是否与日志改动相关。

最终接受标准：

- 实时日志、stdout、active task meta、`task_logs.detail_json` 不再泄露核心敏感信息。
- 注册/手机号注册/手机号绑定/补抓 Auth/邮箱测活/失效测活流程行为不变。
- 旧字段兼容，新字段 additive。
- runner 使用原始数据，display 使用 sanitized copy。
